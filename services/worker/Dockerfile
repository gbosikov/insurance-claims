FROM python:3.11-slim

WORKDIR /app

# Системные зависимости для OpenCV + torch
RUN apt-get update && apt-get install -y \
    libglib2.0-0 libsm6 libxext6 libxrender-dev libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# multilingual-e5-large (~1.1 GB) скачивается при первом старте
# и кэшируется в volume model_cache
ENV TRANSFORMERS_CACHE=/app/.cache/huggingface

CMD ["celery", "-A", "services.worker.celery_app", "worker", "--loglevel=info", "--concurrency=4"]
