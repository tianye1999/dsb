#!/usr/bin/env python3
"""空控件树页的视觉危险判断（2026-10-21，D1/D2 最小实现）。

把凭据截图里**目标点周边的裁剪图**（红圈标出点击位置）交给 deepseek-flash，
问"点这一下会不会立即产生他人可见、或事后无法撤销的后果"。

- 只会收紧：impact 不是 none（含 unclear）→ 调用方拒绝；
  调用失败返回 status=unavailable/error，由调用方维持原有 OCR 规则（不因此放宽）。
- 外发范围：只发裁剪图（默认目标点 ±240px），不发整屏（owner 2026-10-21 定 D2）。
- prompt 不列任何"危险/安全"清单（owner 反枚举原则），只给判断标准。
- key 读 /root/.dsh/.deepseek-key，绝不打印。
"""
import base64
import io
import json
import os
import time
import urllib.error
import urllib.request

KEY_PATH = os.environ.get("DSB_VISION_KEY_PATH", "/root/.dsh/.deepseek-key")
API_URL = os.environ.get("DSB_VISION_API", "https://api.deepseek.com/chat/completions")
MODEL = os.environ.get("DSB_VISION_MODEL", "deepseek-flash")
IMPACTS = ("none", "external", "irreversible", "unclear")

SYSTEM = ("你在审查一次手机界面操作。图片是目标附近的局部截图，红圈中心是即将被操作的位置。"
          "只判断这一次操作本身会立即导致什么，不推测之后的操作。")
USER = ("即将执行的操作：在红圈中心{action}。调用方说明的目的：{intent}\n"
        "判断这个操作是否会立即导致：(1) 别人能看到或收到新内容；"
        "或 (2) 账号、资金或数据发生之后无法简单撤销的改变。\n"
        "只输出 json：{{\"impact\":\"none|external|irreversible|unclear\","
        "\"target\":\"红圈处是什么\",\"reason\":\"一句话\"}}。"
        "红圈处看不清或无法判断时用 unclear。")


def _crop_marked(img_path, x, y, half):
    from PIL import Image, ImageDraw
    im = Image.open(img_path).convert("RGB")
    w, h = im.size
    box = (max(0, x - half), max(0, y - half), min(w, x + half), min(h, y + half))
    crop = im.crop(box)
    cx, cy = x - box[0], y - box[1]
    r = max(14, half // 10)
    ImageDraw.Draw(crop).ellipse((cx - r, cy - r, cx + r, cy + r), outline=(255, 0, 0), width=4)
    buf = io.BytesIO()
    crop.save(buf, "JPEG", quality=85)
    return buf.getvalue(), list(box)


def judge_tap(img_path, x, y, intent="", action="点击", timeout=20.0, half=240):
    """返回 {"status": ok|unavailable|error, "impact", "target", "reason", "ms", "box", "usage"}。

    action 必须是实际要执行的动作（如"点击"、"长按约 800 毫秒"），不能把长按当点击问。"""
    t0 = time.time()
    out = {"status": "error", "impact": None, "target": "", "reason": "", "ms": 0,
           "box": None, "usage": None, "model": MODEL}
    try:
        key = open(KEY_PATH).read().strip()
        if not key:
            raise OSError("empty key")
    except OSError as e:
        out.update(status="unavailable", reason="key: %s" % type(e).__name__)
        return out
    try:
        jpg, box = _crop_marked(img_path, int(x), int(y), int(half))
    except Exception as e:
        out.update(status="error", reason="crop: %s" % type(e).__name__)
        return out
    out["box"] = box
    body = {"model": MODEL, "thinking": {"type": "disabled"}, "temperature": 0, "max_tokens": 200,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": SYSTEM},
                         {"role": "user", "content": [
                             {"type": "image_url",
                              "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(jpg).decode()}},
                             {"type": "text", "text": USER.format(action=action,
                                                                  intent=(intent or "未说明").strip()[:200])}]}]}
    req = urllib.request.Request(API_URL, data=json.dumps(body).encode(),
                                 headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    try:
        resp = json.load(urllib.request.urlopen(req, timeout=timeout))
    except urllib.error.HTTPError as e:
        out.update(status="unavailable", reason="http %s" % e.code, ms=int((time.time() - t0) * 1000))
        return out
    except Exception as e:
        out.update(status="unavailable", reason=type(e).__name__, ms=int((time.time() - t0) * 1000))
        return out
    out["ms"] = int((time.time() - t0) * 1000)
    try:
        out["usage"] = resp.get("usage")
        ans = json.loads(resp["choices"][0]["message"]["content"])
        impact = str(ans.get("impact", "")).strip().lower()
    except Exception:
        out.update(status="error", reason="unparseable model output")
        return out
    if impact not in IMPACTS:
        out.update(status="error", reason="impact=%r not in %s" % (impact, IMPACTS))
        return out
    out.update(status="ok", impact=impact, target=str(ans.get("target", ""))[:60],
               reason=str(ans.get("reason", ""))[:120])
    return out


LOCATE_SYSTEM = ("## Role:\nYou are a GUI click grounding agent.\n"
                 "## Objective:\nIdentify the UI element in the screenshot that matches the user's description "
                 "and return the center point of that element.\n"
                 "## Output Format:\nReturn exactly one point, without any explanation:\n"
                 "<｜｜point｜｜>[x, y]<｜｜/point｜｜>\n"
                 "Coordinate values must be integers, normalized to 0-1000 relative to the full image width and height; "
                 "the origin is the top-left of the image.\n"
                 "If no element in the image matches the description, output exactly: NOT_FOUND")


def locate(img_path, desc, box, timeout=20.0):
    """在凭据截图的 box=[x1,y1,x2,y2] 区域里找 desc（只外发该区域裁剪图，D2）。

    返回 {"status": found|not_found|unavailable|error, "x", "y"（截图像素坐标）, "ms", "usage", "raw"}。
    模型坐标是 0-1000 归一化到**发出去的裁剪图**（Midscene DeepSeek 适配同一约定；
    2026-10-21 真机：微信顶部 0..360 条带里找到搜索图标并成功点开）。
    """
    import re
    t0 = time.time()
    out = {"status": "error", "x": None, "y": None, "ms": 0, "usage": None, "raw": "", "model": MODEL}
    try:
        key = open(KEY_PATH).read().strip()
        if not key:
            raise OSError("empty key")
    except OSError as e:
        out.update(status="unavailable", raw="key: %s" % type(e).__name__)
        return out
    try:
        from PIL import Image
        im = Image.open(img_path).convert("RGB")
        x1, y1, x2, y2 = (int(v) for v in box)
        x1, y1 = max(0, x1), max(0, y1)
        x2, y2 = min(im.size[0], x2), min(im.size[1], y2)
        if x2 - x1 < 8 or y2 - y1 < 8:
            raise ValueError("box too small")
        buf = io.BytesIO()
        im.crop((x1, y1, x2, y2)).save(buf, "JPEG", quality=85)
    except Exception as e:
        out.update(status="error", raw="crop: %s" % type(e).__name__)
        return out
    body = {"model": MODEL, "thinking": {"type": "disabled"}, "temperature": 0, "max_tokens": 40,
            "messages": [{"role": "system", "content": LOCATE_SYSTEM},
                         {"role": "user", "content": [
                             {"type": "image_url",
                              "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()}},
                             {"type": "text", "text": "要找的元素：" + str(desc)[:120]}]}]}
    req = urllib.request.Request(API_URL, data=json.dumps(body).encode(),
                                 headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    try:
        resp = json.load(urllib.request.urlopen(req, timeout=timeout))
    except urllib.error.HTTPError as e:
        out.update(status="unavailable", raw="http %s" % e.code, ms=int((time.time() - t0) * 1000))
        return out
    except Exception as e:
        out.update(status="unavailable", raw=type(e).__name__, ms=int((time.time() - t0) * 1000))
        return out
    out["ms"] = int((time.time() - t0) * 1000)
    try:
        out["usage"] = resp.get("usage")
        raw = str(resp["choices"][0]["message"]["content"] or "")
    except Exception:
        out.update(status="error", raw="unparseable response")
        return out
    out["raw"] = raw[:80]
    if raw.strip() == "NOT_FOUND":
        out["status"] = "not_found"
        return out
    m = re.fullmatch(r"\s*<(?:｜｜|\|)point(?:｜｜|\|)>\s*\[\s*(\d+)\s*,\s*(\d+)\s*\]\s*<(?:｜｜|\|)/point(?:｜｜|\|)>\s*", raw)
    if not m or int(m.group(1)) > 1000 or int(m.group(2)) > 1000:
        out["status"] = "error"          # 解析失败 = 没有候选，绝不当成"找到"
        return out
    # 裁剪图像素是 x1..x2-1：按 (宽-1) 缩放，1000 落在最后一列而不是框外
    out.update(status="found",
               x=x1 + round(int(m.group(1)) / 1000 * (x2 - x1 - 1)),
               y=y1 + round(int(m.group(2)) / 1000 * (y2 - y1 - 1)))
    return out


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 4:
        print("用法: vision_check.py <截图> <x> <y> [目的]", file=sys.stderr)
        sys.exit(2)
    print(json.dumps(judge_tap(sys.argv[1], sys.argv[2], sys.argv[3],
                               sys.argv[4] if len(sys.argv) > 4 else ""), ensure_ascii=False))
