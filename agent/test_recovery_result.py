"""Recovery contract tests with synthetic observations and no device access."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import contextlib
import recovery as r
REAL_LOAD_STATE = r.st.load_state


class RecoveryResult(unittest.TestCase):
    def setUp(self):
        self.state = {"last_shot": {"id": "new", "fg": "test/.Page", "nodes": [], "pixel_nodes": []}}
        self.looks = [{"shot_id": "new", "activity": "test/.Page"}]
        self.rc = 0
        self.calls, self.audits = [], []
        r._counts.clear()
        def save(state):
            self.state = copy.deepcopy(state)
        replacements = {
            "load_state": lambda **kw: copy.deepcopy(self.state), "save_state": save,
            "lock": contextlib.nullcontext, "audit": self.audits.append,
            "cmd_look": self.look, "cmd_key": self.key, "cmd_open": self.open,
            "_settle_stable": lambda **kw: (10, 1, True),
            "sh": lambda *a, **kw: self.fail("raw shell access in recovery"),
        }
        for name, value in replacements.items():
            p = patch.object(r.st, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = patch.dict(os.environ, {"DSB_TASK_ID": "synthetic-task"})
        p.start()
        self.addCleanup(p.stop)

    def look(self, args):
        self.calls.append("look")
        value = self.looks.pop(0) if len(self.looks) > 1 else self.looks[0]
        print(value if isinstance(value, str) else json.dumps(value))
        return self.rc

    def key(self, args):
        self.calls.append("key")
        return self.key_rc

    def open(self, args):
        self.calls.append("open")
        print(json.dumps({"ok": True, "launch_verified": True, "pkg": args.app, "goal_verified": False}))
        return self.open_rc

    def test_failed_look_never_reuses_old_shot(self):
        self.rc = 3
        with self.assertRaises(Exception):
            r._fresh_look()

    def test_malformed_or_mismatched_observation_is_not_usable(self):
        for value in ("not json", {"shot_id": "stale"}, {}):
            with self.subTest(value=value):
                self.looks = [value]
                with self.assertRaises(Exception):
                    r._fresh_look()

    def test_refused_back_is_propagated_without_after_look(self):
        self.key_rc = 2
        result = r.recover("page_drift", {"expected_activity": "test/.Target"})
        self.assertEqual(result["rc"], 2)
        self.assertEqual(result["outcome"], "refused")
        self.assertEqual(self.calls, ["look", "key"])

    def test_successful_back_without_expected_page_is_not_recovered(self):
        self.key_rc = 0
        result = r.recover("page_drift", {"expected_activity": "test/.Target"})
        self.assertEqual(result["rc"], 3)
        self.assertFalse(result["recovery_verified"])

    def test_back_requires_explicit_expected_activity(self):
        self.key_rc = 0
        result = r.recover("page_drift")
        self.assertEqual(result["rc"], 2)
        self.assertEqual(self.calls, [])

    def test_restart_uses_checked_open_once_and_budget_persists(self):
        self.open_rc = 0
        first = r.recover("app_dead", {"app": "test.app"})
        self.assertEqual(first["rc"], 0)
        self.assertTrue(first["recovery_verified"])
        self.assertFalse(first["goal_verified"])
        r._counts.clear()  # Simulate a fresh helper process without clearing persisted task state.
        second = r.recover("app_dead", {"app": "test.app"})
        self.assertEqual(second["rc"], 2)
        self.assertEqual(self.calls, ["open"])

    def test_restart_failure_cannot_be_ok(self):
        self.open_rc = 3
        result = r.recover("app_dead", {"app": "test.app"})
        self.assertEqual(result["rc"], 3)
        self.assertFalse(result["recovery_verified"])
        self.assertEqual(self.calls, ["open"])

    def test_new_task_gets_its_own_budget(self):
        self.open_rc = 0
        self.assertEqual(r.recover("app_dead", {"app": "test.app"})["rc"], 0)
        with patch.dict(os.environ, {"DSB_TASK_ID": "synthetic-second"}):
            self.assertEqual(r.recover("app_dead", {"app": "test.app"})["rc"], 0)

    def test_action_recovery_without_task_context_is_refused(self):
        self.open_rc = 0
        with patch.dict(os.environ, {"DSB_TASK_ID": ""}):
            result = r.recover("app_dead", {"app": "test.app"})
        self.assertEqual(result["rc"], 2)
        self.assertEqual(self.calls, [])

    def test_login_and_dialog_require_human_not_automatic_ack(self):
        for situation in ("login_required", "dialog"):
            with self.subTest(situation=situation):
                result = r.recover(situation, {"dialogs": [{"buttons": ["关闭"]}]})
                self.assertEqual(result["rc"], 2)
                self.assertEqual(result["outcome"], "refused")
        self.assertEqual(self.calls, [])

    def test_refreshed_observation_does_not_claim_target_recovered(self):
        result = r.recover("target_missing")
        self.assertEqual(result["outcome"], "observed")
        self.assertFalse(result["recovery_verified"])
        self.assertFalse(result["goal_verified"])

    def test_corrupt_persisted_budget_must_not_reset_and_dispatch(self):
        self.open_rc = 0
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text("{broken")
            with patch.object(r.st, "STATE", str(path)), patch.object(r.st, "load_state", REAL_LOAD_STATE):
                result = r.recover("app_dead", {"app": "test.app"})
        self.assertEqual(result["rc"], 3)
        self.assertEqual(self.calls, [])

    def test_failed_process_query_is_unknown_not_app_dead(self):
        def query(*args, **kw):
            if kw.get("checked"):
                raise r.st.ActionDispatchError("adb_nonzero", returncode=1)
            return ""
        with patch.object(r.st, "sh", side_effect=query):
            situation, info = r.detect_situation(expected_app="test.app")
        self.assertEqual(situation, "unknown")

    def test_activity_prefix_does_not_count_as_exact_match(self):
        self.looks = [{"shot_id": "new", "activity": "test/.PageOther"}]
        situation, info = r.detect_situation(expected_activity="test/.Page")
        self.assertEqual(situation, "page_drift")

    def test_back_verifies_expected_activity_from_detection_context(self):
        self.key_rc = 0
        self.looks = [{"shot_id": "new", "activity": "test/.Wrong"},
                      {"shot_id": "new", "activity": "test/.Page"}]
        result = r.recover("page_drift", {"expected": "test/.Page"})
        self.assertEqual(result["rc"], 0)
        self.assertTrue(result["recovery_verified"])
        self.assertFalse(result["goal_verified"])

    def test_empty_tree_and_loading_are_observations_not_goal_success(self):
        for situation in ("empty_tree", "loading"):
            with self.subTest(situation=situation):
                result = r.recover(situation)
                self.assertEqual(result["rc"], 0)
                self.assertEqual(result["outcome"], "observed")
                self.assertFalse(result["recovery_verified"])

    def test_budget_persistence_failure_prevents_dispatch(self):
        self.open_rc = 0
        with patch.object(r.st, "save_state", side_effect=OSError("synthetic")):
            result = r.recover("app_dead", {"app": "test.app"})
        self.assertEqual(result["rc"], 3)
        self.assertEqual(self.calls, [])

    def test_audit_failure_is_not_recovery_success(self):
        self.open_rc = 0
        with patch.object(r.st, "audit", side_effect=OSError("synthetic")):
            result = r.recover("app_dead", {"app": "test.app"})
        self.assertEqual(result["rc"], 3)
        self.assertEqual(result["reason"], "audit_failed")
        self.assertFalse(result["recovery_verified"])


if __name__ == "__main__":
    unittest.main()
