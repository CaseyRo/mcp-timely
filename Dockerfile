FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
COPY src/ ./src/

# ca-certificates: httpx2 uses the OS trust store
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates && \
    rm -rf /var/lib/apt/lists/* && \
    pip install --no-cache-dir uv && \
    uv export --frozen --no-dev --no-emit-project -o /tmp/requirements.txt && \
    pip install --no-cache-dir --require-hashes -r /tmp/requirements.txt && \
    pip install --no-cache-dir --no-deps . && \
    rm /tmp/requirements.txt && \
    addgroup --system mcp && adduser --system --ingroup mcp mcp && \
    mkdir -p /data && chown mcp:mcp /data

USER mcp

ENV TRANSPORT=http
ENV HOST=0.0.0.0
ENV TIMELY_TOKEN_FILE=/data/timely_tokens.json

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python3 -c "import urllib.request,json,sys; r=urllib.request.urlopen('http://localhost:8000/health',timeout=3); d=json.loads(r.read()); sys.exit(0 if d.get('status')=='healthy' else 1)"

CMD ["mcp-timely"]
