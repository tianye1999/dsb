#!/usr/bin/env bash
set -euo pipefail
set +x
: "${DSB_KEY_PASS:?existing signing environment required}"
KEYSTORE=${DSB_KEYSTORE:?existing release keystore path required}
[ -r "$KEYSTORE" ] || { echo 'existing release keystore required' >&2; exit 1; }
PROJ=${PROJ:-$(cd -- "$(dirname -- "$0")/.." && pwd)}
OUT=${OUT:-$HOME/dsb-stop-build}
ANDROID_HOME=${ANDROID_HOME:-${ANDROID_SDK_ROOT:-/opt/android-sdk}}
BT=$ANDROID_HOME/build-tools/35.0.0
PLATFORM=$ANDROID_HOME/platforms/android-35/android.jar
TERMUX_PREFIX=${TERMUX_PREFIX:-/data/data/com.termux/files/usr}
TAAPT2=${TAAPT2:-$TERMUX_PREFIX/bin/aapt2}
export JAVA_HOME=${JAVA_HOME:-/usr/lib/jvm/java-17-openjdk-arm64}
export PATH="$JAVA_HOME/bin:$PATH"
mkdir -p "$OUT/classes" "$OUT/dex" "$OUT/test-classes" "$OUT/test-dex"
LD_LIBRARY_PATH="$TERMUX_PREFIX/lib" "$TAAPT2" compile --dir "$PROJ/res" -o "$OUT/res.zip"
javac -encoding UTF-8 -source 11 -target 11 -nowarn -cp "$PLATFORM" -d "$OUT/classes" "$PROJ"/src/com/dsb/assistant/*.java
mapfile -t CLASSES < <(find "$OUT/classes" -name '*.class')
"$BT/d8" --min-api 30 --lib "$PLATFORM" --output "$OUT/dex" "${CLASSES[@]}"
LD_LIBRARY_PATH="$TERMUX_PREFIX/lib" "$TAAPT2" link -I "$PLATFORM" --manifest "$PROJ/AndroidManifest.xml" -o "$OUT/base.apk" "$OUT/res.zip" --min-sdk-version 30 --target-sdk-version 34 --version-code 1 --version-name 1.0
javac -encoding UTF-8 -source 11 -target 11 -nowarn -cp "$PLATFORM" -d "$OUT/test-classes" "$PROJ"/tests/device/*.java
mapfile -t TEST_CLASSES < <(find "$OUT/test-classes" -name '*.class')
"$BT/d8" --min-api 30 --lib "$PLATFORM" --output "$OUT/test-dex" "${TEST_CLASSES[@]}"
LD_LIBRARY_PATH="$TERMUX_PREFIX/lib" "$TAAPT2" link -I "$PLATFORM" --manifest "$PROJ/tests/device/AndroidManifest.xml" -o "$OUT/test-base.apk"
cp "$OUT/base.apk" "$OUT/candidate-unsigned.apk"
jar uf "$OUT/candidate-unsigned.apk" -C "$OUT/dex" classes.dex
cp "$OUT/test-base.apk" "$OUT/tests-unsigned.apk"
jar uf "$OUT/tests-unsigned.apk" -C "$OUT/test-dex" classes.dex
export DSB_KEY_PASS
for name in candidate tests; do
    "$BT/apksigner" sign --ks "$KEYSTORE" --ks-pass env:DSB_KEY_PASS --key-pass env:DSB_KEY_PASS --out "$OUT/$name.apk" "$OUT/$name-unsigned.apk"
    "$BT/apksigner" verify "$OUT/$name.apk"
    sha256sum "$OUT/$name.apk"
done
printf 'BUILD_ONLY: no install, no canonical APK overwrite, no update notification\n'
