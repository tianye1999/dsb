#!/data/data/com.termux/files/usr/bin/bash
# 测量 dsh 的"启动窗口"：TCP 端口可连接  →  插件真正可服务 之间的时间差。
# 旧代码把"端口通/认证路由通"当就绪，窗口内交给 App 加载就会白屏；这个差值就是
# 那个窗口的长度。同时验证改后的 dsh-up.sh 是否真的把 URL 压到就绪之后才放行。
P=/data/data/com.termux/files/usr/bin
H=/data/data/com.termux/files/home
LOG="$H/dsh-web.log"
OUT="$H/dsb/startup-window.log"
: > "$OUT"

port_up() { "$P/python3" -c "import socket,sys;s=socket.socket();s.settimeout(0.5);sys.exit(0 if s.connect_ex(('127.0.0.1',3080))==0 else 1)"; }
last_url() { "$P/grep" -o 'http://127.0.0.1:3080/?token=[A-Za-z0-9_-]*' "$LOG" 2>/dev/null | "$P/tail" -1; }

echo "$(date +%s.%N) MONITOR_START" >> "$OUT"
tcp_up_at=""
ready_at=""
# The iteration variable only bounds the observation window.
# shellcheck disable=SC2034
for i in $(seq 1 240); do
  now=$(date +%s.%N)
  if [ -z "$tcp_up_at" ] && port_up; then
    tcp_up_at=$now
    echo "$now TCP_UP" >> "$OUT"
  fi
  if [ -n "$tcp_up_at" ] && [ -z "$ready_at" ]; then
    U="$(last_url)"
    if [ -n "$U" ] && "$H/dsb/ready-probe.sh" "$U"; then
      ready_at=$now
      echo "$now PLUGINS_READY" >> "$OUT"
      break
    fi
  fi
  sleep 0.3
done
echo "$(date +%s.%N) MONITOR_END" >> "$OUT"

"$P/python3" - <<'PY' >> "$OUT"
import os
f = os.path.expanduser("~/dsb/startup-window.log")
d = {}
for line in open(f):
    p = line.split()
    if len(p) == 2 and p[1] in ("TCP_UP", "PLUGINS_READY"):
        d[p[1]] = float(p[0])
if "TCP_UP" in d and "PLUGINS_READY" in d:
    print("WINDOW_SECONDS=%.2f" % (d["PLUGINS_READY"] - d["TCP_UP"]))
else:
    print("WINDOW_INCOMPLETE", d)
PY
