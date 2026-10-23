#!/data/data/com.termux/files/usr/bin/bash
# usb-mode.sh —— 只在「插着 USB（能连到 Mac 的 adb reverse 端口）」时持 termux 唤醒锁
#
# ⚠️ 2026-09-30 改：**不再管同步**（同步改由 Mac 侧手动触发）。
#   本脚本只做一件事：判断 USB 在不在 → 决定要不要持唤醒锁。
#   另留一个安全网：USB 断了就顺手把"可能还在跑"的 syncthing 停掉（**只停，不起**）。
#
# 判据：127.0.0.1:22300 能否连上（Mac 侧 22000 经 adb reverse 暴露，由 Mac 侧隧道看守维持）。
# 成本：每 30 秒一次 connect()，可忽略。
#
# 强制老行为（永远持锁）：touch ~/dsb/wakelock-always.on
# 停掉本守护：pkill -f usb-mode.sh
set -u
P=/data/data/com.termux/files/usr/bin
H="$HOME"
PIDFILE="$H/dsb/usb-mode.pid"
ALWAYS="$H/dsb/wakelock-always.on"

if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE" 2>/dev/null)" 2>/dev/null; then
  echo "usb-mode 已在跑 pid=$(cat "$PIDFILE")"
  exit 0
fi
echo $$ > "$PIDFILE"
trap '[ "$(cat "$PIDFILE" 2>/dev/null)" = "$$" ] && rm -f "$PIDFILE"; exit 0' TERM INT

mac_up()  { "$P/python3" -c "import socket,sys; s=socket.socket(); s.settimeout(1); sys.exit(0 if s.connect_ex(('127.0.0.1',22300))==0 else 1)"; }
st_up()   { pgrep -f "[s]yncthing serve" >/dev/null 2>&1; }
log()     { echo "$(date '+%F %T') $*"; }

log "usb-mode 启动 pid=$$（判据 127.0.0.1:22300；只管唤醒锁，不管 syncthing）"
held=-1
tick=0
while true; do
  tick=$((tick + 1))
  if mac_up || [ -f "$ALWAYS" ]; then
    want=1
  else
    want=0
    st_up && { log "USB 不可达 → 停掉残留的 syncthing（同步现在是手动的）"; pkill -f "[s]yncthing serve"; }
  fi
  if [ "$held" != "$want" ] || [ $((tick % 20)) -eq 0 ]; then
    if [ "$want" = 1 ]; then "$P/termux-wake-lock" >/dev/null 2>&1 || true
    else "$P/termux-wake-unlock" >/dev/null 2>&1 || true; fi
    if [ "$held" != "$want" ]; then
      [ "$want" = 1 ] && log "USB 在 → 已持 termux 唤醒锁" || log "USB 不在 → 已释放唤醒锁，让 CPU 深睡"
    fi
    held=$want
  fi
  sleep 30
done
