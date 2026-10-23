#!/usr/bin/env bash
# Run from Termux after instrumentation exits. Restore only an already-enabled DSB binding.
set -euo pipefail
ADBQ=/data/data/com.termux/files/home/dsb/adbq.sh
# Bound services 只显示标签不显示组件名；改名前装的 App 标签是「大烧饼」，两个都认。
# 连后面的逗号一起匹配（AOSP 格式 Service[label=X, feedbackType…]），免得「大烧饼助手」这类标签误命中。
LABEL_RE='label=(逗沙包|大烧饼),'
if [ -f /root/.dsh-phone/skills/safe_tap.py ]; then
    python3 /root/.dsh-phone/skills/safe_tap.py open --app com.dsb.assistant
else
    proot-distro login ubuntu -- python3 /root/.dsh-phone/skills/safe_tap.py open --app com.dsb.assistant
fi
if bash "$ADBQ" shell dumpsys accessibility | grep 'Bound services:' | grep -qE "$LABEL_RE"; then
    echo 'DSB existing accessibility binding is active'; exit 0
fi
current=$(bash "$ADBQ" shell settings get secure enabled_accessibility_services | tr -d '\r')
case "$current" in *com.dsb.assistant/*) ;; *) echo 'DSB was not enabled; refusing to grant a new permission'; exit 2;; esac
filtered=$(printf '%s' "$current" | node -e 'let s="";process.stdin.on("data",b=>s+=b);process.stdin.on("end",()=>process.stdout.write(s.split(":").filter(x=>!x.startsWith("com.dsb.assistant/")).join(":")))')
trap 'bash "$ADBQ" shell "settings put secure enabled_accessibility_services '\''$current'\''"' EXIT
bash "$ADBQ" shell "settings put secure enabled_accessibility_services '$filtered'"
sleep 1
bash "$ADBQ" shell "settings put secure enabled_accessibility_services '$current'"
trap - EXIT
# The iteration variable only bounds retries; retain the existing restoration timing.
# shellcheck disable=SC2034
for i in {1..15}; do
    if bash "$ADBQ" shell dumpsys accessibility | grep 'Bound services:' | grep -qE "$LABEL_RE"; then
        echo 'DSB existing accessibility binding restored'; exit 0
    fi
    sleep 1
done
echo 'Accessibility binding not confirmed' >&2
exit 1
