# ShackLog hub for Proxmox (or any Docker host).
# Build:  docker build -f deploy/docker/Dockerfile -t shacklog .   (from the project root)
# Run:    see deploy/docker/docker-compose.yml
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SHACKLOG_DATA=/data \
    SHACKLOG_RUNTIME=docker

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY shacklog ./shacklog

# Runs as an unprivileged user; /data is the volume holding database, files and backups.
# The entrypoint starts as root only to give /data to that user (a bind-mount folder
# Docker creates is owned by root), then drops privileges.
RUN useradd --system --user-group --uid 1000 --home /data shacklog && mkdir -p /data && chown shacklog:shacklog /data
COPY --chmod=0755 deploy/docker/entrypoint.sh /usr/local/bin/shacklog-entrypoint
VOLUME ["/data"]
EXPOSE 8573

HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8573/healthz', timeout=4)"

ENTRYPOINT ["shacklog-entrypoint"]
CMD ["python", "-m", "shacklog", "serve", "--host", "0.0.0.0", "--port", "8573"]
