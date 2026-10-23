#!/bin/bash
# 逗沙包 —— 手机端构建脚本（在 proot Ubuntu 里跑）
# 自己编译、自己签名（与已装版本同 keystore），然后由用户手动安装或用 adb install。
# 公开版不自动下载或安装 APK，也不代点系统确认框。
#
# 工具链（都是踩过坑选出来的，别乱换）：
#   * 资源：Termux 的 aapt2 16.0（proot 里看得到 Termux 的 bin；Ubuntu 自带 2.19 读不懂
#     android-35 的 arsc；build-tools 里的 aapt2 是 x86_64 跑不了；老 aapt v1 编不了渐变矢量）
#   * Java：Ubuntu 的 openjdk-17（javac）
#   * dex/签名：/opt/android-sdk/build-tools/35.0.0 的 d8、apksigner（纯 Java，arm64 能跑）
#   * 平台包：android-35（manifest 用了 API34 的 foregroundServiceType="specialUse"，33 链不过）
#   * ⚠️ LD_LIBRARY_PATH 只能挂在 aapt2 那两行，全局 export 会污染 javac
set -e
set +x
PROJ=${DSB_APP_SOURCE:-$(cd -- "$(dirname -- "$0")" && pwd)}
KEYSTORE=${DSB_KEYSTORE:?existing release keystore path required; never generate a replacement key}
[ -r "$KEYSTORE" ] || { echo "original phone release keystore required" >&2; exit 1; }
: "${DSB_KEY_PASS:?请先 export DSB_KEY_PASS=<签名密钥口令>}"
OUT=$PROJ/build
ANDROID_HOME=${ANDROID_HOME:-${ANDROID_SDK_ROOT:-/opt/android-sdk}}
BT=$ANDROID_HOME/build-tools/35.0.0
TERMUX_PREFIX=${TERMUX_PREFIX:-/data/data/com.termux/files/usr}
TAAPT2=${TAAPT2:-$TERMUX_PREFIX/bin/aapt2}
PLATFORM=$ANDROID_HOME/platforms/android-35/android.jar
export JAVA_HOME=${JAVA_HOME:-/usr/lib/jvm/java-17-openjdk-arm64}
export PATH="$JAVA_HOME/bin:$PATH"

rm -rf "$OUT" && mkdir -p "$OUT/classes" "$OUT/dex"

echo "== 1/6 aapt2 编译资源 =="
LD_LIBRARY_PATH="$TERMUX_PREFIX/lib" "$TAAPT2" compile --dir "$PROJ/res" -o "$OUT/res.zip"

echo "== 2/6 javac =="
mapfile -t SOURCES < <(find "$PROJ/src" -name '*.java')
javac -encoding UTF-8 -source 11 -target 11 -nowarn -cp "$PLATFORM" -d "$OUT/classes" \
  "${SOURCES[@]}"

echo "== 3/6 d8 打 dex =="
mapfile -t CLASSES < <(find "$OUT/classes" -name '*.class')
"$BT/d8" --min-api 30 --lib "$PLATFORM" --output "$OUT/dex" "${CLASSES[@]}"

echo "== 4/6 aapt2 link =="
LD_LIBRARY_PATH="$TERMUX_PREFIX/lib" "$TAAPT2" link -I "$PLATFORM" \
  --manifest "$PROJ/AndroidManifest.xml" -o "$OUT/base.apk" \
  "$OUT/res.zip" --min-sdk-version 30 --target-sdk-version 34 --version-code 1 --version-name 1.0

echo "== 5/6 合并 dex + 签名 =="
cd "$OUT" && cp base.apk app.apk
export DSB_PHONE_BUILD_OUT="$OUT"
python3 - <<'PY'
import zipfile
import os
out = os.environ["DSB_PHONE_BUILD_OUT"]
z = zipfile.ZipFile(out + "/app.apk", "a")
z.write(out + "/dex/classes.dex", "classes.dex")
z.close()
print("classes.dex merged")
PY
"$BT/apksigner" sign --ks "$KEYSTORE" --ks-pass env:DSB_KEY_PASS --key-pass env:DSB_KEY_PASS \
  --v1-signing-enabled true --v2-signing-enabled true --v3-signing-enabled true \
  --out "$PROJ/DSB-1.0.apk" "$OUT/app.apk"
"$BT/apksigner" verify "$PROJ/DSB-1.0.apk" && echo "签名 OK（请核对与已装版本的 signer 一致）"
ls -la "$PROJ/DSB-1.0.apk"

echo "== 6/6 构建完成：请手动打开 APK 确认安装，或执行 adb install -r $PROJ/DSB-1.0.apk =="
echo BUILD_OK
