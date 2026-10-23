#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""jev_loop 的离线自测：不碰手机、不碰网络，验证四个防呆机制真的生效。

跑法：python3 jev_loop_test.py
"""
import jev_loop as L


def test_toctou_discards_stale_decision():
    """决策之后、执行之前状态变了 → 必须丢弃这次决策，不执行。"""
    world = {"n": 0, "actions": 0}

    def observe():
        return {"n": world["n"]}

    def choose(goal, state, history):
        world["n"] += 1                      # 模拟「观察后页面自己变了」
        return {"choice": "a0", "confidence": 0.9, "action": "tap"}

    def act(a):
        world["actions"] += 1

    r = L.run(goal=lambda: "g", observe=observe, act=act,
              verify=lambda: (True, ""), choose=choose,
              budget=L.Budget(max_steps=3, max_seconds=5))
    stale = [h for h in r["history"] if h.get("reason") == "stale_state"]
    assert world["actions"] == 0, "状态过期却执行了动作！"
    assert len(stale) == 3, "应记录 3 次 stale_state，实际 %d" % len(stale)
    assert r["status"] == "step_limit"
    print("  ✓ TOCTOU：状态变了就丢弃决策，一次都没执行")


def test_no_effect_stops_repeat():
    """动作执行后状态无变化 → 标记 no_effect；同一动作再来一次就停。"""
    calls = {"n": 0}

    def choose(goal, state, history):
        calls["n"] += 1
        return {"choice": "a0", "confidence": 0.9, "action": "tap-futile"}

    r = L.run(goal=lambda: "g", observe=lambda: {"n": 0},   # 永远不变
              act=lambda a: None, verify=lambda: (True, ""), choose=choose,
              budget=L.Budget(max_steps=10, max_seconds=5))
    assert r["status"] == "no_progress", "应因重复无效动作停下，实际 %s" % r["status"]
    assert r["steps"] == 1, "只应执行 1 次，实际 %d" % r["steps"]
    assert r["history"][0].get("no_effect") is True
    print("  ✓ noEffect：无效动作只执行一次就停（未空转到步数上限）")


def test_verify_gates_done():
    """Jev 说 DONE，但 verify() 不通过 → 状态必须是 verify_failed，不是成功。"""
    r = L.run(goal=lambda: "g", observe=lambda: {"n": 0}, act=lambda a: None,
              verify=lambda: (False, "当前页面不是限速页"),
              choose=lambda g, s, h: {"choice": "DONE", "confidence": 0.99, "action": None},
              budget=L.Budget())
    assert r["status"] == "verify_failed", r["status"]
    assert r["ok"] is False
    print("  ✓ DONE 不是证明：verify 不通过则为 verify_failed")


def test_jeve_unavailable_fails_open():
    """Jev 返回 None → 明确交还 jev_unavailable，不假装成功、不卡死。"""
    r = L.run(goal=lambda: "g", observe=lambda: {}, act=lambda a: None,
              verify=lambda: (True, ""),
              choose=lambda g, s, h: None, budget=L.Budget())
    assert r["status"] == "jev_unavailable", r["status"]
    assert r["ok"] is False
    print("  ✓ fail-open：Jev 不可用时明确交还，不冒充成功")


def test_success_path_and_verification():
    """正常路径：动作有变化 → 直到 DONE 且 verify 通过 → needs_verification + ok=True。"""
    world = {"n": 0}

    def observe():
        return {"n": world["n"]}

    def act(a):
        world["n"] += 1

    seq = [{"choice": "a0", "confidence": 0.95, "action": "tap"},
           {"choice": "a0", "confidence": 0.95, "action": "tap"},
           {"choice": "DONE", "confidence": 0.93, "action": None}]

    def choose(goal, state, history):
        return seq.pop(0) if seq else {"choice": "BLOCKED", "confidence": 0.9, "action": None}

    r = L.run(goal=lambda: "g", observe=observe, act=act,
              verify=lambda: (True, "activity 已是目标页"), choose=choose,
              budget=L.Budget(max_steps=10, max_seconds=10))
    assert r["status"] == "needs_verification", r["status"]
    assert r["ok"] is True and r["steps"] == 2
    print("  ✓ 成功路径：%s" % L.summarize(r))


def test_low_confidence_hands_back():
    r = L.run(goal=lambda: "g", observe=lambda: {"n": 0}, act=lambda a: None,
              verify=lambda: (True, ""),
              choose=lambda g, s, h: {"choice": "a0", "confidence": 0.40, "action": "tap"},
              min_confidence=0.55, budget=L.Budget())
    assert r["status"] == "low_confidence", r["status"]
    print("  ✓ 置信度闸门：0.40 < 0.55 → low_confidence 交还")


if __name__ == "__main__":
    print("jev_loop 离线自测（不碰手机/网络）：")
    for fn in (test_toctou_discards_stale_decision, test_no_effect_stops_repeat,
               test_verify_gates_done, test_jeve_unavailable_fails_open,
               test_success_path_and_verification, test_low_confidence_hands_back):
        fn()
    print("全部通过 ✓")
