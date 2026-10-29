#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vd_session.py —— DSB 自建独立虚拟屏的会话管理（创建 / 持有 / 校验 / 销毁）。

2026-10-20 接入。外部原型（docs/VIRTUAL-DISPLAY-POC-20261020.md）验证了
"借 ADB shell 建屏 + 在屏上启动 App + 读到画面"，但没有验证 DSB 的按屏观测与点击。
本模块负责**会话这一半**；按屏截图/窗口树/手势在 App 的 AccessibilityService 里，
安全闸门在 safe_tap.py 里。

═══ 关键约束（fail closed）═══
1. 默认用 scrcpy 创建独立虚拟显示器；overlay 后端仅用于诊断。
   ID 由系统临时分配，必须每次现查，**绝不写死**。
2. 核对显示器 uniqueId、会话进程、前台 App；不接管别人的显示器。
3. 会话有 TTL（默认 900s）：过期后 safe_tap 拒绝动作，必须重新建会话。
4. destroy 必须**确认显示器真的消失**；清不掉就把状态标成 orphaned 并如实报告。
5. scrcpy 为维持视频管线写临时录像，安全凭据仍只来自
   AccessibilityService.takeScreenshot(displayId)；stop 时删除录像。
6. 只接受明确的包名/组件；不按 App 名字猜，不碰私人第三方 App（验收只用计算器）。

用法（safe_tap.py 会 import 本模块；也可单独跑）：
    python3 vd_session.py start --package com.obric.calculator --component com.obric.calculator/.Calculator
    python3 vd_session.py status
    python3 vd_session.py verify            # 机器可读；ok=false 时带 reason
    python3 vd_session.py stop
"""
import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SESSION_FILE = os.path.join(HERE, "vd_session.json")
# 2026-10-20：必须有 adbq.sh 的语义（每次调用先同步点橙球 + Termux 的 adb）；shell 命令的合并实现见 _adb_argv。
ADB = ["bash", "/data/data/com.termux/files/home/dsb/adbq.sh"]
# 2026-10-21 提速：adbq.sh 是"一次往返发「橙」+ 一次往返跑命令"（实测 0.30s）。shell 命令改成把
# 同一条「橙」广播和命令放进**同一次**远端 shell（0.17s）：每次照样先同步点橙，不留任何缓存状态。
# adbq 拒绝裸 input 的那几种写法仍交给 adbq（照旧拒绝）；非 shell 命令也仍走 adbq。
from adb_endpoint import adb_command, adb_target
ADB_RAW = ["/data/data/com.termux/files/usr/bin/adb", "-s", "127.0.0.1:5555"]
ORANGE_SH = ("am broadcast -n com.dsb.assistant/.CmdReceiver -a com.dsb.assistant.CMD "
             "--es token dsh-8f3a2c --es cmd '橙' >/dev/null 2>&1; ")
ADBQ_GUARDED = (" input tap ", " input swipe ", " input keyevent ", " input text ", " input touchscreen ")
OVERLAY_KEY = "overlay_display_devices"
DEFAULT_TTL = 900
SIZE_RE = re.compile(r"real (\d+) x (\d+)")
TYPE_RE = re.compile(r"type (\w+)")
UNIQUE_RE = re.compile(r'uniqueId "([^"]+)"')
DID_RE = re.compile(r"displayId (\d+),")
PACKAGE_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+")


class VDError(Exception):
    """虚拟屏操作失败；消息即机器可读原因（写进审计，不允许静默回退）。"""


def _adb_argv(args):
    """adb shell 本来就是把参数按空格拼成一条远端命令；这里在前面拼上「橙」，等价于 adbq 的先点橙后执行。"""
    joined = " ".join(str(a) for a in args)
    # args[1] 以 "-" 开头是 adb 客户端选项（-T/-n/-x…），不能拼进远端命令 → 仍交给 adbq
    if len(args) > 1 and args[0] == "shell" and not str(args[1]).startswith("-") \
            and not any(g in " %s " % joined for g in ADBQ_GUARDED):
        return adb_command(ADB_RAW[0]) + ["shell", ORANGE_SH + " ".join(str(a) for a in args[1:])]
    return ADB + [str(a) for a in args]


def adb(*args, t=20, checked=True):
    try:
        r = subprocess.run(_adb_argv(args), capture_output=True, timeout=t)
    except subprocess.TimeoutExpired:
        raise VDError("adb_timeout")
    except FileNotFoundError:
        raise VDError("adb_missing")
    except Exception as e:
        raise VDError("adb_error:%s" % e)
    out = r.stdout.decode("utf-8", "ignore")
    err = r.stderr.decode("utf-8", "ignore")
    if checked and r.returncode != 0:
        raise VDError("adb_rc=%d:%s" % (r.returncode, (err or out).strip()[:160]))
    return out


def _ball(cmd):
    """橙/蓝球：建屏/销毁也是"在操作手机"，用户要能看到警示。失败不影响主流程。

    直接调 adbq.sh 的 橙/蓝 子命令（它会自己 pulse），不另发裸广播。
    """
    try:
        subprocess.run(ADB + [cmd], capture_output=True, timeout=15)
    except Exception:
        pass


def display_ids(out=None):
    """当前逻辑显示器 ID 列表（cmd display get-displays -i，约 40ms）。out 可复用批量查询结果。"""
    if out is None:
        out = adb("shell", "cmd", "display", "get-displays", "-i")
    ids = []
    for tok in out.split():
        if tok.isdigit():
            ids.append(int(tok))
    return ids


def display_meta(did, dump=None):
    """dumpsys display 里该逻辑 ID 的 real 尺寸 / 类型 / uniqueId。

    ⚠️ SurfaceFlinger 的 64 位 display-id 是另一套编号，不能拿来当手势的 displayId；
    这里只用 Android 逻辑 ID。dump 可复用调用方已取到的输出（verify 一次取全，省往返）。
    """
    if dump is None:
        dump = adb("shell", "dumpsys", "display")
    for line in dump.splitlines():
        if "displayId %d," % int(did) not in line:
            continue
        m = SIZE_RE.search(line)
        t = TYPE_RE.search(line)
        u = UNIQUE_RE.search(line)
        if not m:
            continue
        return {"id": int(did), "width": int(m.group(1)), "height": int(m.group(2)),
                "type": (t.group(1) if t else ""),
                "uniqueId": (u.group(1) if u else "")}
    return None


def foreground(display_id=0, dump=None):
    """某逻辑显示器当前 topResumedActivity（包名/Activity）。

    为什么必须按 Display 块解析：存在虚拟屏时，`dumpsys activity activities` 里
    **第一处** topResumedActivity 可能是虚拟屏上的 App，全局取第一个会把主屏判断成
    虚拟屏（POC 里 DSB 看错屏就是这个根因之一）。这里严格取 `Display #<id>` 块。
    """
    if dump is None:
        dump = adb("shell", "dumpsys", "activity", "activities")
    m = re.search(r"Display #(\d+) \(activities from top to bottom\):(.*?)(?=\n\s*Display #|\Z)",
                  dump, re.S)
    # 逐个块找目标 id（不依赖顺序）
    for blk in re.finditer(r"Display #(\d+) \(activities from top to bottom\):(.*?)(?=\n\s*Display #|\Z)",
                           dump, re.S):
        if int(blk.group(1)) != int(display_id):
            continue
        mm = re.search(r"topResumedActivity=ActivityRecord\{\S+ \S+ (\S+)", blk.group(2))
        return mm.group(1) if mm else ""
    if display_id == 0 and m:
        mm = re.search(r"topResumedActivity=ActivityRecord\{\S+ \S+ (\S+)", m.group(2))
        return mm.group(1) if mm else ""
    return ""


def adb_batch(cmds, t=20, run=None):
    """一次 adb shell 跑多条只读查询，逐条取输出、逐条校验退出码（2026-10-21 提速，省往返不省证据）。

    cmds: [(名字, shell 命令[, 允许的退出码元组])]，默认只允许 0。返回 {名字: 输出}。
    为什么逐条带退出码：`a; b` 的整体退出码只反映最后一条，前面失败会被吞掉——
    dumpsys 失败的空输出会被当成"目标不在前台"而误入 revive（Codex review P1）。
    分隔行带随机 nonce、必须独占一行且按序各出现一次，防命令输出里碰巧含分隔串。
    任何一条退出码不在允许范围 / 分隔行缺失错乱 → VDError（调用方 fail closed）。
    """
    nonce = str(int.from_bytes(os.urandom(6), "big"))
    script = "; ".join("%s; r=$?; echo; echo __DSB_%s_%d__ $r" % (c[1], nonce, i)
                       for i, c in enumerate(cmds))
    out = (run("shell", ORANGE_SH + script, t=t, checked=True) if run is not None
           else adb("shell", script, t=t))
    marks = list(re.finditer(r"^__DSB_%s_(\d+)__ (\d+)\r?$" % nonce, out, re.M))
    if [int(m.group(1)) for m in marks] != list(range(len(cmds))):
        raise VDError("adb_batch_frame_broken:%d/%d" % (len(marks), len(cmds)))
    res, pos = {}, 0
    for c, m in zip(cmds, marks):
        rc = int(m.group(2))
        if rc not in (c[2] if len(c) > 2 else (0,)):
            raise VDError("adb_batch_rc:%s=%d" % (c[0], rc))
        res[c[0]] = out[pos:m.start()]
        pos = m.end()
    if out[pos:].strip():
        raise VDError("adb_batch_trailing_output")
    return res


def _in_background(fn, *a):
    """后台线程跑 fn(*a)，返回取结果的函数；fn 抛异常或超时都取到 None（调用方按失败处理）。"""
    box = {}

    def run():
        try:
            box["v"] = fn(*a)
        except Exception:
            pass
    th = threading.Thread(target=run, daemon=True)
    th.start()

    def result(timeout=20):
        th.join(timeout)
        return box.get("v")
    return result


def overlay_setting():
    return adb("shell", "settings", "get", "global", OVERLAY_KEY).strip()


def load_session():
    try:
        with open(SESSION_FILE, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except FileNotFoundError:
        return None
    except Exception:
        return None


def save_session(d):
    tmp = SESSION_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, SESSION_FILE)


def _cleanup_overlay(expect_value):
    """只清理我们自己设置的那一条 overlay；值不匹配就不动（可能是别人的屏）。"""
    cur = overlay_setting()
    if cur in ("", "null"):
        return True
    if expect_value and cur != expect_value:
        raise VDError("overlay_setting_changed:%s" % cur)
    adb("shell", "settings", "delete", "global", OVERLAY_KEY)
    return True


def create_display(width, height, dpi):
    cur = overlay_setting()
    if cur not in ("", "null"):
        raise VDError("overlay_already_present:%s" % cur)
    value = "%dx%d/%d" % (width, height, dpi)
    before = set(display_ids())
    adb("shell", "settings", "put", "global", OVERLAY_KEY, value)
    deadline = time.time() + 10
    new_id = None
    while time.time() < deadline:
        ids = set(display_ids())
        diff = [i for i in (ids - before) if i != 0]
        if diff:
            new_id = max(diff)
            break
        time.sleep(0.3)
    if new_id is None:
        _cleanup_overlay(value)
        raise VDError("display_not_created")
    meta = None
    deadline = time.time() + 5
    while time.time() < deadline:
        meta = display_meta(new_id)
        if meta and meta["width"] == width and meta["height"] == height:
            break
        time.sleep(0.3)
    if not meta or meta["width"] != width or meta["height"] != height:
        _cleanup_overlay(value)
        raise VDError("display_meta_mismatch:%s" % meta)
    if meta.get("type") not in ("OVERLAY", "VIRTUAL"):
        _cleanup_overlay(value)
        raise VDError("unexpected_display_type:%s" % meta.get("type"))
    meta["overlayValue"] = value
    return meta


TERMUX_SH = "/root/termux-dsb/tsh.sh"
VD_DIR = "/data/data/com.termux/files/home/vdtmp"


def _termux(cmd, t=40):
    """在 Termux 侧执行（scrcpy 与它自带的 adb 必须同环境，不能塞进 proot 的 LD_LIBRARY_PATH）。"""
    try:
        r = subprocess.run(["bash", TERMUX_SH, cmd], capture_output=True, timeout=t)
    except subprocess.TimeoutExpired:
        raise VDError("termux_timeout")
    out = (r.stdout or b"").decode("utf-8", "ignore")
    if r.returncode != 0:
        raise VDError("termux_rc=%d:%s" % (r.returncode, (r.stderr or r.stdout or b"").decode("utf-8", "ignore").strip()[:160]))
    return out


def start_scrcpy(width, height, dpi, package):
    """用 scrcpy --new-display 建屏并启动目标 App，返回 (meta, scrcpyPid, recordPath)。

    ⚠️ 只在 Termux 侧跑：scrcpy 调 Termux 的 adb；容器里 export LD_LIBRARY_PATH 会把
    /usr/bin/adb 的 libc 顶坏（实测 invalid ELF header）。录像只为满足 scrcpy 的视频管线，
    **不用于安全点击凭据**；stop 时删除。
    """
    if not isinstance(package, str) or not PACKAGE_RE.fullmatch(package):
        raise VDError("invalid_package")
    before = set(display_ids())
    tag = "%d-%d" % (int(time.time() * 1000), os.getpid())
    record = "%s/session-%s.mkv" % (VD_DIR, tag)
    pidfile = "%s/scrcpy-%s.pid" % (VD_DIR, tag)
    # ⚠️ 用 `;` 而不是 `&&` 串：`A && B & echo` 会把整条链后台化，
    #    子 shell 继承 ssh 的 stdout → ssh 等不到 EOF（实测 termux_timeout 40s）。
    cmd = ("umask 077; mkdir -p %s; cd %s; export LD_LIBRARY_PATH=$HOME/vdtmp/rt:$PREFIX/lib; "
           # SSH 退出后会话不能撤销 scrcpy/受管 adb 子进程，须独立于 SSH 的进程组。
           "setsid nohup scrcpy -s %s --new-display=%dx%d/%d --no-vd-system-decorations "
           # 2026-10-21：虚拟屏会跟着系统息屏（screen_off_timeout=30s）睡掉，App 变
           # visibleRequested=false / isSleeping=true → look 报 vd_target_not_foreground。
           # --keep-active = scrcpy 定期模拟用户活动，把空闲计时器一直重置（只影响本会话）。
           "--keep-active "
           "--no-window --no-audio --display-ime-policy=local --start-app=%s --record=%s "
           ">scrcpy-%s.log 2>&1 </dev/null & echo $! >%s; cat %s" %
           (VD_DIR, VD_DIR, shlex.quote(adb_target()), width, height, dpi, shlex.quote(package), shlex.quote(record),
            tag, shlex.quote(pidfile), shlex.quote(pidfile)))
    try:
        out = _termux(cmd)
    except VDError as launch_error:
        # SSH can time out after scrcpy has started. The process writes its PID
        # before SSH replies, so a second connection can stop only our launch.
        try:
            rescue = _termux("cat %s" % shlex.quote(pidfile), t=10)
            rescue_pid = int(rescue.strip())
            _termux("kill %d 2>/dev/null; rm -f %s %s" %
                    (rescue_pid, shlex.quote(record), shlex.quote(pidfile)), t=15)
            deadline = time.time() + 10
            while time.time() < deadline and set(display_ids()) - before:
                time.sleep(0.3)
            if set(display_ids()) - before:
                raise VDError("display_still_present_after_launch_failure")
        except Exception as cleanup_error:
            raise VDError("%s; scrcpy_cleanup_unverified:%s" %
                          (launch_error, cleanup_error)) from launch_error
        raise
    try:
        pid = int(out.strip().split()[-1])
    except Exception:
        raise VDError("scrcpy_pid_unknown:%s" % out.strip()[:80])
    deadline = time.time() + 15
    new_id = None
    while time.time() < deadline:
        diff = [i for i in (set(display_ids()) - before) if i != 0]
        if diff:
            new_id = max(diff)
            break
        time.sleep(0.4)
    if new_id is None:
        _termux("kill %d 2>/dev/null; rm -f %s %s" %
                (pid, shlex.quote(record), shlex.quote(pidfile)))
        raise VDError("display_not_created")
    meta = None
    deadline = time.time() + 8
    while time.time() < deadline:
        meta = display_meta(new_id)
        if meta and meta["width"] == width and meta["height"] == height:
            break
        time.sleep(0.3)
    if not meta or meta["width"] != width or meta["height"] != height:
        _termux("kill %d 2>/dev/null; rm -f %s %s" %
                (pid, shlex.quote(record), shlex.quote(pidfile)))
        raise VDError("display_meta_mismatch:%s" % meta)
    if meta.get("type") not in ("VIRTUAL", "OVERLAY"):
        _termux("kill %d 2>/dev/null; rm -f %s %s" %
                (pid, shlex.quote(record), shlex.quote(pidfile)))
        raise VDError("unexpected_display_type:%s" % meta.get("type"))
    meta["overlayValue"] = None
    meta["backend"] = "scrcpy"
    meta["recordPath"] = record
    meta["pidFile"] = pidfile
    return meta, pid, record


def resolve_component(package, component=None):
    if component:
        return component
    out = adb("shell", "cmd", "package", "resolve-activity", "--brief", package)
    for line in reversed(out.splitlines()):
        line = line.strip()
        if "/" in line and " " not in line:
            return line
    raise VDError("activity_not_resolved:%s" % package)


def start(args):
    if not args.package:
        raise VDError("package_required")
    if not PACKAGE_RE.fullmatch(args.package):
        raise VDError("invalid_package")
    if args.component:
        pkg, sep, activity = args.component.partition("/")
        if pkg != args.package or not sep or not re.fullmatch(r"[A-Za-z_.][A-Za-z0-9_.$]*", activity):
            raise VDError("invalid_component")
    # 2026-10-21 用户要求：动手前自动「息屏 20 分钟 + 充电不锁屏」，别让屏睡掉毁掉整条流程。
    try:
        import screen_guard as _sg
        _sg.prepare(20)
    except Exception:
        pass
    # 2026-10-21：锁屏时 input 注入会被系统吞掉（所有窗口 mCurrentFocus=null），
    # 建屏后每次点击都会"看起来没反应"——提前失败，别让上层白跑十几步。
    try:
        _kg = adb("shell", "dumpsys", "window", t=20, checked=False)
        if "isKeyguardShowing=true" in _kg:
            raise VDError("device_locked:手机锁着，input 注入会被吞；请先解锁再建虚拟屏")
    except VDError:
        raise
    except Exception:
        pass
    prev = load_session()
    if prev and prev.get("state") == "active" and not args.force:
        raise VDError("session_already_active:%s" % prev.get("sid"))
    # 上一会话若还挂着显示器，先如实收掉（我们自己建的才收）
    if prev and prev.get("state") == "active" and args.force:
        try:
            stop_internal(prev, "restart")
        except VDError:
            pass
    main_before = foreground(0)
    _ball("橙")
    scrcpy_pid = None
    try:
        if getattr(args, "backend", "scrcpy") == "scrcpy":
            meta, scrcpy_pid, _rec = start_scrcpy(args.width, args.height, args.dpi, args.package)
        else:
            meta = create_display(args.width, args.height, args.dpi)
    except Exception:
        _ball("蓝")
        raise
    did = meta["id"]
    try:
        component = resolve_component(args.package, args.component)
        want_pkg = component.split("/")[0]
        # 实测坑：主屏已有该 App 实例时，`am start --display` 只回
        # "delivered to top-most instance"，Activity **不会**到新屏。
        # 只 force-stop 这次明确指定的包（调用方必须显式给包名），保证是全新任务。
        # 只对专用 canary（计算器）force-stop；其他包一律不自动强停（用户红线）。
        if want_pkg == "com.obric.calculator":
            adb("shell", "am", "force-stop", want_pkg, checked=False)
            time.sleep(0.3)
        out = adb("shell", "am", "start", "--display", str(did), "-n", component)
        if re.search(r"\bError\b|delivered to top-most|Warning: Activity not started", out, re.I):
            raise VDError("am_start_not_new_task:%s" % out.strip()[:160])
        deadline = time.time() + 12
        fg = ""
        while time.time() < deadline:
            fg = foreground(did)
            if fg.startswith(want_pkg):
                break
            time.sleep(0.4)
        if not fg.startswith(want_pkg):
            raise VDError("app_not_on_display:%s (fg=%s)" % (want_pkg, fg or "-"))
        main_after = foreground(0)
        if main_before and main_after and main_before != main_after:
            raise VDError("main_display_changed:%s->%s" % (main_before, main_after))
        sid = "vd-%d-%d" % (int(time.time()), did)
        pidout = adb("shell", "pidof", want_pkg, t=10, checked=False).split()
        sess = {
            "sid": sid,
            "displayId": did,
            "uniqueId": meta.get("uniqueId", ""),
            "width": args.width, "height": args.height, "dpi": args.dpi,
            "overlayValue": meta["overlayValue"],
            "package": want_pkg,
            "component": component,
            "pid": int(pidout[0]) if pidout else None,
            "createdTs": time.time(),
            "expiresAt": time.time() + max(60, int(args.ttl)),
            "ttlSec": max(60, int(args.ttl)),
            "state": "active",
            "mainTopAtStart": main_before,
            "purpose": args.purpose or "",
            "allowForeign": bool(getattr(args, "allow_foreign", False)),
            "backend": getattr(args, "backend", "scrcpy"),
            "scrcpyPid": scrcpy_pid,
            "pidFile": meta.get("pidFile"),
            "recording": meta.get("recordPath"),   # scrcpy 的临时录像，stop 时删
        }
        save_session(sess)
        return {"ok": True, "session": sess, "display": {
            "id": did, "width": meta["width"], "height": meta["height"],
            "type": meta.get("type"), "uniqueId": meta.get("uniqueId")},
            "foregroundOnDisplay": fg, "mainForeground": main_after or main_before}
    except Exception as start_error:
        # 只清理本次创建的显示器。scrcpy 后端绝不能删除别人的 overlay 设置。
        try:
            if scrcpy_pid is not None:
                provisional = {"state": "active", "displayId": did, "overlayValue": None,
                               "scrcpyPid": scrcpy_pid, "recording": meta.get("recordPath"),
                               "pidFile": meta.get("pidFile")}
                cleanup = stop_internal(provisional, "startup_failed")
                if not cleanup.get("ok"):
                    raise VDError("scrcpy_cleanup_unverified:%s" % cleanup.get("evidence"))
            else:
                _cleanup_overlay(meta["overlayValue"])
        except Exception as cleanup_error:
            raise VDError("startup_failed:%s; cleanup_failed:%s" %
                          (start_error, cleanup_error)) from start_error
        raise


def _termux_alive(pid):
    """Termux 侧进程是否还活着（容器看不到 Termux 进程表，用 kill -0 探）。"""
    if not pid:
        return False
    try:
        r = subprocess.run(["bash", "/root/termux-dsb/tsh.sh", "kill -0 %d 2>/dev/null && echo alive" % int(pid)],
                           capture_output=True, timeout=15)
        return b"alive" in r.stdout
    except Exception:
        return False


def _revive_on_display(did, package):
    """虚拟屏睡掉/App 掉后台时的一次有界恢复。返回 "" | "wake" | "relaunch"。

    只做两件事：① 给该屏发 WAKEUP（不动主屏）；② 仍不行就按 launcher 组件在**该屏**
    重新拉起同一个 App。不做无限重试，绝不回退到主屏。
    """
    if not did or not package:
        return ""
    try:
        adb("shell", "input", "-d", str(did), "keyevent", "224", t=10, checked=False)
        time.sleep(1.0)
        if (foreground(did) or "").split("/")[0] == package:
            return "wake"
    except Exception:
        pass
    try:
        out = adb("shell", "cmd", "package", "resolve-activity", "--brief",
                  "-c", "android.intent.category.LAUNCHER", package, t=20, checked=False)
        comp = next((ln.strip() for ln in out.splitlines()
                     if ln.strip().startswith(package + "/")), "")
        did2 = int(did)
        # 显式用 --display：只在这块虚拟屏上拉，不影响主屏。
        adb("shell", "am", "start", "--display", str(did2), "-n", comp, t=30, checked=False)
        time.sleep(1.5)
        if (foreground(did) or "").split("/")[0] == package:
            return "relaunch"
    except Exception:
        pass
    return ""


# 目标 App 拉起的系统级界面：允许在其上操作（仍在同一块虚拟屏，不碰主屏）。
# 目前只放三类：文件选择器、权限/安装确认、系统分享面板。
VD_ON_BEHALF_PKGS = {
    "com.android.documentsui",
    "com.google.android.documentsui",
    "com.android.permissioncontroller",
    "com.google.android.permissioncontroller",
    "android",
    "com.android.intentresolver",
}


def verification_queries(s, integer_literals=False):
    package = str(s.get("package") or "-")
    pid_query = "pidof %s" % shlex.quote(package)
    if integer_literals:
        # Only byte integers enter the combined shell, never package text as code.
        byte_args = " ".join(str(b) for b in package.encode("utf-8"))
        pid_query = ("p=$(printf '%b_' \"$(printf '\\\\0%03o' " + byte_args + ")\"); "
                     'pidof "${p%_}"')
    return [("displays", "cmd display get-displays -i"),
            ("display", "dumpsys display 2>&1"),
            ("overlay", "settings get global %s" % OVERLAY_KEY),
            ("activities", "dumpsys activity activities 2>&1"),
            ("pidof", pid_query, (0, 1))]


def verify(sid=None, snapshot=None):
    s = load_session()
    if not s:
        return {"ok": False, "reason": "no_session"}
    if s.get("state") != "active":
        return {"ok": False, "reason": "session_%s" % s.get("state"), "session": s}
    if sid and s.get("sid") != sid:
        return {"ok": False, "reason": "session_mismatch", "session": s}
    now = time.time()
    if now > float(s.get("expiresAt") or 0):
        return {"ok": False, "reason": "session_expired:%ds" % int(now - s["expiresAt"]), "session": s}
    # 2026-10-21 提速：判据一条不删，只省往返——原来 6 次串行往返（实测 ~1.8s），
    # 现在 5 条查询一次 adb_batch（逐条校验退出码），scrcpy 存活（ssh kill -0）与之并行。
    scrcpy_alive = None if s.get("overlayValue") else _in_background(_termux_alive, s.get("scrcpyPid"))
    try:
        return _verify_device(s, now, scrcpy_alive, snapshot)
    finally:
        if scrcpy_alive:
            scrcpy_alive()      # 提前返回也等探活结束（其 subprocess 自带 15s 超时），不留孤儿 ssh


# dumpsys 服务超时/出错时会只输出一部分、把诊断写到 stderr，退出码仍是 0（AOSP dumpsys.cpp）。
DUMPSYS_FAILURE_RE = re.compile(r"DUMP TIMEOUT|Error dumping service")


def _verify_device(s, now, scrcpy_alive, snapshot=None):
    did = int(s.get("displayId", -1))
    try:
        if snapshot is not None and snapshot[0] == s:
            q = snapshot[1]
            if isinstance(q, VDError):
                raise q
        else:
            q = adb_batch(verification_queries(s))
    except VDError as e:
        return {"ok": False, "reason": str(e), "session": s}
    for name in ("display", "activities"):
        if DUMPSYS_FAILURE_RE.search(q[name]):
            # 部分输出不能当证据：不能据此判"不在前台"去 revive，更不能放行
            return {"ok": False, "reason": "dumpsys_incomplete:%s" % name, "session": s}
    if did not in display_ids(q["displays"]):
        return {"ok": False, "reason": "display_gone:%s" % did, "session": s}
    # ID 会被系统复用：必须核对"还是本会话那一块屏"（uniqueId/尺寸/类型/overlay 值）。
    meta = display_meta(did, q["display"])
    if not meta:
        return {"ok": False, "reason": "display_meta_missing:%s" % did, "session": s}
    if meta.get("uniqueId") != s.get("uniqueId") or not meta.get("uniqueId"):
        return {"ok": False, "reason": "display_unique_id_changed:%s->%s"
                % (s.get("uniqueId"), meta.get("uniqueId")), "session": s, "display": meta}
    if (meta.get("width"), meta.get("height")) != (int(s.get("width") or 0), int(s.get("height") or 0)):
        return {"ok": False, "reason": "display_size_changed:%sx%s" % (meta.get("width"), meta.get("height")),
                "session": s, "display": meta}
    if meta.get("type") not in ("OVERLAY", "VIRTUAL"):
        return {"ok": False, "reason": "display_type_changed:%s" % meta.get("type"),
                "session": s, "display": meta}
    cur = q["overlay"].strip()
    if s.get("overlayValue"):
        if cur != s.get("overlayValue"):
            return {"ok": False, "reason": "overlay_setting_changed:%s (session %s)"
                    % (cur, s.get("overlayValue")), "session": s}
    else:
        # scrcpy 自建屏：不允许用别人的 overlay 屏顶替，也不允许 overlay 值被动过。
        if cur not in ("", "null"):
            return {"ok": False, "reason": "overlay_present:%s" % cur, "session": s}
        if not scrcpy_alive():
            return {"ok": False, "reason": "scrcpy_process_gone:%s" % s.get("scrcpyPid"),
                    "session": s}
    # 目标 App 必须仍在该屏前台；App 跳回主屏 / 被顶掉都会让凭据失效。
    # 例外：目标 App 自己拉起的**系统级选择器/授权框**（同一块屏上，属于流程的一部分）。
    adump = q["activities"]
    if "Display #%d (activities from top to bottom):" % did not in adump:
        # 没有本屏的块（AOSP 对每块已知屏都打印头，缺了只能是截断/不一致）：
        # 不能当成"不在前台"去 revive，直接拒绝
        return {"ok": False, "reason": "activities_display_block_missing:%d" % did, "session": s}
    pids = q["pidof"].split()
    if not all(p.isdigit() for p in pids):
        return {"ok": False, "reason": "pidof_unparsed", "session": s}
    fg = foreground(did, adump)
    main_fg = foreground(0, adump)
    fg_pkg = fg.split("/")[0] if fg else ""
    if s.get("allowForeign") and fg_pkg:
        # 分享/跳转类流程：屏幕上换成了别的 App（选择器→目标 App）是预期行为，
        # 只要还是这块屏、尺寸/uniqueId 没变，就放行。
        return {"ok": True, "session": s, "foreign": fg, "foreground": fg, "main_foreground": main_fg,
                "remainingSec": int(float(s["expiresAt"]) - now)}
    if fg_pkg in VD_ON_BEHALF_PKGS and fg_pkg != s.get("package"):
        return {"ok": True, "session": s, "on_behalf_of": fg, "foreground": fg, "main_foreground": main_fg,
                "remainingSec": int(float(s["expiresAt"]) - now)}
    if fg_pkg != s.get("package"):
        # 2026-10-21：屏睡掉（isSleeping=true）时 App 会变 visibleRequested=false。
        # 先做**一次**有界恢复：WAKEUP → 仍是空就按 launcher 组件在**该屏**重启 App。
        # 恢复成功只代表「能继续看」，凭据仍需重新 look（调用方会做）。
        recovered = _revive_on_display(did, s.get("package"))
        if recovered:
            fg = foreground(did)
            if (fg.split("/")[0] if fg else "") == s.get("package"):
                return {"ok": True, "session": s, "recovered": recovered, "foreground": fg,
                        "remainingSec": int(float(s["expiresAt"]) - now)}
        return {"ok": False, "reason": "target_not_foreground:%s" % (fg or "-"),
                "session": s, "foreground": fg, "revive": recovered}
    # 进程身份：目标 App 被重启过（pid 变）就不再是 look 时那一个实例。
    pid = s.get("pid")
    if pid and str(pid) not in pids:
        return {"ok": False, "reason": "app_process_gone:pid %s -> %s" % (pid, " ".join(pids) or "-"),
                "session": s}
    return {"ok": True, "session": s, "remainingSec": int(float(s["expiresAt"]) - now),
            "display": meta, "foreground": fg, "main_foreground": main_fg}


def stop_internal(s, reason):
    """清 overlay 并确认显示器消失。返回证据 dict。"""
    evidence = {"displayIdBefore": s.get("displayId"), "overlayValue": s.get("overlayValue")}
    if not s.get("overlayValue") and s.get("scrcpyPid"):
        # scrcpy 自建屏：杀进程 → 等显示器消失 → 删临时录像（隐私）。
        try:
            _termux("kill %d 2>/dev/null" % int(s["scrcpyPid"]))
        except VDError as e:
            evidence["killError"] = str(e)
        deadline = time.time() + 10
        gone = False
        while time.time() < deadline:
            try:
                if int(s.get("displayId", -1)) not in display_ids():
                    gone = True
                    break
            except VDError:
                pass
            time.sleep(0.3)
        if s.get("recording") or s.get("pidFile"):
            try:
                paths = [shlex.quote(str(s[key])) for key in ("recording", "pidFile") if s.get(key)]
                _termux("rm -f %s" % " ".join(paths))
                evidence["recordingDeleted"] = True
            except VDError:
                evidence["recordingDeleted"] = False
        s["state"] = "closed" if gone else "orphaned"
        s["closedTs"] = time.time()
        s["closedReason"] = reason
        save_session(s)
        evidence["displayStillPresent"] = not gone
        return {"ok": gone, "session": s, "evidence": evidence}
    try:
        _cleanup_overlay(s.get("overlayValue"))
    except VDError as e:
        s["state"] = "orphaned"
        s["stopError"] = str(e)
        s["closedTs"] = time.time()
        save_session(s)
        return {"ok": False, "reason": str(e), "session": s, "evidence": evidence}
    deadline = time.time() + 10
    gone = False
    while time.time() < deadline:
        try:
            if int(s.get("displayId", -1)) not in display_ids():
                gone = True
                break
        except VDError:
            pass
        time.sleep(0.3)
    s["state"] = "closed" if gone else "orphaned"
    s["closedTs"] = time.time()
    s["closedReason"] = reason
    save_session(s)
    evidence["displayStillPresent"] = not gone
    evidence["overlayAfter"] = overlay_setting()
    return {"ok": gone, "session": s, "evidence": evidence}


def _restore_screen_guard():
    try:
        import screen_guard as _sg
        _sg.restore()
    except Exception:
        pass


def stop(args):
    s = load_session()
    if not s:
        _restore_screen_guard()
        return {"ok": True, "already": "no_session"}
    if s.get("state") != "active":
        _restore_screen_guard()
        return {"ok": True, "already": "session_%s" % s.get("state"), "session": s}
    out = stop_internal(s, args.reason or "manual")
    # 2026-10-21：收工时把息屏时间/充电常亮还原成用户原来的设置。
    try:
        import screen_guard as _sg
        _sg.restore()
    except Exception:
        pass
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="DSB 独立虚拟屏会话")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("start", help="创建 overlay 显示器并在其上启动目标 App")
    p.add_argument("--width", type=int, default=720)
    p.add_argument("--height", type=int, default=1600)
    p.add_argument("--dpi", type=int, default=320)
    p.add_argument("--package", required=True)
    p.add_argument("--component", default=None)
    p.add_argument("--ttl", type=int, default=DEFAULT_TTL, help="会话有效期（秒），到期后闸门拒绝")
    p.add_argument("--purpose", default="")
    p.add_argument("--force", action="store_true", help="收掉旧会话再建（仍会确认旧屏消失）")
    p.add_argument("--allow-foreign", dest="allow_foreign", action="store_true",
                   help="允许该屏前台换成别的 App（分享/跳转流程）")
    p.add_argument("--backend", choices=("scrcpy", "overlay"), default="scrcpy",
                   help="scrcpy=自建屏（默认，无系统装饰）；overlay=settings overlay_display_devices（仅诊断）")
    p.set_defaults(fn=start)

    p = sub.add_parser("stop", help="销毁显示器并确认消失")
    p.add_argument("--reason", default="manual")
    p.set_defaults(fn=stop)

    p = sub.add_parser("verify", help="机器可读校验（safe_tap 每步调用）")
    p.add_argument("--sid", default=None)
    p.set_defaults(fn=lambda a: verify(a.sid))

    p = sub.add_parser("status", help="人读状态 + 显示器元数据")
    p.set_defaults(fn=lambda a: _status())
    try:
        a = ap.parse_args(argv)
        res = a.fn(a)
    except VDError as e:
        if a.cmd == "start":
            _ball("蓝")
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
        return 2
    if a.cmd == "start":
        _ball("蓝")
    print(json.dumps(res, ensure_ascii=False, indent=1))
    return 0 if res.get("ok") else 2


def _status():
    s = load_session()
    if not s:
        return {"ok": True, "session": None, "note": "没有虚拟屏会话"}
    v = verify()
    meta = None
    try:
        meta = display_meta(s.get("displayId"))
    except VDError as e:
        meta = {"error": str(e)}
    return {"ok": v.get("ok", False), "verify": v, "display": meta, "session": s}


if __name__ == "__main__":
    sys.exit(main())
