FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATABASE_PATH=/data/escape_room.db
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN useradd --create-home bot && mkdir -p /data && chown bot /data \
    && chmod +x /app/docker-entrypoint.sh

# /data MUST be a persistent volume, or every redeploy wipes all matches.
# The entrypoint starts as root only to fix the volume's ownership, then
# drops to the "bot" user before starting Python.
VOLUME ["/data"]
ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["python", "bot.py"]
