#!/data/data/com.termux/files/usr/bin/bash
# 划掉 App 时调用：把手机侧跟 dsh 有关的全都停掉（省电、安静）。
#
# 覆盖范围（2026-09-30 补全）：
#   ① wx/supervisor.sh  ② dsh web(3080)  ③ 代理桥(3096)  ④ URL 小服务(3097)
#   ⑤ url-server 的"自愈壳" url-server-loop.sh（**必须先杀它**，否则 1 秒后 ④ 又活了）
#   ⑥ back2dsb 结论卡守护  ⑦ usb-mode USB 门控守护 + syncthing + 释放 Termux 唤醒锁
# 故意**不**停的：
#   · sshd（Mac 侧 ssh 进手机的入口）
#
# ⚠️ 两个坑（都踩过）：
#   1) 顺序：必须**先**停看门狗 wx/supervisor.sh，否则它 30 秒后会把 dsh web 又拉起来。
#   2) ps 必须用绝对路径 /system/bin/ps：一旦把 Termux 的 bin 放到 PATH 前面，
#      ps 会解析到 Termux 的 procps，它不认 -o PID,ARGS，于是静默匹配不到任何进程
#      （第一版就是这样，跑了没报错但一个进程都没杀）。
# 下次打开 App / 重启手机时，boot.sh 会把它们重新拉起来（并补回看门狗）。
PS=/system/bin/ps
P=/data/data/com.termux/files/usr/bin
H=/data/data/com.termux/files/home

kill_pat() {   # 模式里的 [.] 是为了让 grep 不匹配到它自己
  for pid in $($PS -A -o PID,ARGS 2>/dev/null | grep -E "$1" | grep -v grep | awk '{print $1}'); do
    kill "$pid" 2>/dev/null
  done
}

# ① 看门狗（会复活 dsh web 的元凶）
kill_pat "wx/supervisor[.]sh"
rm -f "$H/wx/supervisor.pid"

# ② dsh web（3080）：node 本体 + proot 包装 + 挂在它下面的 MCP
kill_pat "dsh web --host 127[.]0[.]0[.]1 --port 3080"
kill_pat "proot .*dsh web"
kill_pat "jev-mcp/dist/index[.]js"

# ③ 代理桥（3096）：node 本体 + proot 包装
kill_pat "node bridge[.]mjs"
kill_pat "proot .*bridge[.]mjs"

# ④ URL 小服务（3097）：先杀"自愈壳"再杀本体 —— 壳每 1 秒会把本体拉回来
kill_pat "dsb/url-server-loop[.]sh"
rm -f "$H/dsb/url-server-loop.pid"
kill_pat "dsb/url-server[.]py"

# ④b 结论卡守护 back2dsb（proot 包装 + python 本体）
kill_pat "back2dsb_daemon[.]py"

# ④c USB 门控守护 + syncthing + 唤醒锁（2026-09-30 加）
#     它平时只在"拔线"时才释放 Termux 唤醒锁；App 走了就没人管它，
#     所以这里要主动杀守护、停 syncthing、解锁 —— 否则 App 都关了锁还攥着。
# 注意：usb-mode 的 TERM trap 要等它当前的 `sleep 30` 跑完才执行（最长 30 秒），
# 关 App 场景下不等它 —— 直接 -9，pidfile 由下一行清掉（实测 TERM 后它还会赖 30 秒）。
for pid in $($PS -A -o PID,ARGS 2>/dev/null | grep -E "dsb/usb-mode[.]sh" | grep -v grep | awk '{print $1}'); do
  kill -9 "$pid" 2>/dev/null
done
rm -f "$H/dsb/usb-mode.pid"
kill_pat "syncthing serve"
"$P/termux-wake-unlock" >/dev/null 2>&1 || true

# ⑤ 给它们 2 秒优雅退出，赖着不走的补一刀（看门狗要重点盯：它会复活 dsh web）
sleep 2
kill_pat "wx/supervisor[.]sh"
kill_pat "dsh web --host 127[.]0[.]0[.]1 --port 3080"
kill_pat "node bridge[.]mjs"
kill_pat "dsb/url-server-loop[.]sh"
kill_pat "dsb/url-server[.]py"
LEFT=$($PS -A -o PID,ARGS 2>/dev/null | grep -E "dsh web|bridge[.]mjs|url-server|back2dsb|usb-mode|syncthing serve" | grep -v grep | wc -l)
echo "DSH_STOPPED $(date +%H:%M:%S) 残留=${LEFT}"
