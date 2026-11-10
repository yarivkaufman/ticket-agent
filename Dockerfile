FROM python:3.13-slim
COPY --from=ghcr.io/astral-sh/uv:0.13.1 /uv /bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1 \
    DATA_DIR=/data CONFIG_PATH=/config/config.yaml \
    PATH="/app/.venv/bin:$PATH"

COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
RUN uv sync --locked --no-dev

EXPOSE 8097
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8097/healthz')"
CMD ["ticket-agent", "serve"]
