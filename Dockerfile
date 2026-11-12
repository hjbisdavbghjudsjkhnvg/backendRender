FROM python:3.11-slim

# ffmpeg for decode/transcode; yt-dlp is installed via pip
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PORT=8000
ENV CACHE_DIR=/tmp/audio_cache

# single worker: analysis is CPU-heavy and the free tier has ~0.5 CPU.
# threads let a cached request serve while another analyzes. Long timeout so a
# first-time analyze of a long track doesn't get killed.
CMD gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 300
