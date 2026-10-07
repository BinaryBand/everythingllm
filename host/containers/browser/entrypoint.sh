#!/bin/bash
# A workspace's browser: the X screen, x11vnc showing it on /run/browser/vnc.sock (the
# take-over view's; it listens on no port), then browser.driver, which starts Chromium on
# it and answers browser-runner on /run/browser/driver.sock. If any of them stops, the
# container stops, and browser-runner starts it again when it's next needed. On SIGTERM
# (podman stop) the driver is told first, and closes Chromium while its X screen is still
# there, so the profile is saved and marked as closed cleanly; only then is everything
# else. (Stopping them all at once took the screen from under Chromium, which then ended
# as a crash and offered to restore its pages at every start.)
set -u
driver=
trap 'trap - TERM INT; if [ -n "$driver" ]; then kill -TERM "$driver"; wait "$driver"; fi; kill 0; wait' TERM INT
mkdir -p "$HOME"
mkdir -p -m 1777 /tmp/.X11-unix  # Xvfb, not root, won't make it itself
rm -f /run/browser/driver.sock /run/browser/vnc.sock

Xvfb :99 -screen 0 "${BROWSER_SCREEN:-1280x800}x24" -nolisten tcp -dpi 96 &
for _ in $(seq 100); do [ -e /tmp/.X11-unix/X99 ] && break; sleep 0.05; done

x11vnc -display :99 -unixsock /run/browser/vnc.sock -rfbport 0 -shared -forever -nopw \
  -noxdamage -quiet -nossl -noipv6 &
python3 -m browser.driver &
driver=$!

wait -n
exit 1
