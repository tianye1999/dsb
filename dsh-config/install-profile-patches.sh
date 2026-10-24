#!/usr/bin/env bash
# Fresh profiles get templates; existing profiles retain their local customization.
set -euo pipefail
HERE=$(cd -- "$(dirname -- "$0")" && pwd)
ROOT=${DSH_PROFILES_ROOT:-${DSH_HOME:-$HOME/.dsh}/profiles}
[ "$(basename -- "$ROOT")" = profiles ] || { echo 'profile root must end in /profiles' >&2; exit 1; }
mkdir -p "$ROOT"
ROOT=$(cd -- "$ROOT" && pwd)
DSH_HOME=$(dirname -- "$ROOT")
export DSH_HOME
targets=()
backups=()
committed=false
rollback() {
  local i
  if ! "$committed"; then
    for ((i=${#targets[@]}-1; i>=0; i--)); do
      if [ -n "${backups[$i]}" ]; then
        cp -p "${backups[$i]}" "${targets[$i]}"
      else
        rm -f -- "${targets[$i]}"
      fi
    done
    echo 'privacy installation failed; restored touched patch files' >&2
  fi
}
trap rollback EXIT
for profile in web phone headless; do
  dir="$ROOT/$profile"
  mkdir -p "$dir"
  target="$dir/cordis.patch.yml"
  backup=''
  if [ -f "$target" ]; then
    backup=$(mktemp "$target.before-privacy-XXXXXX")
    cp -p "$target" "$backup"
  fi
  targets+=("$target")
  backups+=("$backup")
  [ -f "$target" ] || cp "$HERE/cordis.patch.$profile.yml" "$target"
  python3 "$HERE/privacy_defaults.py" "$target"
  verify_args=(--verify-dump)
  if [ "$profile" = web ] && [ -L "$ROOT/web/node_modules/dsb-report" ]; then
    verify_args+=(--require-report)
  fi
  if ! dsh --profile "$profile" --dump-config 2>/dev/null |
      python3 "$HERE/privacy_defaults.py" "${verify_args[@]}"; then
    printf 'privacy readback failed: %s\n' "$profile" >&2
    exit 1
  fi
  printf 'privacy default verified: %s enabled=false\n' "$profile"
  if [ "$profile" = web ] && [ -L "$ROOT/web/node_modules/dsb-report" ]; then
    node --input-type=module -e 'await import(process.argv[1])' \
      "$ROOT/web/node_modules/dsb-report/index.js" || {
        echo 'dsb-report import failed' >&2
        exit 1
      }
  fi
done
committed=true
