#!/usr/bin/env bash
# Restart the botgear backend + UI dev server systemd --user units.
#
# These units (botgear-backend.service, botgear-ui.service) are host-local:
# they exist only where botgear has been set up to run under systemd --user
# (e.g. worke), not as part of this repo's install. A plain
# `systemctl --user restart` is the right tool here rather than e.g. a
# uvicorn --reload dev loop — it kills the backend's whole cgroup (the
# backend process AND every subprocess service it spawned: sabertooth,
# joystick, video, etc.), and the backend's own boot sequence then
# reconciles every service back to the state its config asks for. A
# narrower reload can't reach those subprocess children (they're spawned
# with os.setsid, detached into their own session) and would leave them
# orphaned, still holding hardware (serial ports, cameras) exclusively.
#
# Usage: ./restart-services.sh [backend|ui]   (default: both)
set -euo pipefail

UNITS=(botgear-backend.service botgear-ui.service)
case "${1:-}" in
  backend) UNITS=(botgear-backend.service) ;;
  ui)      UNITS=(botgear-ui.service) ;;
  "")      ;;
  *)
    echo "usage: $0 [backend|ui]" >&2
    exit 1
    ;;
esac

if ! command -v systemctl >/dev/null 2>&1; then
  echo "systemctl not found — these units only exist on a host where botgear runs under systemd --user." >&2
  exit 1
fi

for unit in "${UNITS[@]}"; do
  if ! systemctl --user cat "$unit" >/dev/null 2>&1; then
    echo "warning: $unit is not installed on this host — skipping" >&2
    continue
  fi
  echo "restarting $unit ..."
  systemctl --user restart "$unit"
done

# Give each unit a moment to report its post-restart state before we check
# it, rather than racing systemd's own async restart.
sleep 2

status=0
for unit in "${UNITS[@]}"; do
  systemctl --user cat "$unit" >/dev/null 2>&1 || continue
  if systemctl --user is-active --quiet "$unit"; then
    echo "$unit: active"
  else
    echo "$unit: NOT active — check: systemctl --user status $unit" >&2
    status=1
  fi
done

exit "$status"
