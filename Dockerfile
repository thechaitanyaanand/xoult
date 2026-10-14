FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PATH="/app/.venv/bin:$PATH"
WORKDIR /app

# Dependencies first, in their own layer, so code edits don't reinstall them. Only the
# API's dependencies: ingest (fastembed, litellm) runs in GitHub Actions, which keeps
# this image small enough for a 512 MB free instance.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-default-groups --no-install-project

COPY README.md ./
COPY db ./db
COPY src ./src
RUN uv sync --locked --no-default-groups

RUN useradd --create-home app
USER app

CMD ["sh", "-c", "uvicorn xoult.api:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*'"]
