# Shopping agent in Docker. The agent's browser runs on a virtual screen (Xvfb);
# you watch and control it from your own browser with noVNC (password protected).
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    DISPLAY=:99

RUN apt-get update \
    && apt-get install -y --no-install-recommends xvfb x11vnc novnc websockify fluxbox fonts-noto-color-emoji tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && printf '%s\n' '<!doctype html><meta charset="utf-8"><title>Shop browser</title>' \
       '<meta http-equiv="refresh" content="0; url=vnc.html?autoconnect=1&amp;resize=scale">' \
       '<a href="vnc.html?autoconnect=1&amp;resize=scale">Open the shop browser</a>' \
       > /usr/share/novnc/index.html

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt \
    && python -m playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

# Same uid/gid as your Linux user, so the mounted data/ folder stays yours.
ARG UID=1000
ARG GID=1000
RUN groupadd -g "$GID" shop && useradd -m -u "$UID" -g "$GID" shop

COPY shopping_agent ./shopping_agent
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod 755 /entrypoint.sh && mkdir -p /app/data && chown shop:shop /app/data

USER shop
# Inside the container the server must listen on all interfaces; docker-compose
# publishes it on 127.0.0.1 only, so it is still reachable only from this computer.
ENV SHOP_IN_DOCKER=1 \
    SHOP_HOST=0.0.0.0 \
    SHOP_PORT=8000 \
    SHOP_OPEN_UI=false
EXPOSE 8000 6080
ENTRYPOINT ["/entrypoint.sh"]
