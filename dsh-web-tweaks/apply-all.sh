#!/usr/bin/env bash
# ============================================================================
# DSH 网页微调：一键恢复脚本（DSH 升级后跑这个）
#
# 背景：10 项界面微调是「注入到 index.html」实现的，而 DSH 升级会覆盖
#       dsh-web-frontend/dist/index.html → 注入全丢。跑本脚本即可全部恢复。
#
# 用法：
#   bash /root/dsh-web-tweaks/apply-all.sh            # 备份 + 重打 + 校验
#   bash /root/dsh-web-tweaks/apply-all.sh --verify   # 只校验，不改文件
#   bash /root/dsh-web-tweaks/apply-all.sh --restore  # 回滚到最近一次自动备份
#
# 为什么要有校验：注入代码必须落在 <script> 标签【内部】且语法正确，
# 否则浏览器静默不执行（表现为「功能没反应」/「某个图标不见了」）。
# 2026-09-20 就因为一个 parse error 让整个侧栏脚本失效。所以每块都做 node --check。
# ============================================================================
set -uo pipefail

TWEAKS=$(cd -- "$(dirname -- "$0")" && pwd)
# 20261020 升级 0.2.1：前端改由运行版本自带的 dsh-web-frontend 提供（不再嵌套在 dsh 包内），
# DIST 自动跟随 /usr/local/bin/dsh 软链所指版本；可用 DSH_TWEAKS_DIST 环境变量覆盖。
# 旧路径（0.1.x 经 profiles 符号链）已弃用：profiles/node_modules/@deepseek-ai/dsh 现也指向 /opt。
TARGET=$(python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); from tweak_paths import targets; print(targets()[0])' "$TWEAKS")
DIST="$(dirname "$TARGET")"
export DSH_TWEAKS_DIST="$DIST"
TARGET="$DIST/index.html"
BACKUP_DIR="$TWEAKS/backups"          # ⚠️ 刻意放在 dist 外面：升级会覆盖 dist，放里面备份会一起没
MODE="${1:-apply}"

c_ok()   { printf '\033[32m%s\033[0m\n' "$*"; }
c_err()  { printf '\033[31m%s\033[0m\n' "$*"; }
c_info() { printf '\033[36m%s\033[0m\n' "$*"; }

[ -f "$TARGET" ] || { c_err "✗ 找不到 $TARGET"; exit 1; }

# ---------------------------------------------------------------- 校验

# 每个注入项的「存在标记」→ 用 grep 确认真的在文件里
MARKS=(
  'sidebar:dsh-rail-toggle'
  'ui2:Sixlwa_bubble'
  'composer:dsh-composer-dot'
  # 2026-09-22 口径改了：原先是强制深色（dsh-dark-enforce），
  # 用户反馈黑底白字刺眼 → 现在强制**浅色**（dsh-light-enforce）
  'theme:dsh-light-enforce'
  'gesture:dsh-composer-gesture'
  'autohide:dsh-composer-autohide'
  'hidetabs:dsh-hide-tabs'
  'fonts:dsh-shrink-content'
  'tight:dsh-tight-blocks'
  # 2026-09-24 用户要求：对话页底色改用暖白 #F7F6F3
  'chatbg:dsh-chat-bg'
  'privacy:dsh-privacy-ui-script'
)
# 第 9 项（回到底部 hook）的标记
MARK_TOBOTTOM='回到底部'
# 转义形式（脚本里写的是 \u56de\u5230\u5e95\u90e8）
MARK_TOBOTTOM_ESC='\u56de\u5230\u5e95\u90e8'

verify() {
  local fail=0
  c_info "── 1) 注入项检查 ──"
  for pair in "${MARKS[@]}"; do
    name=${pair%%:*}; mark=${pair#*:}
    if grep -q "$mark" "$TARGET"; then
      c_ok "  ✓ $name"
    else
      c_err "  ✗ $name （缺 ${mark}）"; fail=1
    fi
  done
  if grep -q "$MARK_TOBOTTOM_ESC" "$TARGET" || grep -q "$MARK_TOBOTTOM" "$TARGET"; then
    c_ok "  ✓ 回到底部 hook（第 9 项）"
  else
    c_err "  ✗ 回到底部 hook（第 9 项）"; fail=1
  fi

  c_info "── 2) 脚本块语法检查（关键：一个 parse error 会让整块静默失效）──"
  python3 - "$TARGET" <<'PY'
import re, subprocess, sys
t = sys.argv[1]
s = open(t, encoding='utf-8').read()
blocks = re.findall(r'<script id="([^"]+)">(.*?)</script>', s, re.S)
if not blocks:
    print('  ✗ 一个注入脚本块都没有'); sys.exit(1)
bad = 0
for sid, body in blocks:
    p = f'/tmp/_dshchk_{sid}.js'
    open(p, 'w').write(body)
    r = subprocess.run(['node', '--check', p], capture_output=True, text=True)
    if r.returncode == 0:
        print(f'  ✓ {sid} ({len(body)} 字节)')
    else:
        bad += 1
        last = (r.stderr or '').strip().splitlines()[-1] if r.stderr else '?'
        print(f'  ✗ {sid}: {last}')
print(f'  脚本块共 {len(blocks)} 个，语法错误 {bad} 个')
sys.exit(1 if bad else 0)
PY
  [ $? -ne 0 ] && fail=1

  c_info "── 3) 标签外残留检查（插到 </script> 外的 JS 不会执行）──"
  python3 - "$TARGET" <<'PY'
import re, sys
s = open(sys.argv[1], encoding='utf-8').read()
# 剥掉 script 和 style 两类块：CSS 里也有 dsh-* 选择器，不剥会误报
body = re.sub(r'<script[^>]*>.*?</script>', '', s, flags=re.S)
body = re.sub(r'<style[^>]*>.*?</style>', '', body, flags=re.S)
# 只查「JS 专有」标识：这些不该出现在任何块外面
leaks = [k for k in ('hookToBottom', 'function boot()', 'scrollConvToBottom') if k in body]
if leaks:
    print(f'  ✗ 脚本块外发现可执行 JS 残留: {leaks}'); sys.exit(1)
print('  ✓ 无标签外 JS 残留')
PY
  [ $? -ne 0 ] && fail=1

  c_info "── 4) 打包资源存在性检查（引用了不存在的 js/css = 页面空白/黑屏）──"
  # 2026-09-25 踩到：DSH 升级后 hash 变了，底稿里的旧文件名被写回 → 404 → App 黑屏
  local missing=0
  for a in $(grep -oE 'assets/[A-Za-z0-9_.-]+\.(js|css)' "$TARGET" | sort -u); do
    if [ -f "$DIST/$a" ]; then c_ok "  ✓ $a"; else c_err "  ✗ $a 不存在"; missing=1; fi
  done
  [ $missing -ne 0 ] && fail=1

  if [ $fail -eq 0 ]; then
    c_ok "══ 全部校验通过 ══"
  else
    c_err "══ 有项目未通过 ══"
  fi
  return $fail
}

# ---------------------------------------------------------------- 回滚

if [ "$MODE" = "--restore" ]; then
  latest=$(ls -t "$BACKUP_DIR"/index.html.* 2>/dev/null | head -1)
  [ -n "$latest" ] || { c_err "✗ 备份目录里没有可回滚的文件"; exit 1; }
  cp "$latest" "$TARGET"
  c_ok "✓ 已回滚到：$(basename "$latest")"
  c_info "  刷新页面生效"
  exit 0
fi

# ---------------------------------------------------------------- 只校验

if [ "$MODE" = "--verify" ]; then
  verify; exit $?
fi

# ---------------------------------------------------------------- 备份 + 重打

mkdir -p "$BACKUP_DIR"
STAMP=$(date +%Y%m%d-%H%M%S)-$$
BEFORE="$BACKUP_DIR/index.html.before-$STAMP"
cp "$TARGET" "$BEFORE"
c_ok "✓ 已备份当前文件 → $BEFORE"

# 记录当前状态，便于对比
BEFORE_SIZE=$(wc -c < "$TARGET")
BEFORE_INJ=$(grep -c 'id="dsh-' "$TARGET" || true)
c_info "  备份前：$BEFORE_SIZE 字节，注入块 $BEFORE_INJ 个"

c_info "── 依次重打补丁脚本 ──"
cd "$TWEAKS" || exit 1
# Each patch owns its blocks; no historical/private index shell is restored.
# Content rules follow fonts; narrow-screen overrides come last.
ORDER=(patch_sidebar.py patch_ui2.py patch_composer.py patch_autohide.py patch_hidetabs.py repair_index.py patch_fonts.py add_missing_rules.py patch_gesture.py patch_chatbg.py patch_privacy_ui.py)
patched=0; skipped=0; failed=0
for p in "${ORDER[@]}"; do
  if [ ! -f "$p" ]; then c_err "  ✗ $p 不存在"; failed=$((failed+1)); continue; fi
  out=$(python3 "$p" 2>&1)
  status=$?
  if [ "$status" -ne 0 ]; then
    c_err "  ✗ $p failed: $out"; failed=$((failed+1)); continue
  fi
  # Status is authoritative; the final verifier also requires every marker.
  case "$out" in
    *already\ patched*|*skip*|*替换失败*) c_info "  ○ $p 已打过，跳过"; skipped=$((skipped+1)) ;;
    *patched*|*file\ size*|*已注入*|*已应用*) c_ok  "  ✓ $p"; patched=$((patched+1)) ;;
    *) c_ok "  ✓ $p (exit 0; markers checked below)"; patched=$((patched+1)) ;;
  esac
done
c_info "  重打 $patched 个 / 跳过 $skipped 个 / 失败 $failed 个"

AFTER_SIZE=$(wc -c < "$TARGET")
c_info "  处理后：$AFTER_SIZE 字节"

echo
verify
RESULT=$?
[ "$failed" -eq 0 ] || RESULT=1

if [ $RESULT -eq 0 ]; then
  c_ok "✓ 恢复完成 —— 刷新浏览器页面即可生效"
else
  c_err "✗ 有项目未通过，请检查上面的 ✗ 项"
  c_info "  回滚命令：bash $0 --restore"
fi
exit $RESULT
