#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Jev 判定客户端（TypeSafe System One）—— 手机代理的「便宜决策层」基座。

设计依据：/root/jev-patterns.md 第 1、3、6 节（生态共识 + 本机实测）。

三条铁律（实测得出，别改）：
  1. **fail-open**：缺 key / 超时 / HTTP 错 / schema 错 → 返回 None，调用方走确定性兜底，
     绝不让便宜模型的网络抖动卡死任务。（本机实测 5 次调用里 1 次连接超时）
  2. **客观分类优先于主观判断**：问「这是什么类型的操作」比问「这算不算危险」稳得多。
     同一条 pm uninstall：主观问法 0.75（漏判），客观分类 1.00。
  3. **schema 硬校验**：概率键集合必须等于候选集合、和为 1、choice 必须是 argmax，
     否则丢弃判定。模型输出永远不能逃出我们给的枚举。

用法：
    from jev_client import ask, classify, check, score
    r = classify(state="...", question="这是什么类型的操作？",
                 options={"read_only": "只读", "destructive": "会删除或卸载"}, act_above=0.8)
    if r and r["action"] == "act" and r["choice"] == "destructive": ...
"""
import json, os, re, sys, time, urllib.request, urllib.error

ENDPOINT = "https://api.typesafe.ai/v1/systemone"
DEFAULT_MODEL = "jev-latest"
KEY_FILE = os.environ.get("JEV_KEY_FILE", "/root/.config/typesafe/key")
DOTENV = "/root/.config/jev-browser-use/credentials.env"
TIMEOUT_S = float(os.environ.get("JEV_TIMEOUT_MS", "8000")) / 1000.0

# 重试参数：照抄官方 SDK 的 RetryPolicy 默认值（见 _post 注释）
RETRY_MAX = int(os.environ.get("JEV_RETRY_MAX", "2"))
RETRY_BASE = float(os.environ.get("JEV_RETRY_BASE", "0.5"))   # 秒
RETRY_CAP = float(os.environ.get("JEV_RETRY_CAP", "5.0"))     # 秒


class JevUnavailable(Exception):
    """Jev 不可用（缺 key / 网络 / schema 错）——调用方必须走兜底，不要重试到死。"""


def load_key():
    """裸 key 文件优先（jev-mcp 同款格式），其次 dotenv，最后环境变量。"""
    try:
        k = open(KEY_FILE, encoding="utf-8").read().strip()
        if k:
            return k
    except OSError:
        pass
    try:
        m = re.search(r"TYPESAFE_API_KEY=(\S+)", open(DOTENV, encoding="utf-8").read())
        if m:
            return m.group(1)
    except OSError:
        pass
    return os.environ.get("TYPESAFE_API_KEY") or os.environ.get("JEV_API_KEY")


def _post(body, timeout=None, _attempt=0):
    """POST 到 TypeSafe，带重试。

    重试策略照抄官方 Python SDK 的 RetryPolicy（`_core/retry.py`，源码确认）：
      max_retries=2（共 3 次尝试）、重试 408/429/5xx、重试连接与超时错误、
      退避 0.5s→5.0s（指数）、带抖动、遵守 Retry-After。
    为什么重要：本机实测单次调用失败率约 20%（多有连接超时）。
    若失败是独立的连接/5xx 错误，重试能把 20% 压到 ~1%（依赖独立性假设）。
    **不重试** 4xx（认证/schema 错）——重试无意义且浪费配额。
    """
    key = load_key()
    if not key:
        raise JevUnavailable("找不到 API key（%s 或 %s）" % (KEY_FILE, DOTENV))
    req = urllib.request.Request(
        ENDPOINT,
        data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
        method="POST",
    )
    # 超时会重试，且每次尝试把超时放大（本机单次延迟实测可达 11s，10s 上限太紧）
    base_timeout = timeout or TIMEOUT_S
    this_timeout = base_timeout * (1 + _attempt * 0.5)

    retryable = True
    detail = ""
    try:
        with urllib.request.urlopen(req, timeout=this_timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        code = e.code
        detail = "HTTP %s: %s" % (code, e.read().decode("utf-8", "ignore")[:200])
        # 只重试 408 / 429 / 5xx
        retryable = code in (408, 429) or 500 <= code < 600
        retry_after = e.headers.get("Retry-After") if e.headers else None
    except Exception as e:                        # 超时 / DNS / 连接重置
        detail = "传输失败: %s" % e
        retry_after = None

    if retryable and _attempt < RETRY_MAX:
        delay = min(RETRY_BASE * (2 ** _attempt), RETRY_CAP)
        try:
            if retry_after:
                delay = max(delay, min(float(retry_after), RETRY_CAP))
        except (TypeError, ValueError):
            pass
        time.sleep(delay)
        return _post(body, timeout=timeout, _attempt=_attempt + 1)
    raise JevUnavailable(detail)


def _validate(ans, expect_type, keys):
    """schema 硬校验：任何一项不过就丢弃这个判定。"""
    if not isinstance(ans, dict):
        return False
    if ans.get("type") != expect_type:
        return False
    probs = ans.get("probabilities")
    if probs is not None:
        if sorted(probs) != sorted(keys):
            return False
        vals = list(probs.values())
        if any((not isinstance(v, (int, float)) or v < 0 or v > 1) for v in vals):
            return False
        if abs(sum(vals) - 1) > 0.02:
            return False
    return True


def _act(conf, act_above, review_above):
    if conf is None:
        return "act"
    if conf >= act_above:
        return "act"
    if conf >= review_above:
        return "review"
    return "abstain"


def classify(state, question, options, act_above=0.8, review_above=0.5, timeout=None):
    """从给定选项里选一个。**安全闸门就该用这个**（客观分类 > 主观判断）。

    返回 {"choice":str, "confidence":float, "probabilities":dict, "action":str,
          "model":str, "latency_ms":int, "usage":dict} 或 None（Jev 不可用）
    """
    # 复核改进②（2026-10-01）：options 传 list 会静默失败（API 的 criteria 要 dict）——
    # 曾有调用方因此拿到 None 被误判成「Jev 服务不可用」。现在自动转换并可见地提示。
    if isinstance(options, (list, tuple)):
        options = {o: o for o in options}
        print("⚠ jev classify: options 是 list/tuple，已自动转 dict（同值）；建议调用方直接给 dict",
              file=sys.stderr)
    elif not isinstance(options, dict):
        raise TypeError("jev classify: options 应为 dict（criteria），收到 %s" % type(options).__name__)
    questions = {"q": {"type": "choice", "instructions": question, "criteria": options}}
    try:
        res = _post({"model": DEFAULT_MODEL, "state": state, "questions": questions}, timeout)
    except JevUnavailable as e:
        print("⚠ jev classify 不可用: %s" % str(e)[:180], file=sys.stderr)
        return None
    ans = (res.get("answers") or {}).get("q")
    if not _validate(ans, "choice", options):
        return None
    probs = ans.get("probabilities") or {}
    choice = ans.get("choice")
    # choice 必须存在且是 argmax（防止模型给出与分布矛盾的选项）
    if choice not in options:
        return None
    if probs and choice != max(probs, key=probs.get):
        return None
    conf = ans.get("confidence")
    return {
        "choice": choice, "confidence": conf, "probabilities": probs,
        "action": _act(conf, act_above, review_above),
        "model": res.get("model"), "usage": res.get("usage"),
    }


def check(state, question, yes_means=None, no_means=None,
          yes_at_or_above=0.7, no_at_or_below=0.3, timeout=None):
    """是/否判断，返回概率。适合「证据里有没有出现 X」这类**可验证**问题。

    注意：不适合问「这算不算危险」这类主观严重性——用 classify 替代。
    """
    q = {"type": "noul", "instructions": question}
    if yes_means or no_means:
        q["criteria"] = {"yes": yes_means or "yes", "no": no_means or "no"}
    try:
        res = _post({"model": DEFAULT_MODEL, "state": state, "questions": {"q": q}}, timeout)
    except JevUnavailable as e:
        print("⚠ jev check 不可用: %s" % str(e)[:180], file=sys.stderr)
        return None
    ans = (res.get("answers") or {}).get("q")
    if not _validate(ans, "noul", []):
        return None
    p = ans.get("noul")
    if not isinstance(p, (int, float)):
        return None
    verdict = "yes" if p >= yes_at_or_above else ("no" if p <= no_at_or_below else "uncertain")
    return {"probability_yes": p, "verdict": verdict, "model": res.get("model"),
            "usage": res.get("usage")}


def score(state, question, levels, act_above=None, timeout=None):
    """在有序档位上打分，返回概率加权分数（可落在两档之间）。"""
    try:
        res = _post({"model": DEFAULT_MODEL, "state": state,
                     "questions": {"q": {"type": "score", "instructions": question, "criteria": levels}}}, timeout)
    except JevUnavailable as e:
        print("⚠ jev score 不可用: %s" % str(e)[:180], file=sys.stderr)
        return None
    ans = (res.get("answers") or {}).get("q")
    if not _validate(ans, "score", [str(i) for i in range(len(levels))]):
        return None
    return {"score": ans.get("score"), "confidence": ans.get("confidence"),
            "probabilities": ans.get("probabilities"), "levels": levels,
            "model": res.get("model"), "usage": res.get("usage")}


def ask(state, questions, timeout=None):
    """一次请求问多个问题（fan-out，生态共识第 2 条：一次决策=一次请求）。

    questions: {id: {"type": "choice"|"noul"|"score", "instructions": str,
                     "criteria": dict|list}}
    """
    try:
        res = _post({"model": DEFAULT_MODEL, "state": state, "questions": questions}, timeout)
    except JevUnavailable as e:
        print("⚠ jev ask 不可用: %s" % str(e)[:180], file=sys.stderr)
        return None
    return {"answers": res.get("answers") or {}, "model": res.get("model"), "usage": res.get("usage")}


def available():
    """探活：能拿到答案就 True。用于决定是否启用判定层。"""
    r = check("ping", "Is this text non-empty?", yes_at_or_above=0.9)
    return bool(r and r["probability_yes"] > 0.9)


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        t0 = time.time()
        print("key 来源:", KEY_FILE if os.path.exists(KEY_FILE) else DOTENV)
        c = classify("adb shell pm uninstall com.tencent.mm", "这是什么类型的操作？",
                     {"read_only": "只读，不改变任何状态",
                      "ui_interaction": "点击/输入/导航，可逆",
                      "destructive": "删除、卸载、清空或永久修改应用与用户数据",
                      "system_change": "修改系统设置或权限"},
                     act_above=0.8)
        print("classify:", json.dumps(c, ensure_ascii=False))
        n = check("The file /tmp/x.txt exists and is 12 bytes.",
                  "Does this text assert that a file exists?",
                  yes_means="It states a file exists", no_means="It does not")
        print("check:", json.dumps(n, ensure_ascii=False))
        s = score("A backup failed and 3 files were lost.",
                  "How severe is this?", ["No impact", "Minor", "Serious", "Critical"])
        print("score:", json.dumps(s, ensure_ascii=False))
        print("总耗时 %.1fs" % (time.time() - t0))
