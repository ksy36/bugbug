FROM python:3.12 AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Workspace metadata first so the dep-download layer caches independently
# of source changes.
COPY pyproject.toml uv.lock VERSION ./
COPY http_service/pyproject.toml ./http_service/
COPY services/hackbot-api/pyproject.toml ./services/hackbot-api/
COPY agents/bug-fix/pyproject.toml ./agents/bug-fix/
COPY libs/hackbot-runtime/pyproject.toml ./libs/hackbot-runtime/

# Install external deps without building workspace members.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-workspace --package hackbot-agent-bug-fix

# Workspace members the agent image actually needs (source included).
COPY agents/bug-fix ./agents/bug-fix
COPY bugbug ./bugbug
COPY libs/hackbot-runtime ./libs/hackbot-runtime

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --package hackbot-agent-bug-fix

FROM python:3.12 AS base

COPY --from=builder /app /app
WORKDIR /app/agents/bug-fix

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV PATH="/app/.venv/bin:$PATH"

FROM base AS agent

# --- Web-compat browser stack -------------------------------------------- #
# Node 20 (runs the firefox-devtools MCP via npx), xvfb (virtual display),
# and Firefox Nightly's runtime libraries. Only used when WEBCOMPAT_TOOLS=true,
# but baked in unconditionally so the single agent image covers both modes.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl xz-utils xvfb xauth \
        # Firefox runtime deps
        libgtk-3-0 libdbus-glib-1-2 libx11-xcb1 libxtst6 libxt6 \
        libpci3 libasound2 libegl1 \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# Pre-fetch the MCP server into the npm cache so the first run doesn't pay the
# download cost (npx still resolves it at runtime).
RUN npm install -g @mozilla/firefox-devtools-mcp-moz

COPY agents/bug-fix/scripts/install-firefox-nightly.sh /tmp/install-firefox-nightly.sh
RUN chmod +x /tmp/install-firefox-nightly.sh \
    && /tmp/install-firefox-nightly.sh /opt/firefox \
    && rm /tmp/install-firefox-nightly.sh

ENV FIREFOX_PATH=/opt/firefox/firefox

RUN useradd --create-home --shell /bin/bash agent \
    && mkdir -p /workspace \
    && chown agent:agent /workspace

USER agent

# xvfb-run provides a virtual display; harmless with --headless, required if a
# tool ever launches Firefox non-headless.
CMD ["xvfb-run", "-a", "python", "-m", "agent_runner"]

FROM base AS broker

RUN useradd --create-home --shell /bin/bash broker

USER broker

EXPOSE 8765

CMD ["python", "-m", "broker"]
