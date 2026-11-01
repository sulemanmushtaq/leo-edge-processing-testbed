#!/usr/bin/env bash
# Container entrypoint: installs static routes so traffic is forced through the
# emulated satellite path (site -> sat1 -> sat2 -> ground) instead of the
# Docker host bridge. Runs entirely inside the container's network namespace.
#
# ROUTES env format:  "<cidr> via <gw>;<cidr> via <gw>;..."
set -euo pipefail

if [[ -n "${ROUTES:-}" ]]; then
  IFS=';' read -ra entries <<< "$ROUTES"
  for r in "${entries[@]}"; do
    r="$(echo "$r" | xargs)"
    [[ -z "$r" ]] && continue
    # shellcheck disable=SC2086
    ip route replace $r
    echo "[node_init:${NODE_ROLE:-?}] route: $r"
  done
fi

mkdir -p /data/raw
echo "[node_init:${NODE_ROLE:-?}] ready"
exec "$@"
