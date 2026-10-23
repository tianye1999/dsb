#!/usr/bin/env bash
# Fresh profiles get templates; existing profiles retain their local customization.
set -euo pipefail
HERE=$(cd -- "$(dirname -- "$0")" && pwd)
ROOT=${DSH_PROFILES_ROOT:-${DSH_HOME:-$HOME/.dsh}/profiles}
[ "$(basename -- "$ROOT")" = profiles ] || { echo 'profile root must end in /profiles' >&2; exit 1; }
mkdir -p "$ROOT"
ROOT=$(cd -- "$ROOT" && pwd)
export DSH_HOME=$(dirname -- "$ROOT")
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
  if ! dsh --profile "$profile" --dump-config 2>/dev/null |
      python3 "$HERE/privacy_defaults.py" --verify-dump; then
    printf 'privacy readback failed: %s\n' "$profile" >&2
    exit 1
  fi
  printf 'privacy default verified: %s enabled=false\n' "$profile"
done
committed=true
