#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""task_templates.py —— 任务模板沉淀（Tier-1①，code-as-memory 的任务级形态）。

规则：同一任务名在审计里 **成功 ≥3 次** → 可固化为 skills/tasks/<安全名>.py。
模板头写清：任务名、已验证次数、最后验证日期、用法。
危险流程（退出登录/删除/支付/转账/卸载/注销）**不固化**。

用法：
  python3 task_templates.py --list
  python3 task_templates.py --promote "查京东订单送达时间"
"""
import argparse
import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
AUDIT = os.path.join(HERE, "safe_tap_audit.jsonl")
TASKS_DIR = os.path.join(HERE, "tasks")
_DENY = ("退出登录", "删除", "支付", "转账", "卸载", "注销")
_MIN_OK = 3


def load_task_ends():
    ends = []
    if not os.path.isfile(AUDIT):
        return ends
    with open(AUDIT, encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("kind") == "task_end":
                ends.append(r)
    return ends


def summarize():
    by = {}
    for r in load_task_ends():
        name = r.get("task_name") or "(未命名)"
        t = by.setdefault(name, {"n": 0, "ok": 0, "last": "", "script": None})
        t["n"] += 1
        if r.get("ok"):
            t["ok"] += 1
            t["last"] = r.get("ts_local") or t["last"]
            t["script"] = r.get("script") or t["script"]
    return by


def main():
    ap = argparse.ArgumentParser(description="任务模板沉淀（成功≥3次才可固化）")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--promote", default="", help="要固化的任务名")
    a = ap.parse_args()
    by = summarize()
    if a.list or not a.promote:
        print("%-32s %4s %4s %6s  %s" % ("任务名", "次数", "成功", "可固化", "最后成功"))
        for name, t in sorted(by.items(), key=lambda x: -x[1]["ok"]):
            print("%-32s %4d %4d %6s  %s"
                  % (name[:32], t["n"], t["ok"],
                     "✓" if t["ok"] >= _MIN_OK else "×", (t["last"] or "-")[:19]))
        if not by:
            print("  （审计里还没有 task_end 记录）")
        return 0
    name = a.promote
    t = by.get(name)
    if not t or t["ok"] < _MIN_OK:
        print("✗ %r 成功 %d 次，不足 %d 次，暂不固化" % (name, (t or {}).get("ok", 0), _MIN_OK))
        return 2
    if any(w in name for w in _DENY):
        print("✗ 危险流程不固化：%s" % name)
        return 2
    if not (t.get("script") and os.path.isfile(t["script"])):
        print("✗ 找不到该任务最近一次成功运行的脚本: %s" % t.get("script"))
        return 2
    os.makedirs(TASKS_DIR, exist_ok=True)
    safe = re.sub(r"[^\w\u4e00-\u9fff]+", "_", name)[:40].strip("_") or "task"
    dest = os.path.join(TASKS_DIR, safe + ".py")
    body = open(t["script"], encoding="utf-8", errors="replace").read()
    header = ("# 任务模板（自动沉淀于 %s）\n"
              "# 任务名: %s\n"
              "# 适用: 以脚本内容与实际页面为准\n"
              "# 已验证: 成功 %d 次（审计 task_end 口径）\n"
              "# 最后验证: %s\n"
              "# 用法: python3 skills/dsb_run.py skills/tasks/%s.py --task-name \"%s\"\n\n"
              % (t["last"], name, t["ok"], t["last"], safe, name))
    with open(dest, "w", encoding="utf-8") as f:
        f.write(header + body)
    print("✓ 已固化: %s" % dest)
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
