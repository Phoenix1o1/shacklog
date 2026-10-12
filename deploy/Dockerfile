# ShackLog hub for Proxmox (or any Docker host).
# Build:  docker build -f deploy/Dockerfile -t shacklog .
# Run:    see deploy/docker-compose.yml
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SHACKLOG_DATA=/data

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY shacklog ./shacklog

# Run as an unprivileged user; /data is the volume holding database, files and backups.
RUN useradd --system --uid 1000 --home /data shacklog && mkdir -p /data && chown shacklog /data
USER shacklog
VOLUME ["/data"]
EXPOSE 8573

HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8573/healthz', timeout=4)"

CMD ["python", "-m", "shacklog", "serve", "--host", "0.0.0.0", "--port", "8573"]
