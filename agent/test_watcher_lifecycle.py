"""Scheduler tests: no real subscriptions, device commands or process signals."""
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
import watcher as w


class Scheduler(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.calls, self.records = [], []
        self.rc, self.error = 0, None
        for name, value in {"HERE": self.tmp.name, "SUBS": str(Path(self.tmp.name) / "subs.json"),
                            "LOCK": str(Path(self.tmp.name) / "lock")}.items():
            p = patch.object(w, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = patch.dict(sys.modules, {"safe_tap": types.SimpleNamespace(audit=self.records.append)})
        p.start()
        self.addCleanup(p.stop)
        for name, value in (("run", self.fake_run), ("Popen", self.fake_popen)):
            p = patch.object(subprocess, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(os, "killpg", side_effect=ProcessLookupError)
        p.start()
        self.addCleanup(p.stop)
        p = patch.object(w.time, "sleep", return_value=None)
        p.start()
        self.addCleanup(p.stop)

    def fake_run(self, argv, **kw):
        self.calls.append((argv, kw))
        if self.error:
            raise self.error
        return subprocess.CompletedProcess(argv, self.rc, "synthetic", "")

    def fake_popen(self, argv, **kw):
        self.calls.append((argv, kw))
        if isinstance(self.error, OSError):
            raise self.error
        owner = self
        class Child:
            pid, returncode, attempts = 99999999, None, 0
            def communicate(self, timeout=None):
                self.attempts += 1
                if owner.error and self.attempts == 1:
                    raise owner.error
                self.returncode = owner.rc
                return "synthetic", ""
            def wait(self, timeout=None):
                self.returncode = owner.rc
                return self.returncode
            def poll(self):
                return self.returncode
            def kill(self):
                self.returncode = -9
        return Child()

    def run_sub(self):
        try:
            rc, detail = w.run_sub({"id": "express"})
        except KeyboardInterrupt:
            self.fail("cancellation escaped without a terminal receipt")
        self.assertEqual([r["kind"] for r in self.records], ["task_start", "task_end"])
        self.assertEqual(len(self.calls), 1)
        self.assertFalse(self.records[-1]["goal_verified"])
        return rc, detail

    def test_failed_subscription_is_not_replayed(self):
        self.rc = 3
        rc, _ = self.run_sub()
        self.assertEqual(rc, 3)
        self.assertFalse(self.records[-1]["ok"])

    def test_refused_subscription_is_not_replayed(self):
        self.rc = 2
        rc, _ = self.run_sub()
        self.assertEqual(rc, 2)
        self.assertEqual(self.records[-1]["terminal_status"], "refused")

    def test_timeout_has_terminal(self):
        self.error = subprocess.TimeoutExpired("synthetic", 1)
        rc, _ = self.run_sub()
        self.assertEqual(rc, 124)
        self.assertEqual(self.records[-1]["terminal_status"], "timed_out")

    def test_cancel_has_terminal(self):
        self.error = KeyboardInterrupt()
        rc, _ = self.run_sub()
        self.assertEqual(rc, 130)
        self.assertEqual(self.records[-1]["terminal_status"], "cancelled")

    def test_start_error_has_terminal(self):
        self.error = FileNotFoundError("synthetic")
        rc, _ = self.run_sub()
        self.assertEqual(rc, 3)
        self.assertFalse(self.records[-1]["ok"])

    def test_success_is_not_goal_verified(self):
        rc, _ = self.run_sub()
        self.assertEqual(rc, 0)
        self.assertEqual(self.records[-1]["result"], "completed")

    def test_once_returns_subscription_failure(self):
        Path(w.SUBS).write_text(json.dumps([{"id": "express", "last_run": 0}]))
        with patch.object(w, "run_sub", return_value=(3, "synthetic")), \
                patch.object(sys, "argv", ["watcher", "--once"]):
            self.assertEqual(w.main(), 3)
        saved = json.loads(Path(w.SUBS).read_text())[0]
        self.assertEqual(saved["last_rc"], 3)
        self.assertEqual(saved["last_tail"], "synthetic")
        self.assertFalse((Path(self.tmp.name) / ".watcher.pid").exists())

    def test_stop_does_not_delete_pid_before_confirmed_exit(self):
        pidfile = Path(self.tmp.name) / ".watcher.pid"
        pidfile.write_text("99999999")
        with patch.object(os, "kill", return_value=None), \
                patch.object(w, "is_watcher_pid", return_value=True, create=True), \
                patch.object(w.time, "monotonic", side_effect=range(100)), \
                patch.object(sys, "argv", ["watcher", "--stop"]), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertNotEqual(w.main(), 0)
        self.assertTrue(pidfile.exists())

    def test_disabled_wechat_does_not_create_task_or_child(self):
        rc, _ = w.run_sub({"id": "wechat"})
        self.assertEqual(rc, 2)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.records, [])

    def test_cleanup_unknown_cannot_be_success(self):
        with patch.object(w, "cleanup_child", return_value=False):
            rc, _ = self.run_sub()
        self.assertEqual(rc, 3)
        self.assertEqual(self.records[-1]["terminal_status"], "cleanup_unknown")
        self.assertFalse(self.records[-1]["group_cleanup_confirmed"])

    def test_terminal_audit_failure_cannot_be_success(self):
        def audit(row):
            if row["kind"] == "task_end":
                raise OSError("synthetic disk failure")
            self.records.append(row)
        with patch.dict(sys.modules, {"safe_tap": types.SimpleNamespace(audit=audit)}):
            rc, detail = w.run_sub({"id": "express"})
        self.assertEqual(rc, 3)
        self.assertIn("audit_failed", detail)

    def test_stop_refuses_unverified_pid_without_signalling(self):
        pidfile = Path(self.tmp.name) / ".watcher.pid"
        pidfile.write_text("99999999")
        with patch.object(os, "kill", return_value=None) as kill, \
                patch.object(w, "is_watcher_pid", return_value=False), \
                patch.object(sys, "argv", ["watcher", "--stop"]):
            self.assertEqual(w.main(), 3)
        self.assertEqual(kill.call_args_list[0].args, (99999999, 0))
        self.assertEqual(kill.call_count, 1)
        self.assertTrue(pidfile.exists())

    def test_cancel_arriving_during_cleanup_is_not_allowed_to_skip_cleanup(self):
        w._STOP = False
        original = w.cleanup_child
        completed = []
        def cleanup(child):
            w.request_stop(signal.SIGTERM, None)
            completed.append(True)
            return original(child)
        try:
            with patch.object(w, "cleanup_child", side_effect=cleanup):
                rc, _ = self.run_sub()
            self.assertEqual(completed, [True])
            self.assertEqual(rc, 130)
            self.assertTrue(self.records[-1]["group_cleanup_confirmed"])
        finally:
            w._STOP = False


if __name__ == "__main__":
    unittest.main()
