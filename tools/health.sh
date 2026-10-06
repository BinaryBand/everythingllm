#!/usr/bin/env bash
# `make health`: is everything this repo runs up? Prints OK/FAIL per check and exits 1 if
# anything failed. Meant for after a reboot, or whenever something seems off. The unit list
# and the in-container checks live in packages/audit (audit.health), next to the audit's own.
set -u
cd "$(dirname "$0")/.."

CONTAINER=systemd-anythingllm
VENV_PY=/app/server/storage/mcp/venv/bin/python
failed=0

ok()   { printf '  OK    %s\n' "$1"; }
fail() { printf '  FAIL  %s\n' "$1"; failed=1; }

echo "Units"
units=$(uv run -q --frozen --package audit python -m audit.health units) || { fail "can't list units"; units=; }
for u in $units; do
  # A oneshot run by a timer is inactive between runs; the timer is what should be up.
  timer=$(systemctl --user show -p TriggeredBy --value "$u" 2>/dev/null)
  case "$timer" in *.timer) u=$timer ;; esac
  state=$(systemctl --user is-active "$u" 2>/dev/null)
  # Quadlet units (anythingllm, static_agent, searxng) are generated, so "enabled" doesn't apply.
  enabled=$(systemctl --user is-enabled "$u" 2>/dev/null)
  case "$state/$enabled" in
    active/enabled|active/generated|active/static) ok "${u%.service} ($state, $enabled)" ;;
    *) fail "${u%.service} ($state, ${enabled:-unknown})" ;;
  esac
done
[ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null)" = yes ] \
  && ok "linger (user units start at boot)" || fail "linger is off: loginctl enable-linger $USER"

echo "HTTP"
# AnythingLLM takes a little while to listen after a restart (as after `make deploy`).
for _ in $(seq 30); do curl -s -o /dev/null --max-time 2 http://127.0.0.1:3001/api/ping && break; sleep 2; done
while IFS='|' read -r name url; do
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$url")
  # Any answer below 500 means the server is up; some roots are a 404 by design.
  if [ "$code" != 000 ] && [ "$code" -lt 500 ]; then ok "$name ($code)"; else fail "$name ($url: ${code/000/no answer})"; fi
done <<'EOF'
AnythingLLM|http://127.0.0.1:3001/api/ping
pages site|http://127.0.0.1:8445/
article writer|http://127.0.0.1:8448/
podcasts-web|http://127.0.0.1:8449/health
Nilson relay|http://127.0.0.1:8446/health
research live cards|http://127.0.0.1:8450/_live/research/dr-00000000.png
EOF

echo "Sockets"
# The host services the container talks to over sockets in storage.
(uv run -q --frozen --package audit python -m audit.health sockets) || failed=1

echo "Inside AnythingLLM"
if podman exec "$CONTAINER" true 2>/dev/null; then
  podman exec -w /tmp "$CONTAINER" "$VENV_PY" -m audit.health || failed=1
else
  fail "can't exec into $CONTAINER"
fi

echo
[ "$failed" = 0 ] && echo "All good." || echo "Something needs a look (FAIL lines above)."
exit "$failed"
