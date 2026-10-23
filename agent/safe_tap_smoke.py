#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""safe_tap_smoke.py —— G2 空转闸门的一键冒烟验收（2026-10-01 加）。

一条命令出证据：启动空树夹具 → 断言 look 出 zone_coverage=none → 断言 tap 被拒 →
断言 --ack-no-zones 仍拒绝 → 断言 HOME 豁免 → 回桌面 → 打 PASS/FAIL 汇总。

前置：手机已装空树夹具 com.dsh.emptytree（源码 /root/emptytree-test/，bash build.sh 一键重建）。
用法：python3 safe_tap_smoke.py
"""
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import safe_tap as st  # noqa: E402
import adb_endpoint  # noqa: E402
SAFE = os.path.join(HERE, "safe_tap.py")
FIXTURE = "com.dsh.emptytree/.MainActivity"
results = []


def adb(*a, t=60):
    try:
        return subprocess.run(adb_endpoint.adb_command() + list(a), capture_output=True, timeout=t).stdout.decode("utf-8", "ignore")
    except Exception:
        return ""


def safe(*args, timeout=240):
    p = subprocess.run([sys.executable, SAFE] + list(args), capture_output=True, timeout=timeout)
    return (p.returncode,
            p.stdout.decode("utf-8", "ignore"),
            p.stderr.decode("utf-8", "ignore"))


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print("  %s %-40s %s" % ("✓" if cond else "✗", name, detail))


def _r_tests():
    """R1-R4 解析单测（合成"前后树"）。

    真机上很难稳定造出 r2/r3/loose 的**正例**（复测者用 400px 滚动直接把目标滚出视口了，
    只能走到"R4 0 个 -> 拒绝"），所以这里把四条路径的**正例与拒绝**都钉死 ——
    否则它们是**没被测过的代码路径**。
    """
    import copy
    real = (st.observe, st.foreground, st.load_state)
    ORIG = {
        "viewId": "com.x:id/title", "text": "设置", "desc": "", "norm": "设置",
        "cls": "android.widget.TextView", "ord": 0,
        "qrect": "40,100,600,200", "rect": [40, 100, 600, 200],
        "digest": "aaaa1111", "handle": "h1.oldhash0.0000.aaaa1111",
        "clickable": False, "enabled": True,
    }
    out = []

    def case(name, orig, new_nodes, new_hash, want_ok, want_by=None):
        st.observe = lambda **kw: {"nodes": copy.deepcopy(new_nodes), "treeHash": new_hash,
                                   "windowId": 62, "fg": "com.x"}
        st.foreground = lambda: "com.x"
        st.load_state = lambda: {"last_shot": {"nodes": [copy.deepcopy(orig)], "fg": "com.x",
                                               "window_id": 62}}
        r = st.resolve_handle(orig["handle"])
        good = (r.get("ok") == want_ok) and (want_by is None or r.get("by") == want_by)
        out.append((name, good, "ok=%s by=%s" % (r.get("ok"), r.get("by") or "-")))

    def nd(**kw):
        n = copy.deepcopy(ORIG)
        n.update(kw)
        return n

    try:
        case("R1 同世界", ORIG, [nd()], "oldhash0", True, "r1")
        case("R1 守卫(空树常量)", ORIG, [], "oldhash0", False)
        case("R2 身份", ORIG,
             [nd(rect=[50, 110, 610, 210], qrect="48,104,608,208")], "newhash1", True, "r2")
        case("R2 不唯一拒绝", ORIG,
             [nd(rect=[50, 110, 610, 210]), nd(rect=[50, 410, 610, 510])], "newhash1", False)
        case("R3 锚定", ORIG, [nd(digest="bbbb2222")], "newhash2", True, "r3")
        case("R4 宽松", nd(viewId=""),
             [nd(viewId="", digest="cccc3333", qrect="40,104,600,204", rect=[40, 104, 600, 204])],
             "newhash3", True, "loose")
        case("R4 位移过大拒绝", nd(viewId=""),
             [nd(viewId="", digest="dddd4444", qrect="40,900,600,1000", rect=[40, 900, 600, 1000])],
             "newhash4", False)
    finally:
        st.observe, st.foreground, st.load_state = real

    print("-- 7) R1-R4 解析单测（合成树，覆盖真机造不出的正例）")
    for n, ok, d in out:
        print("     %s %-18s %s" % ("v" if ok else "x", n, d))
    bad = [x for x in out if not x[1]]
    return (not bad, "%d/%d" % (len(out) - len(bad), len(out)))


def main():
    print("=== 空树 / G2 冒烟验收 ===")

    print("-- 1) 启动空树夹具")
    adb("shell", "input", "keyevent", "KEYCODE_WAKEUP")
    adb("shell", "wm", "dismiss-keyguard")     # 2026-10-01 加：睡醒可能带锁屏/息屏时钟
    adb("shell", "cmd", "statusbar", "collapse") # 2026-10-01 加：通知栏/息屏时钟挂在前台会污染夹具页
    adb("shell", "am", "start", "-n", FIXTURE)
    time.sleep(3)

    print("-- 2) look")
    rc, out, err = safe("look", "--note", "smoke")
    check("look 退出码 0", rc == 0, "rc=%d" % rc)
    d, jok = {}, False
    try:
        d = json.loads(out)
        jok = True
    except Exception as e:
        check("stdout 是单个 JSON", False, str(e)[:60])
    if jok:
        check("stdout 是单个 JSON", True)
        check("zone_coverage == none", d.get("zone_coverage") == "none", str(d.get("zone_coverage")))
        check("node_count == 0", d.get("node_count") == 0, str(d.get("node_count")))
        check("stderr 有「空转」警告", "空转" in err)
        check("有 shot_black / interactive 字段", ("shot_black" in d) and ("interactive" in d))
    sid = d.get("shot_id", "")

    # 2026-10-21：先清熔断计数，避免上一轮运行留下的 G4 状态把本用例误判成失败
    try:
        import json as _json
        _sp = os.path.join(HERE, "safe_tap_state.json")
        _st = _json.load(open(_sp))
        _st["taps"], _st["keys"], _st["gestures"] = [], [], []
        _json.dump(_st, open(_sp, "w"), ensure_ascii=False)
    except Exception:
        pass

    print("-- 3) tap 不带 ack（期望被拒 rc=2）")
    rc, out, err = safe("tap", "--shot", sid, "--x", "600", "--y", "900", "--expect", "smoke")
    check("被闸门拒绝（rc=2）", rc == 2, "rc=%d" % rc)
    both = err + out
    # 2026-10-21 政策更新：空树不再一律拒绝，改为「先看截图 → --ack-no-zones → OCR 扫危险词」
    check("理由提到「空控件树」+「ack」", ("空控件树" in both) and ("ack" in both))

    print("-- 4) tap 带 --ack-no-zones（空树 + OCR 扫描干净 → 放行）")
    rc, out, err = safe("tap", "--shot", sid, "--x", "600", "--y", "900",
                        "--expect", "smoke", "--ack-no-zones")
    check("ack + OCR 干净 → 放行（rc=0）", rc == 0, "rc=%d" % rc)

    print("-- 5) key HOME 不带 ack（期望导航键豁免放行 rc=0）")
    # 上一步 tap 已消费凭据，这里重新 look 取新凭据
    rc2, out2, err2 = safe("look", "--note", "smoke-key")
    try:
        sid = json.loads(out2).get("shot_id", sid)
    except Exception:
        pass
    rc, out, err = safe("key", "--shot", sid, "--code", "3")
    check("豁免放行（rc=0）", rc == 0, "rc=%d" % rc)

    print("-- 6) 复位")
    adb("shell", "input", "keyevent", "3")

    check("R1-R4 解析单测", *_r_tests())

    # 2026-10-01（回归修复防再犯）：收件箱 add() 最小断言——核心写入路径坏一次全断，必须有人报警
    try:
        sys.path.insert(0, HERE)
        import inbox as _inbox
        _it = _inbox.add("smoke", "test", "收件箱探针", "smoke-probe")
        _ok = _it is not None and isinstance(_it.get("ts"), int)
        _rows = []
        for _ln in open(_inbox.PATH, encoding="utf-8", errors="ignore"):
            _ln = _ln.strip()
            if not _ln:
                continue
            try:
                _r = json.loads(_ln)
            except Exception:
                continue
            if _r.get("fingerprint") == (_it or {}).get("fingerprint"):
                continue   # 清理探针条目
            _rows.append(_r)
        with open(_inbox.PATH, "w", encoding="utf-8") as _f:
            for _r in _rows:
                _f.write(json.dumps(_r, ensure_ascii=False) + "\n")
        _seen = _inbox._load_seen()
        _seen.discard((_it or {}).get("fingerprint"))
        _inbox._save_seen(_seen)
        results.append(("收件箱 add() 写入+清理", _ok, "" if _ok else "add 返回异常"))
    except Exception as _e:
        results.append(("收件箱 add() 写入+清理", False, str(_e)[:120]))
    # 2026-10-01（复核防再犯）：long_press/drag 命令在册 + 参数 + 分发（声称的功能必须可被机器验证）
    try:
        src = open(os.path.join(HERE, "safe_tap.py"), encoding="utf-8", errors="ignore").read()
        check("long_press/drag 解析器在册",
              ("add_parser(\"long_press\"" in src) and ("add_parser(\"drag\"" in src), "缺解析器")
        check("long_press/drag --ms 参数", src.count('"--ms"') >= 2, str(src.count('"--ms"')))
        check("do_action 分发在册",
              ('elif kind == "long_press"' in src) and ('elif kind == "drag"' in src), "缺分发")
    except Exception as _e:
        check("long_press/drag 检查异常", False, str(_e)[:120])
    bad = [r for r in results if not r[1]]
    print()
    print("=== 结果：%d/%d 通过 ===" % (len(results) - len(bad), len(results)))
    for name, _, detail in bad:
        print("   ✗ %s  %s" % (name, detail))
    print("SMOKE_PASS" if not bad else "SMOKE_FAIL")
    # 2026-10-01：每次回归顺手打一行基线，方便任何改动做前后对比（复测者建议）
    try:
        p2 = subprocess.run([sys.executable, os.path.join(HERE, "safe_tap_stats.py"), "--exclude-test"],
                            capture_output=True, timeout=60)
        for ln in p2.stdout.decode("utf-8", "ignore").splitlines():
            if "无效率" in ln or "放行的 tap" in ln:
                print("   基线 | " + ln.strip())
    except Exception:
        pass
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
