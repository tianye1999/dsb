#!/usr/bin/env bash
set -euo pipefail
set +x
: "${DSB_KEY_PASS:?existing signing environment required}"
KEYSTORE=${DSB_KEYSTORE:?existing release keystore path required}
[ -r "$KEYSTORE" ] || { echo 'existing release keystore required' >&2; exit 1; }
PROJ=${PROJ:?candidate source required}
OUT=${OUT:?new output directory required}
ANDROID_HOME=${ANDROID_HOME:-${ANDROID_SDK_ROOT:-/opt/android-sdk}}
BT=$ANDROID_HOME/build-tools/35.0.0
PLATFORM=$ANDROID_HOME/platforms/android-35/android.jar
TERMUX_PREFIX=${TERMUX_PREFIX:-/data/data/com.termux/files/usr}
TAAPT2=${TAAPT2:-$TERMUX_PREFIX/bin/aapt2}
mkdir -p "$OUT/classes" "$OUT/dex"
javac -encoding UTF-8 -source 11 -target 11 -nowarn -cp "$PLATFORM" -d "$OUT/classes" "$PROJ"/tests/device/*.java
mapfile -t CLASSES < <(find "$OUT/classes" -name '*.class')
"$BT/d8" --min-api 30 --lib "$PLATFORM" --output "$OUT/dex" "${CLASSES[@]}"
LD_LIBRARY_PATH="$TERMUX_PREFIX/lib" "$TAAPT2" link -I "$PLATFORM" --manifest "$PROJ/tests/device/AndroidManifest.xml" -o "$OUT/test-base.apk"
cp "$OUT/test-base.apk" "$OUT/tests-unsigned.apk"
jar uf "$OUT/tests-unsigned.apk" -C "$OUT/dex" classes.dex
export DSB_KEY_PASS
"$BT/apksigner" sign --ks "$KEYSTORE" --ks-pass env:DSB_KEY_PASS --key-pass env:DSB_KEY_PASS --out "$OUT/tests.apk" "$OUT/tests-unsigned.apk"
"$BT/apksigner" verify "$OUT/tests.apk"
sha256sum "$OUT/tests.apk"
printf 'TEST_BUILD_ONLY: product APK unchanged; no install\n'
