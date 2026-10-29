"""Offline batch behavior with real look/R3 logic and synthetic App-shaped observations."""
import contextlib
import io
import json
import os
import time
import types
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw

import safe_tap as st
import task_report
import test_safe_tap_credentials as fixture


TASK_ID = "11111111-1111-4111-8111-111111111111"
REAL_AUDIT = st.audit
REAL_LOOK = st._look_locked
REAL_R3 = st._r3_verify


def frame(path, width=300):
    image = Image.new("RGB", (720, 1600), "white")
    ImageDraw.Draw(image).rectangle((40, 880, 40 + width, 920), fill="black")
    image.save(path, quality=90)
    return path


class Batch(unittest.TestCase):
    reset = fixture.Credentials.reset

    def setUp(self):
        fixture.Credentials.setUp(self)
        self.audit_path = os.path.join(st.HERE, "audit.jsonl")
        self.width = self.next_width = 300
        self.next_window = 10
        self.next_dialog = False
        self.react = True
        self.observations = []

        state = st.load_state()
        state["last_shot"].update(path=frame(os.path.join(st.SHOTS, "shot-one.jpg")),
                                  window_id=10, dialogs=[], a_hash="before")
        st.save_state(state)

        def observe(**kwargs):
            self.width = self.next_width
            nodes = [{"text": "Ready", "desc": "", "cls": "android.widget.TextView",
                      "rect": [100, 800, 700, 1000]}]
            if self.next_dialog:
                nodes.append({"text": "Popup", "desc": "", "cls": "android.app.Dialog",
                              "rect": [100, 100, 700, 300]})
            result = {"nonce": "1234567890123", "ts": int((time.time() + .02) * 1000),
                      "displayId": 0, "fg": "test", "treeHash": "tree",
                      "nodes": nodes, "image": "look.jpg", "jpg": "look.jpg", "w": 720,
                      "h": 1600, "black": False, "interactive": True, "reused": False,
                      "aHash": "before", "ahash": "before", "evCount": 0, "err": ""}
            if self.next_window is not None:
                result["windowId"] = self.next_window
            self.observations.append(result)
            return result

        def grab(path, **kwargs):
            st.LAST_GRAB = {"a_hash": ("after" if self.react else "before")
                            if "after-" in path else "before"}
            return frame(path, self.width)

        for name, value in (("cfg_get", lambda key, default=None: True if key == "batch.enabled" else default),
                            ("audit", REAL_AUDIT), ("AUDIT", self.audit_path),
                            ("_look_locked", REAL_LOOK), ("_r3_verify", REAL_R3),
                            ("observe", observe), ("grab", grab),
                            ("save_look_jpg", lambda src, dest: bool(frame(dest, self.width)))):
            p = patch.object(st, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = patch.dict(os.environ, {"DSB_TASK_ID": TASK_ID})
        p.start()
        self.addCleanup(p.stop)

    def run_batch(self, steps, shot="one", display=0):
        args = types.SimpleNamespace(shot=shot, steps=json.dumps(steps), display=display,
                                     allow_main=False, why=None, raw_input=False)
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            rc = st.cmd_batch(args)
        return rc, json.loads(stdout.getvalue()), stderr.getvalue()

    def rows(self):
        if not os.path.exists(self.audit_path):
            return []
        with open(self.audit_path, encoding="utf-8") as source:
            return [json.loads(line) for line in source]

    @staticmethod
    def taps(count=3):
        return [{"kind": "tap", "x": 400 + 120 * i, "y": 900, "expect": "digit"}
                for i in range(count)]

    def test_three_taps_audit_and_report(self):
        rc, out, _ = self.run_batch(self.taps())
        self.assertEqual((rc, out["stop_reason_code"]), (0, None))
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(len(self.observations), 2)
        actions = [r for r in self.rows() if r.get("kind") == "tap"]
        self.assertEqual([r["batch_index"] for r in actions], [1, 2, 3])
        self.assertEqual({r["batch_size"] for r in actions}, {3})
        self.assertEqual({r["batch_id"] for r in actions}, {out["batch_id"]})
        self.assertTrue(actions[1]["r3_anchor"]["ok"])
        self.assertEqual(task_report.build_report(self.rows(), {}, TASK_ID)["counts"]["dispatched_actions"], 3)

    def test_first_frame_target_band_changed_stops(self):
        self.next_width = 520
        rc, out, _ = self.run_batch(self.taps())
        self.assertEqual((rc, out["stopped_at"], out["stop_reason_code"]), (2, 2, "R3_CHANGED"))
        self.assertEqual(len(self.calls), 1)
        rows = self.rows()
        anchor = next(r["r3_anchor"] for r in rows if r.get("kind") == "tap" and r.get("batch_index") == 2)
        self.assertFalse(anchor["ok"])
        self.assertGreater(anchor["diff"], st.R3_MAX_DIFF)
        self.assertEqual([r["reason_code"] for r in rows if r.get("kind") == "batch_stop"], ["R3_CHANGED"])

    def test_second_step_edge_refused(self):
        steps = self.taps()
        steps[1]["x"] = 0
        rc, out, _ = self.run_batch(steps)
        self.assertEqual((rc, out["stopped_at"], out["stop_reason_code"]), (2, 2, "G3_EDGE"))
        self.assertEqual(len(self.calls), 1)

    def test_node_handle_uses_existing_tap_resolution(self):
        steps = [{"kind": "tap", "node": "h1.tree.0000.digest", "expect": "digit"}]
        with patch.object(st, "resolve_handle", return_value={"ok": True, "x": 400, "y": 900, "by": "r1"}):
            rc, out, _ = self.run_batch(steps)
        self.assertEqual((rc, out["stop_reason_code"]), (0, None))
        action = next(r for r in self.rows() if r.get("kind") == "tap")
        self.assertEqual(action["handle"], steps[0]["node"])
        self.assertEqual(action["points"], [[400, 900]])

    def test_no_reaction_stops(self):
        self.react = False
        rc, out, _ = self.run_batch(self.taps())
        self.assertEqual((rc, out["stopped_at"], out["stop_reason_code"]),
                         (2, 1, "BATCH_NO_REACTION"))
        self.assertEqual(len(self.calls), 1)

    def test_foreground_change_stops(self):
        with patch.object(st, "_foreground_on", side_effect=lambda did: "test/.Other" if self.calls else "test/.Page"):
            rc, out, _ = self.run_batch(self.taps())
        self.assertEqual((rc, out["stopped_at"], out["stop_reason_code"]), (2, 1, "G1_FOREGROUND"))
        self.assertEqual(len(self.calls), 1)

    def test_new_window_stops_before_second_tap(self):
        self.next_window = 11
        rc, out, _ = self.run_batch(self.taps())
        self.assertEqual((rc, out["stopped_at"], out["stop_reason_code"]),
                         (2, 2, "BATCH_WINDOW_CHANGED"))
        self.assertEqual(len(self.calls), 1)

    def test_unknown_or_missing_window_stops_before_second_tap(self):
        for value in (-1, None):
            with self.subTest(window_id=value):
                self.reset()
                state = st.load_state()
                state["last_shot"].update(path=frame(os.path.join(st.SHOTS, "shot-one.jpg")),
                                          window_id=10, dialogs=[], a_hash="before")
                st.save_state(state)
                self.next_window = value
                rc, out, _ = self.run_batch(self.taps())
                self.assertEqual((rc, out["stopped_at"], out["stop_reason_code"]),
                                 (2, 2, "BATCH_WINDOW_CHANGED"))
                self.assertEqual(len(self.calls), 1)

    def test_unknown_anchor_window_stops_before_second_tap(self):
        state = st.load_state()
        state["last_shot"].pop("window_id")
        st.save_state(state)
        rc, out, _ = self.run_batch(self.taps())
        self.assertEqual((rc, out["stopped_at"], out["stop_reason_code"]),
                         (2, 2, "BATCH_WINDOW_CHANGED"))
        self.assertEqual(len(self.calls), 1)

    def test_new_dialog_stops_before_second_tap(self):
        self.next_dialog = True
        rc, out, _ = self.run_batch(self.taps())
        self.assertEqual((rc, out["stopped_at"], out["stop_reason_code"]),
                         (2, 2, "BATCH_WINDOW_CHANGED"))
        self.assertEqual(len(self.calls), 1)

    def test_bad_shot_refuses_entire_batch(self):
        rc, out, _ = self.run_batch(self.taps(), shot="wrong")
        self.assertEqual((rc, out["stopped_at"], out["stop_reason_code"]), (2, 0, "G1_MISMATCH"))
        self.assertEqual(self.calls, [])

    def test_virtual_display_node_refuses_entire_batch(self):
        steps = self.taps(1) + [{"kind": "tap", "node": "h1.tree.0000.digest", "expect": "digit"}]
        rc, out, _ = self.run_batch(steps, display=4)
        self.assertEqual((rc, out["stopped_at"], out["stop_reason_code"]),
                         (2, 0, "BATCH_UNSUPPORTED"))
        self.assertEqual(self.calls, [])

    def test_unsupported_types_limit_and_disabled_refuse_before_dispatch(self):
        for steps, code in ((self.taps(1) + [{"kind": "type", "text": "hello"}], "BATCH_UNSUPPORTED"),
                            (self.taps(1) + [{"kind": "key", "code": 66}], "BATCH_UNSUPPORTED"),
                            (self.taps(1) + [{"kind": "swipe"}], "BATCH_UNSUPPORTED"),
                            (self.taps(7), "BATCH_LIMIT")):
            with self.subTest(code=code):
                rc, out, _ = self.run_batch(steps)
                self.assertEqual((rc, out["stopped_at"], out["stop_reason_code"]), (2, 0, code))
                self.assertEqual(self.calls, [])
        with patch.object(st, "cfg_get", return_value=False):
            rc, out, stderr = self.run_batch(self.taps())
        self.assertEqual((rc, out["stop_reason_code"]), (2, "BATCH_DISABLED"))
        self.assertIn("请由 owner 开启", stderr)
        self.assertNotIn("config set", stderr)
        self.assertEqual(self.calls, [])

    def test_cancel_stops_before_second_tap(self):
        with patch.object(st, "_ocr_cancel_check", return_value=lambda: bool(self.calls)):
            rc, out, _ = self.run_batch(self.taps())
        self.assertEqual((rc, out["stopped_at"], out["stop_reason_code"]),
                         (2, 2, "BATCH_CANCELLED"))
        self.assertEqual(len(self.calls), 1)

    def test_cancel_immediately_after_look_stops_before_second_tap(self):
        cancelled = [False]
        def look_then_cancel(*args):
            rc = REAL_LOOK(*args)
            cancelled[0] = True
            return rc
        with patch.object(st, "_ocr_cancel_check", return_value=lambda: cancelled[0]), \
                patch.object(st, "_look_locked", side_effect=look_then_cancel):
            rc, out, _ = self.run_batch(self.taps())
        self.assertEqual((rc, out["stopped_at"], out["stop_reason_code"]),
                         (2, 2, "BATCH_CANCELLED"))
        self.assertEqual(len(self.observations), 1)
        self.assertEqual(len(self.calls), 1)

    def test_cancel_before_dispatch_stops_without_second_tap(self):
        cancelled = [False]
        original_grab = st.grab
        def grab_then_cancel(path, **kwargs):
            result = original_grab(path, **kwargs)
            if "r3-" in path and len(self.calls) == 1:
                cancelled[0] = True
            return result
        with patch.object(st, "_ocr_cancel_check", return_value=lambda: cancelled[0]), \
                patch.object(st, "grab", side_effect=grab_then_cancel):
            rc, out, _ = self.run_batch(self.taps())
        self.assertEqual((rc, out["stopped_at"], out["stop_reason_code"]),
                         (2, 2, "BATCH_CANCELLED"))
        self.assertEqual(len(self.calls), 1)
        second = next(r for r in self.rows() if r.get("kind") == "tap" and r.get("batch_index") == 2)
        self.assertEqual(second["dispatch_status"], "not_submitted")
        self.assertEqual(second["reason_code"], "BATCH_CANCELLED")

    def test_dispatch_failure_stops(self):
        with patch.object(st, "sh", side_effect=st.ActionDispatchError("adb_nonzero")):
            rc, out, _ = self.run_batch(self.taps())
        self.assertEqual((rc, out["stopped_at"], out["stop_reason_code"]), (2, 1, "OP_FAILED"))

    def test_stop_audit_is_structural_and_prune_runs_once(self):
        self.next_window = 11
        pruned = []
        with patch.object(st, "_prune_after_look", side_effect=pruned.append):
            rc, out, _ = self.run_batch(self.taps())
        self.assertEqual(rc, 2)
        stop = next(r for r in self.rows() if r.get("kind") == "batch_stop")
        self.assertEqual(stop["reason_code"], "BATCH_WINDOW_CHANGED")
        self.assertEqual(stop["batch_index"], 2)
        self.assertEqual(stop["batch_id"], out["batch_id"])
        self.assertFalse(any(k in stop for k in ("reason", "expect", "text")))
        self.assertEqual(len(pruned), 1)
        self.assertEqual(len(pruned[0]), 2)

    def test_preflight_and_shot_mismatch_do_not_prune_current_shot(self):
        current = st.load_state()["last_shot"]["path"]
        with patch.object(st, "_prune_after_look") as prune:
            for steps, shot in ((self.taps(), "wrong"), (self.taps(7), "one")):
                rc, _, _ = self.run_batch(steps, shot=shot)
                self.assertEqual(rc, 2)
            prune.assert_not_called()
        self.assertTrue(os.path.isfile(current))

    def test_limit_stop_records_requested_size(self):
        rc, out, _ = self.run_batch(self.taps(7))
        self.assertEqual((rc, out["batch_size"]), (2, 7))
        stop = next(r for r in self.rows() if r.get("kind") == "batch_stop")
        self.assertEqual(stop["batch_size"], 7)

    def test_look_and_state_errors_write_structural_stop(self):
        def broken_observe(**kwargs):
            raise RuntimeError("private observation detail")
        with patch.object(st, "observe", side_effect=broken_observe):
            rc, out, _ = self.run_batch(self.taps())
        self.assertEqual((rc, out["stopped_at"], out["stop_reason_code"]), (2, 2, "OP_FAILED"))
        stop = [r for r in self.rows() if r.get("kind") == "batch_stop"][-1]
        self.assertNotIn("reason", stop)
        self.assertNotIn("private observation detail", json.dumps(stop))
        with patch.object(st, "load_state", side_effect=RuntimeError("private state detail")):
            rc, out, _ = self.run_batch(self.taps())
        self.assertEqual((rc, out["stopped_at"], out["stop_reason_code"]), (2, 0, "OP_FAILED"))
        self.assertEqual([r.get("kind") for r in self.rows()].count("batch_stop"), 2)

    def test_batch_reason_classes_are_disjoint(self):
        vocabulary = task_report.VOCABULARY
        gates = set(vocabulary["gate_reason_codes"])
        stops = set(vocabulary["batch_stop_reason_codes"])
        self.assertFalse(gates & stops)
        self.assertTrue({"BATCH_DISABLED", "BATCH_LIMIT", "BATCH_UNSUPPORTED",
                         "BATCH_STEP_INVALID"} <= gates)
        self.assertEqual(stops, {"BATCH_NO_REACTION", "BATCH_CANCELLED", "BATCH_WINDOW_CHANGED"})


if __name__ == "__main__":
    unittest.main()
