#!/data/data/com.termux/files/usr/bin/bash
# DSH App 的手机侧引导（幂等，随便调多少次都行）：
#   ① URL 小服务 127.0.0.1:3097   —— App 靠它拿到"当前可用地址"
#   ② dsh web    127.0.0.1:3080   —— 网页主体（含 token 校验/修复）
#   ③ 手机代理桥 .dsb-ipc/bridge.sock —— 可信任务入口
#
# 坑：proot-distro 内部要调用 proot，必须先把 Termux 的 bin 放进 PATH，
# 否则会报 "proot utility does not exist"（从 App 的 RUN_COMMAND 进来时 PATH 不一定齐）。
export PATH=/data/data/com.termux/files/usr/bin:$PATH
P=/data/data/com.termux/files/usr/bin
H=/data/data/com.termux/files/home
[ ! -e "$H/.dsb-ipc/maintenance" ] || exit 0
mkdir -p "$H/dsb"

alive() {
  "$P/python3" -c "import socket,sys;s=socket.socket();s.settimeout(0.6);sys.exit(0 if s.connect_ex(('127.0.0.1',int(sys.argv[1])))==0 else 1)" "$1"
}

# ① URL 小服务
alive 3097 || setsid nohup "$P/python3" "$H/dsb/url-server.py" >>"$H/dsb/url-server.log" 2>&1 </dev/null &

# ② dsh web（脚本内部有锁，重复调用不会打架）
"$H/dsb/dsh-up.sh" >>"$H/dsb/dsh-up.log" 2>&1 &

# ③ 手机代理桥（proot 里存在才起）
"$P/bash" "$H/dsb/ensure-private-bridge.sh" >>"$H/dsb/bridge-start.log" 2>&1 &
# ④ 看门狗（shutdown.sh 会把它停掉，用户回来时补上；它负责 sshd/dsh web 的长期存活）
if [ -f "$H/wx/supervisor.sh" ]; then
  pgrep -f "wx/supervisor[.]sh" >/dev/null 2>&1 || setsid nohup "$H/wx/supervisor.sh" >>"$H/wx/supervisor.log" 2>&1 </dev/null &
fi
exit 0
