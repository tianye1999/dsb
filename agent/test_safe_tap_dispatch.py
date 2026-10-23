"""Synthetic command failures; never launches ADB or accesses device state."""
import contextlib
import errno
import io
import json
import subprocess
import types
import unittest
from unittest.mock import patch
import safe_tap as st
from test_safe_tap_credentials import Credentials

REAL_SH = st.sh
REAL_RUN = subprocess.run


class Dispatch(unittest.TestCase):
    setUp = Credentials.setUp
    reset = Credentials.reset

    def run_action(self, outcome, kind="tap"):
        args = types.SimpleNamespace(shot="one", fast=True, ack_no_zones=False,
                                     expect=None, expect_activity=None)
        points = [4] if kind == "key" else [(600, 900), (600, 1200)]
        output = io.StringIO()
        self.records = []
        with patch.object(st, "sh", REAL_SH), \
                patch.object(st.subprocess, "run", side_effect=outcome) as run, \
                patch.object(st, "audit", side_effect=self.records.append), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
            rc = st.do_action(args, kind, points)
        return rc, json.loads(output.getvalue()), run.call_args_list

    def test_all_action_kinds_nonzero_fail_without_replay(self):
        for kind in ("tap", "swipe", "key", "long_press", "drag", "scroll"):
            with self.subTest(kind=kind):
                self.reset()
                rc, result, calls = self.run_action(
                    lambda *a, **kw: subprocess.CompletedProcess(a, 1, b"", b"failure"), kind)
                self.assertEqual(rc, 3)
                self.assertEqual(result["status"], "unknown")
                self.assertEqual(result["reason"], "adb_nonzero")
                self.assertFalse(result["goal_verified"])
                self.assertEqual(len(calls), 1)
                self.assertIn("consumed_at", st.load_state()["last_shot"])
                self.assertEqual(self.records[-1]["status"], "unknown")

    def test_timeout_is_unknown_not_success(self):
        def timeout(*a, **kw):
            raise subprocess.TimeoutExpired("synthetic", 1)
        rc, result, calls = self.run_action(timeout)
        self.assertEqual(rc, 3)
        self.assertEqual(result["reason"], "adb_timeout")
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(len(calls), 1)
        self.assertLessEqual(calls[0].kwargs["timeout"], 15)

    def test_spawn_failure_is_not_submitted(self):
        def unavailable(*a, **kw):
            raise FileNotFoundError("synthetic")
        rc, result, calls = self.run_action(unavailable)
        self.assertEqual(rc, 3)
        self.assertEqual(result["status"], "not_submitted")
        self.assertEqual(result["reason"], "adb_start_failed")
        self.assertIn("consumed_at", st.load_state()["last_shot"])

    def test_io_error_after_start_is_unknown(self):
        def transport(*a, **kw):
            raise OSError(errno.EIO, "synthetic pipe read failure")
        rc, result, _ = self.run_action(transport)
        self.assertEqual(rc, 3)
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["reason"], "adb_transport_error")

    def test_failed_dispatch_credential_cannot_be_reused(self):
        rc, _, _ = self.run_action(
            lambda *a, **kw: subprocess.CompletedProcess(a, 1, b"", b"failure"))
        self.assertEqual(rc, 3)
        self.assertEqual(Credentials.action(self), 2)
        self.assertEqual(self.calls, [])

    def test_zero_exit_explicit_error_output_fails_without_fallback(self):
        for stdout, stderr in ((b"Error: unsupported", b""), (b"", b"SecurityException: denied")):
            with self.subTest(stdout=stdout):
                self.reset()
                rc, result, calls = self.run_action(
                    lambda *a, stdout=stdout, stderr=stderr, **kw: subprocess.CompletedProcess(a, 0, stdout, stderr), "drag")
                self.assertEqual(rc, 3)
                self.assertEqual(result["reason"], "adb_error_output")
                self.assertEqual(len(calls), 1)

    def test_command_success_is_not_goal_verified(self):
        rc, result, calls = self.run_action(
            lambda *a, **kw: subprocess.CompletedProcess(a, 0, b"", b""))
        self.assertEqual(rc, 0)
        self.assertEqual(result["dispatch_status"], "command_completed")
        self.assertFalse(result["goal_verified"])

    def test_motion_move_failure_still_releases_and_remains_failed(self):
        for kind in ("drag", "scroll"):
            with self.subTest(kind=kind):
                self.reset()
                _, _, calls = self.run_action(
                    lambda *a, **kw: subprocess.CompletedProcess(a, 0, b"", b""), kind)
                command = calls[0].args[0][-1]
                # Execute only the generated shell with a fake input function.
                fixture = 'input() { printf "%s\\n" "$2"; [ "$2" != MOVE ]; }; '
                result = REAL_RUN(["/bin/sh", "-c", fixture + command], capture_output=True, timeout=3)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout.decode().splitlines(), ["DOWN", "MOVE", "UP"])

    def test_read_only_sh_legacy_behavior_unchanged(self):
        with patch.object(st.subprocess, "run", side_effect=OSError("synthetic")):
            self.assertEqual(REAL_SH("shell", "wm", "size"), "")


if __name__ == "__main__":
    unittest.main()
