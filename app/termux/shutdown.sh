#!/data/data/com.termux/files/usr/bin/bash
# 划掉 App 时调用：把手机侧跟 dsh 有关的全都停掉（省电、安静）。
#
# ⚠️ 两个坑（都踩过）：
#   1) 顺序：必须**先**停看门狗 wx/supervisor.sh，否则它 30 秒后会把 dsh web 又拉起来。
#   2) ps 必须用绝对路径 /system/bin/ps：一旦把 Termux 的 bin 放到 PATH 前面，
#      ps 会解析到 Termux 的 procps，它不认 -o PID,ARGS，于是静默匹配不到任何进程
#      （第一版就是这样，跑了没报错但一个进程都没杀）。
# 下次打开 App / 重启手机时，boot.sh 会把它们重新拉起来（并补回看门狗）。
PS=/system/bin/ps
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

# ④ URL 小服务（3097）
kill_pat "dsb/url-server[.]py"

# ⑤ 给它们 2 秒优雅退出，赖着不走的补一刀（看门狗要重点盯：它会复活 dsh web）
sleep 2
kill_pat "wx/supervisor[.]sh"
kill_pat "dsh web --host 127[.]0[.]0[.]1 --port 3080"
kill_pat "node bridge[.]mjs"
LEFT=$($PS -A -o PID,ARGS 2>/dev/null | grep -E "dsh web|bridge[.]mjs|url-server" | grep -v grep | wc -l)
echo "DSH_STOPPED $(date +%H:%M:%S) 残留=${LEFT}"
