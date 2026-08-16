# syntax=docker/dockerfile:1
ARG PYTHON_VERSION=3.12
FROM python:${PYTHON_VERSION}-slim

WORKDIR /app

# Install dependencies before copying source so the pip-install layer is cached
# and only re-runs when requirements.txt actually changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Non-root system user/group the app process runs as.
RUN groupadd --system trendly \
    && useradd --system --gid trendly --home-dir /app --shell /usr/sbin/nologin trendly

COPY backend/ backend/
COPY frontend/ frontend/
COPY material/ material/

RUN chown -R trendly:trendly /app
USER trendly

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health', timeout=5)" || exit 1

# sh -c so the ${PORT:-8000} expansion works whether or not Render (or any other
# host) injects its own PORT env var; local docker compose falls back to 8000.
CMD ["sh", "-c", "uvicorn backend.app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
