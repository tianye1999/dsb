#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""safe_tap_stats.py —— 坐标点击质量统计（一条命令，口径写死，可当基线）。

复测者要求的两个**独立**指标（定义写死在这里，任何改动都以此为基线）：

  * **无效率** = ui_reacted=False / 样本数。含义："这个动作在语义上是空操作"，与点击机制无关，
    改进方向是**目标选择**（例如点到"已选中"的项）。
  * **脱靶率·上界估计** = (--exclude 后的剩余无效率)。含义："点击位置没落到目标上"。
    ⚠️ **审计单独判不出脱靶**，所以这只是一个**上界**；报数时必须一直带上"上界"二字，
    不能出现孤立的"脱靶率 x%"。
  * 样本按 activity 成簇 → **池化 CI 只能当参考，决策优先看分层（按 Activity）那一段**。

合成流量（我自己的测试批次、空树夹具）必须排除，否则基线会随测试漂移：
显式 --exclude-test 会排掉夹具 activity 和 batch*/b2:/b3/g4/smoke 这些测试标记。
  * **无效率** = ui_reacted=False 的占比 —— "动作在语义上是空操作"（改进方向＝目标选择）
  * **脱靶率** = 点击没落到目标上。**审计单独判不出**，所以本脚本用
    "--exclude <已知空操作目标>" 排除后剩下的无效率作为**脱靶率的上界估计**，
    并同时打印不含排除的原始值，两个数一起看。

旧口径（screen_changed）只作对照 —— 实测假阴性 77%，**不能用于任何统计**。

用法:
  python3 safe_tap_stats.py
  python3 safe_tap_stats.py --exclude 下载 --exclude 图片
  python3 safe_tap_stats.py --json
  python3 safe_tap_stats.py --since 2026-10-01
"""
import argparse
import collections
import json
import math
import os
import sys
import task_status

HERE = os.path.dirname(os.path.abspath(__file__))
AUDIT = os.path.join(HERE, "safe_tap_audit.jsonl")


def wilson(k, n, z=1.96):
    if n <= 0:
        return (0.0, 0.0)
    ph = k / n
    d = 1 + z * z / n
    c = (ph + z * z / (2 * n)) / d
    h = z * math.sqrt(ph * (1 - ph) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def pct(x):
    return "%.1f%%" % (100.0 * x)


def nonnegative_finite(value):
    try:
        return type(value) in (int, float) and math.isfinite(value) and value >= 0
    except OverflowError:
        return False


def aggregate_tasks(rows, states, selected_ids, excluded):
    events = collections.defaultdict(list)
    for row in rows:
        tid = row.get("task_id")
        if isinstance(tid, str) and tid:
            events[tid].append(row)
    by_task = {}
    for state in states:
        tid = state["task_id"]
        if tid not in selected_ids or any(excluded(r) for r in events[tid]):
            continue
        lifecycle = [r for r in events[tid] if r.get("kind") in ("task_start", "task_end")]
        names = {r["task_name"] for r in lifecycle if isinstance(r.get("task_name"), str) and r["task_name"]}
        name = next(iter(names)) if len(names) == 1 else "(名称不一致)" if names else "(未命名)"
        t = by_task.setdefault(name, {"n": 0, "achieved": 0, "done": 0, "script_asserted": 0, "legacy_ok": 0,
            "known_terminal": 0, "statuses": dict.fromkeys((*task_status.TERMINAL_STATES, "running", "unknown"), 0),
            "duration_samples": 0, "step_samples": 0, "secs": 0.0, "steps": 0, "fails": collections.Counter()})
        t["n"] += 1
        status = state["terminal_status"]
        t["statuses"][status] += 1
        ends = [r for r in lifecycle if r.get("kind") == "task_end"]
        if any(r.get("goal_asserted") is True or r.get("result") == "achieved" for r in ends):
            t["script_asserted"] += 1
        if any(not r.get("terminal_status") and not r.get("result") and r.get("ok") is True for r in ends):
            t["legacy_ok"] += 1
        if status not in task_status.TERMINAL_STATES:
            continue
        t["known_terminal"] += 1
        if status == "completed":
            t["done"] += 1
        else:
            t["fails"][status] += 1
        end = ends[0]
        duration, steps = end.get("elapsed_s"), end.get("steps")
        if nonnegative_finite(duration):
            t["duration_samples"] += 1
            t["secs"] += (duration - t["secs"]) / t["duration_samples"]
        if type(steps) is int and nonnegative_finite(steps):
            t["step_samples"] += 1
            t["steps"] += (steps - t["steps"]) / t["step_samples"]
    for t in by_task.values():
        t["rate_achieved"] = 0.0
        t["rate_done"] = t["done"] / t["n"]
        secs, steps = t.pop("secs"), t.pop("steps")
        t["avg_secs"] = secs if t["duration_samples"] else None
        t["avg_steps"] = steps if t["step_samples"] else None
        t["fails"] = dict(t["fails"])
    return by_task


def _main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--audit", default=AUDIT)
    ap.add_argument("--exclude", action="append", default=[],
                    help="已知的空操作目标文字（可重复）；排除后剩下的无效率当脱靶率上界")
    ap.add_argument("--since", default="", help="只看 ts_local >= 这个前缀，如 2026-10-01")
    ap.add_argument("--exclude-activity", action="append", default=[],
                    help="按 act_before 前缀排除（可重复），如 com.dsh.emptytree")
    ap.add_argument("--exclude-test", action="store_true",
                    help="排除合成流量：夹具 activity + batch*/b2:/b3/g4/smoke 测试标记")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--by-task", action="store_true",
                    help="任务身份分母/运行终态/未知与运行中/脚本自述/有效耗时样本；目标未核验")
    ap.add_argument("--by-recovery", action="store_true",
                    help="恢复事件分布：哪种故障最常见、恢复成功率")
    ap.add_argument("--by-g2", action="store_true",
                    help="G2 关闭率：放行动作中 zone_coverage=none 的占比（含 Wilson CI）")
    a = ap.parse_args()

    all_rows = list(task_status.read_audit(a.audit))
    states = task_status.task_states(all_rows, a.audit)
    rows = all_rows
    if a.since:
        rows = [r for r in rows if (r.get("ts_local") or "") >= a.since]

    acts = [r for r in rows if r.get("kind") in ("tap", "swipe", "key")]
    allow = [r for r in acts if r.get("allowed")]
    # 显式选择合成流量过滤；任务按完整生命周期排除，不能丢一半事件。
    TEST_TAGS = ("batch", "b2:", "b3", "g4", "smoke", "acc")
    ex_act = list(a.exclude_activity)
    if a.exclude_test:
        ex_act.append("com.dsh.emptytree")

    def _is_test(r):
        ab = r.get("act_before") or ""
        if any(ab.startswith(p) for p in ex_act):
            return True
        ex = r.get("expect") or ""
        return a.exclude_test and any(ex.startswith(t) or (":" + t) in ex for t in TEST_TAGS)

    cleaned = [r for r in allow if not _is_test(r)]
    taps = [r for r in cleaned if r.get("kind") == "tap" and "ui_reacted" in r]
    noop = [r for r in taps if not r["ui_reacted"]]
    kept = [r for r in taps if not any((r.get("expect") or "").endswith(x) for x in a.exclude)]
    kept_noop = [r for r in kept if not r["ui_reacted"]]

    lo, hi = wilson(len(noop), len(taps))
    klo, khi = wilson(len(kept_noop), len(kept))

    sig = {k: sum(1 for r in taps if r.get(k)) for k in
           ("ui_reacted", "ev_advanced", "tree_changed", "pixel_changed", "screen_changed")}

    def group(keyfn, src):
        g = collections.defaultdict(lambda: [0, 0])
        for r in src:
            g[keyfn(r)][0 if r["ui_reacted"] else 1] += 1
        return dict(g)

    out = {
        "ok": True, "data_complete": True, "task_schema_version": 2,
        "goal_verification_available": False,
        "funnel": {"audit_rows": len(rows), "actions": len(acts), "allowed": len(allow),
                   "rejected": len(acts) - len(allow), "after_exclude": len(cleaned),
                   "samples": len(taps)},
        "excluded_activity": ex_act,
        "definition": {"无效率": "ui_reacted=False / samples（语义空操作，改进方向=目标选择）",
                       "脱靶率": "上界估计 = exclude 后的剩余无效率（审计单独判不出脱靶）"},
        "audit_rows": len(rows), "actions": len(acts), "allowed": len(allow),
        "rejected": len(acts) - len(allow), "samples": len(taps),
        "noop": len(noop), "noop_rate": (len(noop) / len(taps)) if taps else 0.0,
        "noop_ci": [lo, hi],
        "excluded": len(taps) - len(kept), "kept": len(kept),
        "miss_upper": (len(kept_noop) / len(kept)) if kept else 0.0,
        "miss_upper_ci": [klo, khi],
        "signals": {k: (v / len(taps) if taps else 0.0) for k, v in sig.items()},
        "by_activity": group(lambda r: r.get("act_before") or "?", taps),
        "by_resolved_by": group(lambda r: r.get("resolved_by") or "坐标", taps),
    }

    # ---- 任务维度（Tier-1①）与恢复分布（Tier-1②）----
    selected_ids = {r["task_id"] for r in rows if r.get("kind") in ("task_start", "task_end")}

    def excluded_task(row):
        name = row.get("task_name") or ""
        return _is_test(row) or (a.exclude_test and isinstance(name, str) and name.startswith(TEST_TAGS))

    out["by_task"] = aggregate_tasks(all_rows, states, selected_ids, excluded_task)
    out["task_selection"] = "any lifecycle event in date range; complete lifecycle; exclusions use all rows linked by task_id"
    by_rec = {}
    for r in rows:
        if r.get("kind") != "recovery":
            continue
        s = (r.get("situation") or "?")
        t = by_rec.setdefault(s, {"n": 0, "ok": 0})
        t["n"] += 1
        t["ok"] += 1 if r.get("outcome") == "ok" else 0
    out["by_recovery"] = {k: {"n": v["n"], "ok": v["ok"],
                              "rate": (v["ok"] / v["n"]) if v["n"] else 0.0}
                          for k, v in by_rec.items()}
    # ---- G2 关闭率（Tier-2 复核改进）：多少放行动作是在 G2 无法识别危险区下执行的 ----
    g2_closed = [r for r in allow if (r.get("zone_coverage") or "") == "none"]
    g2_ack = [r for r in allow if r.get("ack_source") == "explicit"]
    glo, ghi = wilson(len(g2_closed), len(allow))
    out["g2"] = {"allowed": len(allow), "closed": len(g2_closed),
                 "rate": (len(g2_closed) / len(allow)) if allow else 0.0,
                 "closed_ci": [glo, ghi],
                 "ack_explicit": len(g2_ack),
                 "by_kind": dict(collections.Counter(r.get("kind") for r in g2_closed))}

    if a.json:
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return 0

    print("=== safe_tap 坐标点击质量统计 ===")
    print("【漏斗】")
    print("   审计记录        %4d 条" % len(rows))
    print("     └ 动作(tap/swipe/key) %4d" % len(acts))
    print("        ├ 放行            %4d" % len(allow))
    print("        └ 拒绝            %4d" % (len(acts) - len(allow)))
    print("   放行中排除合成流量后  %4d" % len(cleaned))
    print("     └ 放行的 tap 且带完整前后帧信号 %4d   ← 下面两个指标的分母" % len(taps))
    if a.exclude_test or a.exclude_activity:
        print("   （已排除合成流量：activity %s；测试标记 %s）"
              % (", ".join(ex_act) or "-", "开" if a.exclude_test else "关"))
    print()
    print("【无效率】ui_reacted=False = %d/%d = %s   95%%CI=[%s, %s]"
          % (len(noop), len(taps), pct(out["noop_rate"]), pct(lo), pct(hi)))
    if a.exclude:
        print("           已按 --exclude 排除 %d 条（%s）"
              % (out["excluded"], ", ".join(a.exclude)))
        print("【脱靶率·上界估计】排除后剩余无效率 = %d/%d = %s   95%%CI=[%s, %s]"
              % (len(kept_noop), len(kept), pct(out["miss_upper"]), pct(klo), pct(khi)))
    else:
        print("           （加 --exclude <已知空操作目标> 可得到脱靶率的上界估计）")
    print()
    print("【信号分率】(旧口径只作对照，实测假阴性 77%）")
    for k in ("ui_reacted", "ev_advanced", "tree_changed", "pixel_changed", "screen_changed"):
        print("   %-15s %5d/%d  %s" % (k, sig[k], len(taps), pct(out["signals"][k])))
    print()
    for title, key in (("按 Activity", "by_activity"), ("按解析路径", "by_resolved_by")):
        print("【%s】" % title)
        for k, v in sorted(out[key].items(), key=lambda x: -(x[1][0] + x[1][1])):
            tot = v[0] + v[1]
            print("   %-46s True %2d False %2d" % (k[:46], v[0], v[1]))
    if a.by_g2 or out["g2"]["closed"]:
        print()
        print("【G2 关闭率】(放行动作中，zone_coverage=none——G2 无法识别危险区仍执行的占比)")
        print("   放行 %d 中关闭 %d = %s   95%%CI=[%s, %s]"
              % (len(allow), len(g2_closed), pct(out["g2"]["rate"]), pct(glo), pct(ghi)))
        print("   其中 ack_source=explicit：%d" % len(g2_ack))
        kinds = out["g2"].get("by_kind") or {}
        if kinds:
            print("   按动作：" + ", ".join("%s×%d" % (k, v) for k, v in kinds.items()))
    if a.by_task and out["by_task"]:
        print()
        print("【任务维度】每个task_id计一次；目标未核验，脚本自述不等于达成；完成仅指运行终态completed")
        for name, t in sorted(out["by_task"].items(), key=lambda x: -x[1]["n"]):
            print("   %s: n=%d completed=%d running=%d unknown=%d script_asserted=%d"
                  % (name, t["n"], t["done"], t["statuses"]["running"], t["statuses"]["unknown"], t["script_asserted"]))
            print("     statuses=%s avg_secs=%s(n=%d) avg_steps=%s(n=%d)"
                  % (json.dumps(t["statuses"], ensure_ascii=False), t["avg_secs"], t["duration_samples"],
                     t["avg_steps"], t["step_samples"]))
    if a.by_recovery and out["by_recovery"]:
        print()
        print("【恢复事件分布】(kind=recovery；outcome=ok 占比)")
        for s2, t in sorted(out["by_recovery"].items(), key=lambda x: -x[1]["n"]):
            print("   %-18s %3d 次  恢复成功 %s" % (s2, t["n"], pct(t["rate"])))
    return 0


def main():
    try:
        return _main()
    except (OSError, ValueError, TypeError, AttributeError) as error:
        print(json.dumps({"ok": False, "data_complete": False, "error": "audit_unavailable",
                          "error_type": type(error).__name__}))
        return 3


if __name__ == "__main__":
    sys.exit(main())
