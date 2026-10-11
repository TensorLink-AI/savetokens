# savetokens server: every machine and account adds up here; forecasts with Ephemeris through Gnomon.
#   docker run -d --name savetokens -p 8787:8787 -v savetokens:/data -e EPHEMERIS_API_KEY=... ghcr.io/...
# The first start prints the line that adds a machine (docker logs savetokens):
#   curl -fsSL http://<host>:8787/join | sh -s <code>    (the code is in the same logs)
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    SAVETOKENS_HOME=/data/home

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install . && useradd --uid 10001 --create-home savetokens && mkdir /data && chown savetokens /data

VOLUME /data
EXPOSE 8787
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\", 8787)}/healthz', timeout=4)"
# Hosts that mount a disk owned by root (Render, Railway) get it handed to the savetokens user first;
# the server itself never runs as root. PORT is set by those hosts; 8787 otherwise.
CMD ["sh", "-c", "chown -R savetokens /data 2>/dev/null || true; exec setpriv --reuid=savetokens --regid=savetokens --init-groups env HOME=/home/savetokens savetokens server --host 0.0.0.0 --port \"${PORT:-8787}\" --data /data"]
