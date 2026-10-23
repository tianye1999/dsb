#!/data/data/com.termux/files/usr/bin/bash
# url-server-loop.sh —— 3097 小服务的"自愈壳"
#
# 为什么要有它（2026-09-24 用户遇到"额度取不到 / Failed to fetch"）：
#   实测 10:00~10:22 之间 url-server 静默死过一次（日志里没有崩溃栈，是被外部杀掉/被系统回收），
#   而当时**没有任何东西在盯着它** —— supervisor.sh 里根本没有 3097（grep 过），
#   boot.sh 的 alive 检查要等下一次 /ensure 才触发 → 空档期长达 20 分钟，
#   这期间网页里的额度面板 fetch 直接 "Failed to fetch"。
#   套一层 while 循环后：**崩了 1 秒内自己起来**，并且每次退出都记一行带时间的日志，下次可查。
#
# 用法：setsid nohup bash ~/dsb/url-server-loop.sh >>~/dsb/url-server.log 2>&1 &
#
# ⚠️ 2026-09-30 修复（省电，DSh 手机侧）：
#   boot.sh 的 "alive 3097 || 起 loop" 在**并发调用**时有竞态，实测同时跑起了两个 loop
#   （pid 14299 / 14302）。只有一个能抢到 3097，另一个**每秒 fork 一个 python3**、
#   写一段 traceback 就死，一天 ≈ 8.6 万次进程启动 + 日志涨到 326MB，load average 被抬到 ~10。
#   双保险：① pidfile 判重（同一时刻只允许一个 loop 实例）
#           ② 每次重启前先探端口，已有人监听就只 sleep，不再盲目 fork。
set -u
P=/data/data/com.termux/files/usr/bin
H="$HOME"
PIDFILE="$H/dsb/url-server-loop.pid"
port_open() {
  "$P/python3" -c "import socket,sys; s=socket.socket(); s.settimeout(0.6); sys.exit(0 if s.connect_ex(('127.0.0.1',3097))==0 else 1)"
}
if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE" 2>/dev/null)" 2>/dev/null; then
  echo "[loop] 已有实例 pid=$(cat "$PIDFILE")，本实例退出 $(date "+%F %T")"
  exit 0
fi
echo $$ > "$PIDFILE"
trap 'rm -f "$PIDFILE"; echo "[loop] 收到 TERM，退出 $(date "+%F %T")" ; exit 0' TERM INT
echo "[loop] 启动 $(date "+%F %T") pid=$$"
while true; do
  if port_open; then
    sleep 5
    continue
  fi
  "$P/python3" "$H/dsb/url-server.py" >>"$H/dsb/url-server.log" 2>&1
  rc=$?
  echo "[loop] url-server 退出 rc=$rc $(date "+%F %T") —— 1 秒后自动重启"
  sleep 1
done
