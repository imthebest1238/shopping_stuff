#!/usr/bin/env bash
# Start a virtual screen, a password-protected VNC view of it (via noVNC on port 6080),
# then the shopping agent.
set -euo pipefail

DATA=/app/data
mkdir -p "$DATA"

# VNC password: VNC_PASSWORD if set, else a random one kept in data/ so it survives
# restarts. Anyone with it can control the agent's browser (and your logged-in stores),
# so it is never left empty. VNC only uses the first 8 characters.
if [ -n "${VNC_PASSWORD:-}" ]; then
  printf '%s\n' "$VNC_PASSWORD" > "$DATA/vnc_password"
  chmod 600 "$DATA/vnc_password"
elif [ ! -s "$DATA/vnc_password" ]; then
  python -c 'import secrets; print(secrets.token_urlsafe(6))' > "$DATA/vnc_password"
  chmod 600 "$DATA/vnc_password"
fi
x11vnc -storepasswd "$(cat "$DATA/vnc_password")" /tmp/vncpass >/dev/null 2>&1

rm -f /tmp/.X99-lock
Xvfb :99 -screen 0 1280x900x24 -nolisten tcp &
for _ in $(seq 1 50); do [ -e /tmp/.X11-unix/X99 ] && break; sleep 0.1; done
fluxbox >/dev/null 2>&1 &
x11vnc -display :99 -rfbauth /tmp/vncpass -localhost -forever -shared -quiet -rfbport 5900 >/dev/null 2>&1 &
websockify --web /usr/share/novnc 6080 localhost:5900 >/dev/null 2>&1 &

echo
echo "  Agent's browser window (noVNC):  http://<this-host>:6080/vnc.html?autoconnect=1&resize=scale"
if [ -n "${VNC_PASSWORD:-}" ]; then
  echo "  VNC password: the VNC_PASSWORD you set"
else
  echo "  VNC password: $(cat "$DATA/vnc_password")   (also stored in data/vnc_password)"
fi
exec python -m shopping_agent
