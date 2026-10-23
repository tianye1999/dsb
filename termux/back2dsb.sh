#!/data/data/com.termux/files/usr/bin/bash
# back2dsb —— 任务完成后切回「逗沙包」对话界面。
#
# 用户要求（已讲过多次）：**完成手机操作后，屏幕必须回到逗沙包的对话界面**，
#   让用户一眼看到"我做完了 / 结果在这里"，而不是停在微信或别的 App 上。
#   2026-09-22 我做实验后停在微信界面，用户再次指出。
#
# 为什么用 MainActivity：它是 singleTask + exported，
#   `am start` 会回到**已有实例**（不会重载 WebView、不会丢当前对话）。
#
# 用法：bash ~/dsb/back2dsb.sh
set -u
source "$(dirname "${BASH_SOURCE[0]}")/adb-target.sh"
A=("${DSB_ADB_COMMAND[@]}" "${DSB_ADB_SELECTOR[@]}")

"${A[@]}" shell "am start -n com.dsb.assistant/.MainActivity" >/dev/null 2>&1
sleep 4
FG=$("${A[@]}" shell "dumpsys window 2>/dev/null | grep -m1 -oE 'mCurrentFocus=Window\{[^}]*\}'" 2>/dev/null | tr -d '\r')
case "$FG" in
  *dsb.assistant*) echo "✓ 已回到逗沙包界面"; exit 0 ;;
  *)               echo "⚠ 没切过去，当前前台: $(echo "$FG" | cut -c1-70)"; exit 1 ;;
esac
