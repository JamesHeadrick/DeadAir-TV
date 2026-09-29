FROM python:3.12-slim-bookworm

# adb is only used when ENABLE_ADB=true, but it's small enough to always ship.
RUN apt-get update \
    && apt-get install -y --no-install-recommends adb \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# The entrypoint makes /config and /data writable, then drops to PUID:PGID
# (default 1000:1000) before starting the app; see docker-entrypoint.sh.
# HOME=/data keeps adb's RSA key (~/.android) in the persistent volume, so the
# TV only asks you to authorize the connection once.
RUN useradd --uid 1000 --home-dir /data --no-create-home app \
    && mkdir -p /data /config && chown app:app /data /config
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
ENTRYPOINT ["docker-entrypoint.sh"]
ENV HOME=/data \
    PYTHONUNBUFFERED=1 \
    CONFIG_PATH=/config/config.yaml \
    DB_PATH=/data/deadair.db

EXPOSE 8000
HEALTHCHECK --interval=60s --timeout=5s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
