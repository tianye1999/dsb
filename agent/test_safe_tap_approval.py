"""Caller flags never authorize weak observations; no real device I/O."""
import contextlib
import io
import subprocess
import sys
import types
import unittest
from unittest.mock import patch

import safe_tap as st
import test_safe_tap_credentials as fixture


class Approval(unittest.TestCase):
    setUp = fixture.Credentials.setUp
    reset = fixture.Credentials.reset
    action = fixture.Credentials.action

    def test_untrusted_ack_cannot_authorize_any_non_navigation_action(self):
        for coverage in ("none", None, "", "unexpected"):
            for kind in ("tap", "swipe", "long_press", "drag", "scroll", "key"):
                for ack in (False, True):
                    with self.subTest(coverage=coverage, kind=kind, ack=ack):
                        self.reset(coverage)
                        points = [66] if kind == "key" else [(600, 900), (600, 1200)]
                        self.assertEqual(self.action(kind, points, ack), 2)
                        self.assertEqual(self.calls, [])
                        self.assertNotIn("consumed_at", st.load_state()["last_shot"])

    def test_navigation_exception_is_not_caller_approval(self):
        for key in (3, 4, 187):
            for ack in (False, True):
                with self.subTest(key=key, ack=ack):
                    self.reset("none")
                    with patch.object(st, "audit") as audit:
                        self.assertEqual(self.action("key", [key], ack), 0)
                    record = audit.call_args.args[0]
                    self.assertIs(record["approval_verified"], False)
                    self.assertNotEqual(record["ack_source"], "explicit")
                    self.assertEqual(self.action("key", [key], ack), 2)

    def test_valid_coverage_does_not_turn_ack_into_approval(self):
        for coverage in ("ok", "clean"):
            self.reset(coverage)
            with patch.object(st, "audit") as audit:
                self.assertEqual(self.action(ack=True), 0)
            self.assertEqual(audit.call_args.args[0]["ack_source"], "caller_untrusted")
            self.assertIs(audit.call_args.args[0]["approval_verified"], False)

    def test_refusal_is_audited_without_capture_or_observation(self):
        self.reset("none")
        args = types.SimpleNamespace(shot="one", fast=False, ack_no_zones=True)
        with patch.object(st, "grab") as grab, patch.object(st, "observe") as observe, \
                patch.object(st, "audit") as audit, \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(st.do_action(args, "tap", [(600, 900)]), 2)
        grab.assert_not_called()
        observe.assert_not_called()
        record = audit.call_args.args[0]
        self.assertFalse(record["allowed"])
        self.assertEqual(record["dispatch_status"], "not_submitted")
        self.assertEqual(record["ack_source"], "caller_untrusted")
        self.assertIs(record["approval_verified"], False)

    def test_help_does_not_offer_self_approval(self):
        for command in ("tap", "swipe", "key", "scroll", "long_press", "drag"):
            with self.subTest(command=command):
                result = subprocess.run([sys.executable, st.__file__, command, "--help"],
                                        capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0)
                self.assertIn("不授予权限", result.stdout)

    def test_runner_helpers_cannot_self_ack_and_failure_is_sticky(self):
        import dsb_run as dr
        for helper in (lambda: dr.tap(600, 900, ack=True),
                       lambda: dr.scroll(600, 1200, ack=True)):
            self.reset("none")
            with patch.object(dr, "_FAILURE", [None]), \
                    patch.object(dr, "_CANCELLED", [False]), \
                    patch.object(dr, "_TIMEOUT_HIT", [False]), \
                    patch.object(dr, "stats", dict(actions=0, reads=0, errors=0)), \
                    patch.object(dr, "trace", []):
                with self.assertRaises(dr.HelperFailure):
                    helper()
                with self.assertRaises(dr.HelperFailure):
                    dr.home()
                self.assertEqual(self.calls, [])
                self.assertNotIn("consumed_at", st.load_state()["last_shot"])


if __name__ == "__main__":
    unittest.main()
