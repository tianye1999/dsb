#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""walk.py —— 列表遍历原语（借鉴 phone-harness scroll_collect/scroll_until）。

流程（每步）：look（凭据+内容）→ 防甩飞滚动（走 safe_tap 闸门：G1/G2/G3 + 留证）
→ look（新内容）→ 去重收集。无新文字不证明已经到底。

只读：全程只滚动，不点击任何元素。
默认最多3次滚动；--until-text 命中即停，不点击。去重文字数不是设备/消息数。
退出码：0 有界收集/候选命中，2 闸门拒绝，3 观测或动作失败，4 范围内未命中。
"""
import argparse
import contextlib
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import safe_tap as st  # noqa: E402


def _look(note, ocr=False):
    """跑一次 look（复用 safe_tap 全部逻辑：危险区/熔断清理/状态登记），返回其 JSON。
    look 的 stdout 不含节点列表（§12 已知项）→ 从状态文件 last_shot 补上。"""
    ns = argparse.Namespace(note=note, ocr=ocr)
    buf, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
        rc = st.cmd_look(ns)
    try:
        look = json.loads(buf.getvalue())
    except Exception:
        return None, rc
    try:
        last = (st.load_state() or {}).get("last_shot") or {}
        if last.get("id") != look.get("shot_id"):
            return None, 3
        look["nodes"] = last.get("nodes") or []
    except Exception:
        return None, 3
    return look, rc


def _scroll(a, shot_id, ack):
    """一步防甩飞滚动（走 do_action：闸门+留证+稳定轮询）。返回 (rc, 信息)。"""
    ns = argparse.Namespace(shot=shot_id, x=a.x, y=a.y,
                            dy=(-a.amount if a.dir == "down" else a.amount),
                            steps=a.steps, expect="walk-scroll-%d" % scrolls[0],
                            expect_activity=None, ack_no_zones=ack, fast=False)
    buf, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
        rc = st.do_action(ns, "scroll", [(a.x, a.y), (a.x, a.y + (-a.amount if a.dir == "down" else a.amount))])
    info = {}
    try:
        info = json.loads(buf.getvalue())
    except Exception:
        pass
    if rc:
        info["detail"] = err.getvalue().strip()[-1200:]
    return rc, info


scrolls = [0]


def main():
    ap = argparse.ArgumentParser(description="列表遍历：滚动+去重收集（只读）")
    ap.add_argument("--dir", choices=["down", "up"], default="down")
    ap.add_argument("--max", type=int, default=3, help="最多滚动次数；0只看当前页，不重置共享预算")
    ap.add_argument("--end-after", type=int, default=3, help="连续 N 次无新文字就停，不证明到底")
    ap.add_argument("--until-text", help="可见文本包含此字符串就停；只返回候选，不点击")
    ap.add_argument("--ocr", action="store_true", help="每次观测同时识别像素文字")
    ap.add_argument("--amount", type=int, default=520, help="每步内容位移(px)")
    ap.add_argument("--x", type=int, default=630)
    ap.add_argument("--y", type=int, default=1500)
    ap.add_argument("--steps", type=int, default=8)
    ap.add_argument("--json", action="store_true", help="只输出 JSON")
    a = ap.parse_args()
    if a.max < 0 or a.end_after < 1 or (a.until_text is not None and not a.until_text.strip()):
        ap.error("--max必须非负，--end-after必须为正，--until-text不能空白")

    def extract(look):
        """可见文本行（剔除状态栏/导航条band），dedup key = 文本。"""
        out = []
        for n in look.get("nodes") or []:
            t = (n.get("text") or n.get("desc") or "").strip()
            r = n.get("rect") or [0, 0, 0, 0]
            y = (int(r[1]) + int(r[3])) // 2 if len(r) == 4 else 0
            if t and (a.until_text is not None or 150 < y < 2600):
                out.append({"text": t, "y": y, "rect": r, "source": "tree"})
        for n in look.get("pixel_nodes") or []:
            t = (n.get("text") or "").strip()
            if t and (a.until_text is not None or 150 < n.get("y", 0) < 2600):
                out.append({**n, "text": t, "source": "pixels"})
        return out

    items, seen = [], set()
    stop, stale, scrolls = "max-scrolls", 0, 0
    matches, detail = [], ""

    def observe(note):
        return _look(note, ocr=True) if a.ocr else _look(note)

    def usable(look, rc):
        return bool(look and rc == 0 and look.get("ok") is not False
                    and not look.get("error") and not look.get("shot_black")
                    and look.get("interactive") is not False)

    def target_matches(boxes):
        if a.until_text is None:
            return []
        needle = a.until_text.strip().casefold()
        return [b for b in boxes if needle in b["text"].casefold()]

    def ingest(boxes):
        new = 0
        for b in boxes:
            k = b["text"]
            if k in seen:
                continue
            seen.add(k)
            items.append(b)
            new += 1
        return new

    look, rc = observe("walk-initial")
    if not usable(look, rc):
        stop = "look-failed"
    else:
        boxes = extract(look)
        ingest(boxes)
        matches = target_matches(boxes)
        if matches:
            stop = "target-found"

    while stop == "max-scrolls" and scrolls < a.max:
        shot = (st.load_state().get("last_shot") or {})
        sid, ack = shot.get("id"), (shot.get("node_count") == 0)
        rc, sinfo = _scroll(a, sid, ack)
        if rc != 0:
            stop = "gate-refused" if rc == 2 else "action-failed"
            detail = sinfo.get("detail") or sinfo.get("reason") or ""
            break
        scrolls += 1
        look, rc = observe("walk-%d" % scrolls)
        if not usable(look, rc):
            stop = "look-failed"
            break
        boxes = extract(look)
        new = ingest(boxes)
        matches = target_matches(boxes)
        if matches:
            stop = "target-found"
            break
        if new:
            stale = 0
        else:
            stale += 1
            if stale >= a.end_after:
                stop = "no-new-text"
                break

    out = {"items": items, "stop": stop, "scrolls": scrolls,
           "unique_texts": len(seen), "dir": a.dir, "complete": stop == "target-found",
           "matches": matches, "target": a.until_text,
           "shot_id": (look or {}).get("shot_id"), "screenshot": (look or {}).get("screenshot"),
           "detail": detail, "retryable": False}
    if a.json:
        print(json.dumps(out, ensure_ascii=False, indent=1))
    else:
        print("walk: %d 步，收集 %d 条（去重后 %d），停止原因=%s"
              % (scrolls, len(items), len(seen), stop))
        for b in items[:40]:
            print("  · %s" % b["text"][:60])
    if stop == "gate-refused":
        return 2
    if stop in ("look-failed", "action-failed"):
        return 3
    return 4 if a.until_text is not None and not matches else 0


if __name__ == "__main__":
    sys.exit(main())
