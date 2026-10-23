#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""判定循环（bounded decision loop）—— 把 Jev 夹在确定性代码里的骨架。

设计来源：/root/jev-patterns.md 第 1、3 节 + 生态调研的硬约束。

核心分工（生态共识，别推翻）：
  * **代码拥有循环**：预算、停止、重试、状态新鲜度全由代码决定。
  * **Jev 只做「从给定选项里挑一个」**，用在**真正有歧义**的地方（选哪个元素）。
    实测铁证：元素表逐字节相同、只多给两个操作，正确率从 6/6 掉到 1/8
    ——「An operation that is free to implement is not free to offer.」
  * **确定性检查优先于模型判断**：能算出来的就别问模型（有效变化检测、activity 名）。
  * **DONE 不是证明**：Jev 说完成只意味着「停止交互」，必须由 verify() 独立复核。
  * **fail-open**：Jev 不可用时（返回 None）走确定性兜底，不卡死任务。

四个防呆机制（都来自实测）：
  1. TOCTOU：决策前重新观察；状态变了就丢弃这次决策，不执行。
  2. noEffect：执行后状态没变化 → 标记；下一轮同一个动作被再次提出就直接停（防死循环）。
  3. 有界：步数 / 墙钟 / WAIT 次数三重上限。
  4. 完整 history：每步记录 choice/confidence/latency/executed/reason，出问题能复盘。
"""
import time


class Budget:
    def __init__(self, max_steps=10, max_seconds=60.0, max_waits=3):
        self.max_steps, self.max_seconds, self.max_waits = max_steps, max_seconds, max_waits
        self.steps = self.waits = 0
        self.t0 = time.time()

    @property
    def elapsed(self):
        return time.time() - self.t0

    def over_time(self):
        return self.elapsed >= self.max_seconds

    def can_step(self):
        return self.steps < self.max_steps and not self.over_time()


def same_state(a, b):
    """状态等价性默认用序列化比较；调用方可传更聪明的等价函数
    （例如只比较可操作元素的语义名集合，避免时间戳/动画导致的假变化）。"""
    return a == b


def run(goal, observe, act, verify,
        choose=None, is_effective=None, equivalent=None,
        budget=None, min_confidence=0.55, log=print):
    """跑一个判定循环。

    参数（全部由调用方注入，本模块不碰 adb / 不碰具体 App —— 保持通用）：
      goal()        -> str                     自然语言目标，喂给 Jev
      observe()     -> dict                    当前状态（必须可比较）
      act(action)   -> None                    执行动作
      verify()      -> (bool, str)             独立复核（**必须是确定性的**）
      choose(goal, state, history) -> dict|None
                    Jev 决策，返回 {"choice":.., "confidence":.., "action":..}；
                    约定：choice ∈ {"DONE","BLOCKED","WAIT"} 或调用方自定义的动作 id；
                    action 字段是实际要执行的东西（None 表示无动作）。
                    返回 None 表示 Jev 不可用 → 本函数返回 status="jev_unavailable"。
      is_effective(before, after) -> bool      默认 not equivalent(before, after)
      equivalent(a, b) -> bool                 默认相等比较

    返回 {"status":..., "reason":..., "history":[...], "state":..., "elapsed_s":...}

    status 取值（全是**明确的交还状态**，绝不返回旧结果冒充成功）：
      needs_verification  动作流走完，且 verify() 通过 ← 唯一算成功的
      verify_failed       Jev 报 DONE 但 verify() 不通过
      blocked             Jev 明确说做不了
      low_confidence      置信度低于阈值
      no_progress         同一个无效果动作被重复提出
      loading_timeout     连续 WAIT 超限
      budget / step_limit 预算耗尽
      jev_unavailable     Jev 不可用（fail-open 交还）
      action_error        执行动作时抛异常
    """
    bud = budget or Budget()
    eq = equivalent or same_state
    history = []
    state = observe()
    waits = 0

    for step in range(bud.max_steps):
        if bud.over_time():
            return _done("budget", history, state, bud, "超过时间预算 %.1fs" % bud.elapsed)
        if choose is None:
            return _done("jev_unavailable", history, state, bud, "未提供 choose()")

        decision = choose(goal(), state, history)
        if decision is None:
            return _done("jev_unavailable", history, state, bud, "Jev 不可用（缺 key/超时/schema 错）")

        choice = decision.get("choice")
        conf = decision.get("confidence")
        rec = {"step": step, "choice": choice, "confidence": conf,
               "action": decision.get("action_name") or choice, "executed": False}
        history.append(rec)

        # ---- 防呆 1：TOCTOU —— 决策所用的状态可能已经过期 ----
        fresh = observe()
        if not eq(fresh, state):
            rec["reason"] = "stale_state"
            state = fresh
            continue
        state = fresh

        # ---- 终止类选择 ----
        if choice == "WAIT":
            waits += 1
            rec["reason"] = "wait"
            if waits >= bud.max_waits:
                return _done("loading_timeout", history, state, bud, "连续 WAIT %d 次" % waits)
            time.sleep(0.75)
            continue
        waits = 0

        if choice == "DONE":
            ok, detail = verify()
            rec["reason"] = "verify_pass" if ok else "verify_fail"
            return _done("needs_verification" if ok else "verify_failed",
                         history, state, bud, detail)
        if choice == "BLOCKED":
            rec["reason"] = "blocked"
            return _done("blocked", history, state, bud, "Jev 判定无法推进")

        # ---- 置信度闸门 ----
        if isinstance(conf, (int, float)) and conf < min_confidence:
            rec["reason"] = "low_confidence"
            return _done("low_confidence", history, state, bud,
                         "置信度 %.2f < %.2f" % (conf, min_confidence))

        action = decision.get("action")
        if action is None:
            rec["reason"] = "no_action"
            return _done("blocked", history, state, bud, "决策没有附带可执行动作")

        # ---- 防呆 2：同一个动作上一轮没有效果，这一轮又被提出 → 停 ----
        prev = history[-2] if len(history) >= 2 else None
        if prev and prev.get("no_effect") and prev.get("action") == rec["action"]:
            return _done("no_progress", history, state, bud,
                         "动作「%s」重复且无效果" % rec["action"])

        # ---- 执行 ----
        try:
            act(action)
        except Exception as e:
            rec["reason"] = "action_error"
            return _done("action_error", history, state, bud, str(e))
        rec["executed"] = True
        bud.steps += 1

        # ---- 防呆：有效变化检测（确定性，不问模型）----
        nxt = observe()
        eff = (is_effective or (lambda a, b: not eq(a, b)))(state, nxt)
        if not eff:
            rec["no_effect"] = True
        state = nxt

    return _done("step_limit", history, state, bud, "达到步数上限 %d" % bud.max_steps)


def _done(status, history, state, bud, reason):
    return {"status": status, "reason": reason, "history": history,
            "state": state, "elapsed_s": round(bud.elapsed, 1),
            "steps": bud.steps, "ok": status == "needs_verification"}


def summarize(result):
    """给人和日志看的一行摘要。"""
    executed = sum(1 for h in result["history"] if h.get("executed"))
    lat = [h.get("latency_ms") for h in result["history"] if h.get("latency_ms")]
    return ("status=%s(%s) 步数=%d/%d 执行=%d 耗时=%.1fs%s"
            % (result["status"], result["reason"], result["steps"],
               len(result["history"]), executed, result["elapsed_s"],
               (" 平均决策 %dms" % (sum(lat) // len(lat))) if lat else ""))
