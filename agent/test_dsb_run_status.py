"""Runner interruption queries using temporary audits; never access a device."""
import contextlib
import io
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import unittest
from unittest.mock import patch
import dsb_run as dr
import test_dsb_run_terminal as fixture


class RunStatus(unittest.TestCase):
    setUp = fixture.Terminal.setUp
    cleanup_signals = fixture.Terminal.cleanup_signals
    run_script = fixture.Terminal.run_script

    def query(self, task_id=None):
        args = ["dsb_run", "--status"] + ([task_id] if task_id else [])
        out = io.StringIO()
        with patch.object(sys, "argv", args), contextlib.redirect_stdout(out):
            rc = dr.main()
        return rc, json.loads(out.getvalue())

    def write_rows(self, rows):
        Path(self.audit_path).write_text("".join(json.dumps(r) + "\n" for r in rows))

    def test_completed_status_is_not_verified_goal(self):
        _, summary = self.run_script("set_goal(True)")
        rc, report = self.query(summary["task_id"])
        self.assertEqual(rc, 0)
        task = report["tasks"][0]
        self.assertEqual(task["terminal_status"], "completed")
        self.assertTrue(task["ok"])
        self.assertFalse(task["goal_verified"])
        self.assertFalse(task["retryable"])

    def test_failed_cancelled_timeout_remain_distinct(self):
        for source, status in (("if:", "failed"), ("raise KeyboardInterrupt()", "cancelled"),
                               ("time.sleep(1)", "timed_out")):
            with self.subTest(status=status):
                _, summary = self.run_script(source, timeout="0.05")
                _, report = self.query(summary["task_id"])
                self.assertEqual(report["tasks"][0]["terminal_status"], status)
                self.assertFalse(report["tasks"][0]["ok"])

    def test_legacy_start_without_terminal_is_unknown(self):
        self.write_rows([{"kind": "task_start", "task_id": "legacy", "task_name": "old"}])
        rc, report = self.query("legacy")
        self.assertEqual(rc, 0)
        self.assertEqual(report["tasks"][0]["terminal_status"], "unknown")
        self.assertFalse(report["tasks"][0]["retryable"])

    def test_legacy_claim_or_inconsistent_terminal_is_not_completed(self):
        for end in ({"ok": True, "result": "achieved"},
                    {"ok": False, "terminal_status": "completed", "exit_code": 0},
                    {"ok": True, "terminal_status": "completed", "exit_code": 3}):
            with self.subTest(end=end):
                self.write_rows([{"kind": "task_start", "task_id": "legacy"},
                    dict(end, kind="task_end", task_id="legacy")])
                _, report = self.query("legacy")
                self.assertEqual(report["tasks"][0]["terminal_status"], "unknown")
                self.assertFalse(report["tasks"][0]["ok"])

    def test_duplicate_terminals_are_unknown(self):
        _, summary = self.run_script("pass")
        with open(self.audit_path, "a") as stream:
            stream.write(json.dumps(self.records[-1]) + "\n")
        _, report = self.query(summary["task_id"])
        self.assertEqual(report["tasks"][0]["terminal_status"], "unknown")

    def test_terminal_before_start_is_not_completion(self):
        self.write_rows([{"kind": "task_end", "task_id": "legacy", "ok": True,
                          "exit_code": 0, "terminal_status": "completed"},
                         {"kind": "task_start", "task_id": "legacy"}])
        _, report = self.query("legacy")
        self.assertEqual(report["tasks"][0]["terminal_status"], "unknown")

    def test_invalid_lease_identity_never_reads_outside_lease_directory(self):
        self.write_rows([{"kind": "task_start", "task_id": "../outside", "lease_version": 1}])
        _, report = self.query("../outside")
        self.assertEqual(report["tasks"][0]["terminal_status"], "unknown")
        self.assertEqual(report["tasks"][0]["reason"], "lease_unavailable")

    def test_lease_collision_does_not_replace_an_existing_owner(self):
        tid = "t123-" + "a" * 32
        with dr._run_lease(tid):
            with self.assertRaises(FileExistsError):
                with dr._run_lease(tid):
                    self.fail("duplicate run admitted")

    def test_status_is_read_only_and_does_not_load_script_helpers(self):
        _, summary = self.run_script("pass")
        before = Path(self.audit_path).read_bytes()
        with patch.object(dr, "_source_guard") as guard, \
                patch.object(dr, "_load_agent_helpers") as helpers:
            self.query(summary["task_id"])
        guard.assert_not_called()
        helpers.assert_not_called()
        self.assertEqual(Path(self.audit_path).read_bytes(), before)

    def test_missing_task_and_corrupt_audit_are_explicit_errors(self):
        rc, report = self.query("not-found")
        self.assertEqual(rc, 3)
        self.assertFalse(report["ok"])
        Path(self.audit_path).write_text('{"kind":')
        rc, report = self.query()
        self.assertEqual(rc, 3)
        self.assertFalse(report["ok"])

    def test_lease_setup_error_prevents_script_execution(self):
        Path(self.audit_path + ".runs").write_text("not a directory")
        task = Path(self.tmp.name) / "task.py"
        task.write_text("pass")
        with patch.object(sys, "argv", ["dsb_run", str(task)]), \
                patch.object(dr, "_source_guard") as guard, \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(dr.main(), 3)
        guard.assert_not_called()

    def start_child(self, source):
        task = Path(self.tmp.name) / "child.py"
        task.write_text(source)
        code = '''
import sys
import dsb_run as dr
dr.st.AUDIT = sys.argv[1]
dr.st.sh = lambda *a, **kw: (_ for _ in ()).throw(AssertionError("device access"))
dr._load_agent_helpers = lambda *a: None
sys.argv = ["dsb_run", sys.argv[2], "--timeout", "15"]
sys.exit(dr.main())
'''
        p = subprocess.Popen([sys.executable, "-u", "-c", code, self.audit_path, str(task)],
            cwd=Path(dr.__file__).parent, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(self.stop_child, p)
        with selectors.DefaultSelector() as selector:
            selector.register(p.stdout, selectors.EVENT_READ)
            self.assertTrue(selector.select(10), "child did not become ready")
        self.assertEqual(p.stdout.readline(), b"ready\n")
        rows = [json.loads(line) for line in Path(self.audit_path).read_text().splitlines()]
        return p, rows[-1]["task_id"]

    @staticmethod
    def stop_child(p):
        if p.poll() is None:
            p.kill()
        p.wait(timeout=5)
        for stream in (p.stdout, p.stderr):
            if stream:
                stream.close()

    def test_live_then_sigkill_is_unknown_and_never_replayed(self):
        marker = Path(self.tmp.name) / "executions"
        p, task_id = self.start_child(f"with open({str(marker)!r}, 'a') as f: f.write('x')\nprint('ready')\ntime.sleep(10)")
        _, report = self.query(task_id)
        self.assertEqual(report["tasks"][0]["terminal_status"], "running")
        self.assertFalse(report["tasks"][0]["ok"])
        p.kill()
        p.wait(timeout=5)
        self.assertEqual(p.returncode, -signal.SIGKILL)
        for _ in range(2):
            _, report = self.query(task_id)
            self.assertEqual(report["tasks"][0]["terminal_status"], "unknown")
            self.assertFalse(report["tasks"][0]["retryable"])
        code = '''
import sys, dsb_run as dr
dr.st.AUDIT = sys.argv[1]
sys.argv = ["dsb_run", "--status", sys.argv[2]]
sys.exit(dr.main())
'''
        result = subprocess.run([sys.executable, "-c", code, self.audit_path, task_id],
            cwd=Path(dr.__file__).parent, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["tasks"][0]["terminal_status"], "unknown")
        self.assertEqual(marker.read_text(), "x")

    def test_client_disconnect_does_not_replay_or_invent_cancel(self):
        release = Path(self.tmp.name) / "release"
        p, task_id = self.start_child("import os\nprint('ready')\n"
            f"while not os.path.exists({str(release)!r}): time.sleep(0.02)")
        p.stdout.close()
        _, report = self.query(task_id)
        self.assertEqual(report["tasks"][0]["terminal_status"], "running")
        release.touch()
        p.wait(timeout=5)
        _, report = self.query(task_id)
        self.assertEqual(report["tasks"][0]["terminal_status"], "completed")
        self.assertFalse(report["tasks"][0]["goal_verified"])


if __name__ == "__main__":
    unittest.main()
