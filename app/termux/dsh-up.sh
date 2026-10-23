#!/data/data/com.termux/files/usr/bin/bash
# 让 dsh web 处于"可用"状态，并把一个**已验证**的 URL 写到 ~/.dsb-web-url。
#
# 背景（2026-09-22 实测）：dsh web 每次启动都换 token，而且 ~/.dsh-web-token 会被
# 启动失败的实例写歪——当时文件里的 token 返回 401，真正在用的是 dsh-web.log 最后
# 一行那个（303）。所以这里的规矩是：**从日志取最后一个 URL → curl 验证 → 不行就
# 干净重启一次再取**。顺手把 ~/.dsh-web-token 修正，Chrome 那边的旧链接也能对上。
P=/data/data/com.termux/files/usr/bin
H=/data/data/com.termux/files/home
[ ! -e "$H/.dsb-ipc/maintenance" ] || exit 0
LOG="$H/dsh-web.log"
URLFILE="$H/.dsb-web-url"
LOCK="$H/dsb/.dsh-up.lock"

mkdir -p "$H/dsb"
if [ -f "$LOCK" ] && kill -0 "$(cat "$LOCK" 2>/dev/null)" 2>/dev/null; then exit 0; fi
echo $$ > "$LOCK"
trap 'rm -f "$LOCK"' EXIT

port_up() { "$P/python3" -c "import socket,sys;s=socket.socket();s.settimeout(0.6);sys.exit(0 if s.connect_ex(('127.0.0.1',3080))==0 else 1)"; }
last_url() { "$P/grep" -o 'http://127.0.0.1:3080/?token=[A-Za-z0-9_-]*' "$LOG" 2>/dev/null | "$P/tail" -1; }
code_of() { if [ -n "$1" ]; then "$P/curl" -s -o /dev/null -m 5 -w '%{http_code}' "$1" 2>/dev/null; fi; }

start_web() {
  setsid nohup "$H/start-dsh.sh" >>"$LOG" 2>&1 </dev/null &
  i=0
  while [ $i -lt 90 ]; do
    sleep 1; i=$((i+1))
    if [ -n "$(last_url)" ] && port_up; then return 0; fi
  done
  return 1
}

attempt=0
while [ $attempt -lt 2 ]; do
  attempt=$((attempt+1))
  port_up || start_web
  U="$(last_url)"
  # DSB_HEALTH_RETRY_BEGIN: keep this block in sync with the App copy.
  # A listening server may be busy: a timeout/5xx is not proof of a bad token.
  check=0
  while :; do
    U="$(last_url)"
    C="$(code_of "$U")"
    case "$C" in 200|303|401|403) break ;; esac
    port_up || break
    check=$((check+1))
    [ "$check" -lt 3 ] || break
    sleep "$check"
  done
  case "$C" in
    200|303|401|403) ;;
    *) if port_up; then
         echo "DSH_UP_FAILED: listening server health is uncertain; not restarting" >&2
         exit 1
       fi ;;
  esac
  # DSB_HEALTH_RETRY_END
  if [ "$C" = "303" ] || [ "$C" = "200" ]; then
    printf '%s\n' "$U" > "$URLFILE"
    printf '%s\n' "$U" > "$H/.dsh-web-token"
    printf '%s\n' "$U"
    exit 0
  fi
  # Only confirmed auth failure / lost listener permits a restart (once).
  [ "$attempt" -lt 2 ] || break
  "$P/pkill" -f "dsh web --host 127.0.0.1 --port 3080" 2>/dev/null
  sleep 2
  : > "$LOG" 2>/dev/null
  start_web
done
echo "DSH_UP_FAILED" >&2
exit 1
