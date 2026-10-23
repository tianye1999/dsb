#!/data/data/com.termux/files/usr/bin/bash
# 充电不锁屏守护（2026-10-21 用户要求）
#
# 规则：**只要逗沙包 App（com.dsb.assistant）在跑，就让「充电时不休眠/不锁屏」开着**；
#       App 不在了就还原成 0，不干扰用户平时省电。
#
# 为什么必须：息屏后 input 注入被系统丢弃、无障碍被回收、截图为黑屏、
# 窗口 mCurrentFocus=null —— 自动化会"点什么都没反应"。这不是可选优化。
#
# 关掉：touch ~/dsb/stayon.off    恢复：rm ~/dsb/stayon.off
set -u
H=/data/data/com.termux/files/home
source "$(dirname "${BASH_SOURCE[0]}")/adb-target.sh"
ADB=("${DSB_ADB_COMMAND[@]}" "${DSB_ADB_SELECTOR[@]}")
LOG="$H/dsb/stayon.log"
OFF="$H/dsb/stayon.off"
KEY=stay_on_while_plugged_in

adbs() { "${ADB[@]}" shell "$@" 2>/dev/null | tr -d '\r'; }

apply() {   # $1 = 期望值，变化时才写，避免无谓的 adb 往返
  cur=$(adbs settings get global $KEY)
  if [ "$cur" != "$1" ]; then
    adbs settings put global $KEY "$1"
    echo "$(date '+%F %T') set $KEY $cur -> $1" >> "$LOG"
  fi
}

echo "$(date '+%F %T') stayon-daemon start (pid $$)" >> "$LOG"
while true; do
  if [ -f "$OFF" ]; then
    sleep 30
    continue
  fi
  pid=$(adbs pidof com.dsb.assistant)
  if [ -n "$pid" ]; then
    apply 7
  else
    apply 0
  fi
  sleep 10
done
