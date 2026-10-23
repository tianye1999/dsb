#!/bin/bash
# 逗沙包仓库的 git 入口（容器里没有 git，Termux 侧有）
#
# 为什么要有这个包装：容器（proot Ubuntu）的 PATH 里没有 git，
# 但 Termux 的 git 可以直接跑（实测 2.55.0 可用，无需 LD_LIBRARY_PATH）。
#
# 用法：
#   bash /root/dsb-app/git.sh status
#   bash /root/dsb-app/git.sh log --oneline -10
#   bash /root/dsb-app/git.sh diff
#   bash /root/dsb-app/git.sh add -A && bash /root/dsb-app/git.sh commit -m "说明"
#   bash /root/dsb-app/git.sh show HEAD~1
set -e
GIT=/data/data/com.termux/files/usr/bin/git
[ -x "$GIT" ] || { echo "✗ 找不到 Termux 的 git：$GIT" >&2; exit 1; }
exec "$GIT" -C /root/dsb-app "$@"
