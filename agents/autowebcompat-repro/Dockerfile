FROM python:3.12 AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ENV UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /app

# Install external deps without building workspace members.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=VERSION,target=VERSION \
    uv sync --frozen --no-dev --no-install-workspace --package hackbot-agent-autowebcompat-repro

RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,target=/app,rw \
    uv sync --locked --no-dev --no-editable --package hackbot-agent-autowebcompat-repro

FROM python:3.12 AS base

WORKDIR /app

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV PATH="/opt/venv/bin:$PATH"

FROM base AS agent

# The agent needs Node.js + npm to run the DevTools MCP servers (npm packages;
# the python base ships neither) and the shared libraries the browsers require
# to run headless. The browser binaries themselves are downloaded at agent
# startup (a fresh build per run): Firefox via mozdownload/mozinstall and Chrome
# for Testing via the Chrome-for-Testing JSON API (see browser.py).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        # Node.js, to run the Firefox and Chrome DevTools MCP servers.
        nodejs npm \
        # Shared by both browsers.
        ca-certificates \
        # Firefox runtime deps.
        libgtk-3-0 libdbus-glib-1-2 libx11-xcb1 libxtst6 libxt6 libpci3 \
        # Chrome runtime deps.
        libnss3 libnspr4 libatk1.0-0t64 libatk-bridge2.0-0t64 libatspi2.0-0t64 \
        libcups2t64 libdbus-1-3 libgbm1 libxcomposite1 libxdamage1 libxfixes3 \
        libxrandr2 libxkbcommon0 libasound2t64 libpango-1.0-0 libcairo2 \
        fonts-liberation \
        # Codecs and audio/video related dependencies
        ffmpeg \
        # Window manager and display backend
        fluxbox \
        xvfb \
        # Android SDK tooling: sdkmanager/avdmanager are Java programs, and
        # unzip unpacks the command-line tools.
        unzip \
        default-jre-headless \
        # Shared libraries the emulator binary dlopens even with -no-window.
        libpulse0 libxkbfile1 libglu1-mesa \
    && rm -rf /var/lib/apt/lists/*

# Install the Android SDK and create the AVD at build time. Doing this at
# runtime instead would add minutes to every run; baking it in means container
# start goes straight to booting the emulator. The emulator still needs
# /dev/kvm from the host at runtime, which is granted by the deployment (see
# compose.yml), not by the build.
ENV ANDROID_SDK_ROOT=/opt/android-sdk \
    ANDROID_HOME=/opt/android-sdk \
    ANDROID_AVD_HOME=/opt/android-sdk/.android/avd \
    ANDROID_USER_HOME=/opt/android-sdk/.android \
    ANDROID_EMULATOR_HOME=/opt/android-sdk/.android \
    ADB_VENDOR_KEYS=/opt/android-sdk/.android/adbkey \
    ANDROID_AVD_NAME=webcompat

# Google publishes the emulator for linux-x86_64 only; there is no
# linux-aarch64 build, so the agent image must be built for amd64 to run the
# emulator. The system image ABI is derived from the build platform so the
# image stays correct if that ever changes.
# API 36's Play Store image includes mobile Chrome compatible with the MCP.
ARG ANDROID_API=36
ARG CMDLINE_TOOLS_VERSION=11076708
ARG TARGETARCH

RUN set -eux; \
    case "${TARGETARCH:-amd64}" in \
        amd64) abi=x86_64 ;; \
        arm64) abi=arm64-v8a ;; \
        *) echo "unsupported TARGETARCH: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    mkdir -p "$ANDROID_SDK_ROOT/cmdline-tools"; \
    curl -fsSL -o /tmp/cmdline-tools.zip \
        "https://dl.google.com/android/repository/commandlinetools-linux-${CMDLINE_TOOLS_VERSION}_latest.zip"; \
    unzip -q /tmp/cmdline-tools.zip -d "$ANDROID_SDK_ROOT/cmdline-tools"; \
    mv "$ANDROID_SDK_ROOT/cmdline-tools/cmdline-tools" \
       "$ANDROID_SDK_ROOT/cmdline-tools/latest"; \
    rm /tmp/cmdline-tools.zip; \
    export PATH="$ANDROID_SDK_ROOT/cmdline-tools/latest/bin:$PATH"; \
    yes | sdkmanager --licenses > /dev/null; \
    image="system-images;android-${ANDROID_API};google_apis_playstore;${abi}"; \
    sdkmanager --install "platform-tools" "emulator" \
        "platforms;android-${ANDROID_API}" "$image" > /dev/null; \
    # The data partition must be sized at creation: editing config.ini later
    # does not resize an existing userdata image, and the default leaves too
    # little room for the Fenix APK and compiled app code.
    mkdir -p "$ANDROID_AVD_HOME"; \
    echo no | avdmanager create avd --force \
        --name "$ANDROID_AVD_NAME" --package "$image" \
        --path "$ANDROID_AVD_HOME/${ANDROID_AVD_NAME}.avd"; \
    printf 'disk.dataPartition.size=12G\nhw.ramSize=4096\n' \
        >> "$ANDROID_AVD_HOME/${ANDROID_AVD_NAME}.avd/config.ini"
ENV PATH="/opt/android-sdk/platform-tools:/opt/android-sdk/emulator:$PATH"

# Install the DevTools MCP servers and Puppeteer from the pinned
# package.json/lockfile.
COPY agents/autowebcompat-repro/package.json \
     agents/autowebcompat-repro/package-lock.json \
     agents/autowebcompat-repro/repro_reference.mjs \
     /app/repro/
RUN cd /app/repro && npm ci --omit=dev

# hackbot.toml lives at the agent root (not inside the package), so copy it into
# the working dir; the runtime discovers it there (cwd) at startup.
COPY agents/autowebcompat-repro/hackbot.toml /app/hackbot.toml

# The emulator writes to the AVD's disk images at runtime, so the agent user
# needs to own them. A fresh container restores the pristine AVD from the
# image layer, so state does not leak between runs.
RUN useradd --create-home --shell /bin/bash agent \
    && mkdir -p /workspace \
    && chown agent:agent /workspace /app/repro \
    && chown -R agent:agent /opt/android-sdk/.android /opt/android-sdk/emulator

ENV SCREEN_WIDTH=1280
ENV SCREEN_HEIGHT=1024
ENV SCREEN_DEPTH=24
ENV DISPLAY=:99.0

USER agent

COPY --from=builder /opt/venv /opt/venv

CMD ["python", "-m", "hackbot_agents.autowebcompat_repro"]

FROM base AS broker

RUN useradd --create-home --shell /bin/bash broker

USER broker

EXPOSE 8765

COPY --from=builder /opt/venv /opt/venv

CMD ["python", "-m", "hackbot_agents.autowebcompat_repro.broker"]
