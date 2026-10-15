FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DOCS_DIR=/app/data/docs \
    INDEX_DIR=/app/storage/indexes
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir --require-hashes -r requirements.txt \
    && groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --no-create-home app \
    && mkdir -p /app/storage/indexes /app/reports \
    && chown -R app:app /app/storage /app/reports
COPY app ./app
COPY scripts ./scripts
COPY eval ./eval
COPY data/docs ./data/docs
USER 10001:10001
EXPOSE 8001
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001", "--no-access-log"]
