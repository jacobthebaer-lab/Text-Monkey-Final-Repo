FROM python:3.12-slim-bookworm AS certificates

FROM node:22-bookworm-slim
# Bootstrap HTTPS apt from the CA bundle in the official Python base image.
COPY --from=certificates /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-certificates.crt
RUN sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update && apt-get install -y --no-install-recommends chromium ca-certificates dumb-init \
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
ENTRYPOINT ["dumb-init", "--single-child", "--"]
CMD ["node", "server.mjs"]
