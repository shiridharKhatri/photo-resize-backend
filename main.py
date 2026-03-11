import os
import time
import zipfile
import shutil
import uuid
import json
from typing import List
from fastapi import FastAPI, UploadFile, File, Request
from fastapi.responses import JSONResponse, FileResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from PIL import Image, ImageOps, ImageColor
from rembg import remove as rembg_remove
from io import BytesIO

try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
except ImportError:
    pass

app = FastAPI(title="Studio Engine API")

# Configuration
BASE_DIR = os.getenv("BASE_DIR", os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
UPLOAD_FOLDER = os.getenv("UPLOAD_FOLDER", os.path.join(BASE_DIR, 'uploads'))
OUTPUT_FOLDER = os.getenv("OUTPUT_FOLDER", os.path.join(BASE_DIR, 'outputs'))
ALLOWED_EXTENSIONS_IMG = {'png', 'jpg', 'jpeg', 'webp', 'gif', 'bmp', 'tiff', 'svg', 'heic', 'heif'}

# Ensure directories exist
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(OUTPUT_FOLDER, exist_ok=True)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def cleanup_old_files():
    now = time.time()
    for folder in [UPLOAD_FOLDER, OUTPUT_FOLDER]:
        if not os.path.exists(folder): continue
        for filename in os.listdir(folder):
            filepath = os.path.join(folder, filename)
            if filename.startswith('.'): continue
            try:
                if now - os.path.getmtime(filepath) > 1800: # 30 minutes
                    if os.path.isfile(filepath): os.remove(filepath)
                    elif os.path.isdir(filepath): shutil.rmtree(filepath)
            except Exception as e:
                print(f"Error removing {filepath}: {e}")

# --- IMAGE PROCESSING UTILS ---
def resize_image_task(img, target_w, target_h, mode):
    if mode == 'stretch':
        return img.resize((target_w, target_h), Image.Resampling.BICUBIC)
    elif mode == 'cover':
        return ImageOps.fit(img, (target_w, target_h), method=Image.Resampling.BICUBIC, centering=(0.5, 0.5))
    elif mode == 'contain':
        base = Image.new('RGBA', (target_w, target_h), (0,0,0,0))
        thumb = img.copy()
        thumb.thumbnail((target_w, target_h), Image.Resampling.BICUBIC)
        left = (target_w - thumb.width) // 2
        top = (target_h - thumb.height) // 2
        base.paste(thumb, (left, top))
        return base
    return img

def get_bg_image(w, h, fill, out_mode, bg_color_map):
    if fill in bg_color_map:
        val = bg_color_map[fill]
        return Image.new(out_mode, (w, h), val)
    elif fill.startswith('#'):
        try:
            val = ImageColor.getcolor(fill, 'RGBA' if out_mode == 'RGBA' else 'RGB')
            return Image.new(out_mode, (w, h), val)
        except:
            return Image.new(out_mode, (w, h), (255, 255, 255))
    return Image.new(out_mode, (w, h), (255, 255, 255))

# --- API ENDPOINTS ---

@app.get("/api/health")
def health():
    return {"status": "Studio Engine Online"}

@app.post("/api/upload")
async def upload_files(file: List[UploadFile] = File(...)):
    try:
        cleanup_old_files()
        uploaded_files = []
        for f in file:
            filename = f.filename
            if not filename: continue
            ext = os.path.splitext(filename)[1].lower().replace('.', '')
            if ext not in ALLOWED_EXTENSIONS_IMG: continue
            
            unique_name = f"{uuid.uuid4()}.{ext}"
            filepath = os.path.join(UPLOAD_FOLDER, unique_name)
            
            contents = await f.read()
            with open(filepath, "wb") as buffer:
                buffer.write(contents)
                
            info = {
                'id': unique_name,
                'filename': filename,
                'size': len(contents),
                'width': 0, 'height': 0,
                'type': 'image'
            }
            try:
                with Image.open(BytesIO(contents)) as img:
                    info['width'], info['height'] = img.size
            except: pass
            
            uploaded_files.append(info)
        return {"success": True, "files": uploaded_files}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

@app.post("/api/process")
async def process_images(data: Request):
    body = await data.json()
    file_ids = body.get('ids', [])
    if not file_ids: return {"error": "No Assets Queued"}
    
    async def generate():
        total_files = len(file_ids)
        outputs = []
        bg_color_map = {'white': (255, 255, 255), 'black': (0, 0, 0), 'transparent': (0, 0, 0, 0)}
        
        for idx, file_id in enumerate(file_ids):
            source_path = os.path.join(UPLOAD_FOLDER, file_id)
            if not os.path.exists(source_path): continue
            
            yield json.dumps({
                'type': 'progress',
                'current': idx + 1,
                'total': total_files,
                'message': f"Calculating Batch {idx + 1}..."
            }) + "\n"
            
            try:
                img = Image.open(source_path)
                if img.mode not in ['RGBA', 'RGB']: img = img.convert('RGB')
                
                # BG REMOVE
                if body.get('bg_remove', False):
                    yield json.dumps({'type': 'progress', 'message': "Extracting Background..."}) + "\n"
                    img = rembg_remove(img)
                
                requests = body.get('requests', [])
                if not requests:
                    requests = [{'width': img.width, 'height': img.height, 'mode': 'stretch', 'label': 'output'}]
                
                format_opt = body.get('format', 'JPEG').upper()
                quality = int(body.get('quality', 90))
                bg_fill = body.get('bg_fill', 'white')
                
                for req in requests:
                    req_w = int(req.get('width'))
                    req_h = int(req.get('height'))
                    mode = req.get('mode', 'contain')
                    label = req.get('label', 'processed')
                    
                    current_img = img.copy()
                    
                    # CROP
                    crop_data = req.get('crop')
                    if crop_data:
                        cx, cy = int(crop_data['x']), int(crop_data['y'])
                        cw, ch = int(crop_data['w']), int(crop_data['h'])
                        if cw > 0 and ch > 0: current_img = current_img.crop((cx, cy, cx + cw, cy + ch))
                    
                    processed = resize_image_task(current_img, req_w, req_h, mode)
                    
                    out_mode = 'RGB' if (format_opt == 'JPEG' and bg_fill != 'transparent') else 'RGBA'
                    final = get_bg_image(req_w, req_h, bg_fill, out_mode, bg_color_map)
                    
                    if out_mode == 'RGBA' and format_opt != 'JPEG':
                        if processed.mode != 'RGBA': processed = processed.convert('RGBA')
                        final.alpha_composite(processed)
                    else:
                        if processed.mode == 'RGBA':
                             # Final is RGB, need to paste with alpha channel as mask
                             final.paste(processed, (0,0), processed.split()[-1])
                        else: 
                            if final.mode == 'RGB' and processed.mode == 'RGBA':
                                processed = processed.convert('RGB')
                            final.paste(processed, (0,0))
                    
                    if format_opt == 'JPEG' and final.mode == 'RGBA':
                        final = final.convert('RGB')
                    
                    out_filename = f"{uuid.uuid4()}.{format_opt.lower()}"
                    out_path = os.path.join(OUTPUT_FOLDER, out_filename)
                    
                    save_kwargs = {}
                    if format_opt in ['JPEG', 'WEBP']: save_kwargs['quality'] = quality
                    
                    final.save(out_path, format=format_opt, **save_kwargs)
                    
                    orig_size = os.path.getsize(source_path)
                    new_size = os.path.getsize(out_path)
                    saving = ((orig_size - new_size) / orig_size) * 100 if orig_size > 0 else 0
                    
                    outputs.append({
                        'id': file_id,
                        'filename': out_filename,
                        'size': new_size,
                        'saving': round(saving, 1),
                        'format': format_opt,
                        'url': f'/api/download/{out_filename}'
                    })
            except Exception as e:
                print(f"Process fail {file_id}: {e}")
        
        yield json.dumps({'type': 'complete', 'success': True, 'outputs': outputs}) + "\n"

    return StreamingResponse(generate(), media_type="application/x-ndjson")

@app.get("/api/preview/{filename}")
def preview_file(filename: str):
    path = os.path.join(UPLOAD_FOLDER, filename)
    if not os.path.exists(path):
        return JSONResponse(status_code=404, content={"error": "Not found"})
    
    # Handle browser-incompatible formats for preview
    ext = os.path.splitext(filename)[1].lower().replace('.', '')
    if ext in ['heic', 'heif']:
        try:
            with Image.open(path) as img:
                if img.mode != 'RGB' and img.mode != 'RGBA':
                    img = img.convert('RGB')
                buffer = BytesIO()
                # Use JPEG for speed in preview
                img.save(buffer, format="JPEG", quality=75)
                buffer.seek(0)
                return StreamingResponse(buffer, media_type="image/jpeg")
        except Exception as e:
            print(f"Preview conversion error: {e}")

    return FileResponse(path)

@app.get("/api/download/{filename}")
def download_file(filename: str):
    path = os.path.join(OUTPUT_FOLDER, filename)
    if os.path.exists(path): return FileResponse(path, filename=filename)
    return JSONResponse(status_code=404, content={"error": "Not found"})

@app.post("/api/zip")
async def create_zip(data: Request):
    body = await data.json()
    fnames = body.get('filenames', [])
    if not fnames: return JSONResponse(status_code=400, content={"error": "NO_ASSETS"})
    
    zip_name = f"bundle_{int(time.time())}.zip"
    zip_path = os.path.join(OUTPUT_FOLDER, zip_name)
    
    with zipfile.ZipFile(zip_path, 'w') as z:
        for f in fnames:
            p = os.path.join(OUTPUT_FOLDER, f)
            if os.path.exists(p): z.write(p, f)
            
    return {"success": True, "url": f"/api/download/{zip_name}"}


@app.get("/")
async def root():
    return JSONResponse(
        status_code=200, 
        content={
            "success": True, 
            "message": "Photo resizer server is active",
            "path": "/"
        }
    )

@app.get("/{path:path}")
async def catch_all(path: str):
    return JSONResponse(
        status_code=200, 
        content={
            "success": True, 
            "message": "Photo resizer server is active",
            "captured_path": f"/{path}"
        }
    )

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=3020)
