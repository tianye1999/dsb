#!/bin/bash
# tsh.sh —— 从容器里跑 Termux 侧命令（唯一正确姿势）
# ⚠️ 容器里没有 ssh，必须用 Termux 的绝对路径 + LD_LIBRARY_PATH；
#    且必须 -i 指定钥匙（否则 Termux 侧 HOME 找不到）。
# 用法： bash /root/termux-dsb/tsh.sh 'bash ~/dsb/fix-3097.sh'
TU="${TERMUX_USER:-$(cat /root/termux-dsb/termux-user 2>/dev/null)}"
[ -n "$TU" ] || { echo "tsh.sh: 请设置 TERMUX_USER 或写入 /root/termux-dsb/termux-user（Termux 的 Linux 用户名，形如 u0_aNNN）" >&2; exit 2; }
exec env LD_LIBRARY_PATH=/data/data/com.termux/files/usr/lib \
  /data/data/com.termux/files/usr/bin/ssh \
  -i /root/.ssh/id_ed25519 -p 8022 \
  -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=8 \
  "$TU"@127.0.0.1 "$@"
