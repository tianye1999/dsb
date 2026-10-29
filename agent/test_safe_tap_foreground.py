"""Pre-dispatch foreground contracts; every device boundary is substituted."""
import contextlib
import io
import os
import types
import unittest
from unittest.mock import patch
import safe_tap as st
import test_safe_tap_credentials as fixture


class Foreground(unittest.TestCase):
    setUp = fixture.Credentials.setUp
    reset = fixture.Credentials.reset
    kinds = ("tap", "swipe", "long_press", "drag", "scroll", "key")

    def invoke(self, kind, fast):
        args = types.SimpleNamespace(shot="one", fast=fast, ack_no_zones=False,
                                     expect=None, expect_activity=None)
        points = [4] if kind == "key" else [(600, 900), (600, 1200)]
        with patch.dict(os.environ, {"SAFE_TAP_FAST": "0"}), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            return st.do_action(args, kind, points)

    def assert_refusal(self, foreground):
        for kind in self.kinds:
            for fast in (False, True):
                with self.subTest(kind=kind, fast=fast, foreground=foreground):
                    self.reset()
                    before = st.load_state()
                    # Policy and gate share one read; pre-dispatch and post-action remain live.
                    with patch.object(st, "foreground", side_effect=[
                            "test/.Page", foreground, foreground]), \
                            patch.object(st, "audit") as audit, \
                            patch.object(st, "_settle_stable", return_value=(0, 1, True)) as settle:
                        self.assertEqual(self.invoke(kind, fast), 2)
                    self.assertEqual(self.calls, [])
                    self.assertEqual(st.load_state(), before)
                    settle.assert_not_called()
                    audit.assert_called_once()
                    record = audit.call_args.args[0]
                    self.assertEqual(record["dispatch_status"], "not_submitted")
                    self.assertFalse(record["allowed"])
                    self.assertFalse(record["approval_verified"])
                    self.assertFalse(record["goal_verified"])
                    self.assertEqual(record["shot"], "one")

    def test_different_app_refuses_without_consumption(self):
        self.assert_refusal("other/.Page")

    def test_different_activity_refuses_without_consumption(self):
        self.assert_refusal("test/.Other")

    def test_missing_foreground_refuses_without_consumption(self):
        self.assert_refusal("")

    def test_unchanged_or_package_only_foreground_allows_one_dispatch(self):
        for kind in self.kinds:
            for fast in (False, True):
                for foreground in ("test/.Page", "test"):
                    with self.subTest(kind=kind, fast=fast, foreground=foreground):
                        self.reset()
                        with patch.object(st, "foreground", side_effect=[
                                "test/.Page", foreground, foreground]):
                            self.assertEqual(self.invoke(kind, fast), 0)
                        self.assertEqual(len(self.calls), 1)
                        self.assertIn("consumed_at", st.load_state()["last_shot"])

    def test_refusal_audit_failure_never_dispatches(self):
        before = st.load_state()
        with patch.object(st, "foreground", side_effect=[
                "test/.Page", "other/.Page", "other/.Page"]), \
                patch.object(st, "audit", side_effect=OSError("audit unavailable")):
            with self.assertRaisesRegex(OSError, "audit unavailable"):
                self.invoke("tap", True)
        self.assertEqual(self.calls, [])
        self.assertEqual(st.load_state(), before)

    def test_capture_time_change_is_caught_after_evidence_preparation(self):
        page = ["test/.Page"]
        def capture(*args, **kwargs):
            page[0] = "other/.Page"
            return "/synthetic.png"
        with patch.object(st, "foreground", side_effect=lambda: page[0]), \
                patch.object(st, "grab", side_effect=capture) as grab:
            self.assertEqual(self.invoke("tap", False), 2)
        grab.assert_called_once()
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
