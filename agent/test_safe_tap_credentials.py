"""Credential lifecycle tests; all device and image operations are synthetic."""
import contextlib
import io
import os
import tempfile
import time
import types
import unittest
from unittest.mock import patch
import safe_tap as st


class Credentials(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.calls = []
        replacements = {
            # Virtual-display policy is covered separately by safe_tap_vd_test.py.
            "vd_policy_block": lambda *a, **kw: None,
            # 派发前目标复核（R3）由 test_safe_tap_r3.py 单独覆盖；这里没有真实截图。
            "_r3_verify": lambda *a, **kw: {"ok": True, "diff": 0.0, "why": ""},
            "HERE": tmp.name, "STATE": os.path.join(tmp.name, "state.json"),
            "SHOTS": tmp.name, "foreground": lambda: "test/.Page",
            "screen_size": lambda: (1200, 2400),
            "gesture_insets": lambda *a: {"left": 0, "right": 0, "bottom": 0},
            "cfg_get": lambda k, d=None: d, "audit": lambda *a: None,
            "_ball": lambda *a: None, "_settle_stable": lambda **kw: (0, 1, True),
            "observe": lambda **kw: None, "grab": lambda *a, **kw: "/synthetic.png",
            "dump_nodes": lambda: [{"text": "Settings"}],
            "build_zones": lambda *a: [], "wakefulness": lambda: "Awake",
            "_ocr_cancel_check": lambda: lambda: False,
            "sh": lambda *a, **kw: self.calls.append(a) or "",
        }
        for name, value in replacements.items():
            p = patch.object(st, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.reset()

    def reset(self, coverage="clean"):
        st.save_state({"last_shot": {"id": "one", "ts": time.time(),
            "fg": "test/.Page", "zones": [], "nodes": [],
            "zone_coverage": coverage, "shot_reused": False}})
        self.calls.clear()

    def action(self, kind="tap", points=None, ack=False):
        args = types.SimpleNamespace(shot=st.load_state()["last_shot"]["id"],
            fast=True, ack_no_zones=ack, expect=None, expect_activity=None)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return st.do_action(args, kind, points or [(600, 900)])

    def test_one_shot_cannot_drive_two_taps(self):
        self.assertEqual(self.action(), 0)
        self.assertEqual(self.action(), 2)
        self.assertEqual(len(self.calls), 1)

    def test_ack_cannot_reuse_consumed_empty_tree_shot(self):
        self.reset("none")
        self.assertEqual(self.action("key", [4]), 0)
        self.assertEqual(self.action(ack=True), 2)
        self.assertEqual(len(self.calls), 1)

    def test_nav_exemption_does_not_exempt_reuse(self):
        self.reset("none")
        self.assertEqual(self.action("key", [4]), 0)
        self.assertEqual(self.action("key", [3]), 2)
        self.assertEqual(len(self.calls), 1)

    def test_each_action_kind_consumes_before_device_dispatch(self):
        for kind in ("tap", "swipe", "long_press", "drag", "scroll", "key"):
            with self.subTest(kind=kind):
                self.reset()
                points = [4] if kind == "key" else [(600, 900), (600, 1200)]
                def dispatch(*a, **kw):
                    self.assertIn("consumed_at", st.load_state()["last_shot"])
                    self.calls.append(a)
                    return ""
                with patch.object(st, "sh", side_effect=dispatch):
                    self.assertEqual(self.action(kind, points), 0)
                self.assertEqual(self.action("key", [3]), 2)
                self.assertEqual(len(self.calls), 1)

    def test_dispatch_exception_keeps_credential_consumed(self):
        with patch.object(st, "sh", side_effect=RuntimeError("uncertain dispatch")):
            with self.assertRaises(RuntimeError):
                self.action()
        self.assertEqual(self.action(), 2)
        self.assertEqual(self.calls, [])

    def test_persist_failure_prevents_dispatch(self):
        with patch.object(st, "save_state", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.action()
        self.assertEqual(self.calls, [])

    def test_interrupt_after_submission_keeps_credential_consumed(self):
        with patch.object(st, "sh", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.action()
        self.assertEqual(self.action(), 2)
        self.assertEqual(self.calls, [])

    def test_post_action_observation_error_keeps_credential_consumed(self):
        with patch.object(st, "_settle_stable", side_effect=RuntimeError("observation failed")):
            with self.assertRaises(RuntimeError):
                self.action()
        self.assertEqual(self.action(), 2)
        self.assertEqual(len(self.calls), 1)

    def test_rejected_action_does_not_consume(self):
        self.assertEqual(self.action(points=[(0, 900)]), 2)
        self.assertNotIn("consumed_at", st.load_state()["last_shot"])
        self.assertEqual(self.action(), 0)
        self.assertEqual(len(self.calls), 1)

    def test_non_cached_frame_older_than_action_is_rejected(self):
        state = st.load_state()
        state["last_action_ts"] = time.time() + .01
        st.save_state(state)
        self.assertEqual(self.action(), 2)
        self.assertEqual(self.calls, [])

    def test_explicit_new_look_allows_next_action(self):
        self.assertEqual(self.action(), 0)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(st.cmd_look(types.SimpleNamespace(note="test")), 0)
        self.assertNotIn("consumed_at", st.load_state()["last_shot"])
        self.assertEqual(self.action(), 0)
        self.assertEqual(len(self.calls), 2)

    def test_look_holds_same_lock_during_observation(self):
        entered = []
        @contextlib.contextmanager
        def held():
            entered.append(True)
            try:
                yield
            finally:
                entered.pop()
        def observe(**kw):
            self.assertEqual(entered, [True])
            return None
        with patch.object(st, "lock", held), patch.object(st, "observe", observe), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(st.cmd_look(types.SimpleNamespace(note="lock")), 0)
        self.assertEqual(entered, [])

    def test_cancel_while_waiting_for_lock_prevents_observation(self):
        stop = [False]
        def cancel_check():
            initial = stop[0]
            return lambda: stop[0] != initial
        @contextlib.contextmanager
        def delayed_lock():
            stop[0] = True
            yield
        with patch.object(st, "lock", delayed_lock), \
                patch.object(st, "_ocr_cancel_check", cancel_check), \
                patch.object(st, "observe", return_value=None) as observe, \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(st.cmd_look(types.SimpleNamespace(note="cancel")), 3)
        observe.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
