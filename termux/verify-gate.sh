#!/data/data/com.termux/files/usr/bin/bash
# 端到端验证：在 dsh 冷启动过程中调用 dsh-up.sh，确认新加的"等插件就绪"逻辑
# 真的把 URL 放行时间压到了 PLUGINS_READY 之后（而非 TCP_UP 之后）。
P=/data/data/com.termux/files/usr/bin
H=/data/data/com.termux/files/home

echo "[$(date '+%H:%M:%S')] 杀掉 dsh"
for p in $(pgrep -f "[d]sh web --host"); do kill -9 "$p" 2>/dev/null; done
sleep 2
echo "  残留: [$(pgrep -f "[d]sh web --host" | tr '\n' ' ')]"

setsid nohup "$H/dsb/window-monitor.sh" >/dev/null 2>&1 </dev/null &
sleep 0.5

rm -f "$H/dsb/.dsh-up.lock"
echo "[$(date '+%H:%M:%S')] 调用 dsh-up.sh（会等 dsh 起来 + 等插件就绪）"
"$H/dsb/dsh-up.sh" > "$H/dsb/.verify-up.out" 2>&1
echo "  退出码=$?  $([ -s "$H/dsb/.verify-up.out" ] && echo "输出非空")"

# 等监控器收尾
for i in $(seq 1 20); do grep -q MONITOR_END "$H/dsb/startup-window.log" 2>/dev/null && break; sleep 0.5; done

"$P/python3" - <<'PY'
import os
H = "/data/data/com.termux/files/home"
tcp = ready = None
for line in open(f"{H}/dsb/startup-window.log"):
    p = line.split()
    if len(p) == 2 and p[1] == "TCP_UP": tcp = float(p[0])
    if len(p) == 2 and p[1] == "PLUGINS_READY": ready = float(p[0])
url = os.stat(f"{H}/.dsb-web-url").st_mtime
log = os.stat(f"{H}/dsh-web.log").st_mtime

print()
print("=== 端到端验证 ===")
def rel(x):
    return f"{x - tcp:+.2f}s" if (x is not None and tcp is not None) else "?"
print(f"  TCP_UP          (端口可连接)      {tcp and ' (基准 0.00s)'}")
print(f"  PLUGINS_READY   (插件可服务)      {rel(ready)}")
print(f"  URL 文件写入    (放行给 App)      {rel(url)}")
print()
if ready is None or url is None or tcp is None:
    print("  ❓ 数据不完整")
elif url >= ready:
    print(f"  ✅ 门禁生效：URL 在插件就绪之后 {url - ready:+.2f}s 才放行")
    print(f"     （旧代码会在 {ready - tcp:.2f}s 的窗口内就放行）")
else:
    print(f"  ❌ 门禁未生效：URL 比插件就绪还早 {ready - url:.2f}s")
PY
