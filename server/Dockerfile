FROM node:22-bookworm-slim AS web-builder
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --ignore-scripts
COPY web/ ./
RUN npm run build

FROM python:3.14-slim-bookworm AS builder
COPY --from=ghcr.io/astral-sh/uv:0.11.15 /uv /usr/local/bin/uv
ENV UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_COMPILE_BYTECODE=1
WORKDIR /app
COPY server/pyproject.toml server/uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev --no-install-project

FROM python:3.14-slim-bookworm
LABEL org.opencontainers.image.title="Airkeep relay" \
      org.opencontainers.image.source="https://github.com/danohn/airkeep" \
      org.opencontainers.image.description="Bambu AMS telemetry, history, alerts and Apple push relay"
RUN groupadd --gid 10001 airkeep && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin airkeep \
    && mkdir /data && chown 10001:10001 /data
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY server/*.py server/bambu-ca.pem ./
COPY --from=web-builder /web/dist /app/web
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 AIRKEEP_DB_PATH=/data/airkeep.sqlite3 AIRKEEP_WEB_DIST_PATH=/app/web
USER 10001:10001
EXPOSE 8787
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 CMD ["/app/.venv/bin/python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/health', timeout=3).close()"]
CMD ["uvicorn", "app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8787", "--no-access-log"]
