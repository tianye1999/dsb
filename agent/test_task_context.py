"""Task attribution is local and fail-closed; no device access."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import task_context
import safe_tap


A = "11111111-1111-4111-8111-111111111111"
B = "22222222-2222-4222-8222-222222222222"
G1 = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
G2 = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


class TaskContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.file = Path(self.tmp.name) / "active-task.json"

    def write(self, task_id=A, generation=G1, state="active"):
        self.file.write_text(json.dumps({"version": 1, "task_id": task_id,
                                         "generation": generation, "state": state}), encoding="utf-8")
        os.chmod(self.file, 0o600)

    def test_startup_snapshot_survives_only_same_active_generation(self):
        self.write()
        snapshot = task_context.read_active(self.file)
        self.assertEqual(task_context.audit_fields(snapshot, self.file, {}),
                         {"task_id": A, "task_generation": G1})
        self.write(state="closed")
        self.assertEqual(task_context.audit_fields(snapshot, self.file, {}), {"task_context": "unknown"})
        self.write(task_id=B, generation=G2)
        self.assertEqual(task_context.audit_fields(snapshot, self.file, {}), {"task_context": "unknown"})

    def test_old_or_malformed_context_never_gets_task_id(self):
        self.assertEqual(task_context.audit_fields(None, self.file, {}), {"task_context": "unknown"})
        self.write(task_id="not-a-uuid")
        self.assertIsNone(task_context.read_active(self.file))
        self.write()
        os.chmod(self.file, 0o644)
        self.assertIsNone(task_context.read_active(self.file))

    def test_explicit_runner_identity_keeps_existing_behavior(self):
        self.assertEqual(task_context.audit_fields(None, self.file, {"DSB_TASK_ID": "runner-id"}),
                         {"task_id": "runner-id"})

    def test_late_subprocess_does_not_inherit_next_tasks_identity(self):
        self.write()
        code = ("import json, task_context, sys; print('ready', flush=True); "
                "sys.stdin.readline(); print(json.dumps(task_context.audit_fields()), flush=True)")
        env = {**os.environ, "DSB_ACTIVE_TASK_FILE": str(self.file)}
        env.pop("DSB_TASK_ID", None)
        child = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, text=True, env=env)
        try:
            self.assertEqual(child.stdout.readline().strip(), "ready")
            self.write(state="closed")
            self.write(task_id=B, generation=G2)
            child.stdin.write("go\n")
            child.stdin.flush()
            self.assertEqual(json.loads(child.stdout.readline()), {"task_context": "unknown"})
            self.assertEqual(child.wait(timeout=5), 0)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)
            child.stdin.close()
            child.stdout.close()

    def test_safe_tap_writes_code_without_changing_raw_reason(self):
        audit = Path(self.tmp.name) / "audit.jsonl"
        with patch.object(safe_tap, "AUDIT", str(audit)), patch.dict(os.environ, {"DSB_TASK_ID": A}):
            safe_tap.audit({"kind": "tap", "allowed": False, "reason": "没有截图凭据：请先运行 look"})
        row = json.loads(audit.read_text(encoding="utf-8"))
        self.assertEqual(row["task_id"], A)
        self.assertEqual(row["reason_code"], "G1_NO_SHOT")
        self.assertEqual(row["reason"], "没有截图凭据：请先运行 look")

    def test_safe_tap_open_app_not_found_gets_operation_code(self):
        audit = Path(self.tmp.name) / "audit.jsonl"
        with patch.object(safe_tap, "AUDIT", str(audit)), patch.dict(os.environ, {"DSB_TASK_ID": A}):
            safe_tap.audit({"kind": "open", "status": "refused", "reason": "app_not_found",
                            "dispatch_status": "not_submitted"})
        rows = [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([row["reason_code"] for row in rows], ["OP_APP_NOT_FOUND"])

    def test_safe_tap_open_failed_gets_operation_code_even_when_enrichment_fails(self):
        audit = Path(self.tmp.name) / "audit.jsonl"
        record = {"kind": "open", "status": "failed", "reason": "vd_start_failed",
                  "dispatch_status": "not_submitted"}
        with patch.object(safe_tap, "AUDIT", str(audit)):
            safe_tap.audit(record)
            with patch.object(safe_tap.task_context, "audit_fields", side_effect=RuntimeError("broken context")):
                safe_tap.audit(record)
        rows = [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([row["reason_code"] for row in rows], ["OP_FAILED", "OP_FAILED"])

    def test_safe_tap_enrichment_failure_keeps_audit_and_decision(self):
        audit = Path(self.tmp.name) / "audit.jsonl"
        with patch.object(safe_tap, "AUDIT", str(audit)), \
             patch.object(safe_tap.task_context, "audit_fields", side_effect=RuntimeError("broken context")):
            safe_tap.audit({"kind": "tap", "allowed": False, "reason": "没有截图凭据"})
        row = json.loads(audit.read_text(encoding="utf-8"))
        self.assertIs(row["allowed"], False)
        self.assertEqual(row["reason_code"], "OTHER_REFUSAL")


if __name__ == "__main__":
    unittest.main()
