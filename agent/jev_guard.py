#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Jev 「守门」模块 —— 只做检查，不做控制。

## 为什么有这个模块（2026-09-19 的认知修正）

我一开始把 Jev 放在**控制路径**上（"该点哪个元素"），结果输给确定性代码——
因为那是**需要感知**的判断，而 Jev 是纯文本模型、没有视觉。

**正确的定位是把 Jev 放在「检查/守门」路径上**：对**已经读到的文本**做清晰分类。
三个场景都经过真机/真实数据验证：

| 函数 | 作用 | 实测 |
|---|---|---|
| `check_preconditions()` | 技能执行前：当前屏幕能否开始？ | 6/7（唯一错的那条也是 fail-safe，不会误放行） |
| `check_not_stalled()` | 执行中：是真在推进，还是在原地打转？还能指出**卡在哪个前提** | 卡住判定 5/5 + 前提定位 5/5 |
| `check_action_effective()` | 操作后：这一下到底生效了吗？ | 见下方说明 |

共同点：**它们的错误模式都是 fail-safe 的**——判错时倾向于"不放行/报警"，
而不是"放行一个错的操作"。这对守门用途是必须的。

对应生态证据（`jev-phishing-bench`）：Jev 端到端单选题 62.6% vs Haiku 81.3%，
但**拆成信号问题 + 代码组合后达 95.1%**——本模块就是"信号 + 代码定阈值"的形状。

设计纪律（来自 dbreunig 的诊断表 + 本机实测）：
  * 只用**客观分类**，不问"这算不算危险/好不好"这类主观严重性。
  * 选项要写成**对比式 + 具体情境**，不要用程度词。
  * **一次只问一个维度**——多问会互相污染（实测 fan-out 版置信度显著更低）。
  * **fail-open**：拿不到判定时返回 None，调用方走确定性兜底，别卡死任务。
  * 延迟中位 ~700ms（本机），可用于每条技能一次；**别放进每步循环**。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import jev_client


# ---------------------------------------------------------------- ① 前置条件守门

_PRE_OPTIONS = {
    "yes": "屏幕上已经出现该技能需要操作的目标（目标设备名/目标列表项/目标页面元素），可以直接开始",
    "wrong_screen": "屏幕是同一个 App 的其它页面，或显示「暂无可控设备」「暂无数据」这类空状态，需要先导航过去",
    "login_or_captcha": "屏幕是登录页、注册页、验证码、人机验证（滑块/拼图）、或锁屏，必须停下来找用户",
    "different_app": "屏幕是别的 App 或系统桌面，不是目标 App",
}


def check_preconditions(skill_goal, screen_text, *, act_above=0.6, timeout=None):
    """技能执行前的守门：当前屏幕能不能开始执行这个技能？

    **为什么值得花这一次调用**：本机真实踩过——限速脚本以为在设备列表页，
    实际停在「暂未发现可控设备」那页，白跑了 6 次滑动 + 5 秒，最后靠用户指正才发现。
    有这个守门的话，700ms 内就能知道"进错页面了"。

    返回 dict 或 None（Jev 不可用）：
      {"ok": bool, "verdict": str, "confidence": float, "reason": str}
      ok=True 仅当 verdict == "yes"。**所有非 yes 都不放行**（含 login_or_captcha，
      此时调用方应停下来找用户，遵守用户红线第 3 条）。
    """
    state = ("目标技能：%s\n当前屏幕读到的文字：\n%s" % (skill_goal, (screen_text or "")[:2000]))
    r = jev_client.classify(state, "当前屏幕是否满足开始执行这个技能的前置条件？",
                            _PRE_OPTIONS, act_above=act_above, timeout=timeout)
    if r is None:
        return None
    v = r["choice"]
    hint = {
        "yes": "可以开始",
        "wrong_screen": "进错页面了，先导航到正确的页面",
        "login_or_captcha": "遇到登录/验证码，按红线停下来告诉用户，不要硬闯",
        "different_app": "不在目标 App 里，先启动目标 App",
    }.get(v, "")
    return {"ok": v == "yes", "verdict": v, "confidence": r.get("confidence"),
            "reason": hint, "probabilities": r.get("probabilities")}


# ---------------------------------------------------------------- ② 语义停滞检测

_STUCK_OPTIONS = {
    "progressing": "新动作基于新观察到的事实，正在推进目标",
    "stalled_same_premise": "动作虽然不同，但都建立在同一个未经验证/已被推翻的假设上，实际在原地打转",
    "stalled_repeat": "在重复同样的动作或同样的一步，没有获得新信息",
    "off_goal": "在做和目标无关的事",
}

# 让 Jev 从**枚举**里挑前提，而不是自由生成文本（避免幻觉 + 便于代码分支）
_PREMISE_OPTIONS = {
    "none": "agent 没有卡住，正常推进",
    "p_wrong_page": "假设「目标就在当前这个页面上」，但这个假设从未被验证",
    "p_no_scroll_needed": "假设「不需要滑动/翻页就能看到目标」",
    "p_ui_text_reliable": "假设「控件树读到的文字就是完整的界面内容」",
    "p_action_took_effect": "假设「上一步操作已经生效」，但页面其实没有变化",
    "p_app_responding": "假设「App 会响应这个操作」，但它可能已无响应或需要重新进入",
    "p_other": "其它前提",
}


def check_not_stalled(goal, history, *, act_above=0.6, timeout=None):
    """执行中检查：agent 是真在推进，还是在原地打转？

    **比"连续 N 次相同动作"更本质**：能识别「动作各不相同、但都基于同一个错误前提」
    这种停滞——那是常规重复检测抓不到的。生态里 `progressgate` 就是这个思路。

    history: 字符串列表（每步一句自然语言描述即可，不需要结构化）。
    返回 dict 或 None：
      {"stuck": bool, "verdict": str, "premise": str, "advice": str, "confidence": float}
      stuck=True 时 premise 指出被反复依赖的那个未验证前提（枚举值，便于代码分支）。
    """
    if not history:
        return None
    state = ("任务：%s\n执行历史：\n%s"
             % (goal, "\n".join("%d. %s" % (i + 1, h) for i, h in enumerate(history[-12:]))))
    r = jev_client.ask(state, {
        "stuck": {"type": "choice",
                  "instructions": "agent 是在推进目标，还是卡住了？",
                  "criteria": _STUCK_OPTIONS},
        "premise": {"type": "choice",
                    "instructions": "如果卡住了，它反复依赖的、未被验证的前提是哪一个？没卡住就选 none",
                    "criteria": _PREMISE_OPTIONS},
    }, timeout=timeout)
    if r is None:
        return None
    a = r.get("answers") or {}
    verdict = (a.get("stuck") or {}).get("choice")
    premise = (a.get("premise") or {}).get("choice")
    advice = {
        "p_wrong_page": "先确认自己在哪个页面（读 Activity 名/页面标题），再决定下一步",
        "p_no_scroll_needed": "显式尝试滑动/翻页，或用搜索定位",
        "p_ui_text_reliable": "控件树可能读不完整 → 改用截图 + read_image 做视觉定位",
        "p_action_took_effect": "重新读一次页面确认是否真的变化；没变化就换操作方式",
        "p_app_responding": "App 可能无响应 → 回退到首页/重启 App 再试",
        "p_other": "换一个完全不同的策略，别再重复同一类动作",
        "none": "",
    }.get(premise, "")
    return {"stuck": verdict != "progressing", "verdict": verdict, "premise": premise,
            "advice": advice, "confidence": (a.get("stuck") or {}).get("confidence")}


# ---------------------------------------------------------------- ③ 操作生效检查

_EFFECT_OPTIONS = {
    "effective": "页面内容确实发生了符合预期的变化（出现了新内容/新页面/目标状态）",
    "no_change": "页面内容和操作前一样，没有任何变化",
    "changed_unexpectedly": "页面变了，但变化方向和预期不一致（跳到了别的页面/出现了错误提示）",
    "unclear": "读到的内容不足以判断是否变化",
}


def check_action_effective(intent, before_text, after_text, *, act_above=0.6, timeout=None):
    """操作后检查：这一下到底生效了吗？（补"控件树无变化"这种确定性判断的盲区）

    适用场景：控件树文字没变，但视觉上可能变了（滚动、动画、H5 局部刷新）；
    或文字变了但方向不对（跳错页、弹错误提示）——这两种确定性代码都判不了。

    返回 dict 或 None：{"effective": bool, "verdict": str, "confidence": float}
    """
    state = ("刚执行的操作：%s\n操作前屏幕文字：\n%s\n操作后屏幕文字：\n%s"
             % (intent, (before_text or "")[:1200], (after_text or "")[:1200]))
    r = jev_client.classify(state, "这次操作生效了吗？", _EFFECT_OPTIONS,
                            act_above=act_above, timeout=timeout)
    if r is None:
        return None
    return {"effective": r["choice"] == "effective", "verdict": r["choice"],
            "confidence": r.get("confidence")}


if __name__ == "__main__":
    import json
    print("== ① 前置条件守门 ==")
    print(json.dumps(check_preconditions(
        "给电视机设置限速",
        "小翼1864的家 | 设备·1 | 暂未发现可控设备 | 手机 电脑 平板 | 品牌推荐"),
        ensure_ascii=False, indent=1))
    print("== ② 语义停滞检测 ==")
    print(json.dumps(check_not_stalled(
        "给电视机设置限速",
        ["滑动设备列表找电视机 → 没找到", "再滑一次 → 没找到", "改用搜索框找电视机 → 还是没找到"]),
        ensure_ascii=False, indent=1))
