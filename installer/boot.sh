#!/data/data/com.termux/files/usr/bin/bash
# 公开安装器的启动入口，不包含旧实验服务、自动提交或端口转发。
set -eu
export PATH="${PREFIX:?}/bin:$PATH"
mkdir -p "$HOME/wx"
rm -f "$HOME/.dsb-install-pending"
setsid nohup "$PREFIX/bin/bash" "$HOME/wx/supervisor.sh" \
  >>"$HOME/wx/supervisor.log" 2>&1 </dev/null &
bash "$HOME/dsb/dsh-up.sh" >/dev/null 2>&1 &
bash "$HOME/dsb/ensure-private-bridge.sh"
