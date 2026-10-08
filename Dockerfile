# savetokens server: every machine and account adds up here; forecasts with Ephemeris through Gnomon.
#   docker run -d --name savetokens -p 8787:8787 -v savetokens:/data -e EPHEMERIS_API_KEY=... ghcr.io/...
# The first start prints a token (docker logs savetokens); add it on each machine with
#   savetokens install --server http://<host>:8787 --token <token>
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    SAVETOKENS_HOME=/data/home

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install . && useradd --uid 10001 --create-home savetokens && mkdir /data && chown savetokens /data

USER savetokens
VOLUME /data
EXPOSE 8787
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/healthz', timeout=4)"
CMD ["savetokens", "server", "--host", "0.0.0.0", "--port", "8787", "--data", "/data"]
