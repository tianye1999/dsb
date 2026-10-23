#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""虚拟屏接入的**纯逻辑**单测（不碰真机、不点屏幕）。

覆盖三件最容易悄悄坏掉的事：
  1. 按显示器解析前台（有虚拟屏时，dumpsys 第一处 topResumedActivity 不一定是主屏）；
  2. safe_tap 的 displayId 绑定（跨屏凭据、过期会话、黑图、虚拟屏前台变化 → 必须拒绝）；
  3. 主屏老路径不受影响（display_id=0 不需要虚拟屏会话）。

跑法：python3 -m unittest discover -s /root/.dsh-phone/skills/tests -v
"""
import contextlib
import io
import json
import os
import re
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import Mock, patch

HERE = os.path.dirname(os.path.abspath(__file__))
SKILLS = os.path.dirname(HERE)
if SKILLS not in sys.path:
    sys.path.insert(0, SKILLS)

import safe_tap as st  # noqa: E402
import vd_session as vd  # noqa: E402


DUMP_ACTIVITY = """\
Display #36 (activities from top to bottom):
  * Task{aaa #3662 type=standard A=10xxx:com.obric.calculator}
    mResumedActivity: ActivityRecord{192753947 u0 com.obric.calculator/.Calculator t3662}
    topResumedActivity=ActivityRecord{192753947 u0 com.obric.calculator/.Calculator t3662}
Display #0 (activities from top to bottom):
  * Task{bbb #2274 type=standard A=10xxx:com.android.launcher3}
    topResumedActivity=ActivityRecord{65696116 u0 com.android.launcher3/com.obric.feature.ObricLauncher t2274}
"""

DUMP_DISPLAY = """\
  DisplayDeviceInfo{"内置屏幕": uniqueId="local:1", 1260 x 2800, state ON, type INTERNAL}
    mBaseDisplayInfo=DisplayInfo{"内置屏幕", displayId 0, displayGroupId 0, real 1260 x 2800, density 560, state ON, type INTERNAL, uniqueId "local:1"}
  DisplayDeviceInfo{"叠加视图 #1": uniqueId="overlay:1", 720 x 1600, state ON, type OVERLAY}
    mBaseDisplayInfo=DisplayInfo{"叠加视图 #1", displayId 36, displayGroupId 0, real 720 x 1600, density 320, state ON, type OVERLAY, uniqueId "overlay:1"}
"""

DUMP_WINDOW_DISPLAYS = """\
Display: mDisplayId=0
  InsetsSource type=mandatorySystemGestures frame=[0,2716][1260,2800]
Display: mDisplayId=36
"""


class FakeDevice:
    def __init__(self, ids=(0, 36), activity=DUMP_ACTIVITY, display=DUMP_DISPLAY):
        self.ids = list(ids)
        self.activity = activity
        self.display = display
        self.calls = []

    def adb(self, *args, **kw):
        self.calls.append(args)
        a = list(args)
        if a[:3] == ["shell", "cmd", "display"]:
            return "\n".join(str(i) for i in self.ids) + "\n"
        if a[:3] == ["shell", "dumpsys", "activity"] or a[:3] == ["shell", "dumpsys", "activity"]:
            return self.activity
        if a[:3] == ["shell", "dumpsys", "display"]:
            return self.display
        if a[:3] == ["shell", "settings", "get"]:
            return "null\n"
        return ""


class VdParseTests(unittest.TestCase):
    def setUp(self):
        self.dev = FakeDevice()
        self._orig = vd.adb
        vd.adb = self.dev.adb

    def tearDown(self):
        vd.adb = self._orig

    def test_display_ids(self):
        self.assertEqual(vd.display_ids(), [0, 36])

    def test_display_meta_by_logical_id(self):
        meta = vd.display_meta(36)
        self.assertEqual((meta["width"], meta["height"], meta["type"]), (720, 1600, "OVERLAY"))
        self.assertEqual(meta["uniqueId"], "overlay:1")

    def test_foreground_is_per_display(self):
        # 有虚拟屏时，全局第一处 topResumedActivity 是计算器 —— 主屏必须仍解析成桌面
        self.assertEqual(vd.foreground(36), "com.obric.calculator/.Calculator")
        self.assertEqual(vd.foreground(0), "com.android.launcher3/com.obric.feature.ObricLauncher")

    def test_foreground_missing_display_is_empty(self):
        self.assertEqual(vd.foreground(99), "")


class _GateBase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="vdgate-")
        self.tmp = tmp.name
        self.addCleanup(tmp.cleanup)
        self.windows = DUMP_WINDOW_DISPLAYS
        self.audits = []
        for name, value in {
            "HERE": self.tmp, "SHOTS": self.tmp, "sh": self.shell,
            "EXTRA_ZONES": os.path.join(self.tmp, "zones.json"),
            "audit": self.audits.append, "_INPUT_D_HELP": "-d DISPLAY_ID",
            "_ball": lambda *a: None, "_ocr_cancel_check": lambda: lambda: False,
            # R3 由 test_safe_tap_r3.py 覆盖（这里没有真实截图）
            "_r3_verify": lambda *a, **kw: {"ok": True, "diff": 0.0, "why": ""},
        }.items():
            p = patch.object(st, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(st.subprocess, "run", side_effect=AssertionError("real device access"))
        device_run = p.start()
        self.addCleanup(p.stop)
        self.addCleanup(device_run.assert_not_called)
        self._orig = {
            "STATE": st.STATE,
            "foreground": st.foreground,
            "screen_size": st.screen_size,
            "vd": st.vd,
            "VD_OK": st.VD_OK,
            "cfg_get": st.cfg_get,
        }
        st.STATE = os.path.join(self.tmp, "state.json")
        st.cfg_get = lambda k, d=None: d
        st.foreground = lambda did=0, dump=None: ("com.obric.calculator/.Calculator" if did else
                                       "com.android.launcher3/com.obric.feature.ObricLauncher")
        st.screen_size = lambda did=0: ((720, 1600) if did else (1260, 2800))
        self.now = time.time()
        self.last = {
            "id": "123", "fg": "com.obric.calculator/.Calculator",
            "display_id": 36, "display_width": 720, "display_height": 1600,
            "vd_sid": "vd-1-36", "shot_src": "app", "shot_black": False,
            "fg_app": "com.obric.calculator",
            "display_coverage": "tree",
            "zone_coverage": "clean", "zones": [], "window_id": 7,
            "node_count": 5, "nodes": [], "ts": self.now,
            "main_fg": "com.android.launcher3/com.obric.feature.ObricLauncher",
            "path": "/tmp/none.jpg", "pixel_nodes": [],
        }
        st.save_state({"last_shot": dict(self.last), "last_action_ts": 0,
                       "taps": [], "keys": [], "gestures": []})
        self.sess = {
            "sid": "vd-1-36", "displayId": 36, "width": 720, "height": 1600,
            "package": "com.obric.calculator",
            "expiresAt": self.now + 600, "state": "active",
        }
        self.vd_fake = types.SimpleNamespace(
            verify=lambda sid=None: {"ok": True, "session": dict(self.sess)},
            load_session=lambda: dict(self.sess),
        )
        st.vd = self.vd_fake
        st.VD_OK = True

    def tearDown(self):
        st.STATE = self._orig["STATE"]
        st.foreground = self._orig["foreground"]
        st.screen_size = self._orig["screen_size"]
        st.cfg_get = self._orig["cfg_get"]
        st.vd = self._orig["vd"]
        st.VD_OK = self._orig["VD_OK"]

    def gate(self, display=36, kind="tap", points=((360, 800),)):
        a = types.SimpleNamespace(shot="123", display=display)
        return st.check_gate(a, kind, list(points))

    def shell(self, *args, **kwargs):
        # F2 reads window insets even for otherwise pure gate checks.
        if args == ("shell", "dumpsys", "window", "displays"):
            return self.windows
        raise AssertionError("unexpected shell access: %r" % (args,))

    def action(self, kind="tap", points=((360, 800),), ack=False, **extra):
        a = types.SimpleNamespace(shot=st.load_state()["last_shot"]["id"],
                                 display=36, fast=True, ack_no_zones=ack, **extra)
        with patch.object(st, "_dispatch_display_gesture", return_value={
                "channel": "synthetic"}) as dispatch, \
                patch.object(st, "_settle_stable", return_value=(0, 1, True)), \
                patch.object(st, "observe", return_value=None), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            rc = st.do_action(a, kind, list(points))
        return rc, dispatch.call_args_list

    def reset_empty_tree(self):
        st.save_state({"last_shot": dict(self.last, zone_coverage="none",
                                        node_count=0, nodes=[])})


class VdGateTests(_GateBase):
    def test_virtual_display_happy_path(self):
        ok, why, _ = self.gate()
        self.assertTrue(ok, why)

    def test_cross_display_credential_refused(self):
        ok, why, _ = self.gate(display=0)
        self.assertFalse(ok)
        self.assertIn("displayId", why)

    def test_expired_session_refused(self):
        self.vd_fake.verify = lambda sid=None: {"ok": False, "reason": "session_expired:99"}
        ok, why, _ = self.gate()
        self.assertFalse(ok)
        self.assertIn("会话失效", why)

    def test_gone_display_refused(self):
        self.vd_fake.verify = lambda sid=None: {"ok": False, "reason": "display_gone:36"}
        ok, why, _ = self.gate()
        self.assertFalse(ok)
        self.assertIn("display_gone", why)

    def test_black_frame_refused(self):
        st_ = json.load(open(st.STATE, encoding="utf-8"))
        st_["last_shot"]["shot_black"] = True
        json.dump(st_, open(st.STATE, "w", encoding="utf-8"))
        ok, why, _ = self.gate()
        self.assertFalse(ok)
        self.assertIn("黑图", why)

    def test_screencap_source_refused(self):
        st_ = json.load(open(st.STATE, encoding="utf-8"))
        st_["last_shot"]["shot_src"] = "screencap"
        json.dump(st_, open(st.STATE, "w", encoding="utf-8"))
        ok, why, _ = self.gate()
        self.assertFalse(ok)
        self.assertIn("不是 App 按屏观测", why)

    def test_session_size_mismatch_refused(self):
        self.sess["width"] = 1080
        ok, why, _ = self.gate()
        self.assertFalse(ok)
        self.assertIn("尺寸", why)

    def test_main_foreground_changed_allows_virtual_display_action(self):
        # Owner removed main-screen foreground binding on 2026-10-21.
        st.foreground = lambda did=0, dump=None: ("com.tencent.mm/.ui.LauncherUI" if did == 0
                                       else "com.obric.calculator/.Calculator")
        ok, why, _ = self.gate()
        self.assertTrue(ok, why)
        rc, calls = self.action()
        self.assertEqual(rc, 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].args[:2], (36, "tap"))
        self.assertIn("consumed_at", st.load_state()["last_shot"])

    def test_vd_foreground_changed_refused(self):
        st.foreground = lambda did=0, dump=None: ("com.android.launcher3/com.obric.feature.ObricLauncher"
                                       if did == 0 else "com.other/.Main")
        ok, why, _ = self.gate()
        self.assertFalse(ok)
        self.assertIn("前台变了", why)

    def test_zone_coverage_none_refused(self):
        self.reset_empty_tree()
        before = st.load_state()
        ok, why, _ = self.gate()
        self.assertFalse(ok)
        # No ack: refuse without dispatch or consumption, independent of wording.
        rc, calls = self.action()
        self.assertEqual(rc, 2)
        self.assertEqual(calls, [])
        self.assertEqual(st.load_state(), before)

    def test_empty_tree_look_issues_display_bound_credential(self):
        obs = {"image": True, "jpg": "synthetic.jpg", "displayId": 36,
               "w": 720, "h": 1600, "appWindows": 1, "displayCoverage": "tree",
               "fg": "com.obric.calculator", "nodes": [], "interactive": True,
               "ts": self.now * 1000}
        with patch.object(st, "observe", return_value=obs), \
                patch.object(st, "save_look_jpg", return_value=True), \
                patch.object(st, "OCR_OK", True), \
                patch.object(st, "pixel_recognize_result", return_value={
                    "status": "empty", "rows": []}) as ocr, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(st.cmd_look(types.SimpleNamespace(display=36, note="synthetic")), 0)
        last = st.load_state()["last_shot"]
        self.assertEqual(last["zone_coverage"], "none")
        self.assertEqual(last["node_count"], 0)
        self.assertEqual(last["nodes"], [])
        self.assertEqual(last["display_id"], 36)
        self.assertEqual(last["display_coverage"], "tree")
        self.assertEqual((last["display_width"], last["display_height"]), (720, 1600))
        self.assertEqual(last["vd_sid"], self.sess["sid"])
        self.assertEqual(last["shot_src"], "app")
        self.assertNotIn("consumed_at", last)
        ocr.assert_called_once()

    @staticmethod
    def ocr_zone(word, cx, cy, w=120, h=30):
        return {"word": word, "text": word, "cx": cx, "cy": cy, "pad": st.DANGER_PAD,
                "rect": [cx - w // 2, cy - h // 2, cx + w // 2, cy + h // 2], "source": "ocr_gate"}

    def test_empty_tree_non_navigation_requires_ack_and_clean_ocr(self):
        # 2026-10-21 owner 批准：屏上有危险词时只放宽点按（横带外 + 视觉）和够长的纵向滚动；
        # swipe/drag/键 仍整页拒绝；这里的 scroll 只滑 100px（像点按）→ 也拒绝
        far, near = [self.ocr_zone("删除", 360, 200)], [self.ocr_zone("删除", 360, 905)]
        for kind in ("tap", "swipe", "long_press", "drag", "scroll", "key"):
            for ack in (False, True):
                for hits in (None, near, far, []):
                    with self.subTest(kind=kind, ack=ack, hits=hits):
                        self.reset_empty_tree()
                        # 空树页唯一放行的非导航键是粘贴 279（owner 2026-10-21）；其余见下一个测试
                        points = (279,) if kind == "key" else ((360, 800), (360, 900))
                        judge = Mock(return_value={"status": "ok", "impact": "none"})
                        with patch.object(st, "_ocr_danger_scan", return_value=hits) as scan, \
                                patch.object(st, "VISION_OK", True), \
                                patch.object(st, "vision_check", types.SimpleNamespace(judge_tap=judge)):
                            rc, calls = self.action(kind, points, ack)
                        allowed = ack and (hits == [] or (hits is far and kind in ("tap", "long_press")))
                        self.assertEqual(rc, 0 if allowed else 2)
                        self.assertEqual(len(calls), 1 if allowed else 0)
                        self.assertEqual("consumed_at" in st.load_state()["last_shot"], allowed)
                        self.assertEqual(scan.call_count, 1 if ack else 0)
                        self.assertEqual(judge.call_count,
                                         len(points) if allowed and kind in ("tap", "long_press") else 0)

    def test_empty_tree_non_paste_keys_refused_even_with_ack_and_clean_ocr(self):
        # owner 2026-10-21：空树页回车/空格等可能直接发送，看不清就不按（白名单只留粘贴）
        for code in (66, 62, 160, 23):
            with self.subTest(code=code):
                self.reset_empty_tree()
                before = st.load_state()
                with patch.object(st, "_ocr_danger_scan", side_effect=AssertionError("must refuse before OCR")):
                    rc, calls = self.action("key", (code,), ack=True)
                self.assertEqual(rc, 2)
                self.assertEqual(calls, [])
                self.assertEqual(st.load_state(), before)

    def test_empty_tree_visual_impact_controls_tap_and_long_press(self):
        verdicts = [{"status": "ok", "impact": impact}
                    for impact in ("none", "external", "irreversible", "unclear")]
        verdicts += [{"status": "unavailable"}, {"status": "error"}]
        for kind in ("tap", "long_press"):
            for verdict in verdicts:
                with self.subTest(kind=kind, verdict=verdict):
                    self.reset_empty_tree()
                    judge = Mock(return_value=verdict)
                    with patch.object(st, "_ocr_danger_scan", return_value=[]), \
                            patch.object(st, "VISION_OK", True), \
                            patch.object(st, "vision_check", types.SimpleNamespace(judge_tap=judge)):
                        rc, calls = self.action(kind, ((360, 800), (360, 800)), ack=True)
                    refused = verdict["status"] == "ok" and verdict["impact"] != "none"
                    self.assertEqual(rc, 2 if refused else 0)
                    self.assertEqual(len(calls), 0 if refused else 1)
                    self.assertEqual("consumed_at" in st.load_state()["last_shot"], not refused)
                    judge.assert_called_once()
                    self.assertEqual(judge.call_args.kwargs["action"],
                                     "点击" if kind == "tap" else "长按约 800 毫秒")
                    self.assertEqual(self.audits[-1]["vision"], [verdict])
                    if not refused:
                        self.assertEqual(self.audits[-1]["ack_detail"],
                                         "ocr_vision_clean_ack" if verdict["status"] == "ok"
                                         else "ocr_clean_ack_vision_unavailable")
                        self.assertIs(self.audits[-1]["approval_verified"], False)

    def test_empty_tree_danger_rows_elsewhere_still_need_vision_for_taps(self):
        # 屏上别处有危险词：点按类放行与否完全取决于视觉判断；视觉不可用 → 退回整页规则拒绝
        zones = [self.ocr_zone("删除", 600, 200), self.ocr_zone("付款", 100, 1300)]
        for kind in ("tap", "long_press"):
            for verdict, ok in (({"status": "ok", "impact": "none"}, True),
                                ({"status": "ok", "impact": "irreversible"}, False),
                                ({"status": "ok", "impact": "unclear"}, False),
                                ({"status": "unavailable"}, False), ({"status": "error"}, False)):
                with self.subTest(kind=kind, verdict=verdict):
                    self.reset_empty_tree()
                    judge = Mock(return_value=verdict)
                    with patch.object(st, "_ocr_danger_scan", return_value=zones), \
                            patch.object(st, "VISION_OK", True), \
                            patch.object(st, "vision_check", types.SimpleNamespace(judge_tap=judge)):
                        rc, calls = self.action(kind, ((360, 800), (360, 800)), ack=True)
                    self.assertEqual((rc, len(calls)), (0, 1) if ok else (2, 0))
                    judge.assert_called_once()
                    self.assertEqual(self.audits[-1]["ocr_danger"],
                                     [{"word": "删除", "cx": 600, "cy": 200}, {"word": "付款", "cx": 100, "cy": 1300}])
                    if ok:
                        self.assertEqual(self.audits[-1]["ack_detail"], "ocr_zones_vision_clean_ack")
        # 视觉模块缺失同样按整页规则拒绝
        self.reset_empty_tree()
        with patch.object(st, "_ocr_danger_scan", return_value=zones), patch.object(st, "VISION_OK", False):
            rc, calls = self.action("tap", ((360, 800),), ack=True)
        self.assertEqual((rc, calls), (2, []))

    def test_empty_tree_scroll_with_danger_words_stays_whole_page_refused(self):
        # Codex 第 2 轮：整条纵向轨迹落在高大的危险按钮里（[40,400,680,1200]，文字横带只在 y≈744..856），
        # 起止点都在横带外，View 抬手仍可能 click 且不经视觉 → 危险页上滚动不放宽
        zones = [self.ocr_zone("付款", 360, 800, w=160, h=32),
                 self.ocr_zone("删除", 560, 1400, w=160, h=40)]
        judge = Mock(side_effect=AssertionError("refused before vision"))
        for pts, extra in ((((360, 1100), (360, 500)), {}),
                           (((300, 300), (300, 650)), {}),
                           (((560, 1100), (560, 500)), {"steps": 2})):
            with self.subTest(pts=pts, extra=extra):
                self.reset_empty_tree()
                with patch.object(st, "_ocr_danger_scan", return_value=zones), \
                        patch.object(st, "VISION_OK", True), \
                        patch.object(st, "vision_check", types.SimpleNamespace(judge_tap=judge)):
                    rc, calls = self.action("scroll", pts, ack=True, **extra)
                self.assertEqual((rc, calls), (2, []))
                self.assertIn("扫到危险词", self.audits[-1]["reason"])
        # 屏上没有危险词时滚动照旧放行（不变）
        self.reset_empty_tree()
        with patch.object(st, "_ocr_danger_scan", return_value=[]):
            rc, calls = self.action("scroll", ((360, 1100), (360, 500)), ack=True)
        self.assertEqual((rc, len(calls)), (0, 1))

    def test_empty_tree_swipe_drag_with_danger_words_stay_whole_page_refused(self):
        # 零位移/1px swipe = 点击；横滑删除、滑动确认付款、拖进删除区都是手势本身执行危险操作
        zones = [self.ocr_zone("付款", 360, 1400)]
        for kind, pts in (("swipe", ((100, 800), (100, 800))), ("swipe", ((100, 800), (101, 800))),
                          ("swipe", ((80, 800), (640, 800))), ("swipe", ((360, 1200), (360, 300))),
                          ("drag", ((100, 800), (600, 300)))):
            with self.subTest(kind=kind, pts=pts):
                self.reset_empty_tree()
                with patch.object(st, "_ocr_danger_scan", return_value=zones):
                    rc, calls = self.action(kind, pts, ack=True)
                self.assertEqual((rc, calls), (2, []))
                self.assertIn("扫到危险词", self.audits[-1]["reason"])

    def test_empty_tree_tap_on_wide_button_edge_refused_even_if_vision_says_none(self):
        # 整宽「确认付款」栏：OCR 只框到中间文字，点左边缘也必须拦（横带），且不该轮到视觉放行
        judge = Mock(return_value={"status": "ok", "impact": "none"})
        self.reset_empty_tree()
        with patch.object(st, "_ocr_danger_scan", return_value=[self.ocr_zone("付款", 360, 1400, w=160, h=32)]), \
                patch.object(st, "VISION_OK", True), \
                patch.object(st, "vision_check", types.SimpleNamespace(judge_tap=judge)):
            rc, calls = self.action("tap", ((60, 1400),), ack=True)
        self.assertEqual((rc, calls), (2, []))
        judge.assert_not_called()
        self.assertIn("横带", self.audits[-1]["reason"])

    def test_danger_band_padding_scales_with_screen_height(self):
        z = self.ocr_zone("删除", 600, 1000, w=200, h=40)
        vd_band = st._danger_bands([z], 720, 1600)[0]
        main_band = st._danger_bands([z], 1260, 2800)[0]
        self.assertEqual((vd_band["rect"], vd_band["pad"]), ([0, 980, 720, 1020], 40))
        self.assertEqual((main_band["rect"], main_band["pad"]), ([0, 980, 1260, 1020], 70))
        self.assertTrue(st.in_zone(5, 1089, main_band) and not st.in_zone(5, 1091, main_band))

    def test_empty_tree_paste_key_still_whole_page_rule(self):
        # 没有坐标的动作（粘贴键 / type）照旧：屏上有危险词就不按
        self.reset_empty_tree()
        with patch.object(st, "_ocr_danger_scan", return_value=[self.ocr_zone("发送", 600, 1500)]):
            rc, calls = self.action("key", (279,), ack=True)
        self.assertEqual((rc, calls), (2, []))
        self.assertIn("扫到危险词", self.audits[-1]["reason"])

    def test_empty_tree_navigation_does_not_require_ack_ocr_or_vision(self):
        for key in (3, 4, 187):
            with self.subTest(key=key):
                self.reset_empty_tree()
                judge = Mock(side_effect=AssertionError("navigation vision check"))
                with patch.object(st, "_ocr_danger_scan", side_effect=AssertionError("navigation OCR")), \
                        patch.object(st, "VISION_OK", True), \
                        patch.object(st, "vision_check", types.SimpleNamespace(judge_tap=judge)):
                    rc, calls = self.action("key", (key,))
                self.assertEqual(rc, 0)
                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0].args[:3], (36, "key", [key]))
                self.assertIn("consumed_at", st.load_state()["last_shot"])
                judge.assert_not_called()

    def test_virtual_display_bottom_band_uses_own_insets(self):
        for bottom in (0, 24, 59, 60, 84):
            with self.subTest(bottom=bottom):
                self.windows = DUMP_WINDOW_DISPLAYS
                if bottom:
                    self.windows += ("  InsetsSource type=mandatorySystemGestures "
                                     "frame=[0,%d][720,1600]\n" % (1600 - bottom))
                ok, why, _ = self.gate(points=((360, 1541),))
                self.assertEqual(ok, bottom <= 59, why)

    def test_virtual_display_missing_insets_falls_back_conservatively(self):
        self.windows = DUMP_WINDOW_DISPLAYS.split("Display: mDisplayId=36")[0]
        ok, why, _ = self.gate(points=((360, 1541),))
        self.assertFalse(ok, why)

    def test_vd_key_needs_input_d(self):
        orig = st._INPUT_D_HELP
        st._INPUT_D_HELP = "(no -d support)"
        try:
            ok, why, _ = self.gate(kind="key", points=(4,))
        finally:
            st._INPUT_D_HELP = orig
        self.assertFalse(ok)
        self.assertIn("-d", why)


class OcrDangerScanTests(unittest.TestCase):
    """_ocr_danger_scan 返回带位置的禁区（与 look 建像素禁区同口径）；定位不了就当 OCR 不可用。"""

    def scan(self, rows, status="ok"):
        with tempfile.TemporaryDirectory() as d:
            shot = os.path.join(d, "shot-7.jpg")
            open(shot, "wb").close()
            with patch.object(st, "SHOTS", d), patch.object(st, "OCR_OK", True), \
                    patch.object(st, "pixel_recognize_result", return_value={"status": status, "rows": rows}):
                return st._ocr_danger_scan({"id": "7"})

    def test_rows_become_positional_zones(self):
        z = self.scan([{"text": "删除订单 再次购买", "x": 500, "y": 900, "w": 300, "h": 40},
                       {"text": "某品牌 手机", "x": 300, "y": 500, "w": 200, "h": 40}])
        self.assertEqual(len(z), 1)
        self.assertEqual((z[0]["word"], z[0]["rect"], z[0]["pad"]), ("删除", [350, 880, 650, 920], st.DANGER_PAD))
        self.assertTrue(st.in_zone(500, 955, z[0]))       # 行外 pad 内仍算
        self.assertFalse(st.in_zone(500, 965, z[0]))

    def test_missing_geometry_or_bad_status_is_unavailable(self):
        self.assertIsNone(self.scan([{"text": "删除"}]))
        self.assertIsNone(self.scan([], status="error"))
        self.assertEqual(self.scan([], status="empty"), [])


class ForegroundReuseTests(_GateBase):
    """2026-10-21 提速：绑定检查复用 verify 的前台、G1 复用绑定结果；派发前核对仍必须实时取。"""

    def count_fg(self, live):
        calls = []

        def fg(did=0, dump=None):
            calls.append(did)
            return live() if callable(live) else live
        st.foreground = fg
        return calls

    def verify_with_fg(self, fg):
        self.vd_fake.verify = lambda sid=None: {"ok": True, "session": dict(self.sess), "foreground": fg}

    def test_gate_reuses_verify_foreground_without_extra_queries(self):
        self.verify_with_fg("com.obric.calculator/.Calculator")
        calls = self.count_fg("com.obric.calculator/.Calculator")
        ok, why, _ = self.gate()
        self.assertTrue(ok, why)
        self.assertEqual([d for d in calls if d == 36], [])

    def test_stale_verify_foreground_is_requeried_once(self):
        self.verify_with_fg("com.other/.Main")
        calls = self.count_fg("com.obric.calculator/.Calculator")
        ok, why, _ = self.gate()
        self.assertTrue(ok, why)
        self.assertEqual([d for d in calls if d == 36], [36])      # 绑定里重取一次，G1 复用重取结果
        self.assertEqual(st._GATE_EVIDENCE["vd_fg"], "com.obric.calculator/.Calculator")

    def test_changed_foreground_confirmed_by_requery_refused(self):
        self.verify_with_fg("com.other/.Main")
        self.count_fg("com.other/.Main")
        ok, why, _ = self.gate()
        self.assertFalse(ok)
        self.assertIn("前台变了", why)

    def test_stale_gate_evidence_from_previous_check_is_cleared(self):
        st._GATE_EVIDENCE["vd_fg"] = "com.obric.calculator/.Calculator"    # 上一次动作留下的
        self.count_fg("com.other/.Main")
        with patch.object(st, "_check_display_binding", return_value=(True, "ok")):
            ok, why, _ = self.gate()
        self.assertFalse(ok, "G1 不得用上一次闸门留下的前台")

    def test_pre_dispatch_foreground_is_still_live(self):
        # 闸门用缓存放行之后、派发之前前台变了：派发前实时核对必须拦下，零派发、凭据不消费
        self.verify_with_fg("com.obric.calculator/.Calculator")
        seq = iter(["com.obric.calculator/.Calculator"])      # 只有动作开头的策略预检拿到旧前台
        calls = self.count_fg(lambda: next(seq, "com.other/.Main"))
        rc, dispatched = self.action()
        self.assertEqual(rc, 2)
        self.assertEqual(dispatched, [])
        self.assertNotIn("consumed_at", st.load_state()["last_shot"])
        self.assertIn("派发前前台变化", self.audits[-1]["reason"])
        self.assertEqual(calls, [36, 36])                      # 策略预检 + 派发前；闸门内零次


class MainDisplayRegressionTests(_GateBase):
    def test_main_display_does_not_need_session(self):
        st.vd = types.SimpleNamespace(verify=lambda sid=None: (_ for _ in ()).throw(
            AssertionError("主屏不该查虚拟屏会话")))
        st_ = json.load(open(st.STATE, encoding="utf-8"))
        st_["last_shot"].update({"display_id": 0, "fg": "com.x/.Main", "vd_sid": None,
                                 "main_fg": "com.x/.Main", "shot_src": "app"})
        st.foreground = lambda did=0, dump=None: "com.x/.Main"
        st.save_state(st_)
        a = types.SimpleNamespace(shot="123", display=0)
        ok, why, _ = st.check_gate(a, "tap", [(300, 500)])
        self.assertTrue(ok, why)


SCRCPY_DISPLAY = """\
  DisplayDeviceInfo{"内置屏幕": uniqueId="local:1", 1260 x 2800, state ON, type INTERNAL}
    mBaseDisplayInfo=DisplayInfo{"内置屏幕", displayId 0, displayGroupId 0, real 1260 x 2800, density 560, state ON, type INTERNAL, uniqueId "local:1"}
  DisplayDeviceInfo{"scrcpy": uniqueId="virtual:com.android.shell,2000,scrcpy,36", 720 x 1600, state ON, type VIRTUAL}
    mBaseDisplayInfo=DisplayInfo{"scrcpy", displayId 42, displayGroupId 1, real 720 x 1600, density 320, state ON, type VIRTUAL, uniqueId "virtual:com.android.shell,2000,scrcpy,36"}
"""

SCRCPY_ACTIVITY = DUMP_ACTIVITY.replace("Display #36", "Display #42")


class FakeShell:
    """模拟设备 sh 执行 adb_batch 的脚本：按命令前缀给 (输出, 退出码)，照脚本里的分隔行拼输出。"""
    STEP = re.compile(r"(.*?); r=\$\?; echo; echo (__DSB_[0-9a-f]+_\d+__) \$r(?:; |$)", re.S)

    def __init__(self, responses):
        self.responses = dict(responses)
        self.scripts = []
        self.truncate = None
        self.before_return = None

    def adb(self, *args, **kw):
        assert args[0] == "shell" and len(args) == 2, args
        self.scripts.append(args[1])
        out = ""
        for m in self.STEP.finditer(args[1]):
            body, rc = next(v for k, v in self.responses.items() if m.group(1).startswith(k))
            out += body + "\n" + m.group(2) + " " + str(rc) + "\n"
        if self.before_return:
            self.before_return()
        return out if self.truncate is None else out[:self.truncate]


class ScrcpySessionVerifyTests(unittest.TestCase):
    """scrcpy 自建屏的 verify：只假设备 shell，真走 adb_batch 分帧 + 各解析器（2026-10-21 提速后）。"""

    def setUp(self):
        self.now = time.time()
        self.sess = {
            "sid": "vd-s-42", "displayId": 42, "uniqueId": "virtual:com.android.shell,2000,scrcpy,36",
            "width": 720, "height": 1600, "overlayValue": None, "package": "com.obric.calculator",
            "pid": 9096, "scrcpyPid": 19572, "state": "active",
            "expiresAt": self.now + 600, "backend": "scrcpy",
        }
        self.dev = FakeShell({
            "cmd display get-displays -i": ("0\n42", 0),
            "dumpsys display": (SCRCPY_DISPLAY, 0),
            "settings get global overlay_display_devices": ("null", 0),
            "dumpsys activity activities": (SCRCPY_ACTIVITY, 0),
            "pidof ": ("9096", 0),
        })
        self.revived = []
        self.alive = lambda pid: True
        for name, value in {
            "load_session": lambda: dict(self.sess),
            "adb": self.dev.adb,
            "_termux_alive": lambda pid: self.alive(pid),
            "_revive_on_display": lambda did, pkg: self.revived.append((did, pkg)) or "",
        }.items():
            p = patch.object(vd, name, value)
            p.start()
            self.addCleanup(p.stop)

    def refused(self, reason, revive=False):
        r = vd.verify()
        self.assertFalse(r["ok"], r)
        self.assertIn(reason, r["reason"])
        if not revive:
            self.assertEqual(self.revived, [])      # 证据不完整时绝不能去唤醒/重拉 App
        return r

    def test_scrcpy_happy_path_single_round_trip(self):
        r = vd.verify()
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["display"]["uniqueId"], self.sess["uniqueId"])
        self.assertEqual(r["foreground"], "com.obric.calculator/.Calculator")
        self.assertEqual(r["main_foreground"], "com.android.launcher3/com.obric.feature.ObricLauncher")
        self.assertEqual(len(self.dev.scripts), 1)

    def test_display_gone_even_if_dumpsys_still_mentions_it(self):
        # 成员判定仍以 get-displays 为准：dumpsys 里残留旧行也不能当"屏还在"
        self.dev.responses["cmd display get-displays -i"] = ("0", 0)
        self.refused("display_gone:42")

    def test_display_meta_missing_refused(self):
        self.dev.responses["dumpsys display"] = (SCRCPY_DISPLAY.split("  DisplayDeviceInfo{\"scrcpy\"")[0], 0)
        self.refused("display_meta_missing:42")

    def test_scrcpy_unique_id_reuse_refused(self):
        self.dev.responses["dumpsys display"] = (SCRCPY_DISPLAY.replace("scrcpy,36", "scrcpy,99"), 0)
        self.refused("unique_id_changed")

    def test_scrcpy_overlay_setting_present_refused(self):
        self.dev.responses["settings get global overlay_display_devices"] = ("1600x720/320", 0)
        self.refused("overlay_present:1600x720/320")

    def test_scrcpy_process_gone_refused(self):
        self.alive = lambda pid: False
        self.refused("scrcpy_process_gone")

    def test_scrcpy_liveness_probe_error_fails_closed(self):
        def boom(pid):
            raise RuntimeError("ssh down")
        self.alive = boom
        self.refused("scrcpy_process_gone")

    def test_liveness_probe_runs_in_parallel_with_adb(self):
        # 双向握手：探活要等到 adb 已在跑才报活，adb 也要看到探活已开始——任一方向串行都会失败
        probe_started, adb_started = threading.Event(), threading.Event()

        def alive(pid):
            probe_started.set()
            return adb_started.wait(2)
        self.alive = alive
        seen = []

        def during_adb():
            adb_started.set()
            seen.append(probe_started.wait(2))
        self.dev.before_return = during_adb
        r = vd.verify()
        self.assertTrue(r["ok"], r)
        self.assertEqual(seen, [True])

    def test_activities_query_failure_refused_without_revive(self):
        self.dev.responses["dumpsys activity activities"] = ("Can't find service: activity", 255)
        self.refused("adb_batch_rc:activities=255")

    def test_activities_garbage_refused_without_revive(self):
        self.dev.responses["dumpsys activity activities"] = ("", 0)
        self.refused("activities_display_block_missing:42")

    def test_activities_missing_target_block_refused_without_revive(self):
        # 只有主屏块：不能用别的屏的头证明本屏证据可用，更不能据此 revive
        main_only = SCRCPY_ACTIVITY[SCRCPY_ACTIVITY.index("Display #0"):]
        self.dev.responses["dumpsys activity activities"] = (main_only, 0)
        self.refused("activities_display_block_missing:42")

    def test_dumpsys_timeout_partial_output_refused(self):
        # AOSP dumpsys 超时：部分输出 + 诊断（2>&1 并入），退出码仍 0。前台行已出现也不能放行
        timeout = "\n*** SERVICE 'activity' DUMP TIMEOUT (10000ms) EXPIRED ***\n"
        self.dev.responses["dumpsys activity activities"] = (SCRCPY_ACTIVITY.split("Display #0")[0] + timeout, 0)
        self.refused("dumpsys_incomplete:activities")
        self.dev.responses["dumpsys activity activities"] = (SCRCPY_ACTIVITY, 0)
        self.dev.responses["dumpsys display"] = (SCRCPY_DISPLAY + "*** SERVICE 'display' DUMP TIMEOUT (10000ms) EXPIRED ***", 0)
        self.refused("dumpsys_incomplete:display")

    def test_dumpsys_stderr_is_folded_into_batch_output(self):
        vd.verify()
        self.assertIn("dumpsys display 2>&1", self.dev.scripts[0])
        self.assertIn("dumpsys activity activities 2>&1", self.dev.scripts[0])

    def test_trailing_output_after_last_frame_refused(self):
        orig = self.dev.adb
        self.dev.adb = lambda *a, **k: orig(*a, **k) + "late diagnostic\n"
        vd.adb = self.dev.adb
        self.refused("adb_batch_trailing_output")

    def test_early_return_still_reaps_liveness_probe(self):
        done = []

        def slow_alive(pid):
            time.sleep(0.2)
            done.append(True)
            return True
        self.alive = slow_alive
        self.dev.responses["cmd display get-displays -i"] = ("0", 0)
        self.refused("display_gone:42")
        self.assertEqual(done, [True])          # 返回前已等到探活结束，不留孤儿子进程

    def test_truncated_batch_output_refused_without_revive(self):
        self.dev.truncate = 200
        self.refused("adb_batch_frame_broken")

    def test_marker_like_text_in_output_does_not_confuse_framing(self):
        self.dev.responses["dumpsys display"] = (SCRCPY_DISPLAY + "__DSB_000000000000_3__ 0\n", 0)
        self.assertTrue(vd.verify()["ok"])

    def test_app_process_absent_refused(self):
        self.dev.responses["pidof "] = ("", 1)          # pidof 找不到进程：rc=1 是合法结果
        self.refused("app_process_gone:pid 9096 -> -")

    def test_app_process_restarted_refused(self):
        self.dev.responses["pidof "] = ("1234", 0)
        self.refused("app_process_gone")

    def test_pidof_failure_or_garbage_refused(self):
        self.dev.responses["pidof "] = ("", 2)
        self.refused("adb_batch_rc:pidof=2")
        self.dev.responses["pidof "] = ("9096 oops", 0)
        self.refused("pidof_unparsed")

    def test_scrcpy_app_not_foreground_tries_bounded_revive(self):
        self.dev.responses["dumpsys activity activities"] = (
            SCRCPY_ACTIVITY.replace("com.obric.calculator/.Calculator", "com.android.launcher3/.Launcher"), 0)
        with patch.object(vd, "foreground", wraps=vd.foreground):
            self.refused("target_not_foreground", revive=True)
        self.assertEqual(self.revived, [(42, "com.obric.calculator")])


class OrangeInSameShellTests(unittest.TestCase):
    """2026-10-21 提速：shell 命令把「橙」和命令放进同一次远端 shell（每次仍先同步点橙，无缓存）。"""

    def setUp(self):
        self.argv = []

        def run(cmd, **kw):
            self.argv.append(cmd)
            return types.SimpleNamespace(returncode=self.rc, stdout=b"out", stderr=b"")
        self.rc = 0
        p = patch.object(vd.subprocess, "run", run)
        p.start()
        self.addCleanup(p.stop)

    def test_every_shell_call_lights_orange_first_in_same_shell(self):
        for _ in range(3):
            self.assertEqual(vd.adb("shell", "dumpsys", "display"), "out")
        self.assertEqual(len(self.argv), 3)                       # 一次调用 = 一次 adb 往返
        for cmd in self.argv:
            self.assertEqual(cmd[:len(vd.ADB_RAW)], vd.ADB_RAW)
            remote = cmd[len(vd.ADB_RAW) + 1]
            self.assertEqual(cmd[len(vd.ADB_RAW)], "shell")
            self.assertTrue(remote.startswith(vd.ORANGE_SH))      # 每次都先点橙，没有"已亮就跳过"
            self.assertTrue(remote.endswith("; dumpsys display"))
            self.assertIn("--es cmd '橙' >/dev/null 2>&1;", remote)

    def test_remote_command_is_what_adb_would_join(self):
        vd.adb("shell", "pidof", "com.obric.calculator")
        self.assertEqual(self.argv[0][-1], vd.ORANGE_SH + "pidof com.obric.calculator")

    def test_command_exit_status_still_checked(self):
        self.rc = 1
        with self.assertRaisesRegex(vd.VDError, "adb_rc=1"):
            vd.adb("shell", "dumpsys", "display")

    def test_adbq_guarded_input_and_non_shell_still_go_through_adbq(self):
        vd.adb("shell", "input", "tap", "1", "2", checked=False)
        vd.adb("shell", "input keyevent 4", checked=False)
        vd.adb("forward", "tcp:1", "tcp:2", checked=False)
        vd.adb("shell", "-T", "true", checked=False)       # adb 客户端选项不能被拼进远端命令
        self.assertEqual(len(self.argv), 4)
        self.assertTrue(all(c[:len(vd.ADB)] == vd.ADB for c in self.argv), self.argv)


class ScrcpyStartSafetyTests(unittest.TestCase):
    def args(self, package="com.obric.calculator"):
        return types.SimpleNamespace(package=package, component=None, backend="scrcpy",
                                     force=False, width=720, height=1600, dpi=320,
                                     ttl=900, purpose="test")

    def test_invalid_package_is_rejected_before_launch(self):
        with patch.object(vd, "load_session", return_value=None), \
                patch.object(vd, "foreground", return_value=""), \
                patch.object(vd, "_ball"), \
                patch.object(vd, "start_scrcpy", side_effect=AssertionError("launched")):
            with self.assertRaisesRegex(vd.VDError, "invalid_package"):
                vd.start(self.args("com.example;false"))

    def test_failed_scrcpy_start_cleans_own_display_and_preserves_foreign_overlay(self):
        foreign_overlay = {"value": "1600x720/320"}
        termux_commands = []
        saved = []

        def fake_adb(*args, **kwargs):
            if args[:4] == ("shell", "settings", "delete", "global"):
                foreign_overlay["value"] = "null"
            return ""

        meta = {"id": 42, "overlayValue": None,
                "recordPath": "/data/data/com.termux/files/home/vdtmp/session-test.mkv"}
        with patch.object(vd, "load_session", return_value=None), \
                patch.object(vd, "foreground", return_value=""), \
                patch.object(vd, "_ball"), \
                patch.object(vd, "start_scrcpy", return_value=(meta, 12345, meta["recordPath"])), \
                patch.object(vd, "resolve_component", side_effect=vd.VDError("resolve_failed")), \
                patch.object(vd, "overlay_setting", side_effect=lambda: foreign_overlay["value"]), \
                patch.object(vd, "adb", side_effect=fake_adb), \
                patch.object(vd, "_termux", side_effect=lambda cmd: termux_commands.append(cmd)), \
                patch.object(vd, "display_ids", return_value=[0]), \
                patch.object(vd, "save_session", side_effect=lambda s: saved.append(dict(s))):
            with self.assertRaisesRegex(vd.VDError, "resolve_failed"):
                vd.start(self.args())

        self.assertEqual(foreign_overlay["value"], "1600x720/320")
        self.assertTrue(any("kill 12345" in cmd for cmd in termux_commands))
        self.assertTrue(any("session-test.mkv" in cmd for cmd in termux_commands))
        self.assertEqual(saved[-1]["state"], "closed")

    def test_scrcpy_launch_timeout_attempts_to_stop_spawned_process(self):
        commands = []

        def fake_termux(cmd, **kwargs):
            commands.append(cmd)
            if "nohup scrcpy" in cmd:
                raise vd.VDError("termux_timeout")
            if cmd.startswith("cat "):
                return "12345\n"
            return ""

        with patch.object(vd, "display_ids", return_value=[0]), \
                patch.object(vd, "_termux", side_effect=fake_termux):
            with self.assertRaisesRegex(vd.VDError, "termux_timeout"):
                vd.start_scrcpy(720, 1600, 320, "com.obric.calculator")

        self.assertTrue(any("kill 12345" in cmd for cmd in commands))
        self.assertTrue(any("rm -f" in cmd and ".mkv" in cmd for cmd in commands))
        self.assertTrue(any("setsid nohup scrcpy" in cmd for cmd in commands))


if __name__ == "__main__":
    unittest.main(verbosity=2)
