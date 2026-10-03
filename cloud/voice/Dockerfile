FROM node:22-bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends chromium ca-certificates dumb-init \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --ignore-scripts
COPY core.mjs browser.mjs server.mjs ./
RUN mkdir -p /data/config /data/cache && chown -R node:node /data
# Chromium's crash reporter and cache need writable XDG paths even with a
# read-only image. Keep them on the same private volume as the browser profile.
ENV VOICE_DATA_DIR=/data VOICE_BROWSER_PATH=/usr/bin/chromium PORT=8765 \
    XDG_CONFIG_HOME=/data/config XDG_CACHE_HOME=/data/cache
USER node
EXPOSE 8765
VOLUME ["/data"]
ENTRYPOINT ["dumb-init", "--"]
CMD ["node", "server.mjs"]
