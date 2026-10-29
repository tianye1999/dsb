#!/data/data/com.termux/files/usr/bin/bash
# 启动/检查手机端 dsh web，并自动用「已验证可用」的浏览器完成认证
# 用法: start-dsh.sh [--restart]
[ ! -e /data/data/com.termux/files/home/.dsb-ipc/maintenance ] || exit 0
# If inherited as an exported variable, BROWSER is also passed to proot/dsh children.
# shellcheck disable=SC2034
BROWSER="com.obric.browser"      # ROM 自带浏览器(已验证能正确保存 dsh 的认证 cookie)
# 2026-09-20 电池优先：默认不拿 Termux wakelock —— 实测它会让 CPU 24 小时不进入休眠，
# 是"耗电快/发热"的主因（strace 到锁被持有了 1 天 1 小时）。日常不用它；
# "屏幕熄灭也要继续跑"的长任务由任务脚本自行临时加锁、结束时释放。
# 手动需要时：termux-wake-lock（释放：termux-wake-unlock）
# termux-wake-lock 2>/dev/null
TOKENFILE="$HOME/.dsh-web-token"
port_up() { python3 -c "import socket,sys; s=socket.socket(); s.settimeout(1); sys.exit(0 if s.connect_ex(('127.0.0.1',3080))==0 else 1)"; }
MARK=0
if [ "${1:-}" = "--restart" ]; then
  pkill -f '[d]sh web' 2>/dev/null || true
  STOP_DEADLINE=$((SECONDS + 20))
  while port_up; do
    if [ "$SECONDS" -ge "$STOP_DEADLINE" ]; then
      echo 'START_DSH_WARN: restart failed; port 3080 did not close within 20s; token file unchanged' >&2
      exit 1
    fi
    sleep 1
  done
fi
if port_up; then echo 'DSH_WEB_UP (already)'; else
  [ ! -f "$HOME/dsh-web.log" ] || MARK=$(wc -l < "$HOME/dsh-web.log")
  # Expand the quoted command inside Ubuntu, not in the Termux parent shell.
  # shellcheck disable=SC2016
  setsid nohup proot-distro login ubuntu -- /bin/bash -c \
    'export PATH=/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin; . /root/dsh-config/report-paths.sh || exit 1; [ -f /root/.dsh/.glm-api-key ] && export ZAI_CODING_CN_API_KEY=$(cat /root/.dsh/.glm-api-key); export DEEPSEEK_API_KEY=$(cat /root/.dsh/.deepseek-key 2>/dev/null); mkdir -p /root/dsb-workspaces; cd /root/dsb-workspaces || exit 1; args=(); [ ! -f /root/dsh-config/installer.web.patch.yml ] || args=(--patch /root/dsh-config/installer.web.patch.yml); exec dsh web "${args[@]}" --host 127.0.0.1 --port 3080 --no-open' \
    >> "$HOME/dsh-web.log" 2>&1 &
  # The iteration variable only bounds startup polling; do not change the timing.
  # shellcheck disable=SC2034
  for i in $(seq 1 25); do sleep 3; port_up && break; done
  # Do not dump the log: it contains authentication URLs.
  port_up || { echo 'START_DSH_WARN: DSH_WEB_FAILED; port 3080 did not open; token file unchanged' >&2; exit 1; }
  echo DSH_WEB_UP
fi
# 2026-09-25：写 TOKENFILE 之前必须等插件真正就绪。端口通、认证路由通都**不**代表
# 插件注册表装完；空窗期内消费方加载页面会拿到插件包 404，只剩空壳白屏。
# 判定逻辑见 dsb/ready-probe.sh（要求真实 combo 包返回 200）。
# MARK is zero only for an already-running service (or an initially empty log).
# Reload on every attempt; a new start must never consume pre-MARK URLs.
READY=0
READY_DEADLINE=$((SECONDS + 30))
URL=''
while [ "$SECONDS" -lt "$READY_DEADLINE" ]; do
  URL=$(tail -n +"$(( MARK + 1 ))" "$HOME/dsh-web.log" 2>/dev/null | grep -oE 'http://127\.0\.0\.1:3080/\?token=[A-Za-z0-9_-]+' | tail -1)
  if [ -n "$URL" ] && [ -x "$HOME/dsb/ready-probe.sh" ] && "$HOME/dsb/ready-probe.sh" "$URL" >/dev/null 2>&1; then
    READY=1
    break
  fi
  sleep 1
done
if [ "$READY" != 1 ]; then
  if [ -z "$URL" ]; then
    echo 'START_DSH_WARN: URL wait timed out; no URL in eligible startup log; token file unchanged' >&2
  else
    echo 'START_DSH_WARN: ready-probe timed out; URL not verified; token file unchanged' >&2
  fi
  exit 1
fi
printf '%s\n' "$URL" > "$TOKENFILE" || { echo 'START_DSH_WARN: cannot write verified token file' >&2; exit 1; }
# ── 2026-09-22：**不再用浏览器打开 dsh** ─────────────────────────────────
# 这段原来每次 dsh (重)启动都会 `am start -a VIEW ... -p com.obric.browser` 弹一次浏览器：
#   * boot.sh → supervisor.sh 每 30s 查 3080，不在就调本脚本 → 弹浏览器；
#   * App（逗沙包）现在自带 WebView 显示 dsh，浏览器只会抢前台（用户 2026-09-22 报的
#     "每次启动都开浏览器"就是这个）。
# 认证不再需要它：App 从 3097 拿已验证 URL，WebView 自己走 303 落 cookie。
# 浏览器启动代码保留在 git 历史/备份 start-dsh.sh.bak-20260922-noBrowser 里，需要时再启用。
echo "URL_READY (不弹浏览器；App 的 WebView 会自己加载)"
