#!/usr/bin/env python3
"""Privacy-minimal, offline execution report from task-linked evidence."""

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re

import task_status


ACTION_KINDS = frozenset(("tap", "swipe", "key", "scroll", "long_press", "drag", "type", "open"))
READ_OPS = frozenset(("look", "wait.app", "wait.stable", "wait.text", "wait.text.ocr"))
APP_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+")
TASK_ID_RE = re.compile(
    r"(?:[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}|t[0-9]+-[0-9a-f]{32})"
)

# Ordered: the specific empty-tree branches must precede their general family.
REASONS = (
    ("R3_CHANGED", "派发前目标所在行与观测时不一致", "目标区域已变化，动作没有下发。"),
    ("VD_REQUIRED", "主屏动作被 VD 策略拒绝", "目标应用应在隔离屏幕操作。"),
    ("G2B_KEY", "空控件树页上，非导航按键", "页面结构不可见，危险按键已拦下，需人工接管。"),
    ("G2B_ACK", "空控件树，无法自动识别危险按钮", "无法确认危险按钮，已暂停等待确认。"),
    ("G2B_OCR_UNAVAILABLE", "空控件树且 OCR 不可用", "文字识别不可用，无法确认安全，已拒绝。"),
    ("G2B_OCR_DANGER", "空控件树 + OCR 扫到危险词", "识别到危险操作，已拒绝。"),
    ("G2B_OCR_BAND", "空控件树 + OCR：", "目标落在危险文字所在区域，已拒绝。"),
    ("G2B_VISION_RISK", "空控件树 + 视觉判断：", "视觉判断目标有风险，需人工接管。"),
    ("G2B_VISION_UNAVAILABLE", "空控件树 + 屏上有危险词", "危险文字存在且视觉判断不可用，已拒绝。"),
    ("G2_DANGER_ZONE", "落在禁区：", "目标位于危险操作区域，已拒绝。"),
    ("G1_NO_SHOT", "没有截图凭据", "缺少当前画面凭据，已拒绝。"),
    ("G1_MISMATCH", "凭据不匹配", "画面凭据不匹配，已拒绝。"),
    ("G1_DISPLAY", "凭据属于 displayId=", "画面凭据不属于当前屏幕，已拒绝。"),
    ("G1_CONSUMED", "凭据已用于动作下发", "旧画面凭据不能复用，已拒绝。"),
    ("G1_STALE", "这张凭据的画面不晚于上次动作", "画面已过时，已拒绝。"),
    ("G1_EXPIRED", "截图凭据已过期", "画面凭据已过期，已拒绝。"),
    ("G1_EXPIRED", "凭据在闸门检查期间过期", "检查期间画面凭据过期，已拒绝。"),
    ("G1_EXPIRED", "凭据在派发前准备", "派发前画面凭据过期，动作没有下发。"),
    ("G1_FOREGROUND", "看完截图后前台变了", "前台页面已变化，已拒绝。"),
    ("G1_FOREGROUND", "派发前前台变化或不可确认", "派发前页面变化或不可确认，动作没有下发。"),
    ("G3_EDGE", "落在左/右边缘禁带", "目标位于系统手势边缘，已拒绝。"),
    ("G3_EDGE", "落在顶部禁带", "目标位于顶部禁带，已拒绝。"),
    ("G3_EDGE", "落在**底部手势导航禁带**", "目标位于底部系统手势区，已拒绝。"),
    ("G3_BOUNDS", "超出屏幕", "目标超出屏幕，已拒绝。"),
    ("G4_BUDGET", "同一前台手势预算用尽", "重复手势预算已用尽，需人工接管。"),
    ("G4_BUDGET", "同一个按键", "重复按键已熔断。"),
    ("G4_BUDGET", "已连点", "重复点击已熔断。"),
    ("G4_BUDGET", "该控件在 look 时已是", "无效果的重复点击已熔断。"),
    ("G4_CONFIG", "重复动作预算配置无效", "动作预算配置无效，已拒绝。"),
    ("G4_CONFIG", "手势预算配置无效", "手势预算配置无效，已拒绝。"),
)
EXPLANATIONS = {code: explanation for code, _, explanation in REASONS}
EXPLANATIONS.update({
    "G1_OTHER": "画面或屏幕绑定无法确认，已拒绝。",
    "G5_AUDIT_FAILED": "留证失败，结果不能确认。",
    "R3_OTHER": "派发前目标复核未通过，动作没有下发。",
    "VD_POLICY": "屏幕隔离策略已拦下操作。",
    "CHECK_GATE_PRIVACY": "命令可能暴露隐私，已暂停等待确认。",
    "CHECK_GATE_EXFILTRATION": "命令可能向外发送本机数据，已拦下。",
    "CHECK_GATE_DESTRUCTIVE": "命令可能删除或不可逆地修改数据，已拦下。",
    "CHECK_GATE_SYSTEM_CHANGE": "命令可能更改系统设置，已拦下。",
    "CHECK_GATE_OTHER": "命令钩子已拦下操作。",
    "HANDOFF": "任务停在需要人工接管的地方。",
    "OTHER_REFUSAL": "安全检查未通过，动作没有下发。",
})


def _finite_number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _time(value):
    if not _finite_number(value) or value < 0:
        return None
    try:
        return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="milliseconds")
    except (OverflowError, OSError, ValueError):
        return None


def _app(row):
    for key in ("pkg", "act_before", "fg"):
        value = row.get(key)
        if isinstance(value, str):
            package = value.split("/", 1)[0]
            if APP_RE.fullmatch(package):
                return package
    return None


def _reason_code(row):
    policy = row.get("vd_policy")
    if policy in ("vd_required", "vd_active_elsewhere"):
        return "VD_REQUIRED" if policy == "vd_required" else "VD_POLICY"
    reason = row.get("reason")
    if not isinstance(reason, str):
        return "OTHER_REFUSAL"
    if reason.startswith("vd_required:"):
        return "VD_REQUIRED"
    if reason == "audit_failed":
        return "G5_AUDIT_FAILED"
    if reason.startswith(("虚拟屏", "App 观测前台", "凭据前台", "本机 input 不支持 -d",
                          "拿不到虚拟屏尺寸")):
        return "G1_OTHER"
    for code, marker, _ in REASONS:
        if marker in reason:
            return code
    return "OTHER_REFUSAL"


def _command_code(row):
    category = row.get("category")
    return {
        "privacy": "CHECK_GATE_PRIVACY",
        "data_exfiltration": "CHECK_GATE_EXFILTRATION",
        "destructive": "CHECK_GATE_DESTRUCTIVE",
        "system_change": "CHECK_GATE_SYSTEM_CHANGE",
    }.get(category, "CHECK_GATE_OTHER")


def _ledger_state(ledger, task_id):
    tasks = ledger.get("tasks") if isinstance(ledger, dict) else None
    state = tasks.get(task_id) if isinstance(tasks, dict) else None
    return state if state in ("running", "reply_received", "cancelled") else None


def build_report(audit_rows, ledger, task_id):
    """Build one report without I/O; never copy raw audit text into output."""
    if not isinstance(task_id, str) or not TASK_ID_RE.fullmatch(task_id):
        raise ValueError("invalid task_id")
    rows = [row for row in audit_rows if isinstance(row, dict) and row.get("task_id") == task_id]
    starts = [r for r in rows if r.get("kind") == "task_start"]
    ends = [r for r in rows if r.get("kind") == "task_end"]
    start = starts[0] if len(starts) == 1 else None
    end = ends[0] if len(ends) == 1 else None
    started_at = _time(start.get("ts")) if start else None
    ended_at = _time(end.get("ts")) if end else None
    duration = None
    if start and end and started_at and ended_at and end["ts"] >= start["ts"]:
        duration = round(end["ts"] - start["ts"], 3)
    elif end and _finite_number(end.get("elapsed_s")) and end["elapsed_s"] >= 0:
        duration = end["elapsed_s"]

    refusals = Counter()
    timeline = []
    dispatched = handoffs = hook_refusals = 0
    hook_seen = False
    for row in rows:
        kind = row.get("kind")
        if kind in ACTION_KINDS:
            if row.get("allowed") is False or (kind == "open" and row.get("status") == "refused"):
                code = _reason_code(row)
                refusals[code] += 1
                result = "refused"
            elif row.get("dispatch_status") in ("command_completed", "submitted"):
                dispatched += 1
                code, result = None, "dispatched"
            else:
                code, result = None, "unverified"
            timeline.append({"at": _time(row.get("ts")), "action_type": kind,
                             "app": _app(row), "result": result, "reason_code": code,
                             "explanation": EXPLANATIONS.get(code) if code else None})
        elif kind == "recovery" and row.get("action") == "stop_for_human":
            handoffs += 1
            timeline.append({"at": _time(row.get("ts")), "action_type": "handoff",
                             "app": _app(row), "result": "handoff", "reason_code": "HANDOFF",
                             "explanation": EXPLANATIONS["HANDOFF"]})
        elif kind == "command_gate":
            hook_seen = True
            if row.get("mode") == "enforce" and row.get("decision") in ("deny", "ask"):
                hook_refusals += 1
                code = _command_code(row)
                timeline.append({"at": _time(row.get("ts")), "action_type": "command",
                                 "app": None, "result": "refused", "reason_code": code,
                                 "explanation": EXPLANATIONS[code]})
        elif kind == "read" and row.get("op") in READ_OPS:
            timeline.append({"at": _time(row.get("ts")), "action_type": row["op"],
                             "app": _app(row), "result": "observed", "reason_code": None,
                             "explanation": None})

    status = end.get("terminal_status") if end and start else None
    rc = end.get("exit_code") if end else None
    valid_terminal = bool(start and end and rows.index(start) < rows.index(end) and
                          type(rc) is int and
                          ((status == "completed" and rc == 0 and end.get("ok") is True) or
                           (status in task_status.TERMINAL_STATES[1:] and rc != 0 and end.get("ok") is False)))
    if valid_terminal and status == "cancelled":
        outcome = "user_stopped"
    elif handoffs and valid_terminal:
        outcome = "handoff"
    elif valid_terminal and status == "completed":
        outcome = "completed"
    elif valid_terminal:
        outcome = "error"
    else:
        outcome = "unknown"
    state = _ledger_state(ledger, task_id)
    return {
        "schema_version": 1,
        "task": {"task_id": task_id, "started_at": started_at, "ended_at": ended_at,
                 "duration_s": duration, "outcome": outcome, "goal_verified": False,
                 "transport_state": state},
        "counts": {"dispatched_actions": dispatched,
                   "gate_refusals_by_reason": dict(sorted(refusals.items())),
                   "command_hook_refusals": hook_refusals if hook_seen else None,
                   "handoff_points": handoffs},
        "timeline": timeline,
        "coverage": {"audit_linked": bool(rows), "lifecycle_valid": valid_terminal,
                     "command_hook_linked": hook_seen},
    }


def render_markdown(report):
    """Render only the already-redacted report object."""
    task = report["task"]
    labels = {"completed": "执行结束（目标未独立核验）", "user_stopped": "已取消（停止来源未独立核验）",
              "error": "出错", "handoff": "需要人工接管", "unknown": "结果未知"}
    counts = report["counts"]
    lines = ["# 执行报告", "", f"任务 ID：`{task['task_id']}`",
             f"结果：{labels[task['outcome']]}。", "目标是否独立核验：否。",
             f"开始：{task['started_at'] or '未知'}；结束：{task['ended_at'] or '未知'}；"
             f"耗时：{str(task['duration_s']) + ' 秒' if task['duration_s'] is not None else '未知'}。",
             "", "## 闸门与动作", "",
             f"已确认派发动作：{counts['dispatched_actions']} 次。",
             f"停下等待人工：{counts['handoff_points']} 处。"]
    hook = counts["command_hook_refusals"]
    lines.append("命令钩子拒绝：未覆盖（独立日志尚未按任务关联）。" if hook is None
                 else f"已关联的命令钩子拒绝：{hook} 次；不代表独立日志已完整覆盖。")
    refusals = counts["gate_refusals_by_reason"]
    if refusals:
        for code, count in refusals.items():
            lines.append(f"- {code}：{count} 次。{EXPLANATIONS[code]}")
    else:
        lines.append("未见与本任务 ID 关联的闸门拒绝；不代表未关联审计中没有拒绝。")
    lines.extend(["", "## 时间线", ""])
    if not report["timeline"]:
        lines.append("没有可归属到此任务 ID 的步骤记录。")
    for event in report["timeline"]:
        detail = f"；{event['reason_code']}：{event['explanation']}" if event["reason_code"] else ""
        lines.append(f"- {event['at'] or '时间未知'} · {event['action_type']} · "
                     f"{event['app'] or '应用未知'} · {event['result']}{detail}")
    if not report["coverage"]["lifecycle_valid"]:
        lines.extend(["", "生命周期证据不足；账本回执不能证明任务目标完成或用户停止。"])
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description="离线生成指定任务的隐私最小执行报告")
    parser.add_argument("--audit", required=True, type=Path)
    parser.add_argument("--ledger", required=True, type=Path)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--json-out", required=True, type=Path)
    parser.add_argument("--markdown-out", required=True, type=Path)
    args = parser.parse_args(argv)
    paths = [args.audit.resolve(), args.ledger.resolve(), args.json_out.resolve(), args.markdown_out.resolve()]
    if len(set(paths)) != len(paths):
        parser.error("input and output paths must be distinct")
    try:
        rows = list(task_status.read_audit(args.audit))
        ledger = json.loads(args.ledger.read_text(encoding="utf-8"))
        report = build_report(rows, ledger, args.task_id)
        args.json_out.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                                 encoding="utf-8")
        args.markdown_out.write_text(render_markdown(report), encoding="utf-8")
    except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
        parser.exit(3, "report input or output invalid; no raw content printed\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
