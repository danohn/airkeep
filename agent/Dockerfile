FROM python:3.14-slim-bookworm AS builder
RUN pip install --no-cache-dir uv==0.11.15
ENV UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_COMPILE_BYTECODE=1
WORKDIR /app
COPY agent/pyproject.toml agent/uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev --no-install-project

FROM python:3.14-slim-bookworm
LABEL org.opencontainers.image.title="Airkeep LAN agent" \
      org.opencontainers.image.source="https://github.com/danohn/airkeep" \
      org.opencontainers.image.description="Local Bambu MQTT collector for hosted Airkeep"
RUN groupadd --gid 10001 airkeep && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin airkeep \
    && mkdir /data && chown 10001:10001 /data
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY agent/*.py ./agent/
COPY server/collector.py server/domain.py server/bambu-ca.pem ./server/
COPY LICENSE /usr/share/licenses/airkeep/LICENSE
ENV PATH="/app/.venv/bin:$PATH" PYTHONPATH=/app/server PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 AIRKEEP_AGENT_DATA=/data
USER 10001:10001
CMD ["python", "-m", "agent"]
