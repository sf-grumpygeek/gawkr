FROM python:3.12-slim

# libgomp1 for onnxruntime (fastembed); ffmpeg to extract audio from event clips
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY gawkr ./gawkr

RUN python -c "from fastembed import TextEmbedding; TextEmbedding(model_name='BAAI/bge-small-en-v1.5')" || true

CMD ["python", "-m", "gawkr"]
