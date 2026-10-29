"""Read-only lifecycle evidence shared by the runner and audit statistics."""
import fcntl
import json
import os
import re

TERMINAL_STATES = ("completed", "failed", "refused", "cancelled", "timed_out", "audit_failed")


def _invalid_constant(value):
    raise ValueError("non-JSON numeric constant")


def read_audit(path, missing_ok=False):
    try:
        stream = open(path, encoding="utf-8")
    except FileNotFoundError:
        if missing_ok:
            return
        raise
    with stream:
        for line in stream:
            if not line.strip():
                continue
            row = json.loads(line, parse_constant=_invalid_constant)
            if not isinstance(row, dict):
                raise ValueError("invalid audit row")
            yield row


def lease_path(audit_path, task_id):
    if not re.fullmatch(r"t[0-9]+-[0-9a-f]{32}", task_id):
        raise ValueError("invalid run lease identity")
    return os.path.join(audit_path + ".runs", task_id + ".lock")


def task_states(rows, audit_path, task_id=None):
    grouped = {}
    for row in rows:
        kind = row.get("kind")
        if kind not in ("task_start", "task_end"):
            continue
        tid = row.get("task_id")
        if not isinstance(tid, str) or not tid:
            raise ValueError("missing task identity")
        if task_id and tid != task_id:
            continue
        events = grouped.setdefault(tid, {"task_start": [], "task_end": [], "out_of_order": False})
        if kind == "task_end" and not events["task_start"]:
            events["out_of_order"] = True
        events[kind].append(row)
    if task_id and not grouped:
        raise ValueError("task not found")
    tasks = []
    for tid, events in grouped.items():
        starts, ends = events["task_start"], events["task_end"]
        rec = {"task_id": tid, "terminal_status": "unknown", "result": "unknown",
               "ok": False, "goal_verified": False, "retryable": False,
               "exit_code": None, "reason": "missing_or_ambiguous_lifecycle"}
        if len(starts) == 1 and len(ends) == 1 and not events["out_of_order"]:
            end = ends[0]
            status, rc = end.get("terminal_status"), end.get("exit_code")
            valid = (type(rc) is int and
                ((status == "completed" and rc == 0 and end.get("ok") is True) or
                 (status in TERMINAL_STATES[1:] and rc != 0 and end.get("ok") is False)))
            if valid:
                rec.update(terminal_status=status, result=status, exit_code=rc,
                           ok=status == "completed", reason="terminal_record")
        elif len(starts) == 1 and not ends:
            rec["reason"] = "no_terminal_record"
            if starts[0].get("lease_version") == 1:
                try:
                    with open(lease_path(audit_path, tid), "r") as lease:
                        try:
                            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        except BlockingIOError:
                            rec.update(terminal_status="running", result="running", reason="lease_held")
                except (OSError, ValueError):
                    rec["reason"] = "lease_unavailable"
        tasks.append(rec)
    return tasks
