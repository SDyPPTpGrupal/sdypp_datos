#!/usr/bin/env bash
# Wrapper de compatibilidad: redirige a levantar-redis.sh
set -euo pipefail
RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$RAIZ/levantar-redis.sh" "$@"
