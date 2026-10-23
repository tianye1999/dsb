#!/usr/bin/env bash
# Legacy styling restore must not reintroduce the unauthenticated recap route.
set -euo pipefail
if [ "${1:-}" != --verify ]; then
  echo 'Legacy recap restore disabled. Review hardened/index.js and recap/README.md; do not restore known-good backups.' >&2
  exit 1
fi
DIR=${DSB_RECAP_LIB:-$HOME/.dsh/profiles/web/node_modules/dsh-recap/lib}
SOURCE="$(cd "$(dirname "$0")" && pwd)/hardened/index.js"
cmp -- "$SOURCE" "$DIR/index.js"
node --check "$DIR/index.js"
printf '%s\n' 'RECAP_HOST_MATCHES_HARDENED_SOURCE; no client changes or service restart'
