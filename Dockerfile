# The program online (stage 22). The notebook and its backups live on a disk mounted at /data,
# so a redeploy or a restart never wipes them.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    TZ=Asia/Baghdad \
    DB_PATH=/data/debt.db BACKUP_DIR=/data/backups UPLOAD_DIR=/data/uploads \
    REQUIRE_PASSWORD=1 TRUST_PROXY=1 NO_BROWSER=1 PORT=8000

RUN apt-get update && apt-get install -y --no-install-recommends tzdata && rm -rf /var/lib/apt/lists/* \
    && useradd --system --uid 10001 --home-dir /app app
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY static ./static
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod 755 /usr/local/bin/docker-entrypoint.sh

# the agent writes its tool and model-error logs under /app/logs: the "app" user must be able to
RUN mkdir -p /data /app/logs && chown app:app /app/logs
VOLUME ["/data"]
EXPOSE 8000
# runs the program as the "app" user, not root (see docker-entrypoint.sh)
ENTRYPOINT ["docker-entrypoint.sh"]
# No --forwarded-allow-ips='*': it would let anyone set the client address with a header.
# The app reads the host's proxy headers itself (TRUST_PROXY=1): the last X-Forwarded-For entry
# for who is trying a password, and X-Forwarded-Proto for https links and the login cookie.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --no-proxy-headers"]
