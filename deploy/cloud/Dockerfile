FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --uid 1000 --create-home app \
    && mkdir -p /data /app/logs \
    && chown app:app /data /app/logs
COPY app ./app
COPY prompts ./prompts
COPY data ./data
COPY web/texty/public ./web/texty/public
USER app
EXPOSE 8000
# Exactly one scheduler owns this database. Do not add workers or replicas.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-access-log"]
