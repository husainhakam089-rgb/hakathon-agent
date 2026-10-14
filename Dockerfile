# The program online (stage 22). The notebook and its backups live on a disk mounted at /data,
# so a redeploy or a restart never wipes them.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    TZ=Asia/Baghdad \
    DB_PATH=/data/debt.db BACKUP_DIR=/data/backups UPLOAD_DIR=/data/uploads \
    REQUIRE_PASSWORD=1 NO_BROWSER=1 PORT=8000

RUN apt-get update && apt-get install -y --no-install-recommends tzdata && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY static ./static

RUN mkdir -p /data
VOLUME ["/data"]
EXPOSE 8000
# --proxy-headers: behind the host's HTTPS, links and the login cookie use https
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*'"]
