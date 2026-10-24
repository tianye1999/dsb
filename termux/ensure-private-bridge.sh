#!/data/data/com.termux/files/usr/bin/bash
set -eu
export PATH=/data/data/com.termux/files/usr/bin:$PATH
H=/data/data/com.termux/files/home
IPC="$H/.dsb-ipc"
[ ! -e "$IPC/maintenance" ] || { echo dsb_maintenance; exit 1; }
SOCK="$IPC/bridge.sock"
[ -d "$IPC" ] && [ ! -L "$IPC" ] && [ "$(stat -c %a "$IPC")" = 700 ] || { echo private_directory_required; exit 1; }
exec 9>"$IPC/bridge-start.lock"
flock -n 9 || exit 0
if curl --unix-socket "$SOCK" --max-time 2 -sf http://localhost/health >/dev/null; then exit 0; fi
# A crash/restart must be reconciled, not converted into fresh task ownership.
if [ -e "$SOCK" ] || [ -L "$SOCK" ] || [ -e "$IPC/bridge-launch.pid" ]; then
  echo bridge_state_unknown_manual_reconciliation_required
  exit 1
fi
setsid nohup proot-distro login --no-kill-on-exit ubuntu -- \
  /bin/bash -c 'export PATH=/usr/local/bin:/usr/bin:/bin; . /root/dsh-config/report-paths.sh || exit 1; unset DEEPSEEK_API_KEY ZAI_CODING_CN_API_KEY; export BRIDGE_SOCKET=/data/data/com.termux/files/home/.dsb-ipc/bridge.sock; cd /root/dsh-bridge && exec node bridge.mjs' \
  >>"$H/dsb/bridge.log" 2>&1 </dev/null 9>&- &
printf '%s\n' "$!" > "$IPC/bridge-launch.pid"
for ((n=0;n<30;n++)); do
  if curl --unix-socket "$SOCK" --max-time 1 -sf http://localhost/health >/dev/null; then exit 0; fi
  sleep 0.2
done
echo bridge_start_unconfirmed
exit 1
