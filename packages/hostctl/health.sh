#!/usr/bin/env bash
# `uv run hostctl health`: is everything this repo runs up? Prints OK/FAIL per check and exits 1 if
# anything failed. Meant for after a reboot, or whenever something seems off. The units,
# HTTP checks and runners come from the apps registry, through hostctl.appctl.
set -u
cd "$(dirname "$0")/../.."

appctl() { PYTHONPATH=packages/hostctl/src:packages/apps/src python3 -m hostctl.appctl "$@"; }
failed=0

ok()   { printf '  OK    %s\n' "$1"; }
fail() { printf '  FAIL  %s\n' "$1"; failed=1; }

echo "Units"
units=$(appctl units) || { fail "can't list units"; units=; }
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
# AnythingLLM takes a little while to listen after a restart (as after `uv run hostctl deploy`).
for _ in $(seq 30); do curl -s -o /dev/null --max-time 2 http://127.0.0.1:3001/api/ping && break; sleep 2; done
while IFS='|' read -r name url; do
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$url")
  # Any answer below 500 means the server is up; some roots are a 404 by design.
  if [ "$code" != 000 ] && [ "$code" -lt 500 ]; then ok "$name ($code)"; else fail "$name ($url: ${code/000/no answer})"; fi
done < <(appctl health)
# Without a password, AnythingLLM's internal API answers anyone who reaches :3001.
case "$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:3001/api/scheduled-jobs)" in
  401) ok "AnythingLLM asks for a login" ;;
  *) fail "AnythingLLM answers without a login: set a password (Settings → Security)" ;;
esac

echo "Routes"
# What the machine routes to the apps on PUBLIC_HOST over HTTPS (the apps' `serve`).
appctl routes || failed=1

echo "Sockets"
# The host services the container talks to over sockets in storage.
appctl sockets || failed=1

echo "Prompts"
# Notices only: a workspace's prompt is its own, and update-prompt refreshes its block.
PYTHONPATH=packages/hostctl/src:packages/apps/src python3 -m hostctl.prompt check

echo
[ "$failed" = 0 ] && echo "All good." || echo "Something needs a look (FAIL lines above)."
exit "$failed"
