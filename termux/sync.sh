#!/data/data/com.termux/files/usr/bin/bash
# Termux 侧脚本 <-> 本目录副本 的双向同步（按时间戳判断谁新）
set -u
SRC=/data/data/com.termux/files/home/dsb
DST=/root/termux-dsb
# 只同步脚本类文件，跳过日志/备份/状态
PAT='*.sh *.py'
MODE="${1:---both}"

in_one() {   # Termux -> 副本
  local n=0
  for f in $SRC/*.sh $SRC/*.py; do
    [ -f "$f" ] || continue
    b=$(basename "$f")
    case "$b" in *.bak-*|*.log) continue ;; esac
    if [ ! -f "$DST/$b" ] || [ "$f" -nt "$DST/$b" ]; then
      cp "$f" "$DST/$b"; echo "  ← 拉入 $b"; n=$((n+1))
    fi
  done
  [ $n -eq 0 ] && echo "  ← Termux 侧无更新"
}
out_one() {  # 副本 -> Termux
  local n=0
  for f in $DST/*.sh $DST/*.py; do
    [ -f "$f" ] || continue
    b=$(basename "$f")
    [ "$b" = "sync.sh" ] && continue
    if [ ! -f "$SRC/$b" ] || [ "$f" -nt "$SRC/$b" ]; then
      cp "$f" "$SRC/$b"; chmod +x "$SRC/$b" 2>/dev/null
      echo "  → 推出 $b"; n=$((n+1))
    fi
  done
  [ $n -eq 0 ] && echo "  → 副本侧无更新"
}
diff_one() {
  for f in $SRC/*.sh $SRC/*.py; do
    [ -f "$f" ] || continue
    b=$(basename "$f")
    case "$b" in *.bak-*|*.log) continue ;; esac
    if [ -f "$DST/$b" ] && ! cmp -s "$f" "$DST/$b"; then
      echo "  ≠ $b  (Termux $(stat -c%y "$f" | cut -d. -f1) / 副本 $(stat -c%y "$DST/$b" | cut -d. -f1))"
    fi
  done
  echo "  （无输出=两边一致）"
}

case "$MODE" in
  --in)   in_one ;;
  --out)  out_one ;;
  --diff) echo "差异："; diff_one ;;
  *)      echo "同步："; in_one; out_one ;;
esac
