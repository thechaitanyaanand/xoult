# Build stage: uv resolves and installs; neither uv nor its download cache ships.
FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
WORKDIR /app

# Dependencies first, in their own layer, so code edits don't reinstall them. Only the
# API's dependencies: ingest (fastembed, litellm) runs in GitHub Actions, which keeps
# this image small enough for a 512 MB free instance.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-default-groups --no-install-project

COPY README.md ./
COPY db ./db
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-default-groups

# Runtime stage: same base image, so the virtualenv's interpreter path still matches.
FROM python:3.12-slim
RUN useradd --create-home app
WORKDIR /app
COPY --from=build --chown=app /app /app
ENV PATH="/app/.venv/bin:$PATH"
USER app

CMD ["sh", "-c", "uvicorn xoult.api:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*'"]
