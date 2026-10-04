#!/bin/sh
# Run a node/npm command for the web app inside node:22-alpine (nothing is
# installed on the host). Usage: web/scripts/node.sh npm run typecheck
set -e
WEB_DIR="$(cd "$(dirname "$0")/.." && pwd)"
exec docker run --rm -e npm_config_update_notifier=false -v "$WEB_DIR":/web -w /web ${NODE_DOCKER_ARGS:-} node:22-alpine "$@"
