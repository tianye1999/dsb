"""派发前目标复核（R3）与 type 剪贴板回执（2026-10-21）。全部离线：不碰设备。"""
import base64
import contextlib
import hashlib
import io
import json
import os
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw

import safe_tap as st
import test_safe_tap_credentials as fixture
import task_report


def _img(path, rows):
    """画一张 720x1600 白底图，rows = {y: 文字块宽度}，模拟列表行内容。"""
    im = Image.new("RGB", (720, 1600), "white")
    d = ImageDraw.Draw(im)
    for y, wid in rows.items():
        d.rectangle((40, y - 20, 40 + wid, y + 20), fill="black")
    im.save(path, quality=90)
    return path


class R3Verify(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.cred = _img(os.path.join(self.dir, "cred.jpg"), {800: 300, 950: 500})
        p = patch.object(st, "SHOTS", self.dir)
        p.start()
        self.addCleanup(p.stop)

    def verify(self, fresh_rows=None, fresh_size=(720, 1600), capture=True, pt=(360, 800)):
        def fake_grab(path, force=False, display_id=0):
            if not capture:
                return None
            im = Image.new("RGB", fresh_size, "white")
            d = ImageDraw.Draw(im)
            for y, wid in (fresh_rows or {}).items():
                d.rectangle((40, y - 20, 40 + wid, y + 20), fill="black")
            im.save(path, quality=90)
            return path
        with patch.object(st, "grab", fake_grab):
            return st._r3_verify(self.cred, 7, pt)

    def test_unchanged_target_row_passes(self):
        r = self.verify({800: 300, 950: 500})
        self.assertTrue(r["ok"], r)

    def test_reordered_target_row_refused(self):
        # 行重排：目标行内容换成了别的长度（另一个群名）
        r = self.verify({800: 520, 950: 300})
        self.assertFalse(r["ok"], r)
        self.assertGreater(r["diff"], st.R3_MAX_DIFF)

    def test_change_outside_target_band_ignored(self):
        r = self.verify({800: 300, 950: 500, 200: 400})   # 顶部别处变了
        self.assertTrue(r["ok"], r)

    def test_capture_failure_refused(self):
        r = self.verify(capture=False)
        self.assertFalse(r["ok"])
        self.assertEqual(r["why"], "capture_failed")

    def test_size_mismatch_refused(self):
        r = self.verify({800: 300}, fresh_size=(1260, 2800))
        self.assertFalse(r["ok"])

    def test_missing_credential_image_refused(self):
        r = st._r3_verify(os.path.join(self.dir, "nope.jpg"), 7, (360, 800))
        self.assertFalse(r["ok"])
        self.assertEqual(r["why"], "no_credential_image")

    def test_fresh_frame_not_left_behind(self):
        self.verify({800: 300, 950: 500})
        self.assertEqual([f for f in os.listdir(self.dir) if f.startswith("r3-")], [])


class R3Wiring(fixture.Credentials):
    """do_action 里 R3 不一致 → 拒绝、零派发、凭据不消费；R3 只管 tap/long_press。"""

    def test_mismatch_refuses_without_dispatch_or_consumption(self):
        for kind in ("tap", "long_press"):
            with self.subTest(kind=kind):
                self.reset()
                with patch.object(st, "_r3_verify", return_value={"ok": False, "diff": 30.0, "why": "diff=30.0>6.0"}):
                    rc = self.action(kind, [(600, 900), (600, 900)])
                self.assertEqual(rc, 2)
                self.assertEqual(self.calls, [])
                self.assertNotIn("consumed_at", st.load_state()["last_shot"])

    def test_swipe_and_key_do_not_run_r3(self):
        for kind, pts in (("swipe", [(600, 900), (600, 500)]), ("key", [4])):
            with self.subTest(kind=kind):
                self.reset()
                with patch.object(st, "_r3_verify", side_effect=AssertionError("R3 only for tap/long_press")):
                    self.assertEqual(self.action(kind, pts), 0)


class R3Classification(fixture.Credentials):
    original = {"rect": [500, 850, 700, 950], "cls": "Button", "viewId": "app:id/target",
                "text": "private target text", "clickable": True, "depth": 2}

    def prepare(self, nodes=None):
        self.cred = os.path.join(st.SHOTS, "shot-123.jpg")
        Path(self.cred).touch()
        state = st.load_state()
        state["last_shot"].update(path=self.cred, window_id=10)
        st.save_state(state)
        st._save_target_index(self.cred, nodes if nodes is not None else [self.original])

    def reject(self, fresh, category, evidence, message, index=True, old_nodes=None, fetch=True):
        self.prepare(old_nodes)
        if isinstance(fresh, dict):
            fresh = {"fg": "test", "windowId": 10, **fresh}
        if not index:
            os.unlink(self.cred + ".nodes.json")
        rows, err = [], io.StringIO()
        args = types.SimpleNamespace(shot="one", fast=True, display=0)
        with patch.object(st, "_r3_verify", return_value={"ok": False, "diff": 30., "why": "diff=30.0>6.0"}), \
                patch.object(st, "observe", **({"side_effect": fresh} if isinstance(fresh, Exception)
                                              else {"return_value": fresh})) as tree, \
                patch.object(st, "audit", side_effect=rows.append), contextlib.redirect_stderr(err):
            self.assertEqual(st.do_action(args, "tap", [(600, 900)]), 2)
        self.assertEqual(self.calls, [])
        self.assertNotIn("consumed_at", st.load_state()["last_shot"])
        self.assertEqual(len(rows), 1)
        rec = rows[0]
        self.assertEqual(rec["r3_category"], category)
        self.assertEqual(rec["r3_evidence"], evidence)
        self.assertTrue(rec["reason"].startswith("派发前目标所在行与观测时不一致"))
        self.assertTrue(err.getvalue().startswith("✗ 闸门拒绝：派发前目标所在行与观测时不一致"))
        self.assertIn(message, err.getvalue())
        self.assertEqual(task_report.reason_code_for(rec), "R3_CHANGED")
        incident = st.load_state()["open_incident"]
        self.assertEqual(incident["r3_category"], category)
        for payload in (json.dumps(rec), json.dumps(incident)):
            private_texts = [self.original["text"], "private blocker text beyond twenty chars"]
            private_texts.extend(n[k] for n in old_nodes or [] for k in ("text", "desc") if n.get(k))
            for text in private_texts:
                self.assertNotIn(text, payload)
                self.assertNotIn(text[:20], payload)
                self.assertNotIn(hashlib.sha256(text.encode()).hexdigest()[:16], payload)
            self.assertNotIn("text_sha256", payload)
            self.assertNotIn("desc_sha256", payload)
        if index and fetch:
            tree.assert_called_once_with(with_image=False, display_id=0, timeout=1.0, transport_timeout=2.0)
        else:
            tree.assert_not_called()
        return err.getvalue()

    def test_shifted(self):
        self.reject({"nodes": [{**self.original, "rect": [700, 850, 900, 950]}]},
                    "target_shifted", {"center": [800, 900], "displacement_px": 200.0},
                    "目标已移到 (800, 900) 附近")

    def test_description_only_target_shifted(self):
        old = {"rect": [500, 850, 700, 950], "cls": "ImageButton", "clickable": True,
               "text": "", "desc": "private icon description"}
        self.reject({"nodes": [{**old, "rect": [700, 850, 900, 950]}]},
                    "target_shifted", {"center": [800, 900], "displacement_px": 200.0},
                    "目标已移到", old_nodes=[old])
        raw = Path(self.cred + ".nodes.json").read_text()
        self.assertNotIn(old["desc"], raw)
        self.assertEqual(json.loads(raw), [{"rect": old["rect"], "cls": "ImageButton", "clickable": True,
                         "desc_sha256": hashlib.sha256(old["desc"].encode()).hexdigest()[:16]}])

    def test_description_only_target_still_at_point_is_not_disappeared(self):
        old = {"rect": [500, 850, 700, 950], "desc": "private icon description"}
        self.reject({"nodes": [old]}, "unclassified", {}, "无法确认目标去向", old_nodes=[old])

    def test_target_without_identity_is_unclassified(self):
        old = {"rect": [500, 850, 700, 950], "cls": "ImageButton", "clickable": True}
        self.reject({"nodes": [self.original]}, "unclassified", {}, "无法确认目标去向",
                    old_nodes=[old], fetch=False)

    def test_text_and_description_match_do_not_stack(self):
        old = {"rect": [500, 850, 700, 950], "text": "private label", "desc": "private description"}
        # No matching size/point/ID: both hashes together must still contribute only 0.4.
        self.reject({"nodes": [{**old, "rect": [800, 850, 950, 950]}]},
                    "unclassified", {}, "无法确认目标去向", old_nodes=[old])

    def test_invalid_fresh_nodes_are_skipped(self):
        self.reject({"nodes": [{}, None, {"rect": [0, 0, 0, 0]},
                               {**self.original, "rect": [700, 850, 900, 950]}]},
                    "target_shifted", {"center": [800, 900], "displacement_px": 200.0}, "目标已移到")

    def test_invalid_node_depth_boundary_keeps_original_indexes(self):
        blocker = {"rect": [580, 880, 650, 930], "cls": "Dialog", "viewId": "app:id/popup",
                   "text": "private blocker text beyond twenty chars", "depth": 3}
        # The invalid sibling is still the boundary between target and blocker subtrees.
        self.reject({"nodes": [{"depth": 0}, self.original, {"depth": 2}, blocker]},
                    "target_occupied", {"cls": "Dialog", "resource-id": "app:id/popup",
                                        "rect": blocker["rect"]}, "目标被另一个元素挡住")

    def test_all_fresh_nodes_invalid_is_unclassified(self):
        self.reject({"nodes": [{}, None, {"rect": [0, 0, 0, 0]}]},
                    "unclassified", {}, "无法确认目标去向")

    def test_occupied(self):
        blocker = {"rect": [580, 880, 650, 930], "cls": "Dialog", "viewId": "app:id/popup",
                   "text": "private blocker text beyond twenty chars", "depth": 2}
        err = self.reject({"nodes": [self.original, blocker]}, "target_occupied",
                          {"cls": "Dialog", "resource-id": "app:id/popup", "rect": blocker["rect"]},
                          "目标被另一个元素挡住")
        self.assertIn(blocker["text"][:20], err)
        self.assertNotIn(blocker["text"][:21], err)

    def test_disappeared(self):
        self.reject({"nodes": [{"rect": [500, 850, 700, 950], "text": "unrelated", "depth": 2}]},
                    "target_disappeared", {}, "目标已不在页面上")

    def test_unclassified_without_index(self):
        self.reject(None, "unclassified", {}, "无法确认目标去向", index=False)

    def test_unclassified_without_tree(self):
        self.reject(None, "unclassified", {}, "无法确认目标去向")

    def test_unclassified_point_outside_index(self):
        self.reject(None, "unclassified", {}, "无法确认目标去向",
                    old_nodes=[{**self.original, "rect": [10, 10, 20, 20]}], fetch=False)

    def test_pass_does_not_fetch_tree(self):
        self.prepare()
        with patch.object(st, "_r3_classify") as classify, patch.object(st, "observe", return_value=None) as tree:
            self.assertEqual(self.action(), 0)
        classify.assert_not_called()
        # Existing post-dispatch reaction observation remains; no diagnostic tree request.
        tree.assert_called_once_with(with_image=False, display_id=0, foreground_out={})

    def test_related_or_unknown_hierarchy_is_not_occlusion(self):
        for depth in (3, None):
            self.reset()
            child = {"rect": [580, 880, 650, 930], "text": "child", "depth": depth}
            self.reject({"nodes": [self.original, child]}, "unclassified", {}, "无法确认目标去向")

    def test_equal_best_matches_are_unclassified(self):
        self.reject({"nodes": [self.original, self.original]}, "unclassified", {}, "无法确认目标去向")

    def test_smallest_target_preferred_over_clickable_ancestor(self):
        self.reject({"nodes": [{"rect": [790, 890, 810, 910], "text": "label"}]},
                    "target_shifted", {"center": [800, 900], "displacement_px": 200.0}, "目标已移到",
                    old_nodes=[self.original, {"rect": [590, 890, 610, 910], "text": "label"}])

    def test_clickable_breaks_equal_area_tie(self):
        self.reject({"nodes": [{**self.original, "rect": [700, 850, 900, 950]}]},
                    "target_shifted", {"center": [800, 900], "displacement_px": 200.0}, "目标已移到",
                    old_nodes=[{"rect": self.original["rect"], "text": "other"}, self.original])

    def test_changed_context_black_or_noninteractive_tree_is_unclassified(self):
        for fields in ({"fg": "another/.Page"}, {"windowId": 11}, {"black": True}, {"interactive": False}):
            self.reset()
            self.reject({"nodes": [self.original], **fields}, "unclassified", {}, "无法确认目标去向")

    def test_same_package_with_different_activity_is_valid_context(self):
        self.reject({"fg": "test/.Other", "nodes": [{**self.original, "rect": [700, 850, 900, 950]}]},
                    "target_shifted", {"center": [800, 900], "displacement_px": 200.0}, "目标已移到")

    def test_invisible_original_is_not_indexed(self):
        self.reject(None, "unclassified", {}, "无法确认目标去向",
                    old_nodes=[{**self.original, "visibleToUser": False}], fetch=False)
        self.assertEqual(json.loads(Path(self.cred + ".nodes.json").read_text()), [])

    def test_invisible_new_target_does_not_compete_with_visible_match(self):
        self.reject({"nodes": [{**self.original, "visibleToUser": False},
                               {**self.original, "rect": [700, 850, 900, 950]}]},
                    "target_shifted", {"center": [800, 900], "displacement_px": 200.0}, "目标已移到")

    def test_identity_match_with_resize_is_not_disappeared(self):
        for identity in ({"text": "private label"}, {"desc": "private description"}, {"viewId": "app:id/icon"}):
            self.reset()
            old = {"rect": [500, 850, 700, 950], **identity}
            self.reject({"nodes": [{**old, "rect": [800, 850, 950, 950]}]},
                        "unclassified", {}, "无法确认目标去向", old_nodes=[old])

    def test_wrong_display_and_tree_error_are_unclassified(self):
        for tree in ({"nodes": [self.original], "displayId": 7}, {"nodes": [], "coverage": "none"},
                     {"nodes": [self.original], "err": "private error"}, {"nodes": [{}]}):
            self.reset()
            self.reject(tree, "unclassified", {}, "无法确认目标去向")

    def test_virtual_display_tree_is_requested_once_without_fallback(self):
        self.prepare()
        with patch.object(st, "observe", return_value={"displayId": 7, "nodes": []}) as tree, \
                patch.object(st, "dump_nodes") as fallback:
            self.assertEqual(st._r3_classify(self.cred, 7, (600, 900)), ("unclassified", {}, ""))
        tree.assert_called_once_with(with_image=False, display_id=7, timeout=1.0, transport_timeout=2.0)
        fallback.assert_not_called()

    def test_tree_exception_still_refuses(self):
        self.reject(OSError("private tree failure"), "unclassified", {}, "无法确认目标去向")

    def test_rounded_diff_still_classifies_rejected_comparison(self):
        self.prepare()
        with patch.object(st, "_r3_verify", return_value={"ok": False, "diff": st.R3_MAX_DIFF,
                                                        "why": "diff=6.0>6.0"}), \
                patch.object(st, "_r3_classify", return_value=("unclassified", {}, "")) as classify:
            self.assertEqual(self.action(), 2)
        classify.assert_called_once()

    def test_write_failed_remains_observation_failure(self):
        self.prepare()
        with patch.object(st, "_r3_verify", return_value={"ok": False, "diff": None,
                                                        "why": "capture_failed", "write_failed": True}), \
                patch.object(st, "_r3_classify") as classify:
            self.assertEqual(self.action(), 3)
        classify.assert_not_called()
        self.assertEqual(self.calls, [])

    def test_uiautomator_retains_available_metadata_and_decodes_text(self):
        xml = ('<hierarchy><node text="A &amp; B" bounds="[500,850][700,950]" '
               'class="Button" resource-id="app:id/target" clickable="true" />'
               '<node text="" bounds="[0,0][900,1000]" /></hierarchy>')
        with patch.object(st, "sh", side_effect=["", xml]):
            nodes = st.dump_nodes_uiautomator()
        self.assertEqual(nodes, [{"text": "A &amp; B", "rect": [500, 850, 700, 950], "cx": 600, "cy": 900}])
        self.assertEqual(nodes.index_nodes[0]["text"], "A & B")
        self.assertEqual(nodes.index_nodes[0]["viewId"], "app:id/target")
        self.prepare(nodes.index_nodes)
        self.assertNotIn("A & B", Path(self.cred + ".nodes.json").read_text())

    def test_uiautomator_collects_description_only_nodes(self):
        xml = ('<hierarchy><node text="" content-desc="private icon &amp; label" '
               'bounds="[500,850][700,950]" class="ImageButton" clickable="true" /></hierarchy>')
        with patch.object(st, "sh", side_effect=["", xml]):
            nodes = st.dump_nodes_uiautomator()
        self.assertEqual(nodes, [])
        self.assertEqual(nodes.index_nodes[0]["desc"], "private icon & label")
        self.reject({"nodes": [{"rect": [700, 850, 900, 950], "desc": "private icon & label"}]},
                    "target_shifted", {"center": [800, 900], "displacement_px": 200.0},
                    "目标已移到", old_nodes=nodes.index_nodes)
        raw = Path(self.cred + ".nodes.json").read_text()
        self.assertNotIn("private icon", raw)
        self.assertEqual(json.loads(raw)[0]["desc_sha256"],
                         hashlib.sha256(b"private icon & label").hexdigest()[:16])

    def test_batch_anchor_failure_unchanged(self):
        self.prepare()
        args = types.SimpleNamespace(shot="one", fast=True, display=0, _batch_anchor=self.cred)
        with patch.object(st, "_r3_verify", return_value={"ok": False, "diff": 30., "why": "diff=30"}), \
                patch.object(st, "_r3_classify") as classify, patch.object(st, "audit") as audit, \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(st.do_action(args, "tap", [(600, 900)]), 2)
        classify.assert_not_called()
        self.assertNotIn("r3_category", audit.call_args.args[0])
        self.assertEqual(self.calls, [])

    def test_observation_failure_does_not_classify(self):
        self.prepare()
        for why in ("capture_failed", "pil_unavailable", "compare_failed:OSError", "size 1x1 vs 2x2"):
            with self.subTest(why=why), patch.object(st, "_r3_verify", return_value={
                    "ok": False, "diff": None, "why": why}), patch.object(st, "observe") as tree, \
                    patch.object(st, "audit") as audit:
                self.assertEqual(self.action(), 2)
                tree.assert_not_called()
                self.assertNotIn("r3_category", audit.call_args.args[0])

    def test_index_has_only_minimal_hashed_fields(self):
        self.prepare()
        raw = Path(self.cred + ".nodes.json").read_text()
        self.assertNotIn(self.original["text"], raw)
        self.assertEqual(json.loads(raw), [{"rect": self.original["rect"], "cls": "Button",
                         "resource-id": "app:id/target", "clickable": True,
                         "text_sha256": hashlib.sha256(self.original["text"].encode()).hexdigest()[:16]}])

    def test_look_index_write_failure_is_best_effort(self):
        output, err = io.StringIO(), io.StringIO()
        real_open = st.os.open

        def fail_index(path, *args, **kwargs):
            if ".nodes.json.tmp-" in str(path):
                raise OSError("private disk failure")
            return real_open(path, *args, **kwargs)

        with patch.object(st.os, "open", side_effect=fail_index), contextlib.redirect_stdout(output), \
                contextlib.redirect_stderr(err):
            self.assertEqual(st.cmd_look(types.SimpleNamespace(note="")), 0)
        self.assertIn("shot_id", json.loads(output.getvalue()))
        self.assertEqual(err.getvalue().count("目标索引未保存"), 1)
        self.assertNotIn("private disk failure", err.getvalue())


class TypeReceipt(unittest.TestCase):
    """_dispatch_type：没拿到 App 的 ok 回执就不发任何按键；清空结果如实上报。"""

    def run_type(self, text="测试", write=("ok", True), clear=True, clear_flag=True):
        sent, receipts = [], {}
        self.decoded = []

        def fake_sh(*argv, t=90, checked=False):
            cmd = " ".join(str(x) for x in argv)
            sent.append(cmd)
            if "#写:" in cmd:
                nonce, payload = cmd.split("#写:", 1)[1].split(":", 1)
                payload = payload.strip("'\" ")
                try:      # 像真 App 一样从广播里解码，编码/传参坏了这里就对不上
                    got = base64.b64decode(payload, validate=True).decode("utf-8")
                except Exception:
                    got = None
                if write[0] == "ok" and got is not None:
                    receipts[nonce] = {"nonce": nonce, "op": "write", "ok": write[1],
                                       "len": len(got.encode("utf-16-le")) // 2 + (write[2] if len(write) > 2 else 0)}
                self.decoded.append(got)
            elif "#清:" in cmd:
                nonce = cmd.split("#清:", 1)[1].strip("'\" ")
                receipts[nonce] = {"nonce": nonce, "op": "clear", "ok": clear}
            elif argv[:2] == ("shell", "cat"):
                n = os.path.basename(argv[2])[len("clip-"):-len(".json")]
                return json.dumps(receipts[n]) if n in receipts else ""
            return ""

        st._GATE_EVIDENCE.clear()
        a = types.SimpleNamespace(text=text, clear=clear_flag)
        with patch.object(st, "sh", fake_sh), patch.object(st.time, "sleep", lambda s: None):
            try:
                res = st._dispatch_type(7, a)
            except st.ActionDispatchError as e:
                res = e
        keys = [c for c in sent if "keyevent" in c or "keycombination" in c]
        return res, keys, sent

    def test_confirmed_write_pastes_and_clears(self):
        res, keys, sent = self.run_type()
        self.assertEqual(res["clipboard_cleared"], True)
        self.assertEqual(len(keys), 3)                     # 全选 + 删除 + 粘贴
        self.assertTrue(all(" -d 7 " in " " + k + " " for k in keys))
        self.assertTrue(any("#清:" in c for c in sent))

    def test_missing_receipt_sends_no_keys_but_still_clears(self):
        res, keys, sent = self.run_type(write=("none",))      # 走满 3s 截止
        self.assertIsInstance(res, st.ActionDispatchError)
        self.assertEqual(keys, [])
        self.assertTrue(any("#清:" in c for c in sent))

    def test_failed_or_length_mismatch_receipt_sends_no_keys(self):
        for write in (("ok", False), ("ok", True, 1)):
            with self.subTest(write=write):
                res, keys, _ = self.run_type(write=write)
                self.assertIsInstance(res, st.ActionDispatchError)
                self.assertEqual(keys, [])

    def test_emoji_length_counts_utf16_units(self):
        res, keys, _ = self.run_type(text="好😀")
        self.assertEqual(res["clipboard_cleared"], True)
        self.assertEqual(len(keys), 3)

    def test_clear_failure_reported(self):
        res, keys, _ = self.run_type(clear=False)
        self.assertIs(res["clipboard_cleared"], False)
        self.assertIs(st._GATE_EVIDENCE["clipboard_cleared"], False)

    def test_payload_is_base64_without_plaintext(self):
        res, keys, sent = self.run_type(text="我的 内容")
        write_cmd = [c for c in sent if "#写:" in c][0]
        self.assertNotIn("我的", write_cmd)
        self.assertEqual(self.decoded, ["我的 内容"])      # 载荷能原样解回（含空格）

    def test_broken_encoding_sends_no_keys(self):
        with patch.object(st.base64, "b64encode", return_value=b"%%%"):
            res, keys, _ = self.run_type()
        self.assertIsInstance(res, st.ActionDispatchError)
        self.assertEqual(keys, [])


if __name__ == "__main__":
    unittest.main()
