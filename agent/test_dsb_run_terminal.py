"""Runner lifecycle tests with temporary files and no device access."""
import contextlib
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
import dsb_run as dr

REAL_LOADER = dr._load_agent_helpers


class Terminal(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.records, self.calls = [], []
        self.audit_path = str(Path(self.tmp.name) / "audit.jsonl")
        def audit(rec):
            rec = dict(rec, task_id=os.environ.get("DSB_TASK_ID", ""))
            if rec["kind"] == "task_end":
                rec["test_timer_remaining"] = signal.getitimer(signal.ITIMER_REAL)[0]
            self.records.append(rec)
            with open(self.audit_path, "a") as stream:
                stream.write(json.dumps(rec) + "\n")
        replacements = {"AUDIT": self.audit_path, "audit": audit,
            "foreground": lambda: "test/.Page", "load_state": lambda: {"last_shot": {"id": "old"}},
            "sh": lambda *a, **kw: (_ for _ in ()).throw(AssertionError("device access"))}
        for name, value in replacements.items():
            p = patch.object(dr.st, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(dr, "_load_agent_helpers", lambda *a: None)
        p.start()
        self.addCleanup(p.stop)
        p = patch.dict(sys.modules, {"recovery": types.SimpleNamespace(
            detect_situation=lambda: {}, recover=lambda: {})})
        p.start()
        self.addCleanup(p.stop)
        p = patch.dict(os.environ)
        p.start()
        self.addCleanup(p.stop)
        dr._GOAL_STATE.clear()
        dr.stats.update(actions=0, reads=0, errors=0, duration_s=0)
        dr.trace.clear()
        dr._TIMEOUT_HIT[0] = False
        self.handlers = {s: signal.getsignal(s) for s in (signal.SIGALRM, signal.SIGTERM)}
        self.addCleanup(self.cleanup_signals)

    def cleanup_signals(self):
        signal.setitimer(signal.ITIMER_REAL, 0)
        for s, handler in self.handlers.items():
            signal.signal(s, handler)

    def run_script(self, source, timeout="2", expected_ends=1):
        path = Path(self.tmp.name) / "task.py"
        if source is not None:
            path.write_text(source, encoding="utf-8")
        out = io.StringIO()
        with patch.object(sys, "argv", ["dsb_run", str(path), "--timeout", timeout]), \
                patch.object(dr, "_TAIL", dr._Tail(out)), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            rc = dr.main()
        summary = json.loads(out.getvalue().split("=== RUN_SUMMARY ===")[-1])
        ends = [r for r in self.records if r["kind"] == "task_end" and r["task_id"] == summary["task_id"]]
        self.assertEqual(len(ends), expected_ends)
        return rc, summary

    def fake_action(self, rc):
        def action(*a, **kw):
            self.calls.append(1)
            print(json.dumps({"status": "unknown", "reason": "synthetic", "goal_verified": False}))
            return rc
        return action

    def test_source_rejection_has_terminal(self):
        rc, summary = self.run_script("import subprocess")
        self.assertNotEqual(rc, 0)
        self.assertEqual(summary["result"], "failed")

    def test_missing_script_has_terminal(self):
        rc, summary = self.run_script(None)
        self.assertNotEqual(rc, 0)
        self.assertFalse(summary["ok"])

    def test_terminal_audit_failure_is_not_success(self):
        audit = dr.st.audit
        def fail_end(rec):
            if rec["kind"] == "task_end":
                raise OSError("synthetic disk full")
            audit(rec)
        with patch.object(dr.st, "audit", fail_end):
            rc, summary = self.run_script("pass", expected_ends=0)
        self.assertNotEqual(rc, 0)
        self.assertFalse(summary["ok"])
        self.assertEqual(summary["terminal_status"], "audit_failed")

    def test_timer_is_off_before_terminal_audit(self):
        self.run_script("pass")
        self.assertEqual(self.records[-1]["test_timer_remaining"], 0)

    def test_compile_failure_has_terminal(self):
        rc, summary = self.run_script("if:")
        self.assertNotEqual(rc, 0)
        self.assertEqual(summary["result"], "failed")

    def test_helper_load_failure_has_terminal(self):
        with patch.object(dr, "_load_agent_helpers", side_effect=RuntimeError("bad helper")):
            rc, summary = self.run_script("pass")
        self.assertNotEqual(rc, 0)
        self.assertFalse(summary["ok"])

    def test_real_helper_syntax_failure_is_not_swallowed(self):
        path = Path(self.tmp.name) / "helper.py"
        path.write_text("if:", encoding="utf-8")
        with patch.object(dr, "_load_agent_helpers", lambda _, g: REAL_LOADER(str(path), g)):
            rc, summary = self.run_script("set_goal(True)")
        self.assertNotEqual(rc, 0)
        self.assertFalse(summary["ok"])

    def test_nonzero_action_cannot_continue_or_claim_success(self):
        for action_rc in (2, 3):
            with self.subTest(rc=action_rc), patch.object(dr.st, "cmd_tap", self.fake_action(action_rc)):
                self.calls.clear()
                rc, summary = self.run_script("tap(600, 900)\ntap(600, 900)\nset_goal(True)")
                self.assertNotEqual(rc, 0)
                self.assertFalse(summary["ok"])
                self.assertEqual(self.calls, [1])
                self.assertEqual(summary["terminal_status"], "refused" if action_rc == 2 else "failed")
                self.assertEqual(summary["result"], "ran" if action_rc == 2 else "failed")

    def test_caught_helper_failure_is_sticky(self):
        with patch.object(dr.st, "cmd_tap", self.fake_action(3)):
            rc, summary = self.run_script("try:\n tap(600,900)\nexcept Exception:\n pass\nset_goal(True)\ntap(600,900)")
        self.assertNotEqual(rc, 0)
        self.assertFalse(summary["ok"])
        self.assertEqual(self.calls, [1])

    def test_look_failure_cannot_supply_stale_shot(self):
        with patch.object(dr.st, "cmd_look", self.fake_action(3)):
            rc, summary = self.run_script("look()\nset_goal(True)")
        self.assertNotEqual(rc, 0)
        self.assertFalse(summary["ok"])

    def test_navigation_failure_stops_script(self):
        with patch.object(dr.st, "cmd_key", self.fake_action(3)):
            rc, summary = self.run_script("back()\nhome()\nset_goal(True)")
        self.assertNotEqual(rc, 0)
        self.assertEqual(self.calls, [1])

    def test_false_goal_has_nonzero_exit(self):
        rc, summary = self.run_script("set_goal(False, 'not found')")
        self.assertNotEqual(rc, 0)
        self.assertFalse(summary["ok"])

    def test_fractional_timeout_is_enforced(self):
        rc, summary = self.run_script("time.sleep(0.2)", timeout="0.05")
        self.assertNotEqual(rc, 0)
        self.assertEqual(summary["terminal_status"], "timed_out")

    def test_interrupt_is_cancelled(self):
        rc, summary = self.run_script("raise KeyboardInterrupt()")
        self.assertNotEqual(rc, 0)
        self.assertEqual(summary["terminal_status"], "cancelled")

    def test_next_task_does_not_inherit_goal_or_counts(self):
        self.run_script("set_goal(True, 'only a script claim')")
        rc, summary = self.run_script("pass")
        self.assertEqual(rc, 0)
        self.assertEqual(summary["result"], "completed")
        self.assertEqual(summary["reads"], 0)
        self.assertFalse(summary["goal_verified"])

    def test_script_success_claim_is_not_verified(self):
        rc, summary = self.run_script("set_goal(True, 'script claim')")
        self.assertEqual(rc, 0)
        self.assertEqual(summary["result"], "completed")
        self.assertTrue(summary["goal_asserted"])
        self.assertFalse(summary["goal_verified"])
        self.assertEqual(summary["evidence_kind"], "script_assertion")

    def test_failed_then_new_task_succeeds(self):
        self.run_script("raise RuntimeError('first')")
        rc, summary = self.run_script("pass")
        self.assertEqual(rc, 0)
        self.assertTrue(summary["ok"])

    def test_signal_cancel_in_isolated_child(self):
        code = """
import json
from test_dsb_run_terminal import Terminal
t = Terminal()
t.setUp()
try:
    rc, summary = t.run_script('import os, signal\\nos.kill(os.getpid(), signal.SIGTERM)')
    assert rc == 130, (rc, summary)
    assert summary['terminal_status'] == 'cancelled', summary
finally:
    t.doCleanups()
"""
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_task_environment_and_handlers_restored(self):
        os.environ["DSB_TASK_ID"] = "parent"
        os.environ["DSB_TASK_STEP"] = "9"
        before = {s: signal.getsignal(s) for s in (signal.SIGALRM, signal.SIGTERM, signal.SIGINT)}
        self.run_script("pass")
        self.assertEqual(os.environ["DSB_TASK_ID"], "parent")
        self.assertEqual(os.environ["DSB_TASK_STEP"], "9")
        self.assertEqual({s: signal.getsignal(s) for s in before}, before)


if __name__ == "__main__":
    unittest.main()
