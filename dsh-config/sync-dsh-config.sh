#!/data/data/com.termux/files/usr/bin/bash
# dsh 配置的版本管理同步器（2026-09-22）
#
# ═══ 为什么需要 ═══
# ~/.dsh/ 被 .gitignore 整体排除（里面 sessions/attachments/node_modules 都是大件）。
# 但 ~/.dsh/profiles/*/cordis.patch.yml 是我们**手写的关键配置**：
#   · Jev MCP 接入          · 危险动作闸门（hooks-cc）
#   · 结论卡人设（personaPrefix）· recap 弹窗停用
# 以及 ~/.dsh/settings.yaml（默认模型 / reasoningEffort）。
# 这些一旦随 .dsh 被覆盖，就全丢了 —— 2026-09-22 发现这个漏洞。
#
# ═══ 为什么不用 gitignore 的 ! 放行 ═══
# 被排除的目录 git **不会进入**，! 规则对它内部的子项无效
# （和 termux-dsb 那次同一个坑）。所以改用「同步副本」：
# 把要管的配置复制到本目录（它在 git 里），原件位置不动。
#
# 用法：
#   bash /root/dsh-config/sync-dsh-config.sh          # 同步进副本
#   bash /root/dsh-config/sync-dsh-config.sh --diff   # 只看差异
set -u
HERE=$(cd -- "$(dirname -- "$0")" && pwd)
SRC=${DSH_CONFIG_SOURCE:-/root/.dsh}
DST=${DSH_CONFIG_DEST:-/root/dsh-config}
sync_one() {  # $1=源文件  $2=副本名
  [ -f "$1" ] || { echo "  ✗ 缺少 $1"; return; }
  case "$2" in
    cordis.patch.*.yml)
      python3 "$HERE/privacy_defaults.py" "$1" "$DST/$2" || exit 1
      echo "  → 已同步隐私默认值 $2"
      return ;;
  esac
  if [ -f "$DST/$2" ] && cmp -s "$1" "$DST/$2"; then
    echo "  ＝ $2 无变化"
  else
    cp "$1" "$DST/$2"
    echo "  → 已同步 $2"
  fi
}
diff_one() {
  [ -f "$1" ] || return
  if [ -f "$DST/$2" ] && ! cmp -s "$1" "$DST/$2"; then
    echo "  ≠ $2 有差异"
  fi
}
case "${1:-}" in
  --diff)
    echo "差异："; diff_one "$SRC/settings.yaml" settings.yaml
    diff_one /root/.codex/config.toml codex-config.toml
    for p in "$SRC"/profiles/*/cordis.patch.yml; do
      [ -f "$p" ] || continue
      n=$(basename "$(dirname "$p")")
      diff_one "$p" "cordis.patch.$n.yml"
    done
    echo "  （无输出=一致）" ;;
  *)
    echo "同步 dsh 配置到副本："
    sync_one "$SRC/settings.yaml" settings.yaml
    # Codex CLI 配置（2026-09-23：禁用 websocket 的关键修复，必须留档）
    sync_one /root/.codex/config.toml codex-config.toml
    for p in "$SRC"/profiles/*/cordis.patch.yml; do
      [ -f "$p" ] || continue
      n=$(basename "$(dirname "$p")")
      sync_one "$p" "cordis.patch.$n.yml"
    done
    for p in "$SRC"/profiles/*/cordis.yml; do
      [ -f "$p" ] || continue
      n=$(basename "$(dirname "$p")")
      sync_one "$p" "cordis.$n.yml"
    done ;;
esac
