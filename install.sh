#!/data/data/com.termux/files/usr/bin/bash
# 逗沙包公开安装入口。日志只记录我们生成的状态，不收集子进程输出和输入。
set +x
set +v
set -Eeuo pipefail
umask 077
SOURCE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
APK_URL=''
DSH_VERSION=''
# 立即移出导出环境，apt/npm/日志进程都不继承 Key。
INSTALL_KEY="${DEEPSEEK_API_KEY:-}"
export -n INSTALL_KEY
unset DEEPSEEK_API_KEY
LOG="$HOME/dsb-install.log"
STAGE=环境检查

log() { printf '%s [%s] %s\n' "$(date '+%F %T')" "$STAGE" "$*" | tee -a "$LOG"; }
fail() { log "$(printf '\033[31m%s\033[0m' "$*")"; exit 1; }
pause() {
  [ "${DSB_NONINTERACTIVE:-0}" = 1 ] && return 0
  printf '%s\n' "$*"
  read -r -p '完成后按回车继续（Ctrl-C 可中断，重跑会续装）：' _
}
in_ubuntu() { proot-distro login ubuntu -- "$@" </dev/null; }
# 非交互 ADB 不得抢走安装器的后续输入；pair 单独显式接收隐藏配对码管道。
adb() { command adb "$@" </dev/null; }
device_adb() {
  local transport_id
  transport_id=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["transport_id"])' "$HOME/dsb/adb-transport.json") || return 78
  if [ -d "$PREFIX/var/lib/dsb-adb" ]; then
    adb -t "$transport_id" "$@"
  else
    "$PREFIX/bin/python3" -I "$HOME/dsb/adb_identity.py" --execute -t "$transport_id" "$@" </dev/null
  fi
}
package_adb_transport() {
  # android-tools 的 postinst 会调用 kill-server；依赖安装也不能接触默认 TCP。
  # 新建空私有目录，不复用尚未检查的 bootstrap peer，也不提前启动 server。
  PACKAGE_ADB_DIRECTORY=$(mktemp -d "$HOME/.dsb-package-adb.XXXXXXXX")
  export -n PACKAGE_ADB_DIRECTORY
  chmod 700 "$PACKAGE_ADB_DIRECTORY"
  export ADB_SERVER_SOCKET="localfilesystem:$PACKAGE_ADB_DIRECTORY/server.sock"
  unset ANDROID_ADB_SERVER_ADDRESS ANDROID_ADB_SERVER_PORT
}
find_rootfs() {
  local p
  for p in "$PREFIX/var/lib/proot-distro/containers/ubuntu/rootfs" "$PREFIX/var/lib/proot-distro/installed-rootfs/ubuntu"; do
    if [ -f "$p/etc/os-release" ]; then printf '%s\n' "$p"; return; fi
  done
  return 1
}
open_setting() {
  # 已配对的 shell 可以打开大多数设置页；不代点或写授权设置。
  device_adb shell am start -a "$1" "${@:2}" >/dev/null 2>&1 || \
    log '系统未提供此设置页，请在设置中手动找到对应项目'
}

check_env() {
  [ -n "${PREFIX:-}" ] && [ -x "${PREFIX:-/nonexistent}/bin/pkg" ] && [ -x /system/bin/getprop ] || {
    printf '请在 Android 手机的原生 Termux 中运行，不能在 Mac/Ubuntu 容器里运行。\n'; exit 1;
  }
  [ "$(id -u)" != 0 ] || { printf '请退出 root/proot 后运行。\n'; exit 1; }
  case "${TERMUX_APK_RELEASE:-unknown}" in F_DROID|GITHUB) ;; *)
    printf '只支持 Termux 官方 F-Droid/GitHub 版本，请先更换来源（插件必须同源）。\n'; exit 1 ;; esac
  case "${TERMUX_VERSION:-}" in 0.118.*|0.119.*|0.12[0-9].*) ;; *)
    printf '需要 Termux 0.118.x 或更新的受支持版本。\n'; exit 1 ;; esac
  [ "$(/system/bin/getprop ro.build.version.sdk)" -ge 30 ] || { printf '需要 Android 11 或更新版本。\n'; exit 1; }
  case "$(uname -m)" in aarch64|x86_64) ;; *) printf 'v1 支持 arm64 / x86_64，暂不支持 32 位设备。\n'; exit 1 ;; esac
  export PATH="$PREFIX/bin:$PATH"
  export TMPDIR="$PREFIX/tmp"
  case "$SOURCE/" in "$HOME/"*) ;; *) printf '安装源必须位于 Termux HOME 私有目录。\n'; exit 2 ;; esac
  local source_path="$SOURCE" info owner mode
  while true; do
    info=$(stat -c '%u %a' "$source_path")
    read -r owner mode <<<"$info"
    [ "$owner" = "$(id -u)" ] && (( (8#$mode & 022) == 0 )) || { printf '安装源路径属主/权限不安全。\n'; exit 2; }
    [ "$source_path" != "$HOME" ] || break
    source_path=$(dirname "$source_path")
  done
  [ -z "$(find "$SOURCE" \( -type l -o ! -user "$(id -un)" -o -perm /022 \) -print -quit)" ] || {
    printf '安装源含符号链接、非本人属主或组/其它用户可写文件。\n'; exit 2;
  }
  if [ -e "$PREFIX/var/lib/dsb-adb" ] || [ -L "$PREFIX/var/lib/dsb-adb" ]; then
    python3 "$SOURCE/hardening/adb-private/deploy.py" guard-legacy || exit 2
  fi
  [ ! -L "$LOG" ] || { printf '日志不能是符号链接。\n'; exit 1; }
  touch "$LOG"; chmod 600 "$LOG"
  log "Termux ${TERMUX_VERSION} (${TERMUX_APK_RELEASE}) / Android $(/system/bin/getprop ro.build.version.release) / $(uname -m)"
  exec 8>"$HOME/.dsb-install.lock"
  flock -n 8 || fail '已有安装器在运行'
  trap 'log "有步骤或检查项未完成；请先处理上方提示，再重跑或 doctor；Key 不会重新询问"' ERR
  trap 'log "安装已中断；可重跑续装"; exit 130' INT TERM
}

deploy_files() {
  python3 "$SOURCE/installer/configure.py" ubuntu-https "$RF"
  mkdir -p "$HOME/dsb" "$HOME/wx" "$HOME/.dsb-ipc" \
    "$RF/root/.dsh-phone/skills" "$RF/root/dsh-bridge" "$RF/root/termux-dsb" \
    "$RF/root/dsh-config" "$RF/root/.dsh/skills/dsb-phone" "$RF/root/dsb-workspaces"
  [ ! -L "$HOME/.dsb-ipc" ] || fail 'IPC 目录不能是符号链接'
  chmod 700 "$HOME/.dsb-ipc"
  # 只部署代码，不把仓库里的实验状态、锁、截图或历史凭据复制给新用户。
  cp "$SOURCE"/agent/*.py "$SOURCE/agent/SKILL.md" "$RF/root/.dsh-phone/skills/"
  cp "$SOURCE"/bridge/*.mjs "$RF/root/dsh-bridge/"
  for script in "$SOURCE"/termux/*.sh "$SOURCE"/termux/*.py; do
    case "$(basename "$script")" in supervisor.sh|boot.sh) continue ;; esac
    cp "$script" "$HOME/dsb/"
  done
  cp "$SOURCE/hardening/adb-private/bootstrap.py" "$HOME/dsb/"
  cp "$SOURCE/hardening/adb-private/tcp-owner.py" "$HOME/dsb/adb-tcp-owner.py"
  cp "$SOURCE/hardening/adb-private/payload/adb_policy.py" "$SOURCE/hardening/adb-private/payload/adb_identity.py" "$SOURCE/hardening/adb-private/payload/adb_client.py" "$SOURCE/hardening/adb-private/payload/adb_relay.py" "$HOME/dsb/"
  for script in "$SOURCE"/termux/*.sh "$SOURCE"/termux/*.py; do
    case "$(basename "$script")" in supervisor.sh|boot.sh) continue ;; esac
    cp "$script" "$RF/root/termux-dsb/"
  done
  cp "$SOURCE"/dsh-config/* "$RF/root/dsh-config/"
  mkdir -p "$RF/root/dsh-web-tweaks/dsb-brand"
  cp "$SOURCE"/dsh-web-tweaks/*.py "$SOURCE"/dsh-web-tweaks/*.sh "$RF/root/dsh-web-tweaks/"
  cp "$SOURCE"/dsh-web-tweaks/dsb-brand/*.js "$SOURCE"/dsh-web-tweaks/dsb-brand/*.mjs \
    "$SOURCE"/dsh-web-tweaks/dsb-brand/package.json "$SOURCE"/dsh-web-tweaks/dsb-brand/cordis.patch.yml \
    "$RF/root/dsh-web-tweaks/dsb-brand/"
  cp "$SOURCE/app/res/mipmap-xxxhdpi/ic_launcher_foreground.png" "$RF/root/dsh-web-tweaks/dsb-brand/source-icon.png"
  cp "$SOURCE/installer/profiles.py" "$RF/root/.dsb-profiles.py"
  cp "$SOURCE/agent/SKILL.md" "$RF/root/.dsh/skills/dsb-phone/SKILL.md"
  cp "$SOURCE/installer/boot.sh" "$HOME/dsb/boot.sh"
  cp "$SOURCE/termux/start-dsh.sh" "$HOME/start-dsh.sh"
  cp "$SOURCE/termux/start-dsh.sh" "$SOURCE/termux/wx_adb_connect.sh" "$HOME/wx/"
  if [ ! -e "$HOME/wx/supervisor.sh" ] && [ ! -L "$HOME/wx/supervisor.sh" ] && [ ! -f "$PREFIX/var/lib/dsb-adb/journal.json" ]; then
    cp "$SOURCE/hardening/adb-private/supervisor.sh" "$HOME/wx/supervisor.sh"
    chmod 700 "$HOME/wx/supervisor.sh"
  fi
  chmod 700 "$HOME/dsb/"*.sh "$HOME/wx/wx_adb_connect.sh" "$HOME/start-dsh.sh"
  printf '%s\n' "$(id -un)" > "$RF/root/termux-dsb/termux-user"
  # 用户已有配置不覆盖；公共安装不引入需要第二把 Key 的实验 MCP。
  mkdir -p "$RF/root/.dsh/profiles"
  for profile in web phone headless; do
    mkdir -p "$RF/root/.dsh/profiles/$profile"
    for name in cordis.yml cordis.patch.yml; do
      dst="$RF/root/.dsh/profiles/$profile/$name"
      if [ ! -f "$dst" ]; then
        if [ "$name" = cordis.yml ]; then cp "$SOURCE/dsh-config/cordis.$profile.yml" "$dst"
        else cp "$SOURCE/installer/cordis.patch.yml" "$dst"; fi
      fi
    done
  done
  [ -f "$RF/root/.dsh/settings.yaml" ] || cp "$SOURCE/dsh-config/settings.yaml" "$RF/root/.dsh/settings.yaml"
}

setup_profiles() {
  STAGE=隐私与品牌
  in_ubuntu python3 /root/.dsb-profiles.py /root/.dsh/profiles /root/dsh-web-tweaks/dsb-brand
  in_ubuntu node /root/dsh-web-tweaks/dsb-brand/build.mjs /root/dsh-web-tweaks/dsb-brand/source-icon.png
  # 必须使用 main 的写入+实际 dump-config 读回流程；不调用 plugin add/pnpm。
  in_ubuntu bash /root/dsh-config/install-profile-patches.sh
  in_ubuntu bash /root/dsh-web-tweaks/apply-all.sh
}

setup_ssh() {
  mkdir -p "$RF/root/.ssh" "$HOME/.ssh"
  chmod 700 "$RF/root/.ssh" "$HOME/.ssh"
  if [ ! -f "$RF/root/.ssh/id_ed25519" ]; then
    ssh-keygen -q -t ed25519 -N '' -C dsb-container -f "$RF/root/.ssh/id_ed25519" >/dev/null
  fi
  if [ ! -s "$RF/root/.ssh/id_ed25519.pub" ]; then
    ssh-keygen -y -f "$RF/root/.ssh/id_ed25519" > "$RF/root/.ssh/id_ed25519.pub"
  fi
  pub=$(cat "$RF/root/.ssh/id_ed25519.pub")
  touch "$HOME/.ssh/authorized_keys"
  grep -qxF "$pub" "$HOME/.ssh/authorized_keys" || printf '%s\n' "$pub" >> "$HOME/.ssh/authorized_keys"
  chmod 600 "$RF/root/.ssh/id_ed25519" "$HOME/.ssh/authorized_keys"
  # 先告知、备份，再生成完整配置，避免 OpenSSH 首值优先造成虚假收紧。
  if [ ! -f "$HOME/.dsb-sshd-configured" ]; then
    pause '即将把 Termux SSH 改为仅本机、公钥登录；原配置会备份，已有电脑远程/密码登录方式将停止适用。不同意请 Ctrl-C。'
    python3 "$SOURCE/installer/ssh_config.py" "$PREFIX/etc/ssh/sshd_config"
    sshd -t
    effective=$(sshd -T | tr '[:upper:]' '[:lower:]')
    grep -qx 'passwordauthentication no' <<<"$effective" || fail 'sshd 实际仍允许密码，配置未通过'
    [ "$(grep '^listenaddress ' <<<"$effective")" = 'listenaddress 127.0.0.1:8022' ] || fail 'sshd 实际监听地址与本机 8022 不符，请检查配置备份'
    touch "$HOME/.dsb-sshd-configured"
  fi
  sshd -t
  if pgrep -x sshd >/dev/null 2>&1; then
    log '警告：已运行 sshd 不会自动重载；需本人重启并核对实际监听，配置检查不等于运行中监听检查'
    pause '已有 sshd 进程不会因修改文件自动重载。请本人结束原 SSH 会话并重启 Termux sshd，再确认只监听 127.0.0.1:8022；原配置备份为 sshd_config.pre-dsb。未核对前不要认为现有监听已收紧。'
  else
    sshd
  fi
}

setup_key() {
  STAGE=DeepSeek密钥
  local keyfile="$RF/root/.dsh/.deepseek-key"
  if [ -s "$keyfile" ] && [ ! -L "$keyfile" ]; then
    python3 "$SOURCE/installer/configure.py" key "$RF/root" </dev/null
    log '已有 Key，保留且不再询问'
  else
    if [ -z "$INSTALL_KEY" ]; then
      [ "${DSB_NONINTERACTIVE:-0}" != 1 ] || fail '请用 DEEPSEEK_API_KEY 预置测试 Key'
      read -r -s -p 'DeepSeek API Key（隐藏输入，只问这一次）：' INSTALL_KEY
      printf '\n'
    fi
    printf '%s\n' "$INSTALL_KEY" | python3 "$SOURCE/installer/configure.py" key "$RF/root"
    log 'Key 已私密保存（0600）；未写入日志'
  fi
  unset INSTALL_KEY
}

setup_adb() {
  STAGE=无线调试
  local pair_code pair_address
  export -n pair_code pair_address
  python3 "$SOURCE/hardening/adb-private/bootstrap.py" || fail 'ADB 传输冲突，未配对/连接'
  if [ ! -d "$PREFIX/var/lib/dsb-adb" ]; then
    export ADB_SERVER_SOCKET="localfilesystem:$PREFIX/var/run/dsb-adb-bootstrap/server.sock"
    unset ANDROID_ADB_SERVER_ADDRESS ANDROID_ADB_SERVER_PORT
  fi
  if bash "$HOME/wx/wx_adb_connect.sh" >/dev/null 2>&1; then
    TARGET=$(cat "$HOME/dsb/adb-target")
    log '已有本机无线连接，无需再次配对'
    return
  fi
  TARGET="${DSB_ADB_TARGET:-}"
  if [ -z "$TARGET" ] && [ -s "$HOME/dsb/adb-device-id" ]; then
    am start -a android.settings.APPLICATION_DEVELOPMENT_SETTINGS >/dev/null 2>&1 || \
      log '请手动打开开发者选项 → 无线调试'
    pause '已保存本机配对。请打开无线调试主页，更新连接地址，不必重新取配对码。'
    read -r -p 'IP:连接端口（只有授权被撤销时才输入 pair 重新配对）：' TARGET
    [ "$TARGET" != pair ] || TARGET=''
  fi
  if [ -z "$TARGET" ]; then
    am start -a android.settings.APPLICATION_DEVELOPMENT_SETTINGS >/dev/null 2>&1 || \
      log '请手动打开开发者选项 → 无线调试'
    pause '请打开开发者选项、无线调试，进入「使用配对码配对」。可分屏查看配对码。'
    read -r -p '配对页 IP:配对端口（不是连接端口）：' pair_address
    pair_address=$(python3 "$SOURCE/installer/configure.py" local-endpoint "$pair_address")
    read -r -s -p '六位配对码（隐藏）：' pair_code; printf '\n'
    [[ "$pair_code" =~ ^[0-9]{6}$ ]] || fail '配对码格式不正确'
    # 授权页面可能停留很久，提交配对码前重新检查双栈冲突和私有传输。
    python3 "$SOURCE/hardening/adb-private/bootstrap.py" || fail 'ADB 传输冲突，未提交配对码'
    # 配对码走 stdin，不进 argv、环境或日志。
    printf '%s\n' "$pair_code" | command adb pair "$pair_address"
    unset pair_code
    read -r -p '返回无线调试主页，输入 IP:连接端口：' TARGET
  fi
  TARGET=$(python3 "$SOURCE/installer/configure.py" local-endpoint "$TARGET")
  local verified=0 attempt
  for attempt in 1 2 3; do
    if python3 "$HOME/dsb/adb_identity.py" "$TARGET" >/dev/null; then verified=1; break; fi
    sleep 1
  done
  [ "$verified" = 1 ] || fail '本机 shell 身份挑战失败，连接已断开、未保存端点'
  printf '%s\n' "$TARGET" > "$HOME/dsb/adb-target"
  device_adb shell getprop ro.serialno | tr -d '\r' > "$HOME/dsb/adb-device-id"
  chmod 600 "$HOME/dsb/adb-target"
  log '本机 ADB 已连接并核验身份'
}

verify_apk_file() {
  local file="$1" expected report
  expected=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("apk_certificate_sha256") or "")' "$SOURCE/installer/release.json")
  if [ -n "${DSB_APK_PATH:-}" ]; then expected="${DSB_TEST_APK_CERT_SHA256:-$expected}"; fi
  [[ "$expected" =~ ^[a-f0-9]{64}$ ]] || fail '未固定发布证书 SHA-256；不能仅凭包名接受已安装 App'
  report=$(in_ubuntu apksigner verify --print-certs "$file") || fail 'APK 完整验签失败'
  printf '%s\n' "$report" | python3 "$SOURCE/installer/apk_certificate.py" "$expected" || fail '签名证书不符，未打开安装程序、未继续授权或覆盖'
}

verify_installed_apk() {
  local path installed="$HOME/.cache/dsb-installer/installed.apk"
  path=$(device_adb shell pm path com.dsb.assistant | tr -d '\r' | sed -n 's/^package://p' | head -n 1)
  [ -n "$path" ] || return 1
  mkdir -p "$(dirname "$installed")"
  device_adb pull "$path" "$installed" >/dev/null || fail '已安装 APK 不可读取，未继续'
  verify_apk_file "$installed"
}

install_apk() {
  STAGE=安装逗沙包
  if device_adb shell pm path com.dsb.assistant | grep -q '^package:'; then
    verify_installed_apk
    log '逗沙包已安装且签名证书核验通过，保留现有版本'
    return
  fi
  apk="$HOME/.cache/dsb-installer/DSB.apk"
  mkdir -p "$(dirname "$apk")"
  if [ -n "${DSB_APK_PATH:-}" ]; then cp "$DSB_APK_PATH" "$apk"
  else
    APK_URL=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["apk_url"])' "$SOURCE/installer/release.json")
    curl -fL --retry 3 --retry-all-errors --connect-timeout 15 --speed-time 30 --speed-limit 1024 "$APK_URL" -o "$apk.part" || \
      fail 'APK 下载失败；请检查网络后重跑，未打开旧 APK'
    mv "$apk.part" "$apk"
  fi
  expected=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["apk_sha256"] or "")' "$SOURCE/installer/release.json")
  if [ -n "${DSB_APK_PATH:-}" ]; then
    expected="${DSB_TEST_APK_SHA256:-$expected}"
    log '本地测试 APK：仍强制校验 SHA-256，不使用发布签名'
  fi
  [[ "$expected" =~ ^[a-f0-9]{64}$ ]] || fail '未固定 APK SHA-256；候选包不可对外安装，需 owner 完成发布装配'
  printf '%s  %s\n' "$expected" "$apk" | sha256sum -c - || fail 'APK SHA-256 不匹配，未打开安装程序'
  verify_apk_file "$apk"
  while true; do
    termux-open --view --content-type application/vnd.android.package-archive "$apk"
    pause '如有打开方式，选「软件包安装程序/Package installer」，不要选 Termux，然后点安装。如要求「允许来自此来源」，只允许 Termux 后返回，再按回车重新打开。系统若结束 Termux，重跑同一入口续装即可。'
    if device_adb shell pm path com.dsb.assistant | grep -q '^package:'; then verify_installed_apk; return; fi
    if [ "${DSB_NONINTERACTIVE:-0}" = 1 ]; then
      log 'APK 尚未安装；非交互测试不等待本人确认，doctor 将报告实际状态'
      return 0
    fi
    log '待本人确认 APK 安装；将重新打开安装器，Ctrl-C 可中断后续装'
  done
}

guide_permissions() {
  STAGE=本人授权
  open_setting android.settings.APPLICATION_DETAILS_SETTINGS -d package:com.dsb.assistant
  pause '先不要打开逗沙包。若系统限制侧载权限，请在本页右上菜单中本人确认「允许受限设置」，再继续。'
  open_setting android.settings.action.MANAGE_OVERLAY_PERMISSION -d package:com.dsb.assistant
  pause '请允许逗沙包显示在其它应用上层。'
  local overlay
  while true; do
    overlay=$(device_adb shell cmd appops get com.dsb.assistant SYSTEM_ALERT_WINDOW) || fail '悬浮窗状态不可读，未继续开启服务'
    grep -q 'SYSTEM_ALERT_WINDOW: allow' <<<"$overlay" && break
    log '悬浮窗尚未授权；无障碍会启动悬浮服务，暂不继续开启，避免崩溃'
    [ "${DSB_NONINTERACTIVE:-0}" != 1 ] || return 0
    open_setting android.settings.ACCESSIBILITY_SETTINGS
    pause '若显示「受限设置」，先点一次逗沙包让系统显示限制提示；此时不要开启服务。返回后继续。'
    open_setting android.settings.APPLICATION_DETAILS_SETTINGS -d package:com.dsb.assistant
    pause '在右上菜单本人确认「允许受限设置」。部分系统须先尝试受限权限才出现此菜单；若已允许则继续。'
    open_setting android.settings.action.MANAGE_OVERLAY_PERMISSION -d package:com.dsb.assistant
    pause '重新进入逗沙包，允许显示在其它应用上层；脚本将核对授权状态。'
  done
  open_setting android.settings.ACCESSIBILITY_SETTINGS
  pause '请开启逗沙包无障碍服务。若系统限制侧载设置，在应用详情的菜单中本人确认允许受限设置。'
  open_setting android.settings.ACTION_NOTIFICATION_LISTENER_SETTINGS
  pause '请开启逗沙包通知使用权。'
  open_setting android.settings.APPLICATION_DETAILS_SETTINGS -d package:com.dsb.assistant
  pause '请在逗沙包权限中允许通知和「运行 Termux 命令」(RUN_COMMAND)。不需要提供其它 Key。'
  for package in com.termux com.dsb.assistant; do
    open_setting android.settings.APPLICATION_DETAILS_SETTINGS -d "package:$package"
    pause "请允许 $package 自启动/后台运行，电池设为不受限（各 ROM 名称不同）。"
  done
}

web_ready() {
  local url code
  [ -s "$HOME/.dsb-web-url" ] || return 1
  url=$(cat "$HOME/.dsb-web-url")
  [[ "$url" =~ ^http://127\.0\.0\.1:3080/\?token=[A-Za-z0-9_-]+$ ]] || return 1
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "$url") || return 1
  [ "$code" = 200 ] || [ "$code" = 303 ]
}

doctor() {
  STAGE=自检
  local rc=0
  in_ubuntu python3 /root/.dsh-phone/skills/safe_tap.py doctor || rc=$?
  log "doctor 退出码 $rc（本次自检未调用模型）"
  if [ "$rc" != 0 ]; then
    log '基础检查未通过；未继续发送 ADB 权限查询，请先修复上方失败项'
    return 2
  fi
  local permissions
  permissions=$(device_adb shell dumpsys package com.dsb.assistant) || {
    log '权限状态不可读；请先修复上方 ADB/doctor 失败项，未继续发送 ADB 命令'
    return 2
  }
  if grep -q 'com.termux.permission.RUN_COMMAND: granted=true' <<<"$permissions"; then
    log '已授权：运行 Termux 命令'
  else log '待本人授权：逗沙包应用权限 → 运行 Termux 命令'; rc=2; fi
  if grep -q 'android.permission.POST_NOTIFICATIONS: granted=true' <<<"$permissions"; then
    log '已授权：通知权限'
  elif [ "$(/system/bin/getprop ro.build.version.sdk)" -ge 33 ]; then
    log '待本人授权：逗沙包应用权限 → 通知'; rc=2
  fi
  if device_adb shell cmd appops get com.dsb.assistant SYSTEM_ALERT_WINDOW | grep -q 'SYSTEM_ALERT_WINDOW: allow'; then
    log '已授权：悬浮窗'
  else log '待本人授权：显示在其它应用上层'; rc=2; fi
  if device_adb shell settings get secure enabled_notification_listeners | grep -q 'com.dsb.assistant/'; then
    log '已授权：通知使用权'
  else log '待本人授权：通知使用权'; rc=2; fi
  log '仍需本人核对：ROM 自启动/后台白名单；脚本不能跨 ROM 证明它已生效'
  curl --unix-socket "$HOME/.dsb-ipc/bridge.sock" -fsS --max-time 4 http://localhost/health >/dev/null && \
    log '私有任务桥 health 正常' || { log '警告：私有任务桥未就绪'; rc=2; }
  web_ready && log '网页入口已实际验证可用' || { log '警告：网页尚未就绪，请稍后重跑 doctor'; rc=2; }
  log '安装环境已部署。未通过的授权/亮屏/观测项需本人完成；不要把“环境就绪”当成任务验收。'
  return "$rc"
}

main_locked() {
  # 所有写操作之前识别旧部署，公开工具绝不迁移或回滚 owner 旧版。
  if [ -e "$PREFIX/var/lib/dsb-adb" ] || [ -L "$PREFIX/var/lib/dsb-adb" ]; then
    python3 "$SOURCE/hardening/adb-private/deploy.py" guard-legacy
  fi
  case "${1:-install}" in
    rollback) exec python3 "$SOURCE/hardening/adb-private/deploy.py" rollback ;;
    check|maintenance|resume) exec python3 "$SOURCE/hardening/adb-private/deploy.py" "$1" ;;
    doctor) TARGET=$(cat "$HOME/dsb/adb-target"); doctor; return ;;
    set-key)
      RF=$(find_rootfs)
      if [ -z "$INSTALL_KEY" ]; then read -r -s -p '新的 DeepSeek API Key（隐藏）：' INSTALL_KEY; printf '\n'; fi
      printf '%s\n' "$INSTALL_KEY" | python3 "$SOURCE/installer/configure.py" set-key "$RF/root"
      unset INSTALL_KEY
      log 'Key 已私密替换；重启 DSH 后使用新 Key'
      return ;;
    install) ;;
    *) fail '用法：bash install.sh [install|doctor|check|rollback|maintenance|resume]' ;;
  esac
  if [ ! -f "$HOME/.dsb-installer-managed" ] && { [ -e "$HOME/dsb/boot.sh" ] || [ -e "$HOME/wx/supervisor.sh" ]; }; then
    [ "${DSB_MIGRATE_MANUAL:-0}" = 1 ] || fail '检测到非安装器部署的旧环境；请备份并由本人评估迁移，显式 DSB_MIGRATE_MANUAL=1 才继续'
    backup="$HOME/dsb-manual-backup-$(date +%s)"
    mkdir -m 700 "$backup"
    for dir in dsb wx; do [ ! -d "$HOME/$dir" ] || cp -a "$HOME/$dir" "$backup/"; done
    [ ! -e "$HOME/start-dsh.sh" ] || cp -a "$HOME/start-dsh.sh" "$backup/"
    RF=$(find_rootfs || true)
    if [ -n "$RF" ]; then
      mkdir -m 700 "$backup/container"
      for dir in .dsh-phone .dsh dsh-bridge dsh-config termux-dsb; do
        [ ! -e "$RF/root/$dir" ] || cp -a "$RF/root/$dir" "$backup/container/"
      done
    fi
    log '旧脚本已备份；本次为显式迁移'
  fi
  touch "$HOME/.dsb-installer-managed"
  # 先撤销未完成的切换；否则维护闸会挡住后面的配对/连接步骤。
  if [ -f "$PREFIX/var/lib/dsb-adb/journal.json" ] || compgen -G "$PREFIX/var/lib/dsb-adb-stage-*" >/dev/null; then
    STAGE=中断恢复
    python3 "$SOURCE/hardening/adb-private/deploy.py" recover
    [ ! -e "$PREFIX/var/lib/dsb-adb/maintenance" ] && [ ! -L "$PREFIX/var/lib/dsb-adb/maintenance" ] || \
      fail 'ADB 处于手动维护态；请运行 bash install.sh check / resume，或显式 rollback'
  fi
  package_adb_transport
  STAGE=Termux依赖
  log '安装依赖；首次下载较多，需要可访问 Termux/GitHub/Ubuntu/npm/PyPI 的网络'
  # APK 的 bootstrap 往往比仓库旧；部分升级会导致 curl/adb ABI 不匹配。
  # 用 apt-get 而非依赖 curl 的 pkg，确保中断后也能修复。
  if [ ! -f "$HOME/.dsb-base-upgraded" ] && [ ! -f "$PREFIX/var/lib/dsb-adb/journal.json" ]; then
    pause '即将 full-upgrade Termux 基础包，可能影响你已有的命令和服务；保留配置文件。已有环境请先备份，不同意请 Ctrl-C。'
    apt-get -o Acquire::Retries=3 -o APT::Update::Error-Mode=any update
    apt-get -y -o Dpkg::Options::=--force-confold full-upgrade
    touch "$HOME/.dsb-base-upgraded"
  fi
  missing=()
  for package in openssh git android-tools proot-distro python clang curl util-linux x11-repo; do
    [ "$(dpkg-query -W -f='${db:Status-Status}' "$package" 2>/dev/null || true)" = installed ] || missing+=("$package")
  done
  if [ "${#missing[@]}" -gt 0 ]; then
    apt-get -o Acquire::Retries=3 -o APT::Update::Error-Mode=any update
    pkg install -y --no-upgrade "${missing[@]}"
  fi
  if [ "$(dpkg-query -W -f='${db:Status-Status}' scrcpy 2>/dev/null || true)" != installed ]; then
    # x11-repo 的 postinst 即使索引下载失败也返回成功；续装必须重新刷新。
    apt-get -o Acquire::Retries=3 -o APT::Update::Error-Mode=any update
    pkg install -y --no-upgrade scrcpy
  fi
  STAGE=Ubuntu
  if ! find_rootfs >/dev/null; then
    # 首装先完成本人授权，避免设置页结束 Termux 时留下未确认的任务桥归属。
    [ ! -L "$HOME/.dsb-install-pending" ] || fail '首装状态不能是符号链接'
    touch "$HOME/.dsb-install-pending"
    proot-distro install ubuntu:24.04
  fi
  # login 会把旧布局迁移成新布局；迁移后再定位。
  in_ubuntu /bin/true
  RF=$(find_rootfs)
  STAGE=部署文件
  deploy_files
  setup_ssh
  log '即将开启 allow-external-apps：逗沙包通过 Termux 官方 RUN_COMMAND 启动服务。系统还需你给逗沙包单独授权；请勿给不信任的 App 此权限。'
  pause '确认开启后继续；将先备份 termux.properties，再修改配置。'
  python3 "$SOURCE/installer/configure.py" external "$HOME/.termux/termux.properties"
  termux-reload-settings || true
  STAGE=容器依赖
  cp "$SOURCE/installer/container-deps.sh" "$RF/root/.dsb-container-deps.sh"
  mkdir -p "$RF/root/.dsb-deps/dsh"
  cp "$SOURCE/installer/requirements.txt" "$RF/root/.dsb-deps/"
  cp "$SOURCE/installer/dsh/package.json" "$SOURCE/installer/dsh/npm-shrinkwrap.json" "$RF/root/.dsb-deps/dsh/"
  DSH_VERSION=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["dsh_version"])' "$SOURCE/installer/release.json")
  in_ubuntu bash /root/.dsb-container-deps.sh "$DSH_VERSION" "$ADB_SERVER_SOCKET"
  rmdir "$PACKAGE_ADB_DIRECTORY"
  setup_profiles
  setup_key
  setup_adb
  STAGE=默认ADB加固
  rc=0
  python3 "$SOURCE/hardening/adb-private/deploy.py" apply || rc=$?
  if [ "$rc" -ne 0 ] && [ -d "$PREFIX/var/lib/dsb-adb" ]; then
    fail '已有/未完成的加固检查失败；保留现场，请先 check/rollback，不能降级后继续'
  fi
  [ "$rc" -eq 0 ] || log '加固未成功；查看上方回滚结果和 doctor，安装继续'
  [ ! -f "$PREFIX/var/lib/dsb-adb/maintenance" ] || fail 'ADB 仍在维护态，需先 rollback，不能报告安装完成'
  if [ ! -d "$PREFIX/var/lib/dsb-adb" ]; then
    python3 "$SOURCE/hardening/adb-private/deploy.py" ordinary || fail '普通 ADB 恢复检查失败，禁止继续'
  fi
  unset ADB_SERVER_SOCKET
  install_apk
  guide_permissions
  STAGE=启动
  bash "$HOME/dsb/boot.sh" 8>&- || \
    log '服务未全部启动；doctor 将报告实际状态，任务桥归属不明时不能自动清理账本'
  log '等待网页首次装载完成（最多 150 秒，不调用模型）'
  deadline=$((SECONDS + 150))
  while ! web_ready && [ "$SECONDS" -lt "$deadline" ]; do sleep 2; done
  if device_adb shell cmd appops get com.dsb.assistant SYSTEM_ALERT_WINDOW | grep -q 'SYSTEM_ALERT_WINDOW: allow'; then
    device_adb shell am start -n com.dsb.assistant/.MainActivity >/dev/null 2>&1 || true
  else
    log '悬浮窗尚未授权，暂不打开 App，避免未授权服务启动失败'
  fi
  doctor
}

main() {
  check_env
  # 只有父 shell 持锁。任何安装子进程（含跳过加固时的 ADB daemon）都不能继承锁。
  (exec 8>&-; main_locked "$@")
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then main "$@"; fi
