#!/data/data/com.termux/files/usr/bin/bash
# 保存地址只是候选；每个新 transport 必须重新挑战，无 target 的旧环境不扫描/接管。
set -u
umask 077
export ADB_EMU=0
P="${PREFIX:-/data/data/com.termux/files/usr}/bin"
if [ "${DSB_ADB_CONNECT_LOCKED:-0}" != 1 ]; then
  # 与安装器的 bootstrap 切换互斥；--close 不让 ADB daemon 继承锁。
  exec "$P/flock" --close --wait 30 "$PREFIX/var/run/dsb-adb-bootstrap.lock" \
    "$P/env" DSB_ADB_CONNECT_LOCKED=1 "$P/bash" "$0"
fi
FILE="$HOME/dsb/adb-target"
SELF=$(cat "$HOME/dsb/adb-device-id" 2>/dev/null || true)
if [ ! -d "$PREFIX/var/lib/dsb-adb" ]; then
  # 普通传输每次重连都先核验本 UID 的 TCP server；首次安装使用私有 bootstrap。
  if [[ "${ADB_SERVER_SOCKET:-}" != localfilesystem:* ]]; then
    "$P/python3" "$HOME/dsb/adb-tcp-owner.py" || exit 2
  fi
fi
if [ ! -d "$PREFIX/var/lib/dsb-adb" ]; then
  "$P/adb" start-server >/dev/null 2>&1 || exit 1
fi
local_address() {
  "$P/python3" - "$1" <<'PY' || return 1
import ipaddress, socket, sys
host, port = sys.argv[1].rsplit(':', 1)
ipaddress.IPv4Address(host)
with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as conn:
    conn.connect((host, int(port)))
    sys.exit(0 if conn.getsockname()[0] == host else 1)
PY
}
usable() {
  local_address "$1" || return 1
  "$P/python3" "$HOME/dsb/adb_identity.py" "$1" >/dev/null 2>&1
}
connect_and_verify() {
  local attempt
  usable "$1" && return 0
  for attempt in 1 2 3; do
    usable "$1" && return 0
    sleep 1
  done
  return 1
}
TARGET=$(cat "$FILE" 2>/dev/null || true)
if [[ "$TARGET" =~ ^[0-9.]+:[0-9]{1,5}$ ]]; then
  local_address "$TARGET" || exit 1
  connect_and_verify "$TARGET" && exit 0
fi
while read -r name kind address; do
  [ -n "$SELF" ] || continue
  case "$name" in "adb-$SELF-"*) ;; *) continue ;; esac
  case "$kind" in *_adb-tls-connect._tcp*) ;; *) continue ;; esac
  [[ "$address" =~ ^[0-9.]+:[0-9]{1,5}$ ]] || continue
  local_address "$address" || continue
  if connect_and_verify "$address"; then
    umask 077
    printf '%s\n' "$address" > "$FILE.tmp.$$"
    mv "$FILE.tmp.$$" "$FILE"
    exit 0
  fi
done < <(timeout 8 "$P/adb" mdns services 2>/dev/null)
echo 'ADB_OFFLINE：请打开无线调试；端口变更且 mDNS 不可用时重跑 install.sh'
exit 1
