#!/usr/bin/env python3
"""虚拟屏策略回归（2026-10-20）。

规矩：读/操作「别的 App」的界面必须在虚拟屏上；主屏动作默认拒绝。
本测试**不碰手机**：monkeypatch 前台包名，只验证判定与 CLI 分支。

用法：python3 safe_tap_vd_test.py   → 0=PASS，1=FAIL
"""
import argparse
import contextlib
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import safe_tap as s

FAIL = []


def check(name, cond, detail=""):
    print(("  ✓ " if cond else "  ✗ ") + name + ("  " + str(detail) if detail and not cond else ""))
    if not cond:
        FAIL.append(name)


def ns(**kw):
    base = dict(shot="9999999999", display=0, x=100, y=100, node=None, expect="t",
                fast=False, ack_no_zones=False, expect_activity=None,
                allow_main=False, why=None, fn=None)
    base.update(kw)
    return argparse.Namespace(**base)


def main():
    print("== 判定层 ==")
    check("主屏+第三方 App → 拒绝",
          s.vd_policy_block("tap", 0, "com.taobao.taobao/x") is not None)
    check("主屏+逗沙包 → 放行", s.vd_policy_block("tap", 0, "com.dsb.assistant/.MainActivity") is None)
    check("主屏+系统设置 → 放行", s.vd_policy_block("tap", 0, "com.android.settings/.Settings") is None)
    check("主屏+空树夹具 → 放行", s.vd_policy_block("tap", 0, "com.dsh.emptytree/.Main") is None)
    check("虚拟屏(49)+第三方 → 放行", s.vd_policy_block("tap", 49, "com.taobao.taobao/x") is None)
    check("显式 --allow-main → 放行",
          s.vd_policy_block("tap", 0, "com.taobao.taobao/x", allow_main=True, why="test") is None)
    check("前台读不到 → 拒绝（宁严）", s.vd_policy_block("tap", 0, "") is not None)

    print("== CLI 分支（不碰手机） ==")
    s._foreground_on = lambda did=0: "com.taobao.taobao/com.taobao.tao.welcome.Welcome"
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        rc = s.cmd_tap(ns())
    check("主屏 tap 被拒 rc=2", rc == 2, rc)
    check("拒绝话术含 vd_session 指引", "vd_session.py start" in buf.getvalue())

    buf2 = io.StringIO()
    with contextlib.redirect_stderr(buf2):
        s.cmd_tap(ns(allow_main=True, why="unit"))
    check("显式放行后不再因 VD 拒绝", "VD 策略" not in buf2.getvalue(), buf2.getvalue()[:80])

    s._foreground_on = lambda did=0: "com.dsb.assistant/.MainActivity"
    buf3 = io.StringIO()
    with contextlib.redirect_stderr(buf3):
        s.cmd_tap(ns())
    check("豁免包不被 VD 拦", "VD 策略" not in buf3.getvalue(), buf3.getvalue()[:80])

    print("== look 标注 ==")
    check("主屏看第三方 = violation", s.vd_look_hint(0, "com.taobao.taobao/x") == "violation_main_screen_view")
    check("主屏看逗沙包 = ok_exempt", s.vd_look_hint(0, "com.dsb.assistant/x") == "ok_exempt")
    check("虚拟屏 = ok_vd", s.vd_look_hint(49, "com.taobao.taobao/x") == "ok_vd")

    print()
    if FAIL:
        print("VD_POLICY_FAIL  (%d 项)" % len(FAIL))
        return 1
    print("VD_POLICY_PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
