"""Gesture budget contracts; all observations and dispatches are synthetic."""
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


class Gestures(unittest.TestCase):
    setUp = fixture.Credentials.setUp
    reset = fixture.Credentials.reset
    action = fixture.Credentials.action
    kinds = ("swipe", "scroll", "drag", "long_press")
    points = [(600, 1200), (600, 700)]

    def look(self):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(st.cmd_look(types.SimpleNamespace(note="synthetic")), 0)

    def spend(self, count=16):
        for i in range(count):
            self.look()
            self.assertEqual(self.action(self.kinds[i % 4], self.points), 0)

    def test_all_aliases_share_budget_across_look(self):
        self.spend()
        for kind in self.kinds:
            self.look()
            self.assertEqual(self.action(kind, self.points), 2)
        self.assertEqual(len(self.calls), 16)

    def test_coordinates_direction_duration_and_steps_do_not_reset(self):
        self.spend()
        self.look()
        for kind in self.kinds:
            args = types.SimpleNamespace(shot=st.load_state()["last_shot"]["id"],
                                         ms=2400, steps=30, fast=True)
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(st.do_action(args, kind, [(300, 500), (900, 1800)]), 2)

    def test_long_list_can_use_sixteen_actions_but_not_seventeen(self):
        for i in range(16):
            with patch.object(st, "observe", return_value={"treeHash": str(i), "evCount": i}):
                self.look()
                self.assertEqual(self.action("scroll", self.points), 0)
        self.look()
        self.assertEqual(self.action("scroll", self.points), 2)

    def test_tap_and_key_do_not_erase_gesture_history(self):
        self.spend()
        for kind, points in (("key", [4]), ("tap", [(400, 500)])):
            self.look()
            self.assertEqual(self.action(kind, points), 0)
        self.look()
        self.assertEqual(self.action("swipe", self.points), 2)

    def test_return_to_old_foreground_keeps_its_budget(self):
        self.spend()
        with patch.object(st, "foreground", return_value="other/.Page"):
            self.look()
            self.assertEqual(self.action("scroll", self.points), 0)
        self.look()
        self.assertEqual(self.action("scroll", self.points), 2)

    def test_unknown_prior_foreground_counts_conservatively(self):
        state = st.load_state()
        state["gestures"] = [{"act_before": ""} for _ in range(16)]
        st.save_state(state)
        self.assertEqual(self.action("scroll", self.points), 2)

    def test_unknown_current_foreground_cannot_ignore_history(self):
        self.spend()
        with patch.object(st, "foreground", return_value=""):
            self.look()
            self.assertEqual(self.action("scroll", self.points), 2)

    def test_foreground_transition_does_not_refund_gesture(self):
        for _ in range(16):
            self.look()
            # Reads: policy preflight, gate, pre-dispatch, post-action.
            with patch.object(st, "foreground", side_effect=[
                    "test/.Page", "test/.Page", "test/.Page", "other/.Page"]):
                self.assertEqual(self.action("swipe", self.points), 0)
        self.look()
        self.assertEqual(self.action("swipe", self.points), 2)

    def test_unknown_failure_and_post_capture_failure_spend_budget(self):
        for failure in ("sh", "_settle_stable"):
            self.reset()
            for _ in range(16):
                self.look()
                with patch.object(st, failure, side_effect=RuntimeError("uncertain")):
                    with self.assertRaises(RuntimeError):
                        self.action("scroll", self.points)
            self.look()
            self.assertEqual(self.action("drag", self.points), 2)

    def test_checked_dispatch_failure_spends_budget(self):
        for _ in range(16):
            self.look()
            with patch.object(st, "sh", side_effect=st.ActionDispatchError("unknown")):
                self.assertEqual(self.action("long_press", self.points), 3)
        self.look()
        self.assertEqual(self.action("long_press", self.points), 2)

    def test_attempt_is_durable_before_dispatch(self):
        for kind in self.kinds:
            self.reset()
            def dispatch(*args, **kwargs):
                state = st.load_state()
                self.assertEqual(len(state["gestures"]), 1)
                self.assertIn("consumed_at", state["last_shot"])
                raise KeyboardInterrupt()
            with patch.object(st, "sh", side_effect=dispatch):
                with self.assertRaises(KeyboardInterrupt):
                    self.action(kind, self.points)
            self.assertEqual(len(st.load_state()["gestures"]), 1)

    def test_persistence_failure_prevents_dispatch(self):
        with patch.object(st, "save_state", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.action("scroll", self.points)
        self.assertEqual(self.calls, [])

    def test_gate_rejection_does_not_spend_budget(self):
        for _ in range(20):
            self.look()
            self.assertEqual(self.action("swipe", [(0, 900), (600, 1200)]), 2)
        self.assertEqual(st.load_state().get("gestures", []), [])
        self.assertEqual(self.calls, [])
        self.assertEqual(self.action("scroll", self.points), 0)

    def test_invalid_config_fails_closed(self):
        for value in (0, -1, 65, True, 2.0, "16", None, {}, []):
            for kind in self.kinds:
                self.reset()
                with patch.object(st, "cfg_get", side_effect=lambda k, d=None: value if k == "breaker.gesture_limit" else d):
                    self.assertEqual(self.action(kind, self.points), 2, (value, kind))
                self.assertEqual(self.calls, [])

    def test_config_boundary_values(self):
        for limit in (1, 64):
            self.reset()
            with patch.object(st, "cfg_get", side_effect=lambda k, d=None: limit if k == "breaker.gesture_limit" else d):
                self.spend(limit)
                self.look()
                self.assertEqual(self.action("scroll", self.points), 2)

    def test_gesture_history_bounded_separately_from_other_actions(self):
        for i in range(80):
            with patch.object(st, "foreground", return_value=f"app{i}/.Page"):
                self.look()
                self.assertEqual(self.action("scroll", self.points), 0)
        self.assertEqual(len(st.load_state()["gestures"]), 64)

    def test_bad_gesture_history_fails_before_device_access(self):
        for history in ({}, None, "bad", [False], ["bad"]):
            self.reset()
            state = st.load_state()
            state["gestures"] = history
            st.save_state(state)
            with patch.object(st, "foreground", side_effect=AssertionError("device read")):
                with self.assertRaises(ValueError):
                    self.action("scroll", self.points)
            self.assertEqual(self.calls, [])

    def test_old_state_without_gesture_history_migrates_lazily(self):
        self.assertNotIn("gestures", st.load_state())
        self.assertEqual(self.action("scroll", self.points), 0)
        self.assertEqual(len(st.load_state()["gestures"]), 1)

    def test_attempt_uses_gate_foreground_with_equivalent_package_read(self):
        for _ in range(16):
            self.look()
            with patch.object(st, "foreground", side_effect=[
                    "test/.Page", "test/.Page", "test", "test"]):
                self.assertEqual(self.action("scroll", self.points), 0)
                self.assertEqual(st.load_state()["gestures"][-1]["act_before"], "test/.Page")
        self.look()
        self.assertEqual(self.action("scroll", self.points), 2)

    def test_invalid_foreground_field_fails_closed(self):
        for value in (None, False, 42, [], {}):
            self.reset()
            state = st.load_state()
            state["gestures"] = [{"act_before": value}]
            st.save_state(state)
            with self.assertRaises(ValueError):
                st.load_state()

    def test_walk_explicit_max_is_ceiling_not_budget_exemption(self):
        import walk
        def look(note):
            self.look()
            return {"nodes": [{"text": note, "rect": [200, 300, 800, 500]}]}, 0
        output = io.StringIO()
        with patch.object(walk, "_look", side_effect=look), \
                patch.object(sys, "argv", ["walk.py", "--json", "--max", "30"]), \
                contextlib.redirect_stdout(output):
            self.assertEqual(walk.main(), 2)
        result = json.loads(output.getvalue())
        self.assertEqual(result["stop"], "gate-refused")
        self.assertEqual(result["scrolls"], 16)
        self.assertEqual(len(self.calls), 16)
        self.assertEqual(len(result["items"]), 17)

    def test_new_process_preserves_budget(self):
        self.spend()
        self.look()
        source = """
import json, sys, types
import safe_tap as st
st.STATE = sys.argv[1]
st.foreground = lambda: 'test/.Page'
st.screen_size = lambda: (1200, 2400)
st.gesture_insets = lambda *a: {'left': 0, 'right': 0, 'bottom': 0}
st.cfg_get = lambda k,d=None: d
st.sh = lambda *a,**kw: (_ for _ in ()).throw(AssertionError('device access'))
args = types.SimpleNamespace(shot=st.load_state()['last_shot']['id'])
print(json.dumps(st.check_gate(args, 'drag', [(600,1200),(600,700)])[:2]))
"""
        result = subprocess.run([sys.executable, "-c", source, st.STATE],
                                cwd=os.path.dirname(st.__file__), capture_output=True,
                                text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIs(json.loads(result.stdout)[0], False)


if __name__ == "__main__":
    unittest.main()
