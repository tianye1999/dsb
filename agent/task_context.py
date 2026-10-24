"""Fail-closed task identity snapshot for short-lived execution processes."""

import json
import os
from pathlib import Path
import re


UUID_RE = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")
ACTIVE_FILE = Path(os.environ.get("DSB_ACTIVE_TASK_FILE", Path.home() / ".dsh-phone/active-task.json"))


def read_active(path=ACTIVE_FILE):
    try:
        stat = Path(path).lstat()
        if not Path(path).is_file() or (stat.st_mode & 0o777) != 0o600:
            return None
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        if (not isinstance(value, dict) or value.get("version") != 1 or
                value.get("state") != "active" or
                not isinstance(value.get("task_id"), str) or
                not UUID_RE.fullmatch(value["task_id"]) or
                not isinstance(value.get("generation"), str) or
                not UUID_RE.fullmatch(value["generation"])):
            return None
        return {"task_id": value["task_id"], "generation": value["generation"]}
    except (OSError, UnicodeError, ValueError, TypeError):
        return None


START_CONTEXT = read_active()


def audit_fields(snapshot=START_CONTEXT, path=ACTIVE_FILE, env=None):
    if env is None:
        env = os.environ
    task_id = env.get("DSB_TASK_ID")
    if task_id:
        fields = {"task_id": task_id}
        generation = env.get("DSB_TASK_GENERATION")
        if generation:
            fields["task_generation"] = generation
        return fields
    current = read_active(path)
    if snapshot and current == snapshot:
        return {"task_id": snapshot["task_id"], "task_generation": snapshot["generation"]}
    return {"task_context": "unknown"}
