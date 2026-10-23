#!/usr/bin/env python3
"""语义动作 v1（2026-10-21 R8）：说要什么，不教怎么点。

叠在 safe_tap 之上，**只通过 safe_tap.py 的 CLI 入口**观测与动作：G1–G5、虚拟屏策略、
空树 ack + OCR 扫词 + 视觉危险判断一个不少，本模块不新增任何放行路径。

  find  先 `look --target`（控件树 → OCR，精确匹配文字）；没找到且调用方给了 --region，
        才对**同一帧**截图的该区域裁剪图做视觉定位（只外发裁剪图，D2）。
        多个候选 → ambiguous，不替调用方挑；找不到 / 看不了 → not_found / unknown。
  tap   find 恰好一个候选 → 用同一张凭据走 `safe_tap.py tap`。
        **不自动加 --ack-no-zones**（2026-10-02 根除过自动 ack），调用方显式给才透传。

一次调用完成"看 → 找 → 闸门 → 点"，省掉大脑多轮往返。结果仍只是动作回执，goal_verified=false。

用法：
  python3 act.py find --target 文件传输助手 --display 7
  python3 act.py find --target "顶部栏右侧的搜索（放大镜）图标" --region top --display 7
  python3 act.py tap  --target 通讯录 --display 7 --ack-no-zones
退出码：0 找到/已派发；2 闸门拒绝或候选不唯一；3 观测/派发失败；4 没找到。
"""
import argparse
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SAFE_TAP = os.path.join(HERE, "safe_tap.py")
REGIONS = {"top": (0.0, 0.0, 1.0, 0.25), "middle": (0.0, 0.2, 1.0, 0.8), "bottom": (0.0, 0.75, 1.0, 1.0)}


def _safe_tap(args, timeout=180):
    r = subprocess.run([sys.executable, SAFE_TAP] + args, capture_output=True, text=True, timeout=timeout)
    try:
        out = json.loads(r.stdout) if r.stdout.strip() else None
    except ValueError:
        out = None
    return r.returncode, out, (r.stderr or "")[-400:]


def _center(c):
    """树节点用 rect 中心；OCR 行用 x/y（已是中心）。"""
    rect = c.get("rect")
    if isinstance(rect, (list, tuple)) and len(rect) == 4:
        return (int(rect[0]) + int(rect[2])) // 2, (int(rect[1]) + int(rect[3])) // 2
    if c.get("x") is not None and c.get("y") is not None:
        return int(c["x"]), int(c["y"])
    return None


def _region_box(region, w, h):
    if region in REGIONS:
        fx1, fy1, fx2, fy2 = REGIONS[region]
        return [int(fx1 * w), int(fy1 * h), int(fx2 * w), int(fy2 * h)]
    parts = [int(v) for v in str(region).split(",")]
    if len(parts) != 4:
        raise ValueError("--region 只接受 top/middle/bottom 或 x1,y1,x2,y2")
    return parts


def find(a):
    args = ["look", "--target", a.target, "--note", "act.find"]
    if a.display:
        args += ["--display", str(a.display)]
    rc, look, err = _safe_tap(args)
    if rc != 0 or not look or not look.get("shot_id"):
        return 3, {"ok": False, "status": "unknown", "reason": "look_failed", "rc": rc,
                   "detail": (look or {}).get("error") or err}
    base = {"shot_id": look["shot_id"], "display_id": look.get("display_id", 0),
            "activity": look.get("activity"), "zone_coverage": look.get("zone_coverage")}
    t = look.get("target") or {}
    cands = []
    for c in t.get("candidates") or []:
        pt = _center(c)
        if pt:
            cands.append({"x": pt[0], "y": pt[1], "source": t.get("source"), "text": c.get("text")})
    if t.get("status") == "ambiguous" or len(cands) > 1:
        return 2, dict(base, ok=False, status="ambiguous", candidates=cands)
    if t.get("status") == "found" and len(cands) == 1:
        return 0, dict(base, ok=True, status="found", candidates=cands)
    # 文字没对上：只有调用方给了区域才动用视觉（D2：只发该区域裁剪图）
    if not a.region:
        return 4, dict(base, ok=False, status=t.get("status") or "not_found", candidates=[],
                       hint="文字未命中；若目标是图标或文字不精确，加 --region top|middle|bottom|x1,y1,x2,y2 用视觉定位")
    try:
        import vision_check
        from PIL import Image
        shot = look.get("screenshot") or ""
        w, h = Image.open(shot).size
        box = _region_box(a.region, w, h)
    except Exception as e:
        return 3, dict(base, ok=False, status="unknown", reason="vision_setup_failed:%s" % type(e).__name__)
    loc = vision_check.locate(shot, a.target, box)
    vis = {k: loc.get(k) for k in ("status", "ms", "raw")}
    if loc.get("status") == "found":
        return 0, dict(base, ok=True, status="found", vision=vis, region=box,
                       candidates=[{"x": loc["x"], "y": loc["y"], "source": "vision", "text": None}])
    if loc.get("status") == "not_found":
        return 4, dict(base, ok=False, status="not_found", vision=vis, region=box, candidates=[])
    return 3, dict(base, ok=False, status="unknown", vision=vis, region=box, candidates=[])


def tap(a):
    rc, res = find(a)
    if rc != 0:
        return rc, dict(res, dispatched=False)
    c = res["candidates"][0]
    args = ["tap", "--shot", str(res["shot_id"]), "--x", str(c["x"]), "--y", str(c["y"]),
            "--expect", a.expect or a.target]
    if a.display:
        args += ["--display", str(a.display)]
    if a.ack_no_zones:
        args.append("--ack-no-zones")      # 只透传调用方的显式确认，从不自动加
    if a.fast:
        args.append("--fast")
    trc, tout, err = _safe_tap(args)
    return trc, dict(res, dispatched=(trc == 0), tap_rc=trc, tap=tout,
                     refused=(err if trc == 2 else None))


def main(argv=None):
    ap = argparse.ArgumentParser(description="语义动作 v1：find / tap（全部经 safe_tap 闸门）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("find", "tap"):
        p = sub.add_parser(name)
        p.add_argument("--target", required=True, help="要找的文字（精确匹配）或视觉描述（配 --region）")
        p.add_argument("--display", type=int, default=0)
        p.add_argument("--region", default=None, help="视觉定位区域：top/middle/bottom 或 x1,y1,x2,y2（截图像素）")
        if name == "tap":
            p.add_argument("--expect", default="", help="留证：你以为点的是什么（默认同 --target）")
            p.add_argument("--ack-no-zones", dest="ack_no_zones", action="store_true",
                           help="显式透传给 safe_tap（空树页需要；不授予权限，闸门照常判）")
            p.add_argument("--fast", action="store_true")
    a = ap.parse_args(argv)
    rc, res = (find if a.cmd == "find" else tap)(a)
    print(json.dumps(res, ensure_ascii=False, indent=1))
    return rc


if __name__ == "__main__":
    sys.exit(main())
