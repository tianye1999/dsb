#!/data/data/com.termux/files/usr/bin/bash
# 守护进程: 确保 sshd / adb自连 / dsh web 始终活着
PIDFILE="$HOME/wx/supervisor.pid"
if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then echo SUPERVISOR_ALREADY; exit 0; fi
echo $$ > "$PIDFILE"
# 2026-09-20 电池优先：默认不拿 Termux wakelock —— 实测它会让 CPU 24 小时不进入休眠，
# 是"耗电快/发热"的主因（strace 到锁被持有了 1 天 1 小时）。日常不用它；
# "屏幕熄灭也要继续跑"的长任务由任务脚本自行临时加锁、结束时释放。
# 手动需要时：termux-wake-lock（释放：termux-wake-unlock）
# termux-wake-lock 2>/dev/null
trap 'rm -f "$PIDFILE"; exit 0' TERM INT
port3080() { python3 -c "import socket,sys; s=socket.socket(); s.settimeout(1); sys.exit(0 if s.connect_ex(('127.0.0.1',3080))==0 else 1)"; }
port3001() { python3 -c "import socket,sys; s=socket.socket(); s.settimeout(1); sys.exit(0 if s.connect_ex(('127.0.0.1',3001))==0 else 1)"; }
while true; do
  # ⚠️ 2026-09-23 修：原来这里没后台化（sshd >/dev/null 2>&1）。
  #    如果 sshd 不是自动 daemon 化而是前台运行，这行会**永久阻塞**整个 while 循环 ——
  #    实测 supervisor 卡在 do_wait 里，TTS / auto-commit 守护全都没被拉起。
  pgrep -x sshd >/dev/null 2>&1 || setsid nohup sshd >/dev/null 2>&1 </dev/null &
  "$HOME/wx/wx_adb_connect.sh" >/dev/null 2>&1
  port3080 || setsid nohup "$HOME/wx/start-dsh.sh" >/dev/null 2>&1 </dev/null &

  # ── 2026-09-25 补：git 自动提交守护的拉起块 ─────────────────────────
  # ⚠️ 记忆里一直说"挂在 supervisor"，实际这个块根本不存在（当天实测发现）——
  #    守护被杀后没人管，直到重启手机。现在补上：每 30 秒检查，没了就拉。
  #    验活方式（任何一方都能用）：/root/.dsh-phone/git-heartbeat.txt 超过 3 分钟没更新 = 出事。
  # 2026-09-30 用户要求改成【按需】：存在 off 标记时不再拉起（要提交时手动 --once）。
  if [ ! -f "/data/data/com.termux/files/home/dsb/auto-commit.off" ]; then
    pgrep -f "[a]uto-commit.sh --watch" >/dev/null 2>&1 || \
      setsid nohup /data/data/com.termux/files/usr/bin/bash /root/dsh-config/auto-commit.sh --watch \
        >>/root/dsh-config/auto-commit.log 2>&1 </dev/null &
  fi

  # ── TTS 朗读守护 / git 自动提交守护 **不在这里管**（2026-09-23 清理死代码）─────
  # 这两段原来的判据是 /root/... 路径，而**真 Termux 里根本没有 /root**
  # （`ls -ld /root` → No such file or directory），所以它们永远不会执行，是死代码。
  # 真正在管这两个守护的是**容器侧**的 /root/termux-dsb/supervisor.sh
  # （那边 HOME=/root，路径成立）；本 Termux 侧只负责 sshd / adb 自连 /
  # dsh web（用端口 3080 探测判活）。
  # ⚠️ 容易看走眼：这里的 TTS 块曾让我误以为 Termux 侧能自动拉起 tts_reader。
  # ── 2026-09-20 停用：微信通知轮询 ──────────────────────────────────────
  # 用户确认这套微信监控"只是当时测试验证用的，不需要一直监控"。
  # 它每 30 秒调一次 adb 读 dumpsys notification（1471 行），一天约 2880 次，
  # 且会与其它 adb 调用抢通道。如需临时启用，手动跑：
  #     setsid nohup "$HOME/wx/wx_loop.sh" 30 >>"$HOME/wx/loop.log" 2>&1 &
  # wx_watch.py / wx_reply.py 都保留，可随时手动用（如 wx_watch.py --all）。
  # pgrep -f "wx_loop.sh" >/dev/null 2>&1 || (setsid nohup "$HOME/wx/wx_loop.sh" 30 >>"$HOME/wx/loop.log" 2>&1 &)
  # ─────────────────────────────────────────────────────────────────────
  sleep 30
done
