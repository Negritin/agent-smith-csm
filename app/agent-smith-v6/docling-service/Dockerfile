FROM python:3.11-slim

WORKDIR /app

# System dependencies for Docling (OCR, image processing)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libgl1 \
    libglib2.0-0 \
    tesseract-ocr \
    tesseract-ocr-por \
    && rm -rf /var/lib/apt/lists/*

# Install PyTorch CPU-only FIRST (prevents Docling from pulling full PyTorch).
# torch/torchvision are deliberately NOT in the hash-locked requirements.txt
# (their CPU wheels live on the PyTorch index, not PyPI); installing them here
# satisfies Docling's torch requirement before the hash-locked install below.
RUN pip install --no-cache-dir \
    torch torchvision \
    --index-url https://download.pytorch.org/whl/cpu

# Install Python dependencies (fully pinned + hash-locked, compiled from requirements.in)
COPY requirements.txt .
RUN pip install --no-cache-dir --require-hashes -r requirements.txt

# Copy application
COPY . .

EXPOSE 8001

# Default: run API server
# Override in Railway for worker: celery -A app.celery_app worker -Q docling -c 2 --loglevel=info
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001", "--workers", "2"]
