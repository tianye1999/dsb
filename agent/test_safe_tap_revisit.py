"""Offline look hints; device surfaces use the existing batch fixture."""
import contextlib
import ast
import builtins
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import time
import types
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw

import safe_tap as st
import test_safe_tap_batch as fixture

REAL_BUILD_ZONES = st.build_zones


class Revisit(unittest.TestCase):
    setUp = fixture.Batch.setUp
    reset = fixture.Batch.reset
    rows = fixture.Batch.rows
    run_batch = fixture.Batch.run_batch
    taps = staticmethod(fixture.Batch.taps)

    def look(self, **kwargs):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = st.cmd_look(types.SimpleNamespace(note="", **kwargs))
        self.assertEqual(rc, 0)
        return json.loads(out.getvalue()), err.getvalue()

    def action(self):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return st.cmd_tap(types.SimpleNamespace(
                shot=st.load_state()["last_shot"]["id"], x=400, y=900,
                display=0, fast=False, ack_no_zones=False, expect=None, expect_activity=None))

    def test_same_frame_requires_completed_action(self):
        self.assertIsNone(self.look()[0]["revisit"])
        self.assertIsNone(self.look()[0]["revisit"])
        self.assertEqual(self.action(), 0)
        out, err = self.look()
        self.assertEqual(out["revisit"], {"kind": "unchanged", "similar_to_step": 2,
                                          "steps_ago": 1, "distance": 0})
        self.assertIn("上一步动作后画面几乎没变", err)

    def test_a_b_a_and_nearest_earlier_match(self):
        with patch.object(st, "_shot_dhash", side_effect=["0" * 16, "f" * 16, "0" * 16,
                                                         "f" * 16, "0" * 16]):
            self.look()
            self.assertIsNone(self.look()[0]["revisit"])
            out, err = self.look()
            self.assertEqual(out["revisit"], {"kind": "revisit", "similar_to_step": 1,
                                              "steps_ago": 2, "distance": 0, "times": 1})
            self.assertIn("可能在兜圈", err)
            self.assertIn("此前已出现 1 次", err)
            self.assertIn("如果是有意返回", err)
            self.assertIn("可忽略", err)
            self.look()
            hint = self.look()[0]["revisit"]
            self.assertEqual(hint["similar_to_step"], 3)
            self.assertEqual(hint["times"], 2)

    def test_revisit_threshold_plus_one_does_not_hint(self):
        for bits in (8, 9):
            self.reset()
            with patch.object(st, "_shot_dhash", side_effect=["0" * 16, "f" * 16,
                                                             f"{(1 << bits) - 1:016x}"]):
                self.look()
                self.look()
                hint = self.look()[0]["revisit"]
            if bits == 8:
                self.assertEqual(hint["distance"], 8)
                self.assertEqual(hint["times"], 1)
            else:
                self.assertIsNone(hint)

    def test_repeated_reads_never_report_loop(self):
        for _ in range(4):
            out, err = self.look()
            self.assertIsNone(out["revisit"])
            self.assertNotIn("可能在兜圈", err)

    def test_failed_dispatch_reservation_is_not_completion(self):
        self.look()
        with patch.object(st, "sh", side_effect=st.ActionDispatchError("failed", "unknown")):
            self.assertEqual(self.action(), 3)
        self.assertIsNone(self.look()[0]["revisit"])

    def test_distance_threshold_and_override(self):
        for threshold, bits, expected in ((8, 8, True), (8, 9, False), (9, 9, True),
                                          ("bad", 8, True), (65, 9, False), (True, 9, False)):
            with self.subTest(threshold=threshold, bits=bits):
                self.reset()
                original_cfg = st.cfg_get
                with patch.object(st, "_shot_dhash", side_effect=["0" * 16, f"{(1 << bits) - 1:016x}"]), \
                        patch.object(st, "cfg_get", side_effect=lambda key, default=None,
                                     threshold=threshold, original_cfg=original_cfg:
                                     threshold if key == "look.revisit_threshold" else original_cfg(key, default)):
                    self.look()
                    self.assertEqual(self.action(), 0)
                    hint = self.look()[0]["revisit"]
                self.assertEqual(hint is not None, expected)
                if hint:
                    self.assertEqual(hint["distance"], bits)

    def test_task_generation_unknown_and_display_isolation(self):
        self.look()
        for fields in ({"task_id": "other"}, {"task_id": fixture.TASK_ID, "task_generation": "new"},
                       {"task_context": "unknown"}):
            with patch.object(st.task_context, "audit_fields", return_value=fields):
                self.assertIsNone(self.look()[0]["revisit"])
        self.look()
        state = st.load_state()
        state["look_history"]["display_id"] = 7
        for row in state["look_history"]["entries"]:
            row["display_id"] = 7
        st.save_state(state)
        self.assertIsNone(self.look()[0]["revisit"])
        self.assertEqual(len(st.load_state()["look_history"]["entries"]), 2)

    def test_history_bound_and_step_count(self):
        for _ in range(23):
            self.look()
        history = st.load_state()["look_history"]
        self.assertEqual(history["step"], 23)
        self.assertEqual(len(history["entries"]), 20)
        self.assertEqual(history["entries"][0]["step"], 4)
        self.assertEqual(set(history["entries"][0]), {"step", "dhash", "display_id", "ts"})

    def test_successful_virtual_looks_do_not_compare_other_display(self):
        real_observe = st.observe

        def observe(**kwargs):
            return {**real_observe(**kwargs), "displayId": kwargs["display_id"],
                    "appWindows": 1, "displayCoverage": "tree"}

        with patch.object(st, "observe", side_effect=observe), \
                patch.object(st, "_foregrounds", return_value=("test/.Page", "test/.Page")), \
                patch.object(st, "_screen_size_on", return_value=(720, 1600)), \
                patch.object(st.vd, "verify", return_value={"ok": True, "session": {
                    "displayId": 7, "width": 720, "height": 1600, "package": "test", "sid": "virtual"}}):
            self.look()
            self.assertIsNone(self.look(display=7)[0]["revisit"])
            self.assertEqual(st.load_state()["look_history"]["display_id"], 7)
            hint = self.look()[0]["revisit"]
            self.assertEqual(hint, {"kind": "revisit", "similar_to_step": 1,
                                    "steps_ago": 2, "distance": 0, "times": 1})
            self.assertEqual(st.load_state()["look_history"]["step"], 3)
            self.assertEqual([row["display_id"] for row in st.load_state()["look_history"]["entries"]],
                             [0, 7, 0])
            with patch.object(st, "_shot_dhash", return_value="f" * 16):
                self.look()
            self.assertEqual(self.look()[0]["revisit"]["similar_to_step"], 3)
            for i in range(20):
                self.look(display=7 if i % 2 else 0)
            entries = st.load_state()["look_history"]["entries"]
            self.assertEqual(len(entries), 20)
            self.assertEqual({row["display_id"] for row in entries}, {0, 7})

    def test_unhashable_look_counts_but_never_compares(self):
        with patch.object(st, "_shot_dhash", side_effect=["0" * 16, None, "0" * 16]):
            self.look()
            self.assertIsNone(self.look()[0]["revisit"])
            self.assertEqual(self.look()[0]["revisit"]["steps_ago"], 2)
        self.assertEqual([row["step"] for row in st.load_state()["look_history"]["entries"]], [1, 3])

    def test_malformed_state_is_discarded_without_echo(self):
        self.look()
        valid = st.load_state()["look_history"]
        bad_values = [None, [], "private screen text", 1]
        for key, value in (("entries", valid["entries"] * 21), ("entries", {}),
                           ("step", True), ("step", 2**60), ("step", 2**53 - 1),
                           ("step", 1 + st.LOOK_HISTORY_LIMIT + 1),
                           ("display_id", "private screen text"),
                           ("task_id", ["private screen text"]), ("task_generation", "x" * 129)):
            bad_values.append({**valid, key: value})
        for key, value in (("dhash", "private screen text"), ("dhash", "f" * 17), ("dhash", []),
                           ("ts", float("nan")), ("ts", "private screen text"), ("step", -1),
                           ("display_id", False), ("unexpected", "private screen text")):
            bad_values.append({**valid, "entries": [{**valid["entries"][0], key: value}]})
        for bad in bad_values:
            with self.subTest(bad=bad):
                state = st.load_state()
                state["look_history"] = bad
                st.save_state(state)
                out, err = self.look()
                self.assertIsNone(out["revisit"])
                self.assertNotIn("private screen text", json.dumps(out) + err)
                self.assertEqual(st.load_state()["look_history"]["step"], 1)

    def test_history_rejects_out_of_order_steps_and_timestamps(self):
        self.look()
        self.look()
        valid = st.load_state()["look_history"]
        bad_entries = [list(reversed(valid["entries"])),
                       [valid["entries"][0], {**valid["entries"][1], "step": 1}],
                       [valid["entries"][0], {**valid["entries"][1], "ts": 1}]]
        for entries in bad_entries:
            state = st.load_state()
            state["look_history"] = {**valid, "entries": entries}
            st.save_state(state)
            self.assertIsNone(self.look()[0]["revisit"])
            self.assertEqual(st.load_state()["look_history"]["step"], 1)

    def test_missing_pil_bad_image_and_black_frame_keep_look_working(self):
        real_import = builtins.__import__

        def no_pil(name, *args, **kwargs):
            if name == "PIL":
                raise ImportError("unavailable")
            return real_import(name, *args, **kwargs)

        def bad_image(obs, dest):
            Path(dest).write_bytes(b"broken jpeg")
            return True

        real_observe = st.observe

        def black(**kwargs):
            return {**real_observe(**kwargs), "black": True}

        for fault in (patch.object(builtins, "__import__", side_effect=no_pil),
                      patch.object(st, "_save_observed_jpg", side_effect=bad_image),
                      patch.object(st, "observe", side_effect=black)):
            self.reset()
            with fault:
                out, _ = self.look()
            self.assertIsNone(out["revisit"])
            self.assertIsNone(st.load_state()["last_shot"]["shot_dhash"])
            self.assertEqual(st.load_state()["look_history"]["entries"], [])

    def test_hash_or_history_exception_still_saves_credential_once_under_lock(self):
        original_save = st.save_state
        held = []

        @contextlib.contextmanager
        def lock():
            self.assertFalse(held)
            held.append(True)
            try:
                yield
            finally:
                held.pop()

        def save(state):
            self.assertTrue(held)
            original_save(state)

        for helper in ("_shot_dhash", "_look_history"):
            self.reset()
            self.look()
            with patch.object(st, "lock", lock), patch.object(st, "save_state", side_effect=save) as saved, \
                    patch.object(st, helper, side_effect=RuntimeError("private screen text")):
                out, err = self.look()
            self.assertEqual(saved.call_count, 1)
            self.assertIsNone(out["revisit"])
            self.assertEqual(err.count("revisit 未计算：状态不可用"), 1)
            self.assertNotIn("private screen text", err)
            state = st.load_state()
            self.assertEqual(state["last_shot"]["id"], out["shot_id"])
            self.assertNotIn("shot_dhash", state["last_shot"])
            self.assertNotIn("look_history", state)

    def test_external_and_internal_look_save_once(self):
        for internal in (False, True):
            with patch.object(st, "save_state", wraps=st.save_state) as saved:
                self.look(_batch_internal=internal)
            self.assertEqual(saved.call_count, 1)

    def test_prepare_is_read_only_and_never_loads_or_saves_state(self):
        self.look()
        state = st.load_state()
        before = copy.deepcopy(state)
        with patch.object(st, "load_state", side_effect=AssertionError("extra load")), \
                patch.object(st, "save_state", side_effect=AssertionError("extra save")):
            st._prepare_look_hash(state, state["last_shot"]["path"], False, 0, 0)
        self.assertEqual(state, before)

    def test_internal_hash_exception_still_saves_credential_once(self):
        self.look()
        with patch.object(st, "_shot_dhash", side_effect=RuntimeError("private text")), \
                patch.object(st, "save_state", wraps=st.save_state) as saved:
            out, err = self.look(_batch_internal=True)
        self.assertEqual(saved.call_count, 1)
        self.assertIsNone(out["revisit"])
        self.assertNotIn("private text", err)
        state = st.load_state()
        self.assertEqual(state["last_shot"]["id"], out["shot_id"])
        self.assertNotIn("shot_dhash", state["last_shot"])
        self.assertNotIn("look_history", state)

    def test_long_unhashable_gap_expires_hashes_not_task_counter(self):
        self.look()
        with patch.object(st, "_shot_dhash", return_value=None):
            for _ in range(st.LOOK_HISTORY_LIMIT + 1):
                self.look()
        state = st.load_state()
        self.assertEqual(state["look_history"]["step"], 22)
        self.assertEqual(state["look_history"]["entries"], [])
        self.assertIsNone(self.look()[0]["revisit"])
        self.assertEqual(st.load_state()["look_history"]["step"], 23)

    def test_screencap_fallback_hashes_credential(self):
        with patch.object(st, "observe", return_value=None):
            out, _ = self.look()
        self.assertFalse(out["shot_black"])
        self.assertEqual(out["shot_src"], "screencap")
        self.assertRegex(st.load_state()["last_shot"]["shot_dhash"], "^[0-9a-f]{16}$")
        self.assertEqual(len(st.load_state()["look_history"]["entries"]), 1)

    def test_batch_does_not_count_or_hint_but_audits_hash(self):
        self.look()
        previous = st.load_state()["look_history"]
        saved_counts = []

        def internal_look(*args, **kwargs):
            with patch.object(st, "save_state", wraps=st.save_state) as saved:
                rc = fixture.REAL_LOOK(*args, **kwargs)
            saved_counts.append(saved.call_count)
            return rc

        with patch.object(st, "_look_locked", side_effect=internal_look):
            rc, _, err = self.run_batch(self.taps(), shot=st.load_state()["last_shot"]["id"])
        self.assertEqual(rc, 0)
        self.assertEqual(saved_counts, [1, 1])
        self.assertEqual(st.load_state()["look_history"], previous)
        self.assertNotIn("可能在兜圈", err)
        self.assertNotIn("上一步动作后画面几乎没变", err)
        actions = [row for row in self.rows() if row["kind"] == "tap"]
        self.assertEqual(len(actions), 3)
        self.assertTrue(all(st._valid_dhash(row["shot_dhash"]) for row in actions))

    def test_audit_hash_validated_and_only_additive_field(self):
        self.look()
        state = st.load_state()
        rec = {"kind": "tap", "shot": state["last_shot"]["id"], "allowed": True}
        for value in (state["last_shot"]["shot_dhash"], "private screen text", [], "f" * 17):
            state["last_shot"]["shot_dhash"] = value
            st.save_state(state)
            st.audit(rec)
            row = self.rows()[-1]
            self.assertEqual(row["shot_dhash"], value if st._valid_dhash(value) else None)
            self.assertEqual(set(row) - set(rec), {"shot_dhash", "task_id", "task_name", "ts", "ts_local"})
            self.assertNotIn("private screen text", json.dumps(row))

    def test_status_bar_ignored_and_hash_is_64_bit(self):
        for extension in ("png", "jpg"):
            image = Image.new("RGB", (1080, 2400), "white")
            draw = ImageDraw.Draw(image)
            draw.rectangle((150, 400, 700, 1400), fill="black")
            path = os.path.join(st.HERE, "bar." + extension)
            image.save(path)
            before = st._shot_dhash(path)
            draw.rectangle((0, 0, 1079, 95), fill="black")
            image.save(path)
            self.assertEqual(st._shot_dhash(path), before)
            self.assertRegex(before, "^[0-9a-f]{16}$")
            self.assertIsNone(st._shot_dhash(path, True))

    def test_audit_mismatched_shot_does_not_reuse_hash(self):
        self.look()
        st.audit({"kind": "tap", "shot": "wrong-shot", "allowed": False})
        self.assertIsNone(self.rows()[-1]["shot_dhash"])

    def test_old_pillow_without_resampling_enum(self):
        from PIL import JpegImagePlugin

        path = os.path.join(st.HERE, "old-pillow.jpg")
        fixture.frame(path)
        expected = st._shot_dhash(path)
        legacy_image = types.SimpleNamespace(open=Image.open, LANCZOS=Image.Resampling.LANCZOS)
        real_import = builtins.__import__

        def import_legacy(name, *args, **kwargs):
            if name == "PIL":
                return types.SimpleNamespace(Image=legacy_image, JpegImagePlugin=JpegImagePlugin)
            return real_import(name, *args, **kwargs)

        with patch.object(builtins, "__import__", side_effect=import_legacy):
            self.assertEqual(st._shot_dhash(path), expected)

    def test_gate_comparison_skips_when_origin_main_unavailable(self):
        with patch.object(subprocess, "check_output", side_effect=subprocess.CalledProcessError(128, "git")):
            with self.assertRaisesRegex(unittest.SkipTest, "origin/main.*unavailable"):
                self.test_gate_inputs_match_origin_main()

    def test_gate_inputs_match_origin_main(self):
        try:
            source = subprocess.check_output(["git", "show", "origin/main:agent/safe_tap.py"],
                                             encoding="utf-8", stderr=subprocess.PIPE)
        except (subprocess.CalledProcessError, FileNotFoundError):
            self.skipTest("origin/main unavailable; baseline comparison requires the base ref (shallow CI clone)")
        original_tree = ast.parse(source)
        current_tree = ast.parse(Path(st.__file__).read_text(encoding="utf-8"))
        original = {node.name: node for node in original_tree.body if isinstance(node, ast.FunctionDef)}
        current = {node.name: node for node in current_tree.body if isinstance(node, ast.FunctionDef)}
        for name in ("observe", "_nodes_from", "dump_nodes", "dump_nodes_app", "dump_nodes_uiautomator",
                     "build_zones", "check_gate", "_ocr_danger_scan", "_target_key"):
            self.assertEqual(ast.dump(original[name]), ast.dump(current[name]), name)
        module = ast.Module(body=[original["_look_locked"]], type_ignores=[])
        namespace = {}
        exec(compile(module, "origin/main:agent/safe_tap.py", "exec"), st.__dict__, namespace)
        baseline_look = namespace["_look_locked"]
        real_observe = st.observe
        now = time.time()
        for nodes, target, ocr in (([], None, False), ([{"text": "删除", "rect": [10, 10, 80, 80]}], None, False),
                                   ([{"text": "Ready", "rect": [100, 800, 700, 1000]}], "Ready", False),
                                   ([], "missing", True)):
            def observe(nodes=nodes, **kwargs):
                return {**real_observe(**kwargs), "nodes": copy.deepcopy(nodes)}

            results = []
            with patch.object(st, "observe", side_effect=observe), patch.object(st.time, "time", return_value=now), \
                    patch.object(st, "build_zones", REAL_BUILD_ZONES), \
                    patch.object(st, "_ocr_cancel_check", return_value=lambda: False), \
                    patch.object(st, "OCR_OK", True), patch.object(st, "pixel_recognize_result",
                         return_value={"status": "empty", "rows": []}) as recognize:
                for look in (baseline_look, fixture.REAL_LOOK):
                    self.reset()
                    recognize.reset_mock()
                    with patch.object(st, "_look_locked", look):
                        out, _ = self.look(target=target, ocr=ocr)
                    out.pop("revisit", None)
                    credential = st.load_state()["last_shot"]
                    credential.pop("shot_dhash", None)
                    results.append((out, credential, recognize.call_args_list))
            self.assertEqual(results[0], results[1])


if __name__ == "__main__":
    unittest.main()
