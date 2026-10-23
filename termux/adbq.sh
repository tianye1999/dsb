#!/data/data/com.termux/files/usr/bin/bash
# adbq —— "带红绿灯的 adb"。**操作手机请一律用它，别用裸 adb。**
#
# 为什么：2026-09-22 用户两次指出"切微信时球没变橙"。
#   根因是我把橙信号只接进了 safe_tap.py 等个别脚本，
#   而我平时干活经常直接用 adb —— 那些路径全都不脉冲橙。
#
# 行为：先脉冲一次「橙」（球变橙，警示用户勿碰），再执行你给的 adb 命令。
#   App 侧看门狗 20 秒无心跳自动回蓝，所以不会永久橙色。
#   一串操作连续用 adbq 即可持续保持橙。
#
# ⚠️ 2026-09-22 踩过的坑：`A="$P/adb -s 127.0.0.1:5555"` 然后 `"$A" shell ...`
#    会被当成**一个**命令名（含空格）→ "No such file or directory"。
#    必须用数组：ADB=("$P/adb" -s 127.0.0.1:5555) 然后 "${ADB[@]}" shell ...
#
# 用法：
#   bash ~/dsb/adbq.sh shell "dumpsys power | grep mWakefulness"   # 只读查询
#   ❌ 不要再 `input tap/swipe/keyevent`：点击一律走 safe_tap.py（虚拟屏优先）
#   bash ~/dsb/adbq.sh shell "am start -n com.tencent.mm/.ui.LauncherUI"
#   bash ~/dsb/adbq.sh exec-out screencap -p > /tmp/x.png
#   bash ~/dsb/adbq.sh 蓝            # 只回蓝
#   bash ~/dsb/adbq.sh 状态          # 看球状态
set -u
P=/data/data/com.termux/files/usr/bin
source "$(dirname "${BASH_SOURCE[0]}")/adb-target.sh"
ADB=("${DSB_ADB_COMMAND[@]}" "${DSB_ADB_SELECTOR[@]}")

ball() {
  "${ADB[@]}" shell "am broadcast -n com.dsb.assistant/.CmdReceiver -a com.dsb.assistant.CMD --es token dsh-8f3a2c --es cmd '$1'" >/dev/null 2>&1
}

case "${1:-}" in
  # ★ done = 任务收尾的**一步到位**：球变蓝 + 切回逗沙包对话界面。
  #   用户已多次要求"完成手机操作后必须回到逗沙包界面"（2026-09-22 又漏了一次）。
  #   做成一跳命令，是因为分两步我总会忘第二步。
  done|完成)
    ball "蓝"
    "${ADB[@]}" shell "am start -n com.dsb.assistant/.MainActivity" >/dev/null 2>&1
    # ⚠️ 2026-09-24 修：原来固定 sleep 4 秒就判前台 —— 逗沙包**冷启动**（WebView 起来）
    #    经常要 5~10 秒，于是"其实已经切过去了"却被判失败（自动切回守护的日志里 rc=1）。
    #    改成轮询最多 ~15 秒，切过去就立刻返回。
    FG=""
    for i in 1 2 3 4 5 6 7 8 9 10; do
      sleep 1.5
      FG=$("${ADB[@]}" shell "dumpsys window 2>/dev/null | grep -m1 -oE 'mCurrentFocus=Window\{[^}]*\}'" 2>/dev/null | tr -d '\r')
      case "$FG" in
        *dsb.assistant*) echo "🔵 球→蓝  ✓ 已回到逗沙包界面"; exit 0 ;;
      esac
    done
    echo "🔵 球→蓝  ⚠ 没切回逗沙包，前台: $(echo "$FG" | cut -c1-60)"; exit 1 ;;
  蓝|闲|绿灯) ball "蓝"; echo "🔵 球→蓝（未切界面）"; exit 0 ;;
  橙|忙|红灯) ball "橙"; echo "🟠 球→橙"; exit 0 ;;
  状态) "${ADB[@]}" shell "logcat -d -s DSH:* 2>/dev/null | grep 球状态 | tail -1" 2>/dev/null | tr -d '\r'; exit 0 ;;
  "") echo "用法: $0 <adb 参数...> | done(球蓝+回逗沙包) | 蓝 | 橙 | 状态" >&2; exit 2 ;;
esac

# ★ 2026-10-20 用户规矩（虚拟屏强制）：
#   ① 所有「别的 App 的界面」操作必须在虚拟屏上（vd_session.py start → look/tap --display N）；
#   ② 屏幕动作只能走 safe_tap.py 闸门，不许裸 input。
#   这里挡第 ② 条：`adbq.sh shell "input tap ..."` 会被拒绝。
#   真需要绕过（极少数诊断）：SAFE_TAP_ALLOW_RAW_INPUT=1 bash adbq.sh ...
case " $* " in
  *" input tap "*|*" input swipe "*|*" input keyevent "*|*" input text "*|*" input touchscreen "*)
    if [ "${SAFE_TAP_ALLOW_RAW_INPUT:-0}" != "1" ]; then
      printf '⛔ adbq 拒绝裸 input：%s\n' "$*" >&2
      echo "  → 点击/滑动/按键请用 safe_tap.py（虚拟屏：先 vd_session.py start --package <pkg>，再 look/tap --display N）" >&2
      echo "  → 确需绕过：SAFE_TAP_ALLOW_RAW_INPUT=1 bash ~/dsb/adbq.sh ..." >&2
      exit 2
    fi ;;
esac

ball "橙"                       # ← 关键：任何 adb 操作前先点亮
exec "${ADB[@]}" "$@"
