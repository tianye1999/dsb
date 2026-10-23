#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""vd_selftest.py —— 虚拟屏端到端自测（自有 canary App，不碰私人 App）。

start → verify → 新鲜 look → 读 COUNT → safe_tap 点 TAP+1 → 再 look → COUNT 恰好 +1
→ stop → 严格清理核验。只有业务状态（树文本）与主屏/清理全部可证时才 PASS。

判据不看 completed 回调、像素哈希或录像。失败也一定会尝试 stop 并写 summary。
每次运行写入独立子目录，根目录只放"最新一次"的完整 summary 索引。
"""
import argparse
import json
import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import vd_session as vd  # noqa: E402

BASE = "/root/.dsh-phone/vd-acceptance-20261020"
PKG = "com.dsh.canary"
SAFE = os.path.join(HERE, "safe_tap.py")


def _run(args, timeout=90):
    import subprocess
    r = subprocess.run([sys.executable, SAFE] + args, capture_output=True, timeout=timeout)
    out = r.stdout.decode("utf-8", "ignore")
    try:
        return json.loads(out[out.index("{"):]), r.returncode
    except Exception:
        return {"_raw": out[-800:], "_rc": r.returncode}, r.returncode


def _count(shot):
    for n in shot.get("nodes") or []:
        t = (n.get("text") or "").strip()
        if t.startswith("COUNT="):
            try:
                return int(t.split("=")[1]), t
            except Exception:
                pass
    return None, None


def judge(c1, c2, main_before, main_after, cleanup_ok):
    """业务 +1、主屏可证且未变、清理可证。任一为空/未知都不 PASS。"""
    if c1 is None or c2 is None:
        return False, "count_unreadable"
    if c2 != c1 + 1:
        return False, "count_not_plus_one:%s->%s" % (c1, c2)
    if not main_before or not main_after:
        return False, "main_foreground_unknown:%r->%r" % (main_before, main_after)
    if main_before != main_after:
        return False, "main_display_changed:%s->%s" % (main_before, main_after)
    if not cleanup_ok:
        return False, "cleanup_failed"
    return True, "ok"


def cred_check(shot, look, did, sid):
    """前后共用：凭据必须是**本次** look 的、同屏同会话、App 来源、有非黑截图。"""
    if not isinstance(shot, dict) or not shot:
        return "credential_missing"
    if not look or look.get("shot_id") != shot.get("id"):
        return "credential_id_mismatch"
    if int(shot.get("display_id", -1)) != int(did):
        return "credential_display_mismatch:%s" % shot.get("display_id")
    if shot.get("vd_sid") != sid:
        return "credential_session_mismatch:%s" % shot.get("vd_sid")
    if (shot.get("shot_src") or "") != "app":
        return "credential_not_app_frame:%s" % shot.get("shot_src")
    if (shot.get("fg_app") or "").split("/")[0] != PKG:
        return "credential_fg_mismatch:%s" % shot.get("fg_app")
    if shot.get("shot_black"):
        return "credential_black_frame"
    p = shot.get("path")
    if not p or not os.path.isfile(p) or os.path.getsize(p) < 1000:
        return "credential_screenshot_missing"
    return ""


def cred_meta(shot):
    return {k: shot.get(k) for k in ("id", "display_id", "vd_sid", "shot_src",
                                     "fg_app", "window_id", "tree_hash", "path")}


def cleanup_problems(sess, stop):
    """严格清理：stop ok + 屏确实消失 + 录像确实删除 + 真实文件/PID/进程都不在。"""
    problems = []
    ev = (stop or {}).get("evidence") or {}
    if not (stop or {}).get("ok"):
        problems.append("stop_not_ok")
    if ev.get("displayStillPresent") is not False:
        problems.append("display_present_or_unknown")
    if ev.get("recordingDeleted") is not True:
        problems.append("recording_not_deleted")
    rec = (sess or {}).get("recording")
    if rec and os.path.exists(rec):
        problems.append("recording_file_exists")
    pidf = (sess or {}).get("pidFile")
    if pidf and os.path.exists(pidf):
        problems.append("pidfile_exists")
    pid = (sess or {}).get("scrcpyPid")
    if pid:
        # _termux_alive returns False on transport failure as well as process
        # exit. A cleanup verdict must preserve that distinction.
        try:
            status = vd._termux(
                "if kill -0 %d 2>/dev/null; then echo alive; else echo exited; fi" % int(pid)
            ).strip()
            if status == "alive":
                problems.append("scrcpy_still_alive")
            elif status != "exited":
                problems.append("scrcpy_exit_unverified")
        except Exception:
            problems.append("scrcpy_exit_unverified")
    return problems


class RealDeps:
    def load_active_session(self):
        prev = vd.load_session()
        return prev if prev and prev.get("state") == "active" else None

    def foreground(self, did):
        return vd.foreground(did)

    def start(self):
        args = argparse.Namespace(package=PKG, component=PKG + "/.MainActivity",
                                  width=720, height=1600, dpi=320, ttl=300,
                                  purpose="vd selftest", force=False, backend="scrcpy")
        return vd.start(args)

    def verify(self, sid):
        return vd.verify(sid)

    def look(self, did, note):
        return _run(["look", "--display", str(did), "--note", note])

    def last_shot(self):
        import safe_tap as st
        return st.load_state().get("last_shot") or {}

    def tap(self, shot, x, y, did):
        return _run(["tap", "--shot", shot, "--x", str(x), "--y", str(y),
                     "--display", str(did), "--expect", "TAP+1"])

    def stop(self, sess):
        return vd.stop_internal(sess, "selftest")

    def blue(self):
        vd._ball("蓝")


def _tap_node(shot, want):
    for n in shot.get("nodes") or []:
        if (n.get("text") or "").strip() == want:
            x1, y1, x2, y2 = n["rect"]
            return (x1 + x2) // 2, (y1 + y2) // 2, n["rect"]
    return None


def execute(deps, outdir):
    os.makedirs(outdir, exist_ok=True)
    summary = {"ts": time.time(), "pass": False, "steps": {}, "evidenceDir": outdir}
    sess = None
    try:
        prev = deps.load_active_session() if hasattr(deps, "load_active_session") else None
        if prev:
            raise RuntimeError("已有活动虚拟屏会话，拒绝并行：%s" % prev.get("sid"))
        main_before = deps.foreground(0)
        if not main_before:
            raise RuntimeError("main_foreground_unknown_before_start")
        startup = deps.start()
        sess = startup["session"]
        did, sid = int(sess["displayId"]), sess["sid"]
        summary["displayId"] = did
        summary["steps"]["start"] = startup
        ver = deps.verify(sid)
        summary["steps"]["verify"] = ver
        if not ver.get("ok"):
            raise RuntimeError("verify_failed:%s" % ver.get("reason"))

        look1, rc = deps.look(did, "vd selftest before")
        summary["steps"]["look_before"] = look1
        if rc != 0 or not look1.get("shot_id"):
            raise RuntimeError("look_before_failed:%s" % look1.get("error"))
        shot1 = deps.last_shot()
        bad = cred_check(shot1, look1, did, sid)
        if bad:
            raise RuntimeError("before_" + bad)
        c1, raw1 = _count(shot1)
        if c1 is None:
            raise RuntimeError("count_not_found")
        node = _tap_node(shot1, "TAP+1")
        if not node:
            raise RuntimeError("tap_node_not_found")
        x, y, rect = node
        summary["steps"]["before"] = {"count": c1, "raw": raw1, "rect": rect,
                                      "credential": cred_meta(shot1)}
        tap, rc = deps.tap(shot1["id"], x, y, did)
        summary["steps"]["tap"] = tap
        if rc != 0:
            raise RuntimeError("tap_refused:%s" % (tap.get("reason") or tap.get("_raw")))
        with open(os.path.join(outdir, "tap_%s.json" % shot1["id"]), "w", encoding="utf-8") as f:
            json.dump(tap, f, ensure_ascii=False, indent=1)

        look2, rc = deps.look(did, "vd selftest after")
        summary["steps"]["look_after"] = look2
        if rc != 0 or not look2.get("shot_id"):
            raise RuntimeError("look_after_failed:%s" % look2.get("error"))
        shot2 = deps.last_shot()
        bad = cred_check(shot2, look2, did, sid)
        if bad:
            raise RuntimeError("after_" + bad)
        c2, raw2 = _count(shot2)
        main_after = deps.foreground(0)
        summary["steps"]["after"] = {"count": c2, "raw": raw2, "credential": cred_meta(shot2)}
        summary["steps"]["main"] = {"before": main_before, "after": main_after}
        if main_after != main_before or not main_after:
            raise RuntimeError("main_display_changed_or_unknown:%r->%r" % (main_before, main_after))

        for key, shot in (("before", shot1), ("after", shot2)):
            dst = os.path.join(outdir, "canary_%s.jpg" % key)
            shutil.copyfile(shot["path"], dst)
            summary["steps"][key]["evidenceScreenshot"] = dst
        with open(os.path.join(outdir, "trees.json"), "w", encoding="utf-8") as f:
            json.dump({"before": shot1.get("nodes"), "after": shot2.get("nodes")},
                      f, ensure_ascii=False, indent=1)
        summary["_counts"] = (c1, c2)
        summary["_main"] = (main_before, main_after)
    except Exception as e:
        summary["error"] = str(e)
    finally:
        stop = None
        try:
            if sess and sess.get("state") == "active":
                stop = deps.stop(sess)
                summary["steps"]["stop"] = stop
                problems = cleanup_problems(sess, stop)
                summary["cleanup"] = {"problems": problems, "ok": not problems,
                                      "displayStillPresent": (stop.get("evidence") or {}).get("displayStillPresent"),
                                      "recordingDeleted": (stop.get("evidence") or {}).get("recordingDeleted")}
            else:
                problems = ["no_active_session"]
                summary["cleanup"] = {"problems": problems, "ok": False}
        except Exception as e:
            problems = ["cleanup_exception:%s" % e]
            summary["cleanup"] = {"problems": problems, "ok": False}
        try:
            deps.blue()
        except Exception:
            pass
        # 主屏在 stop 后再核对一次（仅当前面已拿到非空基线）
        try:
            if summary.get("_main"):
                m0 = summary["_main"][0]
                m2 = deps.foreground(0)
                summary["steps"]["main"]["after_stop"] = m2
                if not m2 or m2 != m0:
                    problems.append("main_changed_after_stop")
        except Exception as e:
            problems.append("main_after_stop_unknown:%s" % e)
        summary["cleanup"]["problems"] = problems
        summary["cleanup"]["ok"] = not problems
        okj, why = judge(*(summary.get("_counts") or (None, None)),
                         *(summary.get("_main") or (None, None)),
                         summary["cleanup"]["ok"])
        if okj:
            summary["pass"] = True
        else:
            summary["pass"] = False
            summary["error"] = (summary.get("error") + ";" if summary.get("error") else "") + why
        summary.pop("_counts", None)
        summary.pop("_main", None)
        with open(os.path.join(outdir, "summary.json"), "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=1)
    return summary


def main(argv=None, deps=None):
    ap = argparse.ArgumentParser(description="虚拟屏 canary 自测")
    ap.parse_args(argv)
    runs = os.path.join(BASE, "runs")
    os.makedirs(runs, exist_ok=True)
    run_dir = tempfile.mkdtemp(prefix=time.strftime("%Y%m%d-%H%M%S-"), dir=runs)
    summary = execute(deps or RealDeps(), run_dir)
    # Only the device CLI publishes a latest-run index. Offline execute() tests
    # write solely to their temporary output directory.
    with open(os.path.join(BASE, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=1)
    with open(os.path.join(BASE, "latest.json"), "w", encoding="utf-8") as f:
        json.dump({"ts": summary["ts"], "pass": summary["pass"],
                   "runDir": run_dir, "displayId": summary.get("displayId"),
                   "error": summary.get("error")}, f, ensure_ascii=False, indent=1)
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    return 0 if summary.get("pass") else 2


if __name__ == "__main__":
    sys.exit(main())
