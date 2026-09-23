FROM python:3.11-slim

# onnxruntime (Silero VAD) needs libgomp at runtime; it is not in -slim.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py bot.py ./

# Cloud Run injects PORT; server.py reads it and falls back to 7860 locally.
ENV PORT=8080
EXPOSE 8080

CMD ["python", "server.py"]
