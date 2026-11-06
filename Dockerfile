FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    MCP_HOST=0.0.0.0 \
    PORT=8000

WORKDIR /app
COPY requirements.txt .

RUN python -m pip install --no-cache-dir -r requirements.txt

COPY . /app

USER nobody
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c 'import os, urllib.request; urllib.request.urlopen("http://127.0.0.1:" + os.environ["PORT"] + "/health", timeout=3).close()' || exit 1

CMD ["python", "mcp_server.py"]
