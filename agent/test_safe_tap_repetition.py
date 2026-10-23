"""Persistent tap/key breakers. All screen and dispatch functions are synthetic."""
import contextlib
import io
import json
import os
import subprocess
import sys
import types
import unittest
from unittest.mock import patch

import safe_tap as st
import test_safe_tap_credentials as fixture


class Repetition(unittest.TestCase):
    setUp = fixture.Credentials.setUp
    reset = fixture.Credentials.reset
    action = fixture.Credentials.action

    def look(self):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(st.cmd_look(types.SimpleNamespace(note="synthetic")), 0)

    def test_tap_limit_survives_real_look(self):
        for _ in range(2):
            self.look()
            self.assertEqual(self.action(), 0)
        self.look()
        self.assertEqual(self.action(), 2)
        self.assertEqual(len(self.calls), 2)

    def test_key_limit_survives_real_look(self):
        for _ in range(2):
            self.look()
            self.assertEqual(self.action("key", [4]), 0)
        self.look()
        self.assertEqual(self.action("key", [4]), 2)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.action("key", [3]), 0)

    def test_dispatch_unknown_and_post_observation_failure_spend_budget(self):
        for failure in ("sh", "_settle_stable"):
            for kind, points in (("tap", [(600, 900)]), ("key", [4])):
                with self.subTest(failure=failure, kind=kind):
                    self.reset()
                    for _ in range(2):
                        self.look()
                        with patch.object(st, failure, side_effect=RuntimeError("uncertain")):
                            with self.assertRaises(RuntimeError):
                                self.action(kind, points)
                    self.look()
                    self.assertEqual(self.action(kind, points), 2)

    def test_unrelated_action_does_not_erase_budget(self):
        for _ in range(2):
            self.look()
            self.assertEqual(self.action(), 0)
            self.look()
            self.assertEqual(self.action("swipe", [(600, 900), (600, 1200)]), 0)
        self.look()
        self.assertEqual(self.action(), 2)

    def test_same_coordinates_in_different_foreground_have_separate_budget(self):
        for _ in range(2):
            self.look()
            self.assertEqual(self.action(), 0)
        with patch.object(st, "foreground", return_value="another/.Page"):
            self.look()
            self.assertEqual(self.action(), 0)
        self.look()
        self.assertEqual(self.action(), 2)

    def test_rejected_action_does_not_spend_attempt(self):
        for _ in range(4):
            self.look()
            self.assertEqual(self.action(points=[(0, 900)]), 2)
        self.assertEqual(st.load_state().get("taps", []), [])
        self.assertEqual(self.action(), 0)

    def test_key_history_survives_interleaved_taps(self):
        for i in range(2):
            self.look()
            self.assertEqual(self.action("key", [4]), 0)
            self.look()
            self.assertEqual(self.action(points=[(400 + i * 300, 900)]), 0)
        self.look()
        self.assertEqual(self.action("key", [4]), 2)

    def test_digest_availability_change_does_not_reset_same_position(self):
        for _ in range(2):
            self.look()
            self.assertEqual(self.action(), 0)
        self.look()
        with patch.object(st, "_digest_at", return_value="new-digest"):
            self.assertEqual(self.action(), 2)

    def test_attempt_is_on_disk_before_dispatch(self):
        for kind, key, points in (("tap", "taps", [(600, 900)]), ("key", "keys", [4])):
            self.reset()
            def dispatch(*args, key=key, **kwargs):
                state = st.load_state()
                self.assertEqual(len(state[key]), 1)
                self.assertIn("consumed_at", state["last_shot"])
                raise KeyboardInterrupt()
            with patch.object(st, "sh", side_effect=dispatch):
                with self.assertRaises(KeyboardInterrupt):
                    self.action(kind, points)
            self.assertEqual(len(st.load_state()[key]), 1)

    def test_verified_foreground_transition_does_not_count_as_stuck_tap(self):
        for _ in range(3):
            self.look()
            # Reads: policy preflight, gate, pre-dispatch, post-action.
            with patch.object(st, "foreground", side_effect=[
                    "test/.Page", "test/.Page", "test/.Page", "other/.Page"]):
                self.assertEqual(self.action(), 0)
        self.assertTrue(all(t["changed"] for t in st.load_state()["taps"]))

    def test_tap_history_is_bounded_across_foregrounds(self):
        for i in range(80):
            with patch.object(st, "foreground", return_value=f"app{i}/.Page"):
                self.look()
                self.assertEqual(self.action(), 0)
        self.assertEqual(len(st.load_state()["taps"]), 64)

    def test_missing_post_action_foreground_is_not_verified_progress(self):
        for _ in range(2):
            self.look()
            with patch.object(st, "foreground", side_effect=[
                    "test/.Page", "test/.Page", "test/.Page", ""]):
                self.assertEqual(self.action(), 0)
        self.look()
        self.assertEqual(self.action(), 2)

    def test_history_is_bounded(self):
        with patch.object(st, "cfg_get", side_effect=lambda k, d=None: 64 if k.startswith("breaker.") else d):
            for i in range(80):
                self.look()
                self.assertEqual(self.action("key", [3 if i % 2 else 4]), 0)
        self.assertLessEqual(len(st.load_state()["keys"]), 64)
        self.assertEqual(len(st.load_state()["keys"]), 64)

    def test_invalid_limits_cannot_disable_breaker(self):
        for value in (0, -1, 65, "nonsense", True):
            for kind, points in (("tap", [(600, 900)]), ("key", [4])):
                with self.subTest(value=value, kind=kind):
                    self.reset()
                    with patch.object(st, "cfg_get", side_effect=lambda k, d=None, value=value: value if k.startswith("breaker.") else d):
                        self.assertEqual(self.action(kind, points), 2)
                    self.assertEqual(self.calls, [])

    def test_process_restart_preserves_key_budget(self):
        for _ in range(2):
            self.look()
            self.assertEqual(self.action("key", [4]), 0)
        self.look()
        source = """
import json, sys, types
import safe_tap as st
st.STATE = sys.argv[1]
st.foreground = lambda: 'test/.Page'
st.screen_size = lambda: (1200, 2400)
st.gesture_insets = lambda *a: {'left':0,'right':0,'bottom':0}
st.cfg_get = lambda k,d=None: d
st.sh = lambda *a,**kw: (_ for _ in ()).throw(AssertionError('device access'))
args = types.SimpleNamespace(shot=st.load_state()['last_shot']['id'])
print(json.dumps(st.check_gate(args, 'key', [4])[:2]))
"""
        result = subprocess.run([sys.executable, "-c", source, st.STATE],
                                cwd=os.path.dirname(st.__file__), capture_output=True,
                                text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIs(json.loads(result.stdout)[0], False)


if __name__ == "__main__":
    unittest.main()
