#!/usr/bin/env bash
# Compile every App/instrumentation source without building or installing an APK.
set -euo pipefail
APP=$(cd -- "$(dirname -- "$0")/.." && pwd)
SDK=${ANDROID_HOME:-${ANDROID_SDK_ROOT:-$HOME/Library/Android/sdk}}
PLATFORM=$SDK/platforms/android-35/android.jar
[ -r "$PLATFORM" ] || { echo "Android API 35 required: $PLATFORM" >&2; exit 1; }
OUT=$(mktemp -d)
trap 'rm -rf -- "$OUT"' EXIT
SOURCES=()
while IFS= read -r -d '' file; do SOURCES+=("$file"); done < <(find "$APP/src" "$APP/tests/device" -name '*.java' -print0)
javac -encoding UTF-8 --release 11 -Xlint:all -Werror -cp "$PLATFORM" -d "$OUT" "${SOURCES[@]}"
echo 'JAVA_LINT_OK (all App and instrumentation sources)'
