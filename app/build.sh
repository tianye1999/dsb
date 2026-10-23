#!/bin/bash
# DSH 手机 App —— 无 Gradle 构建（本机 macOS：aapt2 + javac + d8 + zipalign + apksigner）
set -e
PROJ=$(cd "$(dirname "$0")" && pwd)
UNSIGNED=false
if [ "${1:-}" = "--unsigned" ]; then UNSIGNED=true; fi
if ! "$UNSIGNED"; then
  : "${DSB_KEY_PASS:?请先 export DSB_KEY_PASS=<签名密钥口令>（密钥库 dsb.keystore 不入库）}"
fi
OUT=$PROJ/build
BT=$HOME/Library/Android/sdk/build-tools/35.0.0
PLATFORM=$HOME/Library/Android/sdk/platforms/android-35/android.jar
export JAVA_HOME=$(/usr/libexec/java_home -v 17 2>/dev/null || echo /Library/Java/JavaVirtualMachines/jdk-17.jdk/Contents/Home)
export PATH=$JAVA_HOME/bin:$PATH

rm -rf $OUT && mkdir -p $OUT/classes $OUT/dex $OUT/res
echo "== 1. aapt2 compile 资源 =="
$BT/aapt2 compile --dir $PROJ/res -o $OUT/res.zip
echo "== 2. javac =="
javac -encoding UTF-8 -source 11 -target 11 -nowarn -cp "$PLATFORM" -d $OUT/classes $(find $PROJ/src -name '*.java')
echo "== 3. d8 =="
$BT/d8 --min-api 30 --lib "$PLATFORM" --output $OUT/dex $(find $OUT/classes -name '*.class')
echo "== 4. aapt2 link =="
$BT/aapt2 link -I "$PLATFORM" --manifest $PROJ/AndroidManifest.xml -o $OUT/base.apk \
  $OUT/res.zip --min-sdk-version 30 --target-sdk-version 34 --version-code 1 --version-name 1.0 \
  --java $OUT/gen
echo "== 5. 合 dex =="
cd $OUT && cp base.apk withdex.apk && zip -q -j withdex.apk dex/classes.dex && unzip -l withdex.apk | grep -q classes.dex && echo "dex merged"
echo "== 6. 签名 =="
if "$UNSIGNED"; then
  "$BT/zipalign" -f 4 "$OUT/withdex.apk" "$OUT/DSB-unsigned.apk"
  echo "BUILD_OK (unsigned): $OUT/DSB-unsigned.apk"
  exit 0
fi
if [ ! -f $PROJ/dsb.keystore ]; then
  keytool -genkeypair -keystore $PROJ/dsb.keystore -alias dsb -keyalg RSA -keysize 2048 -validity 10000 \
    -storepass "$DSB_KEY_PASS" -keypass "$DSB_KEY_PASS" -dname "CN=DSH Phone App, O=Personal, C=CN"
fi
$BT/zipalign -f 4 $OUT/withdex.apk $OUT/aligned.apk
rm -f $PROJ/DSB-1.0.apk $PROJ/DSH-Phone-1.0.apk
$BT/apksigner sign --ks $PROJ/dsb.keystore --ks-pass env:DSB_KEY_PASS --key-pass env:DSB_KEY_PASS \
  --v1-signing-enabled true --v2-signing-enabled true --v3-signing-enabled true \
  --out $PROJ/DSB-1.0.apk $OUT/aligned.apk
$BT/apksigner verify $PROJ/DSB-1.0.apk && echo SIGN_OK
ls -la $PROJ/DSB-1.0.apk
echo BUILD_OK
