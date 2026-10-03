# Version matches the pinned Python Playwright dependency and bundled browsers.
FROM mcr.microsoft.com/playwright/python:v1.58.0-noble
WORKDIR /app
COPY bridge/google_voice/requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir -r /tmp/requirements.lock
COPY app /app/app
COPY prompts /app/prompts
COPY data /app/data
COPY tests /app/tests
COPY pytest.ini /app/pytest.ini
COPY bridge/google_voice /app/bridge/google_voice
RUN useradd --uid 10001 --create-home bridge && mkdir -p /state && chown bridge:bridge /state
USER bridge
ENV PYTHONUNBUFFERED=1
ENV DISPLAY=:99
# No published ports, no backend credentials or profiles in the image.
ENTRYPOINT ["/bin/bash", "/app/bridge/google_voice/container-entrypoint.sh"]
CMD ["python", "-m", "app.integrations.voice_worker", "--config", "/state/config.json"]
