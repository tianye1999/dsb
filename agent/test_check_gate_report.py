"""Synthetic command-hook audit tests; Jev is always mocked."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import check_gate


ID = "11111111-1111-4111-8111-111111111111"


class CommandGateReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.audit = Path(self.tmp.name) / "audit.jsonl"
        self.log = Path(self.tmp.name) / "legacy.log"

    def run_hook(self, mode, verdict):
        command = "uninstall PRIVATE_CHAT_ORDER_918273"
        with patch.object(check_gate, "MODE", mode), patch.object(check_gate, "AUDIT", str(self.audit)), \
             patch.object(check_gate, "LOG", str(self.log)), patch.object(check_gate.jev_client, "ask", return_value=verdict), \
             patch.object(sys, "argv", ["check_gate", command]), patch.dict(os.environ, {"DSB_TASK_ID": ID}), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as exit_result:
                check_gate.main()
        self.assertEqual(exit_result.exception.code, 0)
        return json.loads(self.audit.read_text(encoding="utf-8"))

    def test_enforce_denial_contains_no_command_text(self):
        row = self.run_hook("enforce", {"answers": {"kind": {"choice": "destructive", "confidence": 0.95}}})
        self.assertEqual((row["kind"], row["mode"], row["decision"], row["category"]),
                         ("command_gate", "enforce", "deny", "destructive"))
        self.assertEqual(row["task_id"], ID)
        self.assertNotIn("PRIVATE_CHAT", json.dumps(row))

    def test_shadow_is_observation_not_refusal(self):
        row = self.run_hook("shadow", {"answers": {"kind": {"choice": "destructive", "confidence": 0.95}}})
        self.assertEqual((row["decision"], row["would_decision"]), ("allow", "deny"))

    def test_typesafe_unavailable_is_allow_warning(self):
        row = self.run_hook("enforce", None)
        self.assertEqual(row["decision"], "allow")
        self.assertEqual(row["warning"], "typesafe_unavailable")
        self.assertNotIn("PRIVATE_CHAT", json.dumps(row))

    def test_malformed_service_category_does_not_break_allow(self):
        for category in (["destructive"], {"choice": "destructive"}):
            with self.subTest(category=category):
                self.audit.unlink(missing_ok=True)
                row = self.run_hook("enforce", {"answers": {"kind": {
                    "choice": category, "confidence": 0.99}}})
                self.assertEqual(row["decision"], "allow")
                self.assertIsNone(row["category"])

    def test_telemetry_context_failure_cannot_change_hook_output(self):
        with patch.object(check_gate.task_context, "audit_fields", side_effect=RuntimeError("broken context")):
            command = "uninstall PRIVATE_CHAT_ORDER_918273"
            with patch.object(check_gate, "MODE", "enforce"), patch.object(check_gate, "AUDIT", str(self.audit)), \
                 patch.object(check_gate, "LOG", str(self.log)), \
                 patch.object(check_gate.jev_client, "ask", return_value={"answers": {"kind": {
                     "choice": "destructive", "confidence": 0.95}}}), \
                 patch.object(sys, "argv", ["check_gate", command]), \
                 contextlib.redirect_stdout(io.StringIO()) as output, contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as result:
                    check_gate.main()
            self.assertEqual(result.exception.code, 0)
            self.assertEqual(json.loads(output.getvalue())["hookSpecificOutput"]["permissionDecision"], "deny")
            self.assertFalse(self.audit.exists())


if __name__ == "__main__":
    unittest.main()
