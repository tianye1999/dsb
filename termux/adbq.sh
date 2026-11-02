#!/data/data/com.termux/files/usr/bin/bash
# adbq —— "带红绿灯的 adb"。**操作手机请一律用它，别用裸 adb。**
#
# 为什么：2026-09-22 用户两次指出"切微信时球没变橙"。
#   根因是我把橙信号只接进了 safe_tap.py 等个别脚本，
#   而我平时干活经常直接用 adb —— 那些路径全都不脉冲橙。
#
# 行为：先脉冲一次「橙」（球变橙，警示用户勿碰），再执行你给的 adb 命令。
#   App 侧看门狗 20 秒无心跳自动回蓝，所以不会永久橙色。
#   一串操作连续用 adbq 即可持续保持橙。
#
# ⚠️ 2026-09-22 踩过的坑：`A="$P/adb -s 127.0.0.1:5555"` 然后 `"$A" shell ...`
#    会被当成**一个**命令名（含空格）→ "No such file or directory"。
#    必须用数组：ADB=("$P/adb" -s 127.0.0.1:5555) 然后 "${ADB[@]}" shell ...
#
# 用法：
#   bash ~/dsb/adbq.sh shell "dumpsys power | grep mWakefulness"   # 只读查询
#   ❌ 不要把 input 当命令执行（含 -d、绝对路径、任意子命令）；屏幕动作走 safe_tap.py。
#   bash ~/dsb/adbq.sh shell "am start -n com.tencent.mm/.ui.LauncherUI"
#   bash ~/dsb/adbq.sh exec-out screencap -p > /tmp/x.png
#   bash ~/dsb/adbq.sh 蓝            # 只回蓝
#   bash ~/dsb/adbq.sh 状态          # 看球状态
set -u

# Honest-mistake guard only. Keep in sync with agent/raw_input_guard.py and
# agent/tests/raw_input_cases.json. Pure Bash: no subprocess on the hot path.
# Quotes preserve literal words; only substitutions and shell programs recurse.
adbq_has_raw_input() {
  local text="$*"
  # Linear fast path. Split spellings such as in\put intentionally pass:
  # adversarial obfuscation is outside this honest-mistake guard's scope.
  [[ "$text" == *input* ]] || return 1
  _raw_input_scan "$text"
}

# These helpers use Bash's dynamic local scope. Each recursive scan owns its
# word buffer/token arrays; RAW_INPUT_END returns only the closing position.
_raw_input_flush() {
  if (( active )); then
    ri_words+=("$word")
    ri_kinds+=(word)
  fi
  word='' active=0
}

_raw_input_scan() {
  # Syntax is ASCII; UTF-8 bytes cannot collide with it. Do not change callers' locale.
  local LC_ALL=C
  local text=$1 i=${2:-0} end=${3:-} char following quote='' word=''
  local active=0 depth=0
  local -a ri_words=() ri_kinds=()
  while (( i < ${#text} )); do
    char=${text:i:1}
    if [[ -z "$quote" && -n "$end" && "$char" == "$end" ]] && (( depth == 0 )); then
      i=$((i + 1))
      break
    fi
    if [[ "$quote" == "'" ]]; then
      if [[ "$char" == "'" ]]; then quote=''; else word+=$char; fi
      i=$((i + 1))
      continue
    fi
    if [[ "$char" == "\\" ]] && (( i + 1 < ${#text} )); then
      following=${text:i+1:1}
      if [[ -z "$quote" || "$following" == [\$\`\"\\] || "$following" == $'\n' ]]; then
        if [[ "$following" != $'\n' ]]; then word+=$following; active=1; fi
        i=$((i + 2))
        continue
      fi
    fi
    if [[ "${text:i:2}" == "\$(" || "$char" == '`' ]]; then
      if [[ "$char" == '$' ]]; then
        _raw_input_scan "$text" "$((i + 2))" ')' && return 0
      else
        _raw_input_scan "$text" "$((i + 1))" '`' && return 0
      fi
      i=$RAW_INPUT_END
      word+="\${sub}" active=1
      continue
    fi
    if [[ "$quote" == '"' ]]; then
      if [[ "$char" == '"' ]]; then quote=''; else word+=$char; fi
      i=$((i + 1))
      continue
    fi
    case "$char" in
      "'"|'"') quote=$char; active=1 ;;
      '#')
        if (( active == 0 )); then
          while (( i < ${#text} )) && [[ "${text:i:1}" != $'\n' ]]; do i=$((i + 1)); done
          continue
        fi
        word+=$char ;;
      ' '|$'\t'|$'\r') _raw_input_flush ;;
      ';'|'&'|'|'|'('|')'|$'\n')
        _raw_input_flush
        if [[ "$char" == '(' ]]; then depth=$((depth + 1))
        elif [[ "$char" == ')' ]] && (( depth > 0 )); then depth=$((depth - 1))
        fi
        ri_words+=("$char"); ri_kinds+=(boundary) ;;
      '<'|'>')
        if [[ "$word" =~ ^[0-9]+$ ]]; then word=''; active=0; fi
        _raw_input_flush
        while (( i + 1 < ${#text} )); do
          case "${text:i+1:1}" in
            '<'|'>'|'&'|'|') i=$((i + 1)) ;;
            *) break ;;
          esac
        done
        ri_words+=("$char"); ri_kinds+=(redirect) ;;
      *) word+=$char; active=1 ;;
    esac
    i=$((i + 1))
  done
  _raw_input_flush
  # Nested program scans may overwrite RAW_INPUT_END, so publish after them.
  if _raw_input_words; then RAW_INPUT_END=$i; return 0; fi
  RAW_INPUT_END=$i
  return 1
}

_raw_input_words() {
  local n word kind base state='command' redirect=0
  for ((n=0; n<${#ri_words[@]}; n++)); do
    word=${ri_words[n]} kind=${ri_kinds[n]}
    base=${word##*/}
    if [[ "$kind" == word && "$state" == su_command ]] && (( redirect == 0 )); then
      if [[ "$word" =~ ^-[a-zA-Z]*c[a-zA-Z]*$ ]]; then state=c_arg; continue; fi
      [[ "$word" == -* ]] && continue
      state='command'
    fi
    if [[ "$kind" == boundary ]]; then
      state='command' redirect=0
    elif [[ "$kind" == redirect ]]; then
      redirect=1
    elif (( redirect )); then
      redirect=0
    elif [[ "$state" == adb ]]; then
      case "$word" in
        shell|exec-out) state=remote ;;
        push|pull) state=args ;;
      esac
    elif [[ "$state" == c_arg ]]; then
      adbq_has_raw_input "$word" && return 0
      state=args
    elif [[ "$state" == shell_c || "$state" == su ]]; then
      if [[ "$word" =~ ^-[a-zA-Z]*c[a-zA-Z]*$ ]]; then state=c_arg
      elif [[ "$word" == -* ]]; then continue
      elif [[ "$base" == adbq.sh ]]; then state=adb
      elif [[ "$state" == su ]]; then state=su_command
      else state=args
      fi
    elif [[ "$state" == timeout ]]; then
      [[ "$word" != -* ]] && state='command'
    elif [[ "$state" == command || "$state" == remote ]]; then
      [[ "$word" =~ ^[a-zA-Z_][a-zA-Z0-9_]*= || "$word" == -* ]] && continue
      if [[ "$state" == remote && "$word" =~ [[:space:]] ]]; then
        adbq_has_raw_input "$word" && return 0
        state=args
        continue
      fi
      case "$base" in
        input) return 0 ;;
        cmd|exec|nohup|env|command|busybox|toybox|time|do|then|else|elif|'{'|'!'|if|while|until) state='command' ;;
        shell|exec-out) state=remote ;;
        sh|bash) state=shell_c ;;
        su|timeout) state=$base ;;
        adb|adbq.sh) state=adb ;;
        *) state=args ;;
      esac
    fi
  done
  return 1
}

adbq_check_raw_input() {
  # Callers supply adb arguments, so selectors must not consume a command slot.
  if adbq_has_raw_input adb "$@"; then
    if [[ "${SAFE_TAP_ALLOW_RAW_INPUT:-0}" == 1 ]]; then
      printf '!!! WARNING: SAFE_TAP_ALLOW_RAW_INPUT=1 bypasses the raw input guard: %s\n' "$*" >&2
    else
      printf '⛔ adbq 拒绝裸 input：%s\n' "$*" >&2
      echo "  → 屏幕动作请用 safe_tap.py（虚拟屏：先 vd_session.py start，再 look/tap --display N）" >&2
      echo "  → 确需诊断绕过：SAFE_TAP_ALLOW_RAW_INPUT=1 bash ~/dsb/adbq.sh ..." >&2
      return 2
    fi
  fi
}

# Explicit test command precedes endpoint discovery; ambient variables cannot
# silently turn normal invocations into selftests.
if [[ "${1:-}" == --raw-input-selftest ]]; then
  mode=${2:-}
  [[ $# -ge 2 ]] || { echo 'selftest requires adb|command|guard' >&2; exit 2; }
  shift 2
  case "$mode" in
    adb) if adbq_has_raw_input adb "$@"; then echo blocked; else echo allowed; fi ;;
    command) if adbq_has_raw_input "$@"; then echo blocked; else echo allowed; fi ;;
    guard) adbq_check_raw_input "$@"; exit $? ;;
    *) echo 'selftest requires adb|command|guard' >&2; exit 2 ;;
  esac
  exit 0
fi

# Resolved relative to this script on-device; lint the endpoint helper separately.
# shellcheck disable=SC1091
source "$(dirname "${BASH_SOURCE[0]}")/adb-target.sh"
ADB=("${DSB_ADB_COMMAND[@]}" "${DSB_ADB_SELECTOR[@]}")

ball() {
  "${ADB[@]}" shell "am broadcast -n com.dsb.assistant/.CmdReceiver -a com.dsb.assistant.CMD --es token dsh-8f3a2c --es cmd '$1'" >/dev/null 2>&1
}

case "${1:-}" in
  # ★ done = 任务收尾的**一步到位**：球变蓝 + 切回逗沙包对话界面。
  #   用户已多次要求"完成手机操作后必须回到逗沙包界面"（2026-09-22 又漏了一次）。
  #   做成一跳命令，是因为分两步我总会忘第二步。
  done|完成)
    ball "蓝"
    "${ADB[@]}" shell "am start -n com.dsb.assistant/.MainActivity" >/dev/null 2>&1
    # ⚠️ 2026-09-24 修：原来固定 sleep 4 秒就判前台 —— 逗沙包**冷启动**（WebView 起来）
    #    经常要 5~10 秒，于是"其实已经切过去了"却被判失败（自动切回守护的日志里 rc=1）。
    #    改成轮询最多 ~15 秒，切过去就立刻返回。
    FG=""
    # The iteration variable only bounds retries; preserve foreground recovery timing.
    # shellcheck disable=SC2034
    for i in 1 2 3 4 5 6 7 8 9 10; do
      sleep 1.5
      FG=$("${ADB[@]}" shell "dumpsys window 2>/dev/null | grep -m1 -oE 'mCurrentFocus=Window\{[^}]*\}'" 2>/dev/null | tr -d '\r')
      case "$FG" in
        *dsb.assistant*) echo "🔵 球→蓝  ✓ 已回到逗沙包界面"; exit 0 ;;
      esac
    done
    echo "🔵 球→蓝  ⚠ 没切回逗沙包，前台: $(echo "$FG" | cut -c1-60)"; exit 1 ;;
  蓝|闲|绿灯) ball "蓝"; echo "🔵 球→蓝（未切界面）"; exit 0 ;;
  橙|忙|红灯) ball "橙"; echo "🟠 球→橙"; exit 0 ;;
  状态) "${ADB[@]}" shell "logcat -d -s DSH:* 2>/dev/null | grep 球状态 | tail -1" 2>/dev/null | tr -d '\r'; exit 0 ;;
  "") echo "用法: $0 <adb 参数...> | done(球蓝+回逗沙包) | 蓝 | 橙 | 状态" >&2; exit 2 ;;
esac

# ★ 2026-10-20 用户规矩（虚拟屏强制）：
#   ① 所有「别的 App 的界面」操作必须在虚拟屏上（vd_session.py start → look/tap --display N）；
#   ② 屏幕动作只能走 safe_tap.py 闸门，不许裸 input。
#   这里按命令词挡第 ② 条：含 -d、路径、空白变体、包装命令的 input 都拒绝。
#   真需要绕过（极少数诊断）：SAFE_TAP_ALLOW_RAW_INPUT=1 bash adbq.sh ...
adbq_check_raw_input "$@" || exit $?

ball "橙"                       # ← 关键：任何 adb 操作前先点亮
exec "${ADB[@]}" "$@"
