#!/bin/bash
# 在 Ubuntu 中执行；只装环境，不读取 API Key、不向模型发请求。
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
export PATH=/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin
DSH_VERSION="${1:?}"
# proot 会清理环境，显式传入依赖安装专用私有传输，包脚本也不能回退 TCP。
export ADB_SERVER_SOCKET="${2:?}"
unset ANDROID_ADB_SERVER_ADDRESS ANDROID_ADB_SERVER_PORT
NODE_VERSION=v22.20.0
DEPS=/root/.dsb-deps
fingerprint=$(sha256sum "$0" "$DEPS/requirements.txt" "$DEPS/dsh/package.json" "$DEPS/dsh/npm-shrinkwrap.json" | sha256sum | cut -d ' ' -f 1)
env_marker="/root/.cache/dsb-installer/container-$DSH_VERSION-$fingerprint"
if [ -f "$env_marker" ] && [ "$(node --version 2>/dev/null || true)" = "$NODE_VERSION" ] && \
    [ "$(dsh --version 2>/dev/null || true)" = "$DSH_VERSION" ] && \
    [ "$(dpkg-query -W -f='${db:Status-Status}\n' ca-certificates curl xz-utils python3 python3-venv python3-pip adb apksigner libglib2.0-0t64 libgl1 2>/dev/null | sort -u)" = installed ] && \
    python3 -c 'import PIL, cv2, rapidocr_onnxruntime, onnxruntime' 2>/dev/null; then
  echo 'DSB_CONTAINER_DEPS_ALREADY_VERIFIED'
  exit 0
fi
available=$(df -Pk /root | awk 'NR==2 {print $4}')
[ "$available" -ge 1572864 ] || { echo 'DSB_DEPENDENCIES_NEED_SPACE：容器依赖安装/更新前至少需 1.5 GiB 可用；未开始包操作。请保留日志，清理可再下载缓存或扩大测试磁盘后重跑。' >&2; exit 1; }
apt-get -o Acquire::Retries=3 update
apt-get -o Acquire::Retries=3 install -y --no-upgrade --no-install-recommends ca-certificates curl xz-utils python3 python3-venv python3-pip adb apksigner libglib2.0-0t64 libgl1
case "$(uname -m)" in
  aarch64) arch=arm64; node_sha=06907b9c088ce62305bc1530e5c1ae1510245114645768f7750c349c5b6fe667 ;;
  x86_64) arch=x64; node_sha=00bbd05e306ea68b6e13e17360d0e2f680b493ef95f2fea1c4296ff7437530bc ;;
  *) exit 1 ;;
esac
if [ "$(node --version 2>/dev/null || true)" != "$NODE_VERSION" ]; then
  tmp=$(mktemp -d)
  trap 'rm -rf "$tmp"' EXIT
  file="node-$NODE_VERSION-linux-$arch.tar.xz"
  curl -fL --retry 3 --retry-all-errors --connect-timeout 15 --speed-time 30 --speed-limit 1024 "https://nodejs.org/dist/$NODE_VERSION/$file" -o "$tmp/$file"
  printf '%s  %s\n' "$node_sha" "$tmp/$file" | sha256sum -c -
  tar -xJf "$tmp/$file" -C /usr/local --strip-components=1
fi
lock_sha=$(sha256sum "$DEPS/dsh/npm-shrinkwrap.json" | cut -d ' ' -f 1)
marker="/root/.cache/dsb-installer/dsh-$DSH_VERSION-$lock_sha.installed"
if [ ! -f "$marker" ] || [ "$(readlink /usr/local/bin/dsh)" != /opt/dsb-dsh/node_modules/.bin/dsh ] || [ "$(dsh --version 2>/dev/null || true)" != "$DSH_VERSION" ]; then
  python3 -c 'import json,sys; assert json.load(open(sys.argv[1]))["dependencies"]["@deepseek-ai/dsh"] == sys.argv[2]' "$DEPS/dsh/package.json" "$DSH_VERSION"
  mkdir -p /opt/dsb-dsh
  cp "$DEPS/dsh/package.json" "$DEPS/dsh/npm-shrinkwrap.json" /opt/dsb-dsh/
  installed=0
  for attempt in 1 2 3; do
    if npm ci --prefix /opt/dsb-dsh --ignore-scripts --no-audit --no-fund --fetch-retries=4 --fetch-retry-mintimeout=2000 --fetch-retry-maxtimeout=10000 --fetch-timeout=120000; then
      installed=1
      break
    fi
    sleep 3
  done
  [ "$installed" = 1 ]
  ln -sfn /opt/dsb-dsh/node_modules/.bin/dsh /usr/local/bin/dsh
  dsh --version
  mkdir -p "$(dirname "$marker")"
  touch "$marker"
fi
python3 -m venv /root/.dsb-venv
installed=0
# The iteration variable only bounds retries; keep installation retry behavior unchanged.
# shellcheck disable=SC2034
for attempt in 1 2 3; do
  # 不复用可能被断流污染的 HTTP 缓存；hash 不匹配仍然严格失败。
  if /root/.dsb-venv/bin/pip install --no-cache-dir --retries 5 --require-hashes --only-binary=:all: -r "$DEPS/requirements.txt"; then
    installed=1
    break
  fi
done
[ "$installed" = 1 ]
# 现有运行入口调用 /usr/bin/python3；用 .pth 接入 venv，不改系统 Python 包。
site=$(/root/.dsb-venv/bin/python -c 'import site; print(site.getsitepackages()[0])')
dist=/usr/local/lib/python$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')/dist-packages
mkdir -p "$dist"
printf '%s\n' "$site" > "$dist/dsb-venv.pth"
python3 -c 'import PIL, rapidocr_onnxruntime, onnxruntime'
node --version
dsh --version
mkdir -p "$(dirname "$env_marker")"
touch "$env_marker"
