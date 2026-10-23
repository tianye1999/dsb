#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""inbox.py —— 统一收件箱（Tier-2 盯梢→汇报的主通道）。

所有"要告诉用户的事"进这里（JSONL，一行一条）。safe_tap.py inbox 是汇报入口。
- 指纹去重：source + 归一化文本 hash，24h 内只进一次；
- 脱敏：写盘前对手机号/身份证/银行卡/验证码打码（保留结构）。
"""
import hashlib
import json
import os
import re
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(HERE, "inbox.jsonl")
SEEN = os.path.join(HERE, "inbox_seen.json")   # 指纹独立存放：删除条目不删指纹（复核遗留三）
WINDOW_S = 24 * 3600

_RE_PHONE = re.compile(r"(?<!\d)(1[3-9]\d)\d{4}(\d{4})(?!\d)")
_RE_ID = re.compile(r"(?<!\d)(\d{6})\d{8}(\d{3}[\dXx])(?!\d)")
_RE_CARD = re.compile(r"(?<!\d)(\d{6})\d{6,9}(\d{4})(?!\d)")
_RE_CODE = re.compile(r"(验证码[^^\d]{0,4})(\d{2})\d{2,6}")


def mask(s):
    """脱敏：手机号 138****1234 / 身份证前6后4 / 银行卡前6后4 / 验证码留末2位。"""
    if not s:
        return s
    s = _RE_ID.sub(r"\1********\2", s)
    s = _RE_CARD.sub(r"\1******\2", s)
    s = _RE_PHONE.sub(r"\1****\2", s)
    s = _RE_CODE.sub(r"\1****\2", s)
    return s


def _norm(s):
    return re.sub(r"\s+", "", s or "")


def fingerprint(source, text):
    return hashlib.sha1((source + _norm(text)).encode("utf-8")).hexdigest()[:16]


def items(all=False):
    if not os.path.isfile(PATH):
        return []
    out = []
    with open(PATH, encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            if all or not r.get("read"):
                out.append(r)
    return sorted(out, key=lambda r: -r.get("ts", 0))


def _load_seen():
    try:
        return set(json.load(open(SEEN, encoding="utf-8")))
    except Exception:
        return set()


def _save_seen(seen):
    json.dump(sorted(seen), open(SEEN, "w", encoding="utf-8"), ensure_ascii=False)


def add(source, kind, title, summary="", detail="", priority="normal"):
    """加一条（指纹独立存放去重：删除条目/划掉通知都不会导致重复入箱）。返回新条目；重复 None。"""
    title = mask(str(title or ""))[:80]
    summary = mask(str(summary or ""))[:200]
    fp = fingerprint(source, title + " " + summary)
    seen = _load_seen()
    if fp in seen:
        return None
    now = time.time()   # 2026-10-01 修：指纹独立存放重构时误删了这行 → add() NameError（收件箱写入路径全断）
    item = {"id": fp[:12], "ts": int(now),
            "ts_local": time.strftime("%Y-%m-%d %H:%M"),
            "source": source, "kind": kind, "title": title,
            "summary": summary, "detail": mask(str(detail or ""))[:300],
            "priority": priority, "fingerprint": fp, "read": False}
    with open(PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(item, ensure_ascii=False) + "\n")
    seen.add(fp)
    _save_seen(seen)
    return item


def drop_test(keyword="测试"):
    """删除测试条目（title/summary 含 keyword）。返回删除数。（验收后清测试数据用）"""
    if not os.path.isfile(PATH):
        return 0
    rows, n = [], 0
    with open(PATH, encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            if keyword in (r.get("title") or "") or keyword in (r.get("summary") or ""):
                n += 1
                continue
            rows.append(r)
    with open(PATH, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return n


def mark_read(id_or_all):
    if not os.path.isfile(PATH):
        return 0
    rows = []
    n = 0
    with open(PATH, encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            if id_or_all == "all" or r.get("id") == id_or_all or r.get("fingerprint") == id_or_all:
                if not r.get("read"):
                    n += 1
                r["read"] = True
            rows.append(r)
    with open(PATH, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return n


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="统一收件箱")
    ap.add_argument("--add", nargs=4, metavar=("SOURCE", "KIND", "TITLE", "SUMMARY"))
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--mark-read", default=None)
    ap.add_argument("--drop-test", action="store_true", help="删除含'测试'字样的条目")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    if a.add:
        it = add(*a.add)
        print(json.dumps(it or {"dedup": True}, ensure_ascii=False))
    elif a.drop_test:
        print("已删除 %d 条测试条目" % drop_test())
    elif a.mark_read:
        print("已标记 %d 条" % mark_read(a.mark_read))
    else:
        rows = items(all=a.all)
        if a.json:
            print(json.dumps(rows, ensure_ascii=False, indent=1))
        elif not rows:
            print("收件箱：无未读")
        else:
            for r in rows:
                print("* [%s] %s | %s — %s (%s)"
                      % (r.get("priority"), r.get("kind"), r.get("title"),
                         r.get("summary"), r.get("ts_local")))
            print("共 %d 条" % len(rows))
