FROM node:22-bookworm-slim@sha256:43ac6c60b8f89723f746e8a92ce91abd5017e627ce1ddfe4238355d3a30b772c AS frontend
WORKDIR /build/frontend
RUN corepack enable && corepack prepare pnpm@11.19.0 --activate
COPY frontend/package.json frontend/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY frontend/ ./
RUN pnpm build

FROM python:3.13-slim-bookworm@sha256:2325bb286ec344af3e5898cc224b5844e2707ac6e26b1632516fd3edc84a5e26 AS platform
COPY --from=ghcr.io/astral-sh/uv:0.11.21@sha256:ff07b86af50d4d9391d9daf4ff89ce427bc544f9aae87057e69a1cc0aa369946 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1 PATH="/app/.venv/bin:$PATH"
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY backend/ backend/
COPY worker/ worker/
COPY gateway/ gateway/
COPY alembic.ini ./
COPY --from=frontend /build/frontend/dist frontend/dist/
RUN useradd --uid 10001 --create-home talos \
    && install -d -m 0700 -o talos -g talos /var/lib/talos-provider
USER talos
CMD ["uvicorn", "backend.app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers"]

FROM platform AS verification
USER root
COPY tests/ tests/
RUN uv sync --frozen
CMD ["python", "-m", "tests.runtime_proof"]

# Native runtime: the upstream image plus a pinned key-free search plugin.
FROM ghcr.io/openclaw/openclaw@sha256:0a5ff5e682e62afa19149df126aa50063bf65ef885b5c94713ce32dc0eb12e15 AS native-runtime
USER root
COPY deploy/native/ /opt/talos-plugins/
RUN cd /opt/talos-plugins && npm ci --omit=dev --ignore-scripts --legacy-peer-deps
USER node

# Standard forwarding proxy; agents retain their internal Docker networks.
FROM python:3.13-slim-bookworm@sha256:2325bb286ec344af3e5898cc224b5844e2707ac6e26b1632516fd3edc84a5e26 AS egress
RUN apt-get update && apt-get install -y --no-install-recommends squid && rm -rf /var/lib/apt/lists/*
COPY deploy/squid.conf /etc/squid/squid.conf
USER proxy
CMD ["squid", "-N", "-f", "/etc/squid/squid.conf"]

# Official Hermes image; native dashboard and bundled tools are unchanged.
FROM nousresearch/hermes-agent@sha256:d4da4a40cd7a28aba983775d9fd31d94cbf153eeb0cb9e844d6d0f612b7c24db AS hermes-runtime
