#!/data/data/com.termux/files/usr/bin/bash
# DSH App 的手机侧引导（幂等，随便调多少次都行）：
#   ① URL 小服务 127.0.0.1:3097   —— 仅保留本次迁移外的旧功能
#   ② dsh web    127.0.0.1:3080   —— 网页主体（含 token 校验/修复）
#   ③ 手机代理桥 .dsb-ipc/bridge.sock —— 可信任务入口
#   ④ 看门狗 wx/supervisor.sh（sshd / dsh web）
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

# ① URL 小服务（2026-09-24 起套一层自愈壳：崩了 1 秒内自己起来。
#    起因：它静默死过一次，而 supervisor.sh 里根本没有 3097、boot 的 alive 要等下次 /ensure，
#    空档期 20 分钟，网页里的额度面板一直 "Failed to fetch"）
alive 3097 || setsid nohup "$P/bash" "$H/dsb/url-server-loop.sh" >>"$H/dsb/url-server.log" 2>&1 </dev/null &

# ② dsh web（脚本内部有锁，重复调用不会打架）
"$H/dsb/dsh-up.sh" >>"$H/dsb/dsh-up.log" 2>&1 &

# ③ 手机代理桥（proot 里存在才起）
"$P/bash" "$H/dsb/ensure-private-bridge.sh" >>"$H/dsb/bridge-start.log" 2>&1 &
# ④ 看门狗（shutdown.sh 会把它停掉，用户回来时补上；它负责 sshd/dsh web 的长期存活）
#   ⚠️ 2026-09-23 加固：原来写成 `pgrep ... || setsid ... &` —— 后台符 & 作用在**整个
#      A||B 列表**上，条件判断与启动被裹在一起（"启动命令被 fi 提前收尾 / 偶尔没起来"
#      的温床）。改成显式嵌套，一眼可验。
# ④c 充电不锁屏守护（2026-10-21 用户要求：DSB App 在跑就别锁屏）
if [ -x "$H/dsb/stayon-daemon.sh" ] && ! pgrep -f "stayon-daemo[n].sh" >/dev/null 2>&1; then
  setsid nohup "$H/dsb/stayon-daemon.sh" >>"$H/dsb/stayon.log" 2>&1 </dev/null &
  echo "boot: stayon-daemon 已拉起（充电不锁屏）"
fi

if [ -f "$H/wx/supervisor.sh" ]; then
  if ! pgrep -f "wx/supervisor[.]sh" >/dev/null 2>&1; then
    setsid nohup "$H/wx/supervisor.sh" >>"$H/wx/supervisor.log" 2>&1 </dev/null &
    echo "boot: supervisor 已拉起"
  fi
fi

# ④c 防"全家被杀"三件套（2026-09-25 用户报长对话总被打断）
#   取证结论（logcat am_kill + device_config）：
#   ① Android 12+ 幻影进程杀手（phantom process killer）默认最多 32 个子进程 —— Termux 底下
#      node/proot/python/sshd/Chromium 一超就被整批砍（= sshd+3097+DSH 一起暴毙那次）；
#   ② Termux 没持有 wake-lock 时只是普通后台进程，一挤就让位。
#   这三条都是"让系统别杀"，不是死后重启；重启手机后 device_config 会复位 → 必须在 boot 里补。
#   ⚠️ 2026-09-30 用户要求省电：不再无条件持锁。
#     wx/supervisor.sh 2026-09-20 的结论就是"这锁让 CPU 24 小时不睡，是耗电快/发热主因"，
#     而这里 2026-09-25 又无条件加上 → 两个脚本打架。现在统一交给 ④d 的 usb-mode.sh：
#     **插着 USB（Mac 可达）时才持锁**，拔线立即释放。想永久持锁：touch ~/dsb/wakelock-always.on
# "$P/termux-wake-lock" >/dev/null 2>&1 || true
if [ -e "$H/dsb/adb-target" ] || [ -e "$H/.dsb-installer-managed" ]; then
  "$P/bash" "$H/wx/wx_adb_connect.sh" >/dev/null 2>&1 || true
else
  # 重连脚本不接管无 target 的旧手动环境，仅此分支保留旧默认值。
  "$P/adb" connect 127.0.0.1:5555 >/dev/null 2>&1 || true
fi
(
  # 选择器会主动 exit；只终止本组 ADB 设置，不影响后续守护。
  source "$(dirname "${BASH_SOURCE[0]}")/adb-target.sh" || exit $?
  "${DSB_ADB_COMMAND[@]}" "${DSB_ADB_SELECTOR[@]}" shell "settings put global settings_enable_monitor_phantom_procs false" >/dev/null 2>&1 || true
  "${DSB_ADB_COMMAND[@]}" "${DSB_ADB_SELECTOR[@]}" shell "device_config put activity_manager max_phantom_processes 2147483647" >/dev/null 2>&1 || true
  "${DSB_ADB_COMMAND[@]}" "${DSB_ADB_SELECTOR[@]}" shell "dumpsys deviceidle whitelist +com.termux" >/dev/null 2>&1 || true
) >/dev/null 2>&1 || echo 'boot: ADB 选择器不可用，跳过三项 ADB 设置；其它服务继续启动' >&2

# ④d USB 模式守护（2026-09-30 用户要求省电）：
#   只在「插着 USB、Mac 可达（adb reverse 22300）」时才跑 syncthing、才持 termux 唤醒锁；
#   拔线后自动停 syncthing + 释放锁，让 CPU 深睡。判据与成本见该脚本头部注释。
if [ -f "$H/dsb/usb-mode.sh" ]; then
  if ! pgrep -f "[u]sb-mode[.]sh" >/dev/null 2>&1; then
    setsid nohup "$P/bash" "$H/dsb/usb-mode.sh" >>"$H/dsb/usb-mode.log" 2>&1 </dev/null &
    echo "boot: usb-mode 守护已拉起"
  fi
fi

# ⑤ git 自动提交守护（2026-09-22 用户要求"用程序保证"）
#   ⚠️ 它住在 proot 容器里（/root/dsh-config/），而 Termux 的 ~/.termux/boot/ 钩子
#      **不会执行**（这台没装 Termux:Boot）—— 只有本脚本是重启后真正跑的引导入口，
#      所以必须挂在这里（2026-09-22 踩到：钩子装了但从未运行）。
AC=/data/data/com.termux/files/usr/var/lib/proot-distro/containers/ubuntu/rootfs/root/dsh-config/auto-commit.sh
# 2026-09-30：改成【按需】——存在 ~/dsb/auto-commit.off 时不再常驻（用户要求）。
if [ -f "$AC" ] && [ ! -f "/data/data/com.termux/files/home/dsb/auto-commit.off" ]; then
  #   ⚠️ 用 [a]uto-commit 惯用法：正则里的 [a] 不匹配字面量 "a"，
  #      否则 pgrep -f 会匹配到**本脚本自己**的命令行（含同样的字符串）
  #      → 误判"已在跑"而跳过启动（2026-09-22 踩到）。
  pgrep -f "[a]uto-commit.sh --watch" >/dev/null 2>&1 || \
    setsid nohup "$P/proot-distro" login --no-kill-on-exit ubuntu -- \
      /bin/bash -c '/data/data/com.termux/files/usr/bin/bash /root/dsh-config/auto-commit.sh --watch' \
      >>"$H/dsb/auto-commit.log" 2>&1 </dev/null &
fi
# ⑥ 结论卡显示完"自动切回逗沙包"守护（2026-09-24 用户要求：用程序保证，不靠代理记性）
#   守护住在容器里（要读容器的会话存储），所以用 proot-distro 进去起；
#   脚本自带 pidfile 判重，重复调用无害。
B2D=/data/data/com.termux/files/usr/var/lib/proot-distro/containers/ubuntu/rootfs/root/.dsh-phone/back2dsb_daemon.py
if [ -f "$B2D" ]; then
  if ! pgrep -f "[b]ack2dsb_daemon.py" >/dev/null 2>&1; then
    setsid nohup "$P/proot-distro" login --no-kill-on-exit ubuntu -- \
      /bin/bash -c '/usr/bin/python3 /root/.dsh-phone/back2dsb_daemon.py --watch' \
      >>"$H/dsb/back2dsb.log" 2>&1 </dev/null &
    echo "boot: back2dsb 守护已拉起"
  fi
fi

exit 0
