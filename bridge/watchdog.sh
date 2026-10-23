#!/bin/bash
# dsh-bridge 看门狗：每分钟检查一次，挂了就拉起来（用 setsid 跑，脱离终端）
export PATH=/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin
cd /root/dsh-bridge || exit 1
while true; do
  if ! curl --unix-socket /data/data/com.termux/files/home/.dsb-ipc/bridge.sock -sf -m 3 http://localhost/health >/dev/null 2>&1; then
    echo "$(date '+%F %T') bridge unavailable; manual reconciliation required" >> /root/dsh-bridge/watchdog.log
  fi
  sleep 20
done
