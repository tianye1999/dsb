#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dsb_run.py —— DSB 批处理脚本执行器（借鉴 phone-harness 的 heredoc 工作流）。

用法：
    python3 dsb_run.py <脚本.py> [--timeout 180] [--max-actions 60]
    python3 dsb_run.py --status [TASK_ID]  # 只读；查询成功不等于任务成功

脚本内可用的助手（**全部走 safe_tap 闸门，G1-G5 无一绕过**）：
    look(note="")                    观测（截图+控件树+空树页像素OCR），返回 dict
    tap(x, y, expect="")             点击（自动用最近一次 look 的凭据；G1 TTL 内有效）
    tap_text(text, index=0)          在控件树/像素文本里找文字并点它的中心
    swipe(x1, y1, x2, y2)            滑动（走闸门）
    scroll(x=630, y=1500, dy=-420)   防甩飞滚动（内容方向：负=向下看）
    back() / home()                  返回 / 桌面
    open_app(name)                   按名字打开 App
    wait_for_text(text, timeout=10)  等文本出现（空树页自动 OCR），返回 {found, x, y, text}
    wait_stable(timeout=3)           等屏幕稳定（treeHash+evCount 连续两次相同）
    walk(dir="down", max=30)         列表遍历去重收集（返回 {items, stop, scrolls}）
    ocr()                            当前屏像素文本（空树页也能读）
    current_app()                    前台 Activity
    shot()                           最近截图路径（给 agent 读图用）

工作法（借鉴 phone-harness SKILL.md，适配 DSB）：
    1. 脚本开头写 # task:（用户的一句话）和 # step:（本脚本做什么）
    2. print() 你看到的；先写预期变化再动作；验证用 wait_for_text/wait_stable，别 sleep 瞎等
    3. 验证过的序列整段批处理；失败就拆成单步隔离，不要重跑整批
    4. 学到的新组合写进 agent_workspace/agent_helpers.py（下次自动加载）

安全边界：
    脚本源码禁止 subprocess / os.system / eval( / exec( / __import__ / 裸 "input tap|swipe|keyevent"
    （一切屏幕动作必须走闸门助手）；助手调用数超过 --max-actions 或墙钟超时 → 终止并打印轨迹。
"""
import argparse
import contextlib
import fcntl
import io
import json
import math
import os
import signal
import sys
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import safe_tap as st            # noqa: E402
import task_status              # noqa: E402

MAX_ACTIONS_DEFAULT = 60
MAX_SCRIPT_BYTES = 200_000
_FORBIDDEN = ("subprocess", "os.system", "eval(", "exec(", "__import__",
              "input tap", "input swipe", "input keyevent")

stats = {"actions": 0, "reads": 0, "errors": 0, "duration_s": 0.0}
trace = []
_TIMEOUT_HIT = [False]
_MAX_ACTIONS = [MAX_ACTIONS_DEFAULT]
_PRINT_TAIL = [""]
_GOAL_STATE = {}   # Script assertion only, never independent goal verification.
_FAILURE = [None]
_CANCELLED = [False]


class HelperFailure(RuntimeError):
    def __init__(self, rc, detail):
        super().__init__("helper rc=%s: %s" % (rc, detail))
        self.rc = rc


class Cancelled(BaseException):
    pass


def _on_term(sig, frame):
    _CANCELLED[0] = True
    raise Cancelled(signal.Signals(sig).name)


class Budget(Exception):
    pass


class Timeout(Exception):
    pass


def _tick(read=False):
    if os.environ.get("DSB_TASK_ID"):
        os.environ["DSB_TASK_STEP"] = str(int(os.environ.get("DSB_TASK_STEP") or 0) + 1)
    if read:
        stats["reads"] += 1
        return
    stats["actions"] += 1
    if stats["actions"] > _MAX_ACTIONS[0]:
        _FAILURE[0] = Budget("助手动作数超过上限（--max-actions %d）" % _MAX_ACTIONS[0])
        raise _FAILURE[0]


def traced(read=False):
    def deco(fn):
        def wrapper(*a, **kw):
            if _CANCELLED[0]:
                raise Cancelled("cancelled")
            if _FAILURE[0] is not None:
                raise _FAILURE[0]
            if _TIMEOUT_HIT[0]:
                raise Timeout("已超时")
            _tick(read)
            entry = {"helper": fn.__name__,
                     "args": ", ".join([repr(x)[:36] for x in a] +
                                       ["%s=%r" % (k, v) for k, v in kw.items()])[:120]}
            t0 = time.time()
            try:
                r = fn(*a, **kw)
                entry["ms"] = round((time.time() - t0) * 1000)
                trace.append(entry)
                return r
            except Exception as e:
                if _FAILURE[0] is None:
                    _FAILURE[0] = e
                entry["error"] = str(e)[:160]
                trace.append(entry)
                stats["errors"] += 1
                raise
        wrapper.__name__ = fn.__name__
        return wrapper
    return deco


def _on_alarm(sig, frame):
    _TIMEOUT_HIT[0] = True
    raise Timeout("墙钟超时")


class _Tail:
    """print 直通 + 留尾部（摘要用）。"""
    def __init__(self, wrapped, limit=4000):
        self._w = wrapped
        self._limit = limit
        self.tail = ""
    def write(self, s):
        self.tail = (self.tail + s)[-self._limit:]
        return self._w.write(s)
    def flush(self):
        return self._w.flush()
    def __getattr__(self, name):
        return getattr(self._w, name)


# 助手转调safe_tap；open单独校验启动，不代表所有入口已统一G1-G5。

def _ns(**kw):
    import argparse
    return argparse.Namespace(**kw)


def _state():
    return st.load_state() or {}


def _last_shot():
    return _state().get("last_shot") or {}


def _capture(fn, *a, **kw):
    buf, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
        rc = fn(*a, **kw)
    if rc != 0:
        try:
            result = json.loads(buf.getvalue())
            detail = result.get("reason") or result.get("status") or "command_failed"
        except (ValueError, AttributeError):
            detail = err.getvalue()[:160] or "command_failed"
        raise HelperFailure(rc, detail)
    return rc, buf.getvalue(), err.getvalue()


def _recovery_call(fn, *args, **kwargs):
    import recovery
    try:
        result = fn(*args, **kwargs)
    except recovery.RecoveryFailure as exc:
        raise HelperFailure(exc.rc, str(exc)) from exc
    if isinstance(result, dict):
        if result.get("rc") != 0:
            raise HelperFailure(result.get("rc", 3), result.get("reason", "recovery_failed"))
    elif isinstance(result, tuple) and result[0] == "unknown":
        raise HelperFailure(3, "recovery_observation_unknown")
    return result


@traced(read=True)
def look(note=""):
    """观测：截图 + 控件树 + 空树页像素 OCR。返回 dict（含 shot_id/nodes/pixel_nodes）。"""
    rc, out, err = _capture(st.cmd_look, _ns(note=note))
    try:
        d = json.loads(out)
    except Exception:
        d = {}
    last = _last_shot()
    d.setdefault("shot_id", last.get("id"))
    d["nodes"] = last.get("nodes") or []
    d["pixel_nodes"] = last.get("pixel_nodes") or []
    return d


@traced()
def tap(x, y, expect="", ack=False):
    """点击（自动携带最近一次 look 的凭据；G1 TTL 内有效）。
    ack 参数仅为兼容保留，不授予权限；危险区覆盖不足时拒绝并要求人工接管。"""
    last = _last_shot()
    rc, out, err = _capture(st.cmd_tap, _ns(shot=last.get("id"), x=int(x), y=int(y),
                                            expect=expect, expect_activity=None,
                                            ack_no_zones=bool(ack),
                                            fast=False))
    try:
        return json.loads(out)
    except Exception:
        return {"rc": rc, "stderr": err[:200]}


@traced(read=True)
def set_goal(ok, note=""):
    """脚本自述目标状态，不是独立验证。失败/拒绝优先，不能被成功声明覆盖。
    set_goal(True)  → result=completed, goal_asserted=True, goal_verified=False
    set_goal(False) → result=failed（ok=False）
    不断言         → 干净跑完=completed；有被拒/恢复=ran（目标未验证，ok=False）。"""
    if not isinstance(ok, bool):
        raise ValueError("set_goal requires a boolean script assertion")
    _GOAL_STATE.clear()
    _GOAL_STATE.update({"ok": bool(ok), "note": str(note)[:200]})
    st.audit({"kind": "goal", "task_id": os.environ.get("DSB_TASK_ID", ""),
              "goal_ok": bool(ok), "note": str(note)[:200]})
    return {"goal_ok": bool(ok), "note": str(note)[:200]}


@traced()
def long_press(x=None, y=None, ms=800, node=None, expect=""):
    """长按（同点按下保持 ms）。--node 传句柄则走 R1-R4 解析。自动携带最近一次 look 的凭据。"""
    last = _last_shot()
    rc, out, err = _capture(st.cmd_long_press, _ns(shot=last.get("id"),
                                                   x=(int(x) if x is not None else None),
                                                   y=(int(y) if y is not None else None),
                                                   ms=int(ms), node=node, expect=expect,
                                                   expect_activity=None, fast=False))
    try:
        return json.loads(out)
    except Exception:
        return {"rc": rc, "stderr": err[:200]}


@traced()
def drag(x1=None, y1=None, x2=None, y2=None, ms=600, node=None, expect=""):
    """按住拖拽（分步慢速移动到终点）。--node 为起点句柄（终点仍需 x2/y2）。"""
    last = _last_shot()
    rc, out, err = _capture(st.cmd_drag, _ns(shot=last.get("id"),
                                             x1=(int(x1) if x1 is not None else None),
                                             y1=(int(y1) if y1 is not None else None),
                                             x2=(int(x2) if x2 is not None else None),
                                             y2=(int(y2) if y2 is not None else None),
                                             ms=int(ms), node=node, expect=expect,
                                             expect_activity=None, fast=False))
    try:
        return json.loads(out)
    except Exception:
        return {"rc": rc, "stderr": err[:200]}


@traced()
def tap_text(text, index=0, exact=False):
    """在控件树+像素文本里找 text 并点它的中心。找不到抛 RuntimeError。"""
    d = look("tap_text:" + text)
    cands = []
    for n in (d.get("nodes") or []) + (d.get("pixel_nodes") or []):
        t = (n.get("text") or n.get("desc") or "").strip()
        r = n.get("rect") or [0, 0, 0, 0]
        if t and ((t == text) if exact else (text in t)):
            if len(r) == 4:
                x = (int(r[0]) + int(r[2])) // 2
                y = (int(r[1]) + int(r[3])) // 2
            else:
                x, y = int(n.get("x", 0)), int(n.get("y", 0))
            cands.append({"text": t[:60], "x": x, "y": y})
    if not cands:
        visible = [(n.get("text") or n.get("desc") or "")[:26]
                   for n in (d.get("nodes") or [])][:20]
        raise RuntimeError("没找到 %r；屏上可见: %s" % (text, visible))
    hit = cands[min(index, len(cands) - 1)]
    return tap(hit["x"], hit["y"], expect="tap_text:" + text[:30])


@traced()
def swipe(x1, y1, x2, y2):
    last = _last_shot()
    rc, out, err = _capture(st.cmd_swipe, _ns(shot=last.get("id"), x1=int(x1), y1=int(y1),
                                              x2=int(x2), y2=int(y2), expect="swipe",
                                              expect_activity=None, ack_no_zones=False))
    return {"rc": rc}


@traced()
def scroll(x=630, y=1500, dy=-420, steps=8, ack=False):
    """防甩飞滚动（内容方向：负 dy = 向下看更多）。ack 不授予权限，覆盖不足时拒绝。"""
    last = _last_shot()
    rc, out, err = _capture(st.cmd_scroll, _ns(shot=last.get("id"), x=int(x), y=int(y),
                                               dy=int(dy), steps=int(steps), expect="scroll",
                                               expect_activity=None,
                                               ack_no_zones=bool(ack)))
    return {"rc": rc}


@traced()
def back():
    last = _last_shot()
    rc, _, _ = _capture(st.cmd_key, _ns(shot=last.get("id"), code=4, expect="back", expect_activity=None))
    return rc


@traced()
def home():
    last = _last_shot()
    rc, _, _ = _capture(st.cmd_key, _ns(shot=last.get("id"), code=3, expect="home", expect_activity=None))
    return rc


@traced()
def open_app(name):
    rc, out, err = _capture(st.cmd_open, _ns(app=name))
    wait_stable(4)
    try:
        return json.loads(out)
    except Exception:
        return {"rc": rc, "out": out[:120]}


@traced(read=True)
def current_app():
    return st.foreground()


@traced(read=True)
def shot():
    """最近截图路径（给 agent 读图用）。"""
    return _last_shot().get("path") or ""


@traced(read=True)
def ocr(scale=0.45):
    """当前屏像素文本（空树页也能读）。"""
    from ocr import recognize
    return recognize(shot(), 0.5, scale)


@traced(read=True)
def find_text(text, exact=False):
    """控件树 + 像素文本里找 text → [{text, x, y, rect, source}]。"""
    d = look("find_text:" + text)
    out = []
    for n in (d.get("nodes") or []) + (d.get("pixel_nodes") or []):
        t = (n.get("text") or n.get("desc") or "").strip()
        r = n.get("rect") or [0, 0, 0, 0]
        if t and ((t == text) if exact else (text in t)):
            out.append({"text": t[:60],
                        "x": (int(r[0]) + int(r[2])) // 2 if len(r) == 4 else 0,
                        "y": (int(r[1]) + int(r[3])) // 2 if len(r) == 4 else 0,
                        "rect": r, "source": "tree"})
    return out


@traced(read=True)
def wait_for_text(text, timeout=10.0, exact=False):
    """轮询直到 text 出现；返回 {found, text, x, y} 或 {found: False}。"""
    deadline = time.time() + timeout
    while True:
        hits = find_text(text, exact=exact)
        if hits:
            return hits[0]
        if time.time() >= deadline:
            return {"found": False, "text": text, "timeout": timeout}
        time.sleep(0.5)


@traced(read=True)
def wait_stable(timeout=3.0):
    """等屏幕稳定（treeHash+evCount 连续两次相同）。"""
    ms, polls, settled = st._settle_stable(budget=timeout, interval=0.35)
    return {"settled": settled, "settle_ms": ms, "polls": polls}


@traced()
def walk(dir="down", max=30, end_after=3, amount=240, ack=False):
    """列表遍历去重收集（滚动走闸门）。返回 {items, stop, scrolls}。"""
    seen, order = set(), []
    stale, scrolls, stop = 0, 0, "max-scrolls"

    def extract(d):
        out = []
        for n in (d.get("nodes") or []) + (d.get("pixel_nodes") or []):
            t = (n.get("text") or n.get("desc") or "").strip()
            r = n.get("rect") or [0, 0, 0, 0]
            y = (int(r[1]) + int(r[3])) // 2 if len(r) == 4 else 0
            if t and 150 < y < 2600:
                out.append({"text": t[:70], "y": y})
        return out

    def ingest(boxes):
        new = 0
        for b in boxes:
            if b["text"] in seen:
                continue
            seen.add(b["text"])
            order.append(b)
            new += 1
        return new

    d = look("walk-initial")
    ingest(extract(d))
    while scrolls < max:
        last = _last_shot()
        ns = _ns(shot=last.get("id"), x=630, y=1500,
                 dy=(-amount if dir == "down" else amount), steps=8, expect="walk",
                 expect_activity=None, ack=ack)
        rc, out, err = _capture(st.do_action, ns, "scroll",
                                [(630, 1500), (630, 1500 + (-amount if dir == "down" else amount))])
        if rc != 0:
            stop = "gate-refused"
            break
        scrolls += 1
        d2 = look("walk-%d" % scrolls)
        new = ingest(extract(d2))
        if new:
            stale = 0
        else:
            stale += 1
            if stale >= end_after:
                stop = "reached-end"
                break
    return {"items": order, "stop": stop, "scrolls": scrolls}


# ── 执行器 ─────────────────────────────────────────────────────────────

def _load_agent_helpers(path, g):
    """agent 学到的组合函数（借鉴 phone-harness 的 agent_helpers.py 机制）。
    关键：exec 进**脚本的命名空间**——学习函数里引用 look/tap 等助手才有定义
    （若是独立模块 import，open_app 等名字解析不到，一调就 NameError）。"""
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8", errors="replace") as stream:
        src = stream.read()
    exec(compile(src, path, "exec"), g)


def _source_guard(path):
    """脚本源码安全扫描：禁止绕过闸门的裸输入/危险调用。"""
    src = open(path, encoding="utf-8", errors="replace").read()
    bad = [p for p in _FORBIDDEN if p in src]
    if bad:
        raise RuntimeError("脚本包含被禁止的调用 %s —— 屏幕动作必须走闸门助手" % bad)
    if len(src.encode("utf-8")) > MAX_SCRIPT_BYTES:
        raise RuntimeError("脚本过大（>%d 字节）" % MAX_SCRIPT_BYTES)


def _intents(src):
    """脚本顶部的 # task: / # step: 注释（借鉴 phone-harness 的意图记录）。"""
    out = {"task": "", "steps": []}
    for line in src.splitlines()[:20]:
        s = line.strip()
        if not s:
            continue
        if not s.startswith("#"):
            break
        body = s.lstrip("#").strip()
        if body.startswith("task:"):
            out["task"] = body[5:].strip()
        elif body.startswith("step:"):
            out["steps"].append(body[5:].strip())
    return out


class _Tail:
    """print 直通 + 留尾部（摘要用）。"""
    def __init__(self, wrapped, limit=4000):
        self._w = wrapped
        self._limit = limit
        self.tail = ""
    def write(self, s):
        self.tail = (self.tail + s)[-self._limit:]
        return self._w.write(s)
    def flush(self):
        return self._w.flush()
    def __getattr__(self, name):
        return getattr(self._w, name)


_TAIL = _Tail(sys.stdout)


def _safe_print(*a, **kw):
    sep = kw.get("sep", " ")
    s = sep.join(str(x) for x in a)
    _TAIL.write(s + "\n")


def _lease_path(task_id):
    return task_status.lease_path(st.AUDIT, task_id)


@contextlib.contextmanager
def _run_lease(task_id):
    path = _lease_path(task_id)
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as lease:
        os.set_inheritable(lease.fileno(), False)
        fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def _status_query(task_id):
    """Read existing evidence only; a released lease never authorizes replay."""
    try:
        tasks = task_status.task_states(task_status.read_audit(st.AUDIT, missing_ok=True), st.AUDIT, task_id)
        print(json.dumps({"ok": True, "query_only": True, "tasks": tasks}, ensure_ascii=False))
        return 0
    except (OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "query_only": True, "tasks": [],
                          "error": "status_unavailable", "error_type": type(exc).__name__}))
        return 3


def main():
    ap = argparse.ArgumentParser(description="DSB 批处理脚本执行器（闸门内）")
    ap.add_argument("script", nargs="?")
    ap.add_argument("--status", nargs="?", const="", default=None,
                    help="只读查询任务状态；省略TASK_ID列出审计中全部任务，不重跑")
    ap.add_argument("--timeout", type=float, default=180.0)
    ap.add_argument("--max-actions", type=int, default=60)
    ap.add_argument("--json", action="store_true", help="结尾只输出 JSON 摘要")
    ap.add_argument("--task-name", default="",
                    help="任务名（写审计 task_start/task_end，进 stats --by-task；缺省取脚本 # task: 注释）")
    a = ap.parse_args()
    if a.status is not None:
        if a.script:
            ap.error("status query cannot run a script")
        return _status_query(a.status)
    if not a.script:
        ap.error("script is required unless --status is used")
    task_id = "t%d-%s" % (int(time.time()), uuid.uuid4().hex)
    try:
        with _run_lease(task_id):
            return _run_script(a, task_id)
    except OSError as exc:
        print(json.dumps({"ok": False, "task_id": task_id,
                          "terminal_status": "unknown", "goal_verified": False,
                          "retryable": False, "error_type": type(exc).__name__}))
        return 3


def _run_script(a, task_id):
    _MAX_ACTIONS[0] = max(1, a.max_actions)
    if not math.isfinite(a.timeout) or a.timeout <= 0:
        print("timeout must be finite and positive", file=sys.stderr)
        return 2
    stats.update(actions=0, reads=0, errors=0, duration_s=0.0)
    trace.clear()
    _GOAL_STATE.clear()
    _FAILURE[0] = None
    _CANCELLED[0] = _TIMEOUT_HIT[0] = False
    _TAIL.tail = ""
    previous_env = {k: os.environ.get(k) for k in ("DSB_TASK_ID", "DSB_TASK_NAME", "DSB_TASK_STEP")}
    # ── 任务上下文（Tier-1①）：id/名写环境变量 → 所有 safe_tap 子进程审计自动带上；──
    # ── 步号由 _tick 自增。task_start / task_end 成对写审计。──
    os.environ["DSB_TASK_ID"] = task_id
    os.environ["DSB_TASK_NAME"] = a.task_name or ""
    os.environ["DSB_TASK_STEP"] = "0"
    t0 = time.time()
    err = None
    intent = {"task": "", "steps": []}
    terminal_status, exit_code = "completed", 0
    handlers = {s: signal.getsignal(s) for s in (signal.SIGALRM, signal.SIGTERM, signal.SIGINT)}
    try:
        signal.signal(signal.SIGALRM, _on_alarm)
        signal.signal(signal.SIGTERM, _on_term)
        signal.signal(signal.SIGINT, _on_term)
        signal.setitimer(signal.ITIMER_REAL, a.timeout)
        st.audit({"kind": "task_start", "task_id": task_id,
                  "task_name": os.environ["DSB_TASK_NAME"], "script": a.script,
                  "lease_version": 1})
        _source_guard(a.script)
        with open(a.script, encoding="utf-8", errors="replace") as stream:
            src = stream.read()
        intent = _intents(src)
        if not os.environ["DSB_TASK_NAME"]:
            os.environ["DSB_TASK_NAME"] = intent.get("task") or os.path.basename(a.script)
        names = ("look", "tap", "long_press", "drag", "tap_text", "swipe", "scroll", "back", "home",
                 "open_app", "wait_for_text", "wait_stable", "walk", "ocr",
                 "find_text", "current_app", "shot", "set_goal")
        g = {"__name__": "__dsb_script__", "json": json, "time": time, "sys": sys, "print": _safe_print}
        g.update({k: globals()[k] for k in names})
        _load_agent_helpers(os.path.join(HERE, "agent_workspace", "agent_helpers.py"), g)
        import recovery as _rec
        def detect_situation(*args, **kwargs):
            return _recovery_call(_rec.detect_situation, *args, **kwargs)
        def recover(*args, **kwargs):
            return _recovery_call(_rec.recover, *args, **kwargs)
        g["detect_situation"] = traced(read=True)(detect_situation)
        g["recover"] = traced()(recover)
        code = compile(src, a.script, "exec")
        exec(code, g)
        if _CANCELLED[0]:
            raise Cancelled("cancelled")
        if _TIMEOUT_HIT[0]:
            raise Timeout("timeout caught by script")
        if _FAILURE[0] is not None:
            raise _FAILURE[0]
    except (Cancelled, KeyboardInterrupt) as exc:
        err, terminal_status, exit_code = "取消: %s" % exc, "cancelled", 130
    except Timeout as exc:
        err, terminal_status, exit_code = "超时: %s" % exc, "timed_out", 124
    except HelperFailure as exc:
        err = str(exc)
        terminal_status, exit_code = (("cancelled", 130) if exc.rc == 130 else
                                     ("refused", 2) if exc.rc == 2 else ("failed", 3))
    except BaseException as exc:
        err = "%s: %s" % (type(exc).__name__, str(exc)[:200])
        terminal_status, exit_code = "failed", 3
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        for sig, handler in handlers.items():
            signal.signal(sig, handler)
    if err:
        print("✗ 脚本异常: %s" % err, file=sys.stderr)
    dt = round(time.time() - t0, 1)
    stats["duration_s"] = dt
    # Script claims never override failed actions or count as independent verification.
    # 判定用已有审计数据：本任务 task_id 下的 recovery 记录数 + allowed=False 的动作数。
    recoveries = refusals = 0
    last_stop = None
    try:
        with open(st.AUDIT, encoding="utf-8", errors="ignore") as f:
            for line in f:
                if task_id not in line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if r.get("task_id") != task_id:
                    continue
                if r.get("kind") == "recovery":
                    recoveries += 1
                    if r.get("action") == "stop_for_human":
                        last_stop = r
                elif r.get("allowed") is False and r.get("kind") in ("tap", "swipe", "key", "scroll",
                                                                     "long_press", "drag"):
                    refusals += 1
    except Exception:
        pass
    goal_ok = _GOAL_STATE.get("ok")          # None=未断言 / True / False
    if err is not None:
        result = "ran" if terminal_status == "refused" else "failed"
        fail_reason = err
        ok = False
    elif last_stop is not None:
        result = "failed"
        fail_reason = "需人工介入：%s" % (last_stop.get("reason") or last_stop.get("detail") or "")
        ok = False
    elif goal_ok is False:
        result = "failed"
        fail_reason = "目标未达成断言：%s" % _GOAL_STATE.get("note", "")
        ok = False
    elif recoveries or refusals:
        result = "ran"                    # 跑完但有被拒/恢复——目标未断言，不算达成
        fail_reason = "脚本完成但有 %d 次被拒/恢复，目标未断言" % (recoveries + refusals)
        ok = False
    else:
        result = "completed"             # 干净跑完（未断言）：所有动作均放行且执行
        fail_reason = ""
        ok = True
    if not ok and exit_code == 0:
        terminal_status, exit_code = "failed", 3
    try:
        st.audit({"kind": "task_end", "task_id": os.environ.get("DSB_TASK_ID", ""),
                  "task_name": os.environ.get("DSB_TASK_NAME", ""),
                  "result": result,
                  "terminal_status": terminal_status, "exit_code": exit_code,
                  "goal_asserted": goal_ok, "goal_verified": False,
                  "evidence_kind": "script_assertion" if goal_ok is not None else "none",
                  "ok": ok, "steps": stats["actions"] + stats["reads"],
                  "elapsed_s": dt, "fail_reason": fail_reason[:160],
                  "script": a.script})
    except Exception as exc:
        ok, result, terminal_status, exit_code = False, "failed", "audit_failed", 3
        fail_reason = "terminal audit failed: %s" % type(exc).__name__
    summary = {"task": intent.get("task", ""), "steps": intent.get("steps", []),
               "task_id": os.environ.get("DSB_TASK_ID", ""), "ok": ok, "result": result,
               "terminal_status": terminal_status, "exit_code": exit_code,
               "goal_asserted": goal_ok, "goal_verified": False,
               "evidence_kind": "script_assertion" if goal_ok is not None else "none",
               "goal_note": _GOAL_STATE.get("note", ""),
               "fail_reason": fail_reason[:160] if fail_reason else "",
               "actions": stats["actions"], "reads": stats["reads"],
               "errors": stats["errors"], "duration_s": dt,
               "print_tail": _TAIL.tail[-1200:], "trace_tail": trace[-12:]}
    if err:
        summary["script_error"] = err
    print("=== RUN_SUMMARY ===")
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    for key, value in previous_env.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
