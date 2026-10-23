#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""recovery.py —— 失败恢复手册（Tier-1②）。

被 dsb_run.py 注入脚本命名空间（脚本里直接调 detect_situation() / recover()），
也可经 safe_tap.py recover --situation <x> 手动用。每次恢复动作写审计
{"kind":"recovery", situation, action, outcome}。

铁律（承 P1-1 复核教训——宁可少报，不要狼来了）：
  * 授权类按钮（同意/允许/权限/授权）**绝不自动点**，一律 stop_for_human；
  * **绝不自动登录**（login_required → stop_for_human）；
  * login_required 是新启发式（登录/验证码类文案 + 节点 <25），已在 5 个正常页
    （桌面/设置/图库/文件/浏览器，前置校验+新帧断言）做过 0 误报验证；
  * 除此外尽量复用已有信号：dialogs 字段 / settle 不收敛 / zone_coverage=none / pidof。
"""
import json
import os

import safe_tap as st

# 有任务上下文时先持久化消费预算；无上下文仅允许只读刷新/等待。
_LIMITS = {"loading": 2, "page_drift": 2, "target_missing": 1, "app_dead": 1,
           "login_required": 0, "dialog": 0, "empty_tree": 1}
_SITUATIONS = ("dialog", "loading", "page_drift", "empty_tree", "target_missing",
               "app_dead", "login_required")
_counts = {}
_LOGIN_WORDS = ["登录", "请先登录", "验证码", "获取验证码", "快捷登录", "手机号登录"]
class RecoveryFailure(RuntimeError):
    def __init__(self, rc, reason):
        self.rc = rc
        super().__init__(reason)


def _reserve(situation):
    task_id = os.environ.get("DSB_TASK_ID", "")
    if not task_id:
        if situation in ("app_dead", "page_drift"):
            raise RecoveryFailure(2, "task_context_required")
        if _counts.get(situation, 0) >= _LIMITS[situation]:
            raise RecoveryFailure(2, "recovery_budget_exhausted")
        _counts[situation] = _counts.get(situation, 0) + 1
        return
    # Reserve before dispatch, and keep failures consumed across helper processes.
    with st.lock():
        state = st.load_state(strict=True)
        ledger = state.setdefault("recovery_counts", {})
        counts = ledger.setdefault(task_id, {})
        if counts.get(situation, 0) >= _LIMITS[situation]:
            raise RecoveryFailure(2, "recovery_budget_exhausted")
        counts[situation] = counts.get(situation, 0) + 1
        st.save_state(state)


def _ns(**kw):
    import argparse
    return argparse.Namespace(**kw)


def _state_last():
    return (st.load_state() or {}).get("last_shot") or {}


def _fresh_look(note="recovery"):
    rc, out, err = _cap(st.cmd_look, _ns(note=note))
    try:
        d = json.loads(out)
    except (ValueError, TypeError):
        raise RecoveryFailure(3, "invalid_observation") from None
    last = _state_last()
    if not isinstance(d, dict) or not d.get("shot_id") or d["shot_id"] != last.get("id"):
        raise RecoveryFailure(3, "observation_identity_mismatch")
    d["nodes"] = last.get("nodes") or []
    d["pixel_nodes"] = last.get("pixel_nodes") or []
    return d


def _cap(fn, *a, **kw):
    import contextlib
    import io
    buf, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
        rc = fn(*a, **kw)
    if rc != 0:
        raise RecoveryFailure(rc, "helper_failed:%s" % getattr(fn, "__name__", "unknown"))
    return rc, buf.getvalue(), err.getvalue()


def detect_situation(expected_app="", expected_activity=""):
    """按优先级返回 (situation, info)；都没中 → ("ok", {...})。
    优先用已有信号：dialogs / zone_coverage / pidof / settle；login_required 是唯一新启发式。
    复核改进②：参数类型防御——传错类型立刻 TypeError，绝不静默给出看似合理的错误结论。"""
    if not isinstance(expected_app, str):
        raise TypeError("expected_app 应为包名字符串，收到 %s" % type(expected_app).__name__)
    if not isinstance(expected_activity, str):
        raise TypeError("expected_activity 应为 Activity 字符串，收到 %s"
                        % type(expected_activity).__name__)
    d = _fresh_look("recovery-detect")
    if d.get("error"):
        return "unknown", {"error": d.get("error")}
    nodes = d.get("nodes") or []
    texts = [(n.get("text") or n.get("desc") or "") for n in nodes]
    texts += [(p.get("text") or "") for p in (d.get("pixel_nodes") or [])]
    joined = " ".join(texts)
    fg = d.get("activity") or st.foreground()
    # 1) dialog：复用 look 已修好误报的 dialogs 字段
    if d.get("dialogs"):
        return "dialog", {"dialogs": d["dialogs"], "hint": d.get("interruption_hint", ""),
                          "activity": fg}
    # 2) login_required（新启发式）：登录类文案 + 节点少（<25）
    if len(nodes) < 25:
        ev = [w for w in _LOGIN_WORDS if w in joined]
        if ev:
            return "login_required", {"nodes": len(nodes), "evidence": ev, "activity": fg}
    # 3) page_drift：期望前台不符
    if expected_activity and expected_activity != fg:
        return "page_drift", {"expected": expected_activity, "now": fg}
    # 4) empty_tree
    if d.get("zone_coverage") == "none":
        return "empty_tree", {"pixel_nodes": len(d.get("pixel_nodes") or []), "activity": fg}
    # 5) app_dead：目标包进程不在
    if expected_app:
        try:
            pid = st.sh("shell", "pidof", expected_app, checked=True).strip()
        except st.ActionDispatchError as exc:
            return "unknown", {"reason": "process_query_unverified", "detail": exc.reason}
        if not pid or not all(part.isdigit() for part in pid.split()):
            return "unknown", {"reason": "process_query_unverified", "activity": fg}
    # 6) loading：短稳定检查不收敛
    ms, polls, settled = st._settle_stable(budget=1.6, interval=0.35)
    if not settled:
        return "loading", {"settle_ms": ms, "polls": polls, "activity": fg}
    return "ok", {"activity": fg, "nodes": len(nodes)}


def _audit_recovery(situation, action, outcome, detail=""):
    st.audit({"kind": "recovery", "situation": situation, "action": action,
              "outcome": outcome, "detail": str(detail)[:160]})


def recover(situation, ctx=None):
    """按处置策略表恢复。返回 {situation, action, outcome|reason, ...}。
    复核改进②：situation 必须是已知值、ctx 必须是 dict——否则报错而非静默走默认分支。"""
    if situation not in _SITUATIONS:
        raise ValueError("未知情形 %r；已知: %s" % (situation, ", ".join(_SITUATIONS)))
    if ctx is not None and not isinstance(ctx, dict):
        raise TypeError("ctx 应为 dict，收到 %s" % type(ctx).__name__)
    ctx = ctx or {}
    act = {"situation": situation, "action": "stop", "outcome": "failed", "rc": 3,
           "recovery_verified": False, "goal_verified": False, "retryable": False}
    try:
        if situation in ("login_required", "dialog"):
            act["action"] = "stop_for_human"
            raise RecoveryFailure(2, "human_intervention_required")
        expected = ctx.get("expected_activity") or ctx.get("expected")
        if situation == "page_drift" and (not isinstance(expected, str) or not expected.strip()):
            raise RecoveryFailure(2, "expected_activity_required")
        if situation == "app_dead" and (not isinstance(ctx.get("app"), str) or not ctx["app"].strip()):
            raise RecoveryFailure(2, "app_required")
        _reserve(situation)
        if situation == "loading":
            act["action"] = "wait-longer"
            ms, polls, settled = st._settle_stable(budget=5.0, interval=0.5)
            act.update(settled=settled, settle_ms=ms)
            if not settled:
                raise RecoveryFailure(3, "page_not_stable")
            act.update(rc=0, outcome="observed")
        elif situation == "page_drift":
            act["action"] = "back+look"
            before = _fresh_look("recovery:page_drift")
            _cap(st.cmd_key, _ns(shot=before["shot_id"], code=4, expect="recovery:page_drift",
                                 expect_activity=None, ack_no_zones=False))
            after = _fresh_look("recovery:page_drift-after")
            act["now_at"] = after.get("activity") or ""
            if act["now_at"] != expected:
                raise RecoveryFailure(3, "expected_activity_unverified")
            act.update(rc=0, outcome="ok", recovery_verified=True)
        elif situation in ("target_missing", "empty_tree"):
            act["action"] = "re-look"
            observed = _fresh_look("recovery:" + situation)
            act.update(rc=0, outcome="observed", shot_id=observed["shot_id"])
        elif situation == "app_dead":
            act["action"] = "restart"
            rc, out, err = _cap(st.cmd_open, _ns(app=ctx["app"]))
            launched = json.loads(out)
            if launched.get("launch_verified") is not True:
                raise RecoveryFailure(3, "launch_unverified")
            act.update(rc=0, outcome="ok", recovery_verified=True, app=launched["pkg"])
    except RecoveryFailure as exc:
        act.update(rc=exc.rc, outcome="refused" if exc.rc == 2 else "cancelled" if exc.rc == 130 else "failed",
                   reason=str(exc), recovery_verified=False)
    except Exception as exc:
        act.update(rc=3, outcome="failed", reason=type(exc).__name__, recovery_verified=False)
    try:
        _audit_recovery(situation, act["action"], act["outcome"], act.get("reason", ""))
    except Exception:
        act.update(rc=3, outcome="failed", reason="audit_failed", recovery_verified=False)
    return act
