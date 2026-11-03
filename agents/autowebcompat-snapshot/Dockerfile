FROM python:3.14 AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ENV UV_PROJECT_ENVIRONMENT=/opt/venv
ENV UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Install external deps without building workspace members.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=VERSION,target=VERSION \
    uv sync --frozen --no-dev --no-install-workspace --package hackbot-agent-autowebcompat-snapshot

RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,target=/app,rw \
    uv sync --locked --no-dev --no-editable --package hackbot-agent-autowebcompat-snapshot

FROM python:3.14

WORKDIR /app

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV PATH="/opt/venv/bin:$PATH"

# Node.js for the DevTools MCP servers and Puppeteer scripts, plus the shared
# libraries the browsers need. The browsers themselves are downloaded at
# startup (see browser.py).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        nodejs npm \
        ca-certificates \
        # Firefox runtime deps.
        libgtk-3-0 libdbus-glib-1-2 libx11-xcb1 libxtst6 libxt6 libpci3 \
        # Chrome runtime deps.
        libnss3 libnspr4 libatk1.0-0t64 libatk-bridge2.0-0t64 libatspi2.0-0t64 \
        libcups2t64 libdbus-1-3 libgbm1 libxcomposite1 libxdamage1 libxfixes3 \
        libxrandr2 libxkbcommon0 libasound2t64 libpango-1.0-0 libcairo2 \
        fonts-liberation \
        ffmpeg \
        fluxbox \
        xvfb \
    && rm -rf /var/lib/apt/lists/*

COPY agents/autowebcompat-snapshot/package.json \
     agents/autowebcompat-snapshot/package-lock.json \
     /app/snapshot/
RUN cd /app/snapshot && npm ci --omit=dev

RUN useradd --create-home --shell /bin/bash agent \
    && chown agent:agent /app/snapshot

ENV SCREEN_WIDTH=1280
ENV SCREEN_HEIGHT=1024
ENV SCREEN_DEPTH=24
ENV DISPLAY=:99.0

USER agent

COPY --from=builder /opt/venv /opt/venv

CMD ["python", "-m", "hackbot_agents.autowebcompat_snapshot"]
