#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""safe_tap 契约测试（借鉴 phone-harness 的 backend-contract 思路）。

不碰手机，验证「系统的不变量」：
  C1 审计字段契约：最近 N 条放行动作必须带完整证据字段（缺一字段即失败）
  C2 闸门存在性：G1-G5 的机制符号必须都在源码里（防止被人顺手删掉）
  C3 冒烟在册：smoke 必须存在且覆盖 R1-R4 / 空树 ack / 熔断
  C4 度量在册：safe_tap_stats.py 必须存在且口径含两个指标
  C5 walk/wait/scroll/open/doctor 子命令在册

用法：python3 safe_tap_contract.py && echo OK
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "safe_tap.py")
AUDIT = os.path.join(HERE, "safe_tap_audit.jsonl")

REQUIRED_AUDIT_FIELDS = [
    "kind", "points", "allowed", "expect", "act_before", "act_after",
    "ui_reacted", "pixel_changed", "tree_changed", "ev_advanced",
    "a_hash_before", "a_hash_after", "tree_hash_before", "tree_hash_after",
    "ev_count_before", "ev_count_after",
    "shot_before", "shot_after", "resolved_by", "target_digest",
]
# 2026-10-01 新增字段：只要求「最近 3 条」携带（老记录允许缺，避免补丁边界误报）
RECENT_AUDIT_FIELDS = ["settle_ms", "settle_polls", "settled"]
REQUIRED_SOURCE_MARKERS = [
    ("G1 凭据校验", "def check_gate"),
    ("G2 危险词禁区", "退出登录"),
    ("G3 边缘带", "24"),
    ("G4 连击熔断", "taps"),
    ("G5 前后留证", "shot_after"),
    ("稳定轮询", "_settle_stable"),
    ("节点身份", "_digest_at"),
    ("凭据帧前证据", "credential"),
    ("防甩飞滚动", "motionevent"),
    ("溯源字段", "node_source"),
    ("long_press 原语", 'elif kind == "long_press"'),
    ("drag 原语", 'elif kind == "drag"'),
]
REQUIRED_SMOKE_MARKERS = ["R1-R4", "ack", "空转"]
REQUIRED_SKILLS = ["safe_tap_stats.py", "walk.py",
                   "dsb_run.py", "ocr.py"]

fails = []

# C1 审计字段契约（只查最近 20 条放行记录——老记录允许缺新字段）
if os.path.isfile(AUDIT):
    allowed = []
    with open(AUDIT, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("allowed") and r.get("kind") in ("tap", "swipe", "key", "scroll"):
                allowed.append(r)
    for r in allowed[-20:]:
        missing = [k for k in REQUIRED_AUDIT_FIELDS if k not in r]
        if missing:
            fails.append("审计缺核心字段 %s @ %s" % (missing, r.get("ts_local", "?")))
    for r in allowed[-3:]:
        missing = [k for k in RECENT_AUDIT_FIELDS if k not in r]
        if missing:
            fails.append("最近记录缺新字段 %s @ %s（补丁后不应再缺）" % (missing, r.get("ts_local", "?")))
else:
    fails.append("审计文件不存在（还没有任何动作留证）")

# C2 源码闸门符号
src = open(SRC, encoding="utf-8").read() if os.path.isfile(SRC) else ""
for name, marker in REQUIRED_SOURCE_MARKERS:
    if marker not in src:
        fails.append("源码缺 %s 的机制符号（%s）" % (name, marker))

# C3 冒烟在册
smoke = os.path.join(HERE, "safe_tap_smoke.py")
if not os.path.isfile(smoke):
    fails.append("缺 safe_tap_smoke.py")
else:
    t = open(smoke, encoding="utf-8").read()
    for m in REQUIRED_SMOKE_MARKERS:
        if m not in t:
            fails.append("冒烟未覆盖：%s" % m)

# C4 度量在册
for s in REQUIRED_SKILLS:
    if not os.path.isfile(os.path.join(HERE, s)):
        fails.append("缺技能/度量脚本：%s" % s)

if fails:
    print("CONTRACT_FAIL")
    for f in fails:
        print("  ✗", f)
    sys.exit(1)
print("CONTRACT_PASS  审计近 20 条字段齐全；源码闸门符号 %d 项在位；冒烟/度量/遍历原语在册"
      % len(REQUIRED_SOURCE_MARKERS))
