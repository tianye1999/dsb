#!/data/data/com.termux/files/usr/bin/bash
# 每次调用重新读，重启后的无线端口变更不需要重启 agent。
DSB_ADB_HOME=/data/data/com.termux/files/home
[ ! -e "$DSB_ADB_HOME/dsb/adb-blocked" ] && [ ! -L "$DSB_ADB_HOME/dsb/adb-blocked" ] || {
  echo 'ADB_POLICY_PAUSED: ordinary transport conflict; run doctor' >&2; exit 78;
}
if [ -e "$DSB_ADB_HOME/dsb/adb-target" ] || [ -e "$DSB_ADB_HOME/.dsb-installer-managed" ]; then
  DSB_ADB_ID=$(/data/data/com.termux/files/usr/bin/python3 -c \
    'import sys; sys.path.insert(0, sys.argv[1]); from adb_identity import read_binding; print(read_binding(sys.argv[1]+"/adb-transport.json")["transport_id"])' \
    "$DSB_ADB_HOME/dsb") || { echo 'ADB_POLICY_DENIED: transport not verified' >&2; exit 78; }
  DSB_ADB_SELECTOR=(-t "$DSB_ADB_ID")
  if [ ! -d /data/data/com.termux/files/usr/var/lib/dsb-adb ]; then
    DSB_ADB_COMMAND=(/data/data/com.termux/files/usr/bin/python3 -I "$DSB_ADB_HOME/dsb/adb_identity.py" --execute)
  else
    DSB_ADB_COMMAND=(/data/data/com.termux/files/usr/bin/adb)
  fi
else
  DSB_ADB_SELECTOR=(-s '127.0.0.1:5555')
  DSB_ADB_COMMAND=(/data/data/com.termux/files/usr/bin/adb)
fi
