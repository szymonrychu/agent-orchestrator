FROM node:24-bookworm-slim AS frontend
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY openapi.json /build/openapi.json
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim-bookworm
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_PROJECT_ENVIRONMENT=/app/.venv UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && uv sync --frozen --no-dev \
    && apt-get purge -y git && apt-get autoremove -y && rm -rf /var/lib/apt/lists/* /root/.cache
COPY --from=frontend /build/frontend/dist /app/static
ENV PATH="/app/.venv/bin:$PATH" ORCH_STATIC_DIR=/app/static
USER 1000
ENTRYPOINT ["orch"]
