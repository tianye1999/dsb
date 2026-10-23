#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""phone_lock.py —— 手机操作时的"红绿灯"信号。

═══ 为什么需要（2026-09-22 用户指出）═══

悬浮球本来有橙/蓝两种状态（橙=我在操作手机请勿触碰，蓝=空闲）。
但那个机制**只在 App 内部的 agent 任务路径里被触发**；
而我平时是**用 adb 直接操作手机**，根本不走那条路径 →
**球一直是蓝的，用户看不到"我正在操作"的警示**。

App 里其实早就有「橙」「蓝」命令（OverlayService 的 handleCommand），
只是没人从外部调。本模块就是那个调用方。

═══ 用法 ═══

    from phone_lock import locked

    with locked("读列表"):          # 进入时球变橙，退出时必回蓝
        ... 期间所有手机操作 ...

或用显式接口：
    bus = Busy("任务名")
    bus.on()            # 球橙
    bus.heartbeat()     # 长时间任务期间续心跳（App 侧看门狗 120 秒）
    bus.off()           # 球蓝

**兜底**：App 侧有 **20 秒**看门狗 —— 即使脚本崩溃、被 kill，
球也会在 20 秒后自动回蓝。所以不会永久卡在橙色。
"""
import subprocess
import sys
import time

from adb_endpoint import adb_command
try:
    ADB = adb_command()
except RuntimeError:
    ADB = ["adb", "-t", "0"]
DSH_TOKEN = "dsh-8f3a2c"
PKG = "com.dsb.assistant"
DSB_MAIN = f"{PKG}/.MainActivity"        # singleTask，am start 回已有实例不重载 WebView
HEARTBEAT_SEC = 12          # App 看门狗是 20 秒；心跳要明显早于它


def _broadcast(cmd):
    try:
        subprocess.run(
            ADB + ["shell", "am", "broadcast", "-n", f"{PKG}/.CmdReceiver",
                   "-a", f"{PKG}.CMD", "--es", "token", DSH_TOKEN, "--es", "cmd", cmd],
            capture_output=True, timeout=30)
        return True
    except Exception:
        return False


class Busy:
    """橙/蓝信号。线程安全的"重入安全"——重复 on() 只是续心跳。"""

    def __init__(self, task="", verbose=True):
        self.task = task
        self.verbose = verbose
        self._on = False
        self._last_hb = 0.0

    def on(self):
        _broadcast("橙")
        self._on = True
        self._last_hb = time.time()
        if self.verbose:
            print(f"  🟠 球→橙（{self.task or '操作中'}）：请勿触碰手机", file=sys.stderr)
        return self

    def heartbeat(self, force=False):
        """长时间任务期间调用；距上次心跳超过 HEARTBEAT_SEC 才真正发包。"""
        if not self._on:
            return
        if force or (time.time() - self._last_hb) >= HEARTBEAT_SEC:
            _broadcast("橙")
            self._last_hb = time.time()

    def off(self):
        if not self._on:
            return
        _broadcast("蓝")
        self._on = False
        if self.verbose:
            print(f"  🔵 球→蓝（空闲）：手机交还给你", file=sys.stderr)

    def back_to_dsb(self):
        """切回「逗沙包」对话界面。

        用户已多次要求：**完成手机操作后屏幕必须回到逗沙包界面**，
        让用户一眼看到结果，而不是停在微信/别的 App 上（2026-09-22 又漏一次）。
        MainActivity 是 singleTask → am start 回到已有实例，不会重载 WebView。
        """
        try:
            subprocess.run(ADB + ["shell", "am start -n", DSB_MAIN],
                           capture_output=True, timeout=40)
            time.sleep(4)
            r = subprocess.run(ADB + ["shell",
                    "dumpsys window 2>/dev/null | grep -m1 -oE 'mCurrentFocus=Window\\{[^}]*\\}'"],
                    capture_output=True, timeout=40)
            fg = r.stdout.decode("utf-8", "ignore")
            ok = PKG in fg
            if self.verbose:
                print(f"  🏠 切回逗沙包: {'✓' if ok else '✗ ' + fg.strip()[:70]}", file=sys.stderr)
            return ok
        except Exception as e:
            if self.verbose:
                print(f"  🏠 切回逗沙包失败: {e}", file=sys.stderr)
            return False

    def finish(self):
        """**任务收尾的标准动作**：回蓝 + 切回逗沙包。做成一跳，避免我忘第二步。"""
        self.off()
        return self.back_to_dsb()

    # 支持 with 语句
    def __enter__(self):
        return self.on()

    def __exit__(self, exc_type, exc, tb):
        self.off()
        return False            # 不吞异常


def locked(task="", finish_on_exit=False):
    """with locked("任务名"): ... —— 进入橙、退出蓝（异常也保证回蓝）。

    finish_on_exit=True 时，退出还会**切回逗沙包界面**（任务收尾标准动作）。
    """
    b = Busy(task)
    if finish_on_exit:
        _orig_exit = b.__exit__
        def _exit(exc_type, exc, tb):
            b.finish()
            return False
        b.__exit__ = _exit
    return b


def status():
    """读最后一次球状态（从 App 日志）。"""
    try:
        r = subprocess.run(
            ADB + ["shell", "logcat -d -s DSH:* 2>/dev/null | grep 球状态 | tail -1"],
            capture_output=True, timeout=30)
        return r.stdout.decode("utf-8", "ignore").strip() or "（读不到球状态）"
    except Exception as e:
        return f"（查询失败：{e}）"


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "状态"
    if cmd in ("橙", "忙", "on"):
        Busy("手动").on()
    elif cmd in ("蓝", "闲", "off"):
        _broadcast("蓝"); print("  🔵 球→蓝")
    else:
        print("  " + status())
