#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""watcher.py —— 盯梢调度器（Tier-2）。按 watch_subs.json 到期执行订阅。

- 订阅配置：safe_tap.py watch --add wechat --every 30m --hours 7-23
- 执行结果进 Tier-1 任务审计（环境变量 DSB_TASK_ID/NAME，任务名 "watch:<id>"）；
- 低频：最小粒度 10 分钟；--hours 限定时段（如 7-23）；--once 供测试。
"""
import argparse
import json
import os
import signal
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
SUBS = os.path.join(HERE, "watch_subs.json")
LOCK = os.path.join(HERE, ".watcher.lock")
SCRIPTS = {"express": "watch_express.py"}
DISABLED_SUBS = {
    "wechat": "legacy_ui_unmigrated: 微信巡检未接入动作闸门，已停用；未启动设备操作"
}
MIN_EVERY_S = 600
_STOP = False
_CLEANING = False


class WatcherCancelled(BaseException):
    pass


def request_stop(signum, frame):
    global _STOP
    if not _STOP:
        _STOP = True
        if not _CLEANING:
            raise WatcherCancelled()


def group_exists(pgid):
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False


def cleanup_child(child):
    # Only the session we created is owned here, never the watcher's own group.
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(child.pid, sig)
        except ProcessLookupError:
            pass
        try:
            child.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        deadline = time.monotonic() + 2
        while group_exists(child.pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        if not group_exists(child.pid) and child.poll() is not None:
            return True
    return False


def run_child(argv, env, timeout=300):
    global _CLEANING
    child = None
    rc, out, err, status = 3, "", "", "failed"
    cleaned = True
    try:
        child = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 stdin=subprocess.DEVNULL, text=True, env=env,
                                 start_new_session=True)
        out, err = child.communicate(timeout=timeout)
        rc = child.returncode
        status = "completed" if rc == 0 else "refused" if rc == 2 else "failed"
    except subprocess.TimeoutExpired:
        rc, status, err = 124, "timed_out", "subscription timeout"
    except (WatcherCancelled, KeyboardInterrupt):
        rc, status, err = 130, "cancelled", "subscription cancelled"
    except Exception as exc:
        err = type(exc).__name__ + ": " + str(exc)
    finally:
        if child is not None:
            _CLEANING = True
            try:
                cleaned = cleanup_child(child)
            except Exception as exc:
                cleaned = False
                err += " cleanup: " + type(exc).__name__
            finally:
                _CLEANING = False
    if _STOP:
        rc, status, err = 130, "cancelled", "subscription cancelled"
    if not cleaned:
        rc, status = 3, "cleanup_unknown"
    return rc, out or "", err or "", status, cleaned


def is_watcher_pid(pid):
    if pid <= 1 or pid == os.getpid():
        return False
    try:
        with open("/proc/%d/cmdline" % pid, "rb") as f:
            args = f.read().split(b"\0")
        return os.fsencode(os.path.join(HERE, "watcher.py")) in args
    except OSError:
        return False


def load_subs():
    try:
        with open(SUBS, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def save_subs(subs):
    with open(SUBS, "w", encoding="utf-8") as f:
        json.dump(subs, f, ensure_ascii=False, indent=1)


def every_s(s):
    try:
        n = int(str(s).rstrip("mMhH"))
        unit = str(s)[-1].lower()
        return n * 60 if unit == "m" else n * 3600
    except Exception:
        return 1800


def in_hours(hours):
    if not hours:
        return True
    try:
        a, b = [int(x) for x in str(hours).split("-")]
        h = time.localtime().tm_hour
        return (a <= h < b) if a <= b else (h >= a or h < b)
    except Exception:
        return True


def run_sub(sub):
    # Refuse before task_start or child creation; retry must not revive this path.
    if sub.get("id") in DISABLED_SUBS:
        return 2, DISABLED_SUBS[sub["id"]]
    script = SCRIPTS.get(sub.get("id"))
    if not script:
        return 3, "unknown-sub"
    tid = "t%d-%04d" % (int(time.time()), int.from_bytes(os.urandom(2), "big"))
    env = dict(os.environ)
    env.update({"DSB_TASK_ID": tid, "DSB_TASK_NAME": "watch:" + sub["id"]})
    import safe_tap as st
    t0 = time.time()
    rc, out, err, status, cleaned = 3, "", "", "failed", True
    try:
        st.audit({"kind": "task_start", "task_id": tid,
                  "task_name": "watch:" + sub["id"], "script": script})
        rc, out, err, status, cleaned = run_child(
            [sys.executable, os.path.join(HERE, script)], env)
    except (WatcherCancelled, KeyboardInterrupt):
        rc, status, err = 130, "cancelled", "subscription cancelled"
    except Exception as exc:
        err = type(exc).__name__ + ": " + str(exc)
    finally:
        try:
            st.audit({"kind": "task_end", "task_id": tid,
                      "task_name": "watch:" + sub["id"], "result": status,
                      "terminal_status": status, "ok": rc == 0,
                      "goal_verified": False, "cleanup_scope": "owned_process_group",
                      "group_cleanup_confirmed": cleaned, "steps": 0,
                      "elapsed_s": round(time.time() - t0, 1),
                      "fail_reason": err[:160], "last_activity": "", "script": script})
        except Exception:
            rc, err = 3, "audit_failed"
    return rc, (out + " | " + err).strip()[-200:]


def main():
    global _STOP
    _STOP = False
    ap = argparse.ArgumentParser(description="盯梢调度器：watch 订阅的常驻执行器")
    ap.add_argument("--once", action="store_true", help="跑一轮到期订阅后退出")
    ap.add_argument("--status", action="store_true", help="打印订阅与实例状态后退出（不启动守护）")
    ap.add_argument("--stop", action="store_true", help="停掉正在运行的实例")
    a = ap.parse_args()
    PIDF = os.path.join(HERE, ".watcher.pid")

    def read_pid():
        try:
            with open(PIDF) as f:
                return int(f.read().strip())
        except Exception:
            return None

    def alive(pid):
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    if a.status:
        subs = load_subs()
        pid = read_pid()
        print("watcher 实例: %s" % (("运行中 pid=%d" % pid) if pid and alive(pid) else "未运行"))
        print("订阅 %d 条:" % len(subs))
        for s in subs:
            last = time.strftime("%m-%d %H:%M", time.localtime(s.get("last_run"))) if s.get("last_run") else "未跑过"
            print("  %-10s 每 %-6s 时段 %-6s 上次 %s rc=%s"
                  % (s.get("id"), s.get("every", "30m"), s.get("hours", "-") or "-",
                     last, s.get("last_rc")))
            if s.get("id") in DISABLED_SUBS:
                print("    " + DISABLED_SUBS[s["id"]])
        return 0

    if a.stop:
        pid = read_pid()
        if pid and alive(pid):
            if not is_watcher_pid(pid):
                print("PID 身份未确认，未发送停止信号", file=sys.stderr)
                return 3
            os.kill(pid, signal.SIGTERM)
            deadline = time.monotonic() + 10
            while alive(pid) and time.monotonic() < deadline:
                time.sleep(0.1)
            if alive(pid):
                print("停止尚未确认，保留 PID 文件", file=sys.stderr)
                return 3
            print("已停 watcher pid=%d" % pid)
        else:
            print("没有正在运行的实例")
        return 0

    ap_lock = open(LOCK, "w")
    try:
        import fcntl
        fcntl.flock(ap_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        ap_lock.close()
        print("另一个 watcher 在跑，拒绝并发", file=sys.stderr)
        return 3
    handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
    for sig in handlers:
        signal.signal(sig, request_stop)
    try:
        with open(PIDF, "w") as f:
            f.write(str(os.getpid()))
        while True:
            round_rc = 0
            subs = load_subs()
            for sub in subs:
                if not in_hours(sub.get("hours")):
                    continue
                ev = max(MIN_EVERY_S, every_s(sub.get("every", "30m")))
                if time.time() - sub.get("last_run", 0) >= ev:
                    rc, tail = run_sub(sub)
                    sub["last_run"] = time.time()
                    sub["last_rc"] = rc
                    sub["last_tail"] = tail
                    save_subs(subs)
                    if rc:
                        round_rc = rc
                    if _STOP or rc == 130:
                        return 130
            if a.once:
                return round_rc
            time.sleep(20)
    except (WatcherCancelled, KeyboardInterrupt):
        return 130
    finally:
        try:
            if read_pid() == os.getpid():
                os.remove(PIDF)
        except OSError:
            pass
        for sig, handler in handlers.items():
            signal.signal(sig, handler)
        ap_lock.close()


if __name__ == "__main__":
    sys.exit(main())
