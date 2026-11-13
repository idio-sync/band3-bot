# syntax=docker/dockerfile:1

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CONFIG_DIR=/config \
    LIBRARY_PAGE=/app/web/library.html \
    PORT=8080

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
# band3's library page, copied from band3 by scripts/sync_page.py
COPY web/library.html /app/web/library.html

# Unraid convention: nobody:users (99:100), so /config is editable from the share.
RUN mkdir -p /config && chown 99:100 /config
USER 99:100
VOLUME /config
EXPOSE 8080/tcp 21072/udp

HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
  CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT', '8080'), timeout=4)"

CMD ["python", "-m", "app"]
