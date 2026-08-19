# syntax=docker/dockerfile:1.7

FROM node:22-bookworm-slim AS frontend-builder
WORKDIR /build/front
COPY front/package.json front/package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci
COPY front/ ./
RUN npm run build

FROM ubuntu:24.04 AS python-builder
ARG DEBIAN_FRONTEND=noninteractive
ENV PATH=/opt/venv/bin:$PATH \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    XDG_CACHE_HOME=/opt/camoufox-cache

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates python3 python3-pip python3-venv \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY requirements.txt ./
COPY docker/install_camoufox.py ./install_camoufox.py
RUN python3 -m venv /opt/venv \
    && pip install --upgrade pip \
    && pip install -r requirements.txt

# 浏览器引擎直接内置到镜像。不用 `camoufox fetch` CLI：它在失败时仍返回 0。
RUN --mount=type=secret,id=GITHUB_TOKEN,required=false \
    sh -c 'if [ -f /run/secrets/GITHUB_TOKEN ]; then export GITHUB_TOKEN="$(cat /run/secrets/GITHUB_TOKEN)"; fi; \
    python install_camoufox.py'

FROM ubuntu:24.04 AS runtime
ARG DEBIAN_FRONTEND=noninteractive
ARG APP_UID=10001
ARG APP_GID=10001
ARG GROK_REGISTER_VERSION=""

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/home/app \
    XDG_CACHE_HOME=/opt/camoufox-cache \
    CLOAKBROWSER_CACHE_DIR=/app/data/cloakbrowser-cache \
    CLOAKBROWSER_AUTO_UPDATE=false \
    DISPLAY=:99 \
    GROK_WEB_HOST=0.0.0.0 \
    GROK_WEB_PORT=8787 \
    GROK_CONFIG_FILE=/app/data/config.json \
    GROK_FORCE_HEADED=1

# Camoufox/Firefox 与 CloakBrowser/Chromium 有头模式依赖 + Xvfb 虚拟显示器。
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates dumb-init gosu procps python3 xvfb xauth \
        libasound2t64 libatk1.0-0t64 libavcodec60 \
        libatk-bridge2.0-0t64 libatspi2.0-0t64 libcups2t64 \
        libcairo-gobject2 libcairo2 libdbus-1-3 \
        libdrm2 libfontconfig1 libfreetype6 libgbm1 libgdk-pixbuf-2.0-0 \
        libglib2.0-0t64 libgtk-3-0t64 libpango-1.0-0 \
        libpangocairo-1.0-0 libx11-6 libx11-xcb1 \
        libxcb-shm0 libxcb1 libxcomposite1 libxcursor1 \
        libxdamage1 libxext6 libxfixes3 libxi6 libxrandr2 \
        libxkbcommon0 libxrender1 libxshmfence1 libxss1 libxtst6 \
        libnspr4 libnss3 fonts-freefont-ttf fonts-liberation \
        fonts-noto-color-emoji fonts-unifont fonts-wqy-zenhei \
    && groupadd --gid "$APP_GID" app \
    && useradd --uid "$APP_UID" --gid "$APP_GID" --create-home --shell /bin/bash app \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY --chown=app:app --from=python-builder /opt/venv /opt/venv
COPY --chown=app:app --from=python-builder /opt/camoufox-cache /opt/camoufox-cache
COPY --chown=app:app backend/ ./backend/
COPY --chown=app:app config.example.json requirements.txt VERSION ./
COPY --chown=app:app --from=frontend-builder /build/front/dist ./front/dist/
COPY --chown=app:app --chmod=755 docker/entrypoint.sh ./docker/entrypoint.sh
COPY --chown=app:app docker/camoufox_smoke.py ./docker/camoufox_smoke.py
COPY --chown=app:app docker/cloakbrowser_smoke.py ./docker/cloakbrowser_smoke.py

RUN if [ -n "$GROK_REGISTER_VERSION" ]; then \
      printf '%s\n' "$GROK_REGISTER_VERSION" > /app/VERSION; \
    fi \
    && chown app:app /app/VERSION \
    && install -d -o app -g app /app/data /app/logs

VOLUME ["/app/data", "/app/logs"]
EXPOSE 8787

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/api/health', timeout=3).read()"

ENTRYPOINT ["/usr/bin/dumb-init", "--", "/app/docker/entrypoint.sh"]
CMD ["python", "-m", "backend.web.cli", "--host", "0.0.0.0", "--port", "8787"]
