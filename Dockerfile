FROM python:3.10-slim

WORKDIR /app

COPY requirements.txt .

# CPU-only PyTorch (smaller image; swap the index URL for a CUDA build if needed)
RUN pip install --default-timeout=1000 --no-cache-dir \
    torch --index-url https://download.pytorch.org/whl/cpu

RUN pip install --default-timeout=1000 --no-cache-dir \
    -r requirements.txt

COPY . .

CMD ["python", "-u", "main.py"]
