FROM node:22-bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends chromium ca-certificates dumb-init \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --ignore-scripts
COPY core.mjs browser.mjs server.mjs ./
RUN mkdir -p /data && chown node:node /data
ENV VOICE_DATA_DIR=/data VOICE_BROWSER_PATH=/usr/bin/chromium PORT=8765
USER node
EXPOSE 8765
VOLUME ["/data"]
ENTRYPOINT ["dumb-init", "--"]
CMD ["node", "server.mjs"]
