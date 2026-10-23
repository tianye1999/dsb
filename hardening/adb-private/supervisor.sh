#!/data/data/com.termux/files/usr/bin/bash
# 单实例守护；维护闸只暂停 ADB，不暂停网页、桥接或 SSH。
set -u
P="${PREFIX:?}"
R60="$P/var/lib/dsb-adb"
mkdir -p "$HOME/wx"
exec 9>"$HOME/wx/supervisor.lock"
flock -n 9 || exit 0
PIDFILE="$HOME/wx/supervisor.pid"
echo $$ > "$PIDFILE"
trap 'rm -f "$PIDFILE"; exit 0' TERM INT
while true; do
  pgrep -x sshd >/dev/null 2>&1 || "$P/bin/sshd" 9>&- 2>/dev/null
  if [ -e "$R60/maintenance" ] || [ -L "$R60/maintenance" ]; then
    echo 'ADB_MAINTENANCE: 其它服务继续'
  elif [ -f "$R60/manifest.json" ]; then
    if timeout 10 "$P/bin/python3" -I "$R60/adb_guard.py" "$$" 9>&-; then
      timeout 25 "$P/bin/bash" "$HOME/wx/wx_adb_connect.sh" 9>&- >/dev/null 2>&1 || true
    fi
  else
    timeout 25 "$P/bin/bash" "$HOME/wx/wx_adb_connect.sh" 9>&- >/dev/null 2>&1 || true
  fi
  if [ ! -e "$HOME/.dsb-install-pending" ] && [ ! -L "$HOME/.dsb-install-pending" ]; then
    "$P/bin/bash" "$HOME/dsb/dsh-up.sh" 9>&- >/dev/null 2>&1 &
    "$P/bin/bash" "$HOME/dsb/ensure-private-bridge.sh" 9>&- >/dev/null 2>&1 &
  fi
  sleep 30 9>&-
done
