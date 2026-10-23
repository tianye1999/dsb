#!/data/data/com.termux/files/usr/bin/bash
# 悬浮球红绿灯（橙=我正在操作手机，请勿触碰；蓝=空闲）
#
# 为什么要这个包装：App 里本来就有「橙 / 蓝」命令（OverlayService 第 832-833 行），
#   但那是给"App 内部 agent 任务路径"用的。**我用 adb 直接操作时不会走那条路径**，
#   所以球一直是蓝的、不给我操作中的警示（2026-09-22 用户指出）。
#   现在所有自动化脚本在动手前后调这个脚本，球就会自己亮。
#
# 兜底：App 侧有 120 秒看门狗（每次「橙」都是心跳），脚本崩溃也会自动回蓝。
#
# 用法：
#   bash ~/dsb/ball.sh 橙     # 开始操作
#   bash ~/dsb/ball.sh 蓝     # 操作结束
#   bash ~/dsb/ball.sh 状态   # 读日志里最后一次球状态
set -u
source "$(dirname "${BASH_SOURCE[0]}")/adb-target.sh"
CMD="${1:-}"
case "$CMD" in
  橙|忙|红灯) CMD="橙" ;;
  蓝|闲|绿灯) CMD="蓝" ;;
  状态)
    "${DSB_ADB_COMMAND[@]}" "${DSB_ADB_SELECTOR[@]}" shell "logcat -d -s DSH:* 2>/dev/null | grep '球状态' | tail -1" 2>/dev/null | tr -d '\r'
    exit 0 ;;
  *) echo "用法: $0 {橙|蓝|状态}" >&2; exit 2 ;;
esac
"${DSB_ADB_COMMAND[@]}" "${DSB_ADB_SELECTOR[@]}" shell "am broadcast -n com.dsb.assistant/.CmdReceiver -a com.dsb.assistant.CMD --es token dsh-8f3a2c --es cmd '$CMD'" >/dev/null 2>&1
echo "球 → $CMD"
