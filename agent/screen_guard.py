#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""屏幕常亮 / 充电不锁屏（用户 2026-10-21 要求）。

背景：本机 `screen_off_timeout` 默认 30 秒。息屏后：
  - `input` 注入被系统吞掉（点什么都没反应）
  - 无障碍服务可能被回收，控件树变空、截图变黑
表现为"App 不支持自动化"的假象，实际是屏睡了。

用法：
    python3 screen_guard.py prepare [--minutes 20]   # 开始干活前
    python3 screen_guard.py restore                 # 收工
    python3 screen_guard.py status

prepare 做三件事：
  1) 记录原值（只记一次，重复调用不会覆盖）
  2) screen_off_timeout = minutes*60*1000
  3) stay_on_while_plugged_in = 7（AC/USB/无线充电时都不休眠 → 充电时不锁屏）
  4) 立即唤醒屏幕
"""
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "screen_guard_state.json")
from adb_endpoint import adb_command
try:
    ADB = adb_command()
except RuntimeError:
    ADB = ["adb", "-t", "0"]


def sh(*args, t=20):
    try:
        p = subprocess.run(ADB + list(args), capture_output=True, text=True, timeout=t)
        return (p.stdout or "").strip()
    except Exception:
        return ""


def _get(ns, key):
    return sh("shell", "settings", "get", ns, key)


def _put(ns, key, value):
    sh("shell", "settings", "put", ns, key, str(value))


def load():
    try:
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save(d):
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)


def prepare(minutes=20):
    d = load()
    if not d.get("saved"):
        original = {"screen_off_timeout": _get("system", "screen_off_timeout"),
                    "stay_on": _get("global", "stay_on_while_plugged_in")}
        # 读不到原值（adb 不通等）或键未设置（"null"，restore 无法写回）就不记录、不改设置：
        # 否则 restore 无值可还，用户原设置被永久改掉
        if any(v in ("", "null") for v in original.values()):
            return {"ok": False, "error": "settings_read_failed", "original": original}
        d = {"saved": True, **original, "prepared_at": time.time()}
        save(d)
    ms = max(1, int(minutes)) * 60 * 1000
    _put("system", "screen_off_timeout", ms)
    _put("global", "stay_on_while_plugged_in", 7)
    # 唤醒（KEYCODE_WAKEUP）。锁屏若设有 PIN，这里只能亮屏、不能解锁。
    sh("shell", "input", "keyevent", "224")
    return {"ok": True, "screen_off_timeout": _get("system", "screen_off_timeout"),
            "stay_on_while_plugged_in": _get("global", "stay_on_while_plugged_in"),
            "original": {"screen_off_timeout": d.get("screen_off_timeout"),
                         "stay_on": d.get("stay_on")}}


def restore():
    d = load()
    if not d.get("saved"):
        return {"ok": True, "note": "nothing_to_restore"}
    if d.get("screen_off_timeout"):
        _put("system", "screen_off_timeout", d["screen_off_timeout"])
    # stay_on 的归属：如果常驻守护 stayon-daemon 在跑，就由它管理（App 在跑时保持 7）；
    # 没有守护时才由我们还原，避免两边互相覆盖。
    daemon_alive = False
    try:
        out = subprocess.run(["pgrep", "-f", "stayon-daemo[n].sh"],
                             capture_output=True, text=True, timeout=5)
        daemon_alive = bool((out.stdout or "").strip())
    except Exception:
        pass
    if d.get("stay_on") not in (None, "") and not daemon_alive:
        _put("global", "stay_on_while_plugged_in", d["stay_on"])
    try:
        os.remove(STATE)
    except Exception:
        pass
    return {"ok": True, "restored": {"screen_off_timeout": d.get("screen_off_timeout"),
                                     "stay_on": d.get("stay_on")}}


def status():
    return {"screen_off_timeout": _get("system", "screen_off_timeout"),
            "stay_on_while_plugged_in": _get("global", "stay_on_while_plugged_in"),
            "keyguard": "isKeyguardShowing=true" in sh("shell", "dumpsys", "window", t=20),
            "wakefulness": next((ln.split("=", 1)[1] for ln in
                                 sh("shell", "dumpsys", "power", t=20).splitlines()
                                 if "mWakefulness=" in ln), ""),
            "saved": load()}


def main(argv):
    cmd = (argv[1] if len(argv) > 1 else "status").lower()
    if cmd == "prepare":
        minutes = 20
        if "--minutes" in argv:
            try:
                minutes = int(argv[argv.index("--minutes") + 1])
            except Exception:
                pass
        out = prepare(minutes)
    elif cmd == "restore":
        out = restore()
    else:
        out = status()
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
