FROM python:3.10-slim

# Prevent Python from writing .pyc files and enable unbuffered logging
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Install system dependencies for OpenCV and rembg
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 \
    libglib2.0-0 \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*
 
# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create directories for uploads and outputs
RUN mkdir -p /app/uploads /app/outputs && chmod -R 777 /app/uploads /app/outputs

# Expose the API port
EXPOSE 3020

# Set environment variables for the application
ENV BASE_DIR=/app
ENV UPLOAD_FOLDER=/app/uploads
ENV OUTPUT_FOLDER=/app/outputs

# Use gunicorn with uvicorn workers for high-concurrency performance
CMD ["gunicorn", "main:app", "-w", "4", "-k", "uvicorn.workers.UvicornWorker", "-b", "0.0.0.0:3020", "--timeout", "120"]
