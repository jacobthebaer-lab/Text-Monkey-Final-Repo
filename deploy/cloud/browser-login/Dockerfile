# Reuse the connector's exact Chromium package and profile ownership.
FROM voice
USER root
RUN apt-get update && apt-get install -y --no-install-recommends \
    xvfb x11vnc novnc websockify python3 x11-utils \
    && rm -rf /var/lib/apt/lists/*
COPY start.py /app/browser-login.py
USER node
ENV DISPLAY=:99
ENTRYPOINT ["dumb-init", "--single-child", "--"]
CMD ["python3", "/app/browser-login.py"]
