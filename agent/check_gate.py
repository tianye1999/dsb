#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Jev 危险动作闸门 —— 接在 DSH 的 PreToolUse 拦截点上。

设计来源：生态调研里的 y0usaf/pi-jev（同款思路），但按本机实测做了三处关键改动：
  1. **用 classify 而不是 check**：实测同一条 `pm uninstall`，
     问「这算危险吗」= 0.75（会被 0.9 阈值漏掉），问「这是什么类型的操作」= 1.00。
  2. **确定性白名单先行**：明显只读的命令（dumpsys/screencap/input tap 等）直接放行，
     不问 Jev，省钱也省网络抖动风险。
  3. **fail-open**：Jev 不可用/超时 → 放行，绝不让判定层卡死手机代理。

模式（环境变量 JEV_GATE_MODE）：
  shadow （默认）— 命中只写日志+stderr，不拦
  enforce        — 命中返回 deny/ask 真拦住

用法（由 DSH 的 hooks.json 以 command hook 形式调用）：
  PreToolUse payload 从 stdin 进来（JSON，含 tool_name / tool_input）：
    {"tool_name":"bash","tool_input":{"command":"adb shell pm uninstall xxx"}}
  也支持直接命令行传：check_gate.py "adb shell pm uninstall xxx"

输出（Claude Code hook 协议）：
  {"hookSpecificOutput":{"hookPermissionDecision":"deny|ask|allow",
                         "permissionDecisionReason":"..."}}
"""
import json, os, re, sys, datetime
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import jev_client
try:
    import task_context
except ImportError:
    task_context = None

MODE = os.environ.get("JEV_GATE_MODE", "shadow").strip().lower()
LOG = os.environ.get("JEV_GATE_LOG", "/root/.dsh-phone/jev-gate.log")
AUDIT = os.environ.get("DSB_AUDIT_FILE", os.path.join(os.path.dirname(__file__), "safe_tap_audit.jsonl"))

# ---- 明显只读 / 低风险：直接放行，不问模型 ----
READ_ONLY_PATTERNS = [
    r"\bdumpsys\b", r"\bscreencap\b", r"\bcat\b", r"\bls\b", r"\bpm list\b",
    r"\binput keyevent\b", r"\binput tap\b", r"\binput swipe\b", r"\binput text\b",
    r"\bam start\b", r"\bmonkey\b", r"\bwm size\b", r"\bgetprop\b", r"\bping\b",
    r"\bcurl\b", r"\bnode\b.*--selftest", r"jev_client\.py",
]

# ---- 命中这些词一律送 Jev 判定（不依赖模型自己想得起来）----
SUSPECT_HINTS = [
    "uninstall", "pm clear", "pm disable", "pm hide", "pm suspend", "force-stop",
    "rm -rf", "rm -r", "mkfs", "dd if=", "> /dev/", "chmod 777", "kill -9",
    "reboot", "shutdown", "factory", "wipe", "format", "su ", "sudo ",
    "settings put", "svc data", "svc wifi", "cmd connectivity", "am force-stop",
    "input keyevent 26", "pm revoke", "appops set", "git push --force", "npm publish",
    "curl.*-d.*token", "curl.*api_key", "curl.*apikey",
    # 敏感路径/隐私：单条 cat 也会读到凭据（实测漏过——这些命令本身没有"危险动词"，
    # 但输出会暴露密钥/验证码/聊天记录，所以必须送 Jev 的 leak 维度判一次）
    "credentials", "typesafe/key", "\\.env", "api[_-]?key", "token", "secret",
    "transcript[.]jsonl", r"[.]ssh/", "id_rsa", "passwd", "shadow",
    "noredact", "dumpsys notification", "验证码", "sms", "mms",
]

CATEGORY_OPTIONS = {
    "read_only": "只读取状态或信息，不改变设备、文件或数据（dumpsys、cat、ls、截图、查询）",
    "ui_interaction": "只是点击、滑动、输入文字、启动 App 等可逆的界面操作",
    "app_launch": "只是启动某个应用",
    "destructive": "会删除、卸载、清空、强制停止、永久修改应用或用户数据（pm uninstall、pm clear、rm -rf、force-stop）",
    "system_change": "会修改系统设置、网络开关、权限、或重启设备",
    "data_exfiltration": "会把本地数据、密钥、token、截图等内容发送到机外（curl 上传、POST 到外部地址）",
}

# 第二个问题：隐私泄露维度（辅助信号）。
# 为什么需要：`cat credentials.env` 在分类学上**确实是 read_only**，只靠 kind 会放行（实测置信度 1.0）。
# 但它的输出会把凭据暴露进会话记录，违反用户红线第 5 条。
# 实测 8 条（2026-09-19）：凭据 0.92 拦✓ / API key 0.90 拦✓ / 通知验证码 0.83 拦✓
#   普通文档 0.31 放✓ / 列目录 0.38 放✓ / 读电量 0.06 放✓ / 读源码 0.37 放✓
#   **漏判：读 transcript.jsonl（对话记录）= 0.46** ← 与"凭据"之间没有干净阈值
# 对应生态经验「有些不确定性调不掉，只能上报」→ leak 只作辅助，别指望它全覆盖。
LEAK_OPTIONS = {
    "yes": "它读取或打印密码、API key、token、验证码、聊天记录、通知内容等敏感信息（无论是否发送到机外）",
    "no": "它输出的是普通状态、文件名、日志、屏幕截图等非凭据内容",
}
DANGER_KINDS = ("destructive", "system_change", "data_exfiltration")
LEAK_THRESHOLD = 0.6


def log(entry):
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def report_event(decision, category=None, would_decision=None, warning=None):
    """Append only fixed, non-command fields; failure cannot change hook policy."""
    try:
        allowed_categories = {"privacy", "read_only", "ui_interaction", "app_launch",
                              "destructive", "system_change", "data_exfiltration"}
        row = {"kind": "command_gate", "ts": time.time(),
               "mode": "enforce" if MODE == "enforce" else "shadow",
               "decision": decision,
               "category": category if isinstance(category, str) and category in allowed_categories else None,
               **(task_context.audit_fields() if task_context is not None else {})}
        if would_decision in ("ask", "deny"):
            row["would_decision"] = would_decision
        if warning == "typesafe_unavailable":
            row["warning"] = warning
        fd = os.open(AUDIT, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, (json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8"))
        finally:
            os.close(fd)
    except Exception:
        pass


def emit(decision, reason):
    """按 Claude Code hook 协议输出。decision ∈ allow/deny/ask。"""
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
            "permissionDecisionReason": reason,
        }
    }, ensure_ascii=False))
    sys.exit(0)


def extract_command(payload):
    if isinstance(payload, dict):
        ti = payload.get("tool_input")
        if isinstance(ti, dict):
            for k in ("command", "cmd", "script", "code"):
                v = ti.get(k)
                if isinstance(v, str) and v.strip():
                    return v
        if isinstance(ti, str):
            return ti
    return ""


def main():
    raw = sys.argv[1] if len(sys.argv) > 1 else sys.stdin.read()
    payload = None
    try:
        payload = json.loads(raw)
    except Exception:
        payload = {"tool_name": "bash", "tool_input": {"command": raw}}

    tool = (payload or {}).get("tool_name") or "bash"
    cmd = extract_command(payload) or raw
    if not cmd.strip():
        emit("allow", "空命令")

    # 只对 shell 类工具判定；其它工具（read/write 等）交给调用方自己的策略
    if tool not in ("bash", "shell", "run_code", "process", "exec"):
        emit("allow", "工具 %s 不在闸门范围内" % tool)

    # ---- 顺序很重要（实测踩过坑）----
    # 先算可疑特征。**必须在白名单之前**：白名单的粒度是"命令动词"，
    # 但风险取决于"动词 + 目标"——`cat` 是只读动词，可 `cat ~/.config/typesafe/key`
    # 会把凭据打印进会话记录。早期版本先跑白名单，导致读 key 的命令被
    # `\bcat\b` 直接放行、Jev 根本没被问过（实测发现）。
    hits = [h for h in SUSPECT_HINTS if re.search(h, cmd, re.I)]

    # 只读白名单：仅当**没有**可疑特征时才直接放行
    if not hits:
        for p in READ_ONLY_PATTERNS:
            if re.search(p, cmd):
                log({"ts": datetime.datetime.now().isoformat(timespec="seconds"),
                     "mode": MODE, "decision": "allow", "by": "whitelist", "cmd": cmd[:300]})
                report_event("allow")
                emit("allow", "只读/低风险命令，白名单放行")
        log({"ts": datetime.datetime.now().isoformat(timespec="seconds"),
             "mode": MODE, "decision": "allow", "by": "no-hint", "cmd": cmd[:300]})
        report_event("allow")
        emit("allow", "未命中可疑特征")

    # ---- 送 Jev 判定（一次请求问两个维度，避免两次往返）----
    # kind  : 客观操作分类（主判据，实测 12/12 + 对抗 6/6）
    # leak  : 隐私泄露概率（辅助，补 kind 覆盖不到的"只读但读凭据"）
    state = ("即将在手机上执行的命令：\n%s\n\n所在工具：%s\n命中可疑特征：%s"
             % (cmd[:1500], tool, "、".join(hits)))
    packed = jev_client.ask(state, {
        "kind": {"type": "choice", "instructions": "这条命令属于哪一类操作？",
                 "criteria": CATEGORY_OPTIONS},
        "leak": {"type": "noul", "instructions": "这条命令的输出会把凭据、密钥、验证码或用户隐私内容暴露出来吗？",
                 "criteria": LEAK_OPTIONS},
    })

    entry = {"ts": datetime.datetime.now().isoformat(timespec="seconds"), "mode": MODE,
             "cmd": cmd[:300], "hits": hits}
    if packed is None:
        entry.update({"decision": "allow", "by": "jev-unavailable(fail-open)"})
        log(entry)
        report_event("allow", warning="typesafe_unavailable")
        emit("allow", "Jev 不可用，按 fail-open 放行（请自行确认命令安全性）")

    ans = packed.get("answers") or {}
    kind_ans = ans.get("kind") or {}
    cat, conf = kind_ans.get("choice"), kind_ans.get("confidence")
    leak = (ans.get("leak") or {}).get("noul")
    entry.update({"category": cat, "confidence": conf, "leak": leak,
                  "probs": kind_ans.get("probabilities"), "model": packed.get("model")})

    # 隐私泄露：单独成一条拦截理由（kind 会是 read_only）
    leak_hit = isinstance(leak, (int, float)) and leak >= LEAK_THRESHOLD
    danger_kind = cat in DANGER_KINDS

    if not danger_kind and not leak_hit:
        entry.update({"decision": "allow", "by": "jev"})
        log(entry)
        report_event("allow", cat)
        emit("allow", "Jev 判定为 %s（置信度 %s，泄露概率 %s）" % (cat, conf, leak))

    if leak_hit and not danger_kind:
        reason = ("Jev 判定这条命令虽然只读，但输出可能暴露凭据/隐私（泄露概率 %s）。"
                  "命中特征：%s" % (leak, "、".join(hits)))
        # 隐私类用 ask（要用户确认），不用 deny —— 阈值边界比"破坏性"模糊
        entry.update({"decision": "ask", "reason": reason})
        log(entry)
        if MODE == "enforce":
            report_event("ask", "privacy")
            emit("ask", reason)
        report_event("allow", "privacy", would_decision="ask")
        print("[jev-gate][shadow] 本应 ask：%s" % reason, file=sys.stderr)
        emit("allow", "shadow 模式仅记录（本应 ask：%s）" % reason)

    # 危险类别
    if cat == "data_exfiltration":
        reason = "Jev 判定这条命令可能把本地数据/密钥发到机外（置信度 %s）。命中特征：%s" % (conf, "、".join(hits))
    elif cat == "destructive":
        reason = "Jev 判定这条命令具有破坏性/不可逆（置信度 %s）。命中特征：%s" % (conf, "、".join(hits))
    else:
        reason = "Jev 判定这条命令会修改系统设置/权限（置信度 %s）。命中特征：%s" % (conf, "、".join(hits))

    # 高置信度 → deny；中等 → ask；低 → 放行但记录
    if conf is not None and conf >= 0.85:
        decision = "deny"
    elif conf is not None and conf >= 0.6:
        decision = "ask"
    else:
        decision = "allow"
        reason = "Jev 判定为 %s 但置信度仅 %s，不足以拦截，放行并记录" % (cat, conf)

    entry.update({"decision": decision, "reason": reason})
    log(entry)
    if MODE == "enforce" and decision in ("deny", "ask"):
        report_event(decision, cat)
        emit(decision, reason)
    report_event("allow", cat, would_decision=decision if decision in ("deny", "ask") else None)
    # shadow 模式：记录 + stderr 提示，但放行
    print("[jev-gate][shadow] 本应 %s：%s" % (decision, reason), file=sys.stderr)
    emit("allow", "shadow 模式仅记录（本应 %s：%s）" % (decision, reason))


if __name__ == "__main__":
    main()
