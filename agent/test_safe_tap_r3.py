"""派发前目标复核（R3）与 type 剪贴板回执（2026-10-21）。全部离线：不碰设备。"""
import base64
import json
import os
import tempfile
import types
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw

import safe_tap as st
import test_safe_tap_credentials as fixture


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
