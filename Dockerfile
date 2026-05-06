FROM python:3.11-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends cron \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./
COPY web/ ./web/

# Controller runs every minute via cron
RUN printf '* * * * * root /usr/local/bin/python /app/main.py >> /data/logs/cron.log 2>&1\n' \
    > /etc/cron.d/pvcontroller \
    && chmod 0644 /etc/cron.d/pvcontroller

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/status')" || exit 1

ENTRYPOINT ["/entrypoint.sh"]
