import contextlib
import io
import json
import sys
import types
import unittest
from unittest.mock import patch
import recovery
import safe_tap as st
import test_dsb_run_terminal as fixture


class RunnerRecovery(unittest.TestCase):
    setUp = fixture.Terminal.setUp
    cleanup_signals = fixture.Terminal.cleanup_signals
    run_script = fixture.Terminal.run_script

    def test_failed_and_refused_recovery_cannot_continue_or_claim_goal(self):
        for code, status in ((2, "refused"), (3, "failed"), (130, "cancelled")):
            with self.subTest(code=code), patch.dict(sys.modules, {"recovery": recovery}), \
                    patch.object(recovery, "recover", new=lambda *a: {"rc": code, "reason": "synthetic"}):
                self.records.clear()
                rc, result = self.run_script("try:\n    recover('app_dead')\nexcept Exception:\n    pass\nset_goal(True, 'must not override')")
                self.assertEqual(rc, code)
                self.assertEqual(result["terminal_status"], status)
                self.assertFalse(result["goal_verified"])

    def test_detect_failed_observation_stops_task(self):
        with patch.dict(sys.modules, {"recovery": recovery}), \
                patch.object(recovery, "detect_situation", new=lambda: (_ for _ in ()).throw(recovery.RecoveryFailure(3, "synthetic"))):
            rc, result = self.run_script("detect_situation()\nset_goal(True)")
        self.assertEqual(rc, 3)
        self.assertEqual(result["terminal_status"], "failed")


class RecoveryCli(unittest.TestCase):
    def test_cli_propagates_structured_recovery_exit(self):
        for code in (0, 2, 3, 130):
            with self.subTest(code=code), patch.object(recovery, "recover", return_value={
                    "rc": code, "outcome": "observed" if code == 0 else "failed"}), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(st.cmd_recover(types.SimpleNamespace(situation="loading", app="")), code)


if __name__ == "__main__":
    unittest.main()
