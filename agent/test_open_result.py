"""Application launch contract; all shell/device/state access is intercepted."""
import contextlib
import io
import json
import types
import unittest
from unittest.mock import patch
import safe_tap as st


class OpenResult(unittest.TestCase):
    def setUp(self):
        self.packages = "package:test.app\n"
        self.component = "test.app/.Main\n"
        self.fg = "test.app/.Main"
        self.failure = None
        self.calls, self.audits = [], []
        self.cancelled = False
        for name, value in {
            # Virtual-display policy is covered separately by safe_tap_vd_test.py.
            "vd_policy_block": lambda *a, **kw: None,
            "sh": self.shell, "foreground": lambda: self.fg,
            "audit": self.audits.append, "lock": contextlib.nullcontext,
            "_ocr_cancel_check": lambda: lambda: self.cancelled,
        }.items():
            p = patch.object(st, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(st.time, "sleep", return_value=None)
        p.start()
        self.addCleanup(p.stop)
        p = patch.object(st.subprocess, "run", side_effect=AssertionError("real device access"))
        p.start()
        self.addCleanup(p.stop)

    def shell(self, *argv, **kwargs):
        self.calls.append((argv, kwargs))
        if self.failure and self.failure in argv and kwargs.get("checked"):
            raise st.ActionDispatchError("adb_nonzero", returncode=1)
        if "list" in argv:
            return self.packages
        if "resolve-activity" in argv:
            return self.component
        return "Status: ok\n"

    def run_open(self, app="test.app"):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = st.cmd_open(types.SimpleNamespace(app=app))
        return rc, json.loads(out.getvalue())

    def launched(self):
        return [c for c in self.calls if "start" in c[0]]

    def test_ambiguous_fragment_refuses_without_launch(self):
        self.packages = "package:test.app\npackage:test.application\n"
        rc, result = self.run_open("test")
        self.assertEqual(rc, 2)
        self.assertEqual(result["reason"], "ambiguous_app")
        self.assertEqual(self.launched(), [])

    def test_package_query_failure_does_not_launch(self):
        self.failure = "list"
        rc, result = self.run_open()
        self.assertEqual(rc, 3)
        self.assertFalse(result["ok"])
        self.assertEqual(self.launched(), [])

    def test_resolver_must_return_selected_package_component(self):
        for value in ("Error / data unavailable", "other.app/.Main", "test.app/.One\ntest.app/.Two"):
            with self.subTest(value=value):
                self.component = value
                self.calls.clear()
                rc, result = self.run_open()
                self.assertNotEqual(rc, 0)
                self.assertEqual(self.launched(), [])

    def test_dispatch_error_is_unknown_not_success_or_replay(self):
        self.failure = "start"
        rc, result = self.run_open()
        self.assertEqual(rc, 3)
        self.assertEqual(result["status"], "unknown")
        self.assertFalse(result["goal_verified"])
        self.assertEqual(len(self.launched()), 1)

    def test_foreground_mismatch_is_not_launch_success(self):
        self.fg = "other.app/.Main"
        rc, result = self.run_open()
        self.assertEqual(rc, 3)
        self.assertFalse(result["ok"])
        self.assertEqual(len(self.launched()), 1)

    def test_verified_launch_is_not_business_goal_verified(self):
        rc, result = self.run_open()
        self.assertEqual(rc, 0)
        self.assertTrue(result["launch_verified"])
        self.assertFalse(result["goal_verified"])
        self.assertEqual(len(self.launched()), 1)
        self.assertTrue(self.audits)

    def test_no_match_does_not_disclose_installed_app_inventory(self):
        rc, result = self.run_open("missing")
        self.assertNotEqual(rc, 0)
        self.assertNotIn("installed_third_party", result)

    def test_cancel_before_dispatch_prevents_launch(self):
        self.cancelled = True
        rc, result = self.run_open()
        self.assertEqual(rc, 130)
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(self.launched(), [])


if __name__ == "__main__":
    unittest.main()
