FROM python:3.12-slim

COPY --from=denoland/deno:bin /deno /usr/local/bin/deno

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN pip install --no-cache-dir yt-dlp yt-dlp-ejs fastapi uvicorn httpx pydantic pyftpdlib

COPY server.py /app/server.py
COPY static /app/static

EXPOSE 8090

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8090"]
