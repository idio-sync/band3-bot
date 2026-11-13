# syntax=docker/dockerfile:1

# Stage 1: band3's library page (web/library.html), from a band3 tag or branch.
# For a local build with a page that isn't upstream yet, copy it to
# web/library.html here and build with --build-arg LOCAL_PAGE=1.
FROM python:3.12-slim AS page
ARG BAND3_REF=main
ARG LOCAL_PAGE=0
COPY web/ /local/
RUN mkdir -p /page && \
    if [ "$LOCAL_PAGE" = "1" ]; then \
      test -f /local/library.html || { echo "LOCAL_PAGE=1 but web/library.html is missing" >&2; exit 1; }; \
      cp /local/library.html /page/library.html; \
    else \
      python -c "import sys, urllib.request; \
url = 'https://raw.githubusercontent.com/idio-sync/band3_recomp/' + sys.argv[1] + '/web/library.html'; \
print('Fetching', url); \
open('/page/library.html', 'wb').write(urllib.request.urlopen(url, timeout=60).read())" "$BAND3_REF"; \
    fi

# Stage 2: the bot.
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
COPY --from=page /page/library.html /app/web/library.html

# Unraid convention: nobody:users (99:100), so /config is editable from the share.
RUN mkdir -p /config && chown 99:100 /config
USER 99:100
VOLUME /config
EXPOSE 8080/tcp 21072/udp

HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
  CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT', '8080'), timeout=4)"

CMD ["python", "-m", "app"]
