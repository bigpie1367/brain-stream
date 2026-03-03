FROM python:3.12-slim

# System deps: ffmpeg for audio conversion, beets
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libchromaprint-tools \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/

# Directories that will be bind-mounted or created at runtime
RUN mkdir -p /app/data/staging /app/data/music /app/data/logs /app/data/navidrome

ENV PYTHONUNBUFFERED=1
ENV CONFIG_PATH=/app/config.yaml

CMD ["python", "-m", "src.main"]
