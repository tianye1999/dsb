#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""safe_tap.py —— 点击闸门。**所有手机点击必须走这个程序，不许再用裸 input tap。**

为什么要有它（2026-09-20 立的规矩）：
    我（agent）反复犯同一个错——**不看当前屏幕就凭记忆点坐标**。后果包括：
    - 用预览图坐标当设备坐标（差 2.35 倍），连点 7~8 次全空
    - 把"Mac 微信已登录"横幅当成通讯录 tab，误入【已登录设备】页（那页有「退出登录」）

    光靠"我保证"没有用——我着急时会丢掉纪律。所以把纪律**写进程序**，
    让"不先截图就点击"在技术上**做不到**。

═══ 硬性约束（全部机器可验证，过不了就拒绝执行）═══

G1. **必须提供新鲜的截图凭据**
    先用「看」子命令截图 → 得到 shot_id → 点击时必须带上它。
    shot_id 超过 TTL（默认 90 秒）即失效 → 强制重新看屏幕。
    每张凭据只接纳一次动作请求；下发前消费，失败也不恢复。下一动作必须重新 look。
    截图后若前台 Activity 变了 → 凭据作废（防止"看着 A 页面却点了 B 页面"）。

G2. **禁止点击危险区域（禁区）**
    从当前截图里自动定位危险按钮（退出登录/删除/注销/解绑/转账…），
    在其周围建禁区；点击落在禁区内 → 直接拒绝，并说明是什么按钮。

G3. **禁止点击屏幕边缘带**
    距左右边缘 < 24px、距顶部 < 40px 一律拒绝。
    原因：系统手势监视器（edge-swipe / swipe-up / swipe-to-screenshot）会截获边缘触摸，
    而且边缘常有返回箭头、状态栏等易误触元素。

G4. **连续点击熔断**
    tap/key 历史各保留最近 64 次尝试，look 和其他动作不会清零。
    同一前台的同 digest 或 60px 邻近位置达到上限且没有已验证前台迁移时拒绝；
    同 keycode 连续达到上限也拒绝。未知派发仍计数，不能靠刷新观测解锁。
    同页内容改变可能保守误拒；这不是完整任务台账或业务目标核验。
    swipe/scroll/drag/long_press 共用最近64次手势历史，同一前台默认最多16次。
    这是动作上限，不判定内容是否有进展；达到上限需人工接管，look不解锁。
    （治我"点不中就再试一次"的毛病。）

G5. **每次点击自动留证**
    点击前截图、点击后截图，落到 audit 目录，附 Activity 与判定结果。

═══ 用法 ═══

    # 1) 看：截图并取凭据（会打印 shot_id）
    python3 safe_tap.py look --note "微信聊天列表"

    # 2) 点：必须带 shot_id，并声明你以为点的是什么
    python3 safe_tap.py tap --shot <shot_id> --x 446 --y 2680 \
            --expect "底部通讯录tab" --expect-activity com.tencent.mm/.ui.LauncherUI

    # 3) 其他动作也要走闸门；每个动作前重新 look，使用各自的新 shot_id
    python3 safe_tap.py swipe --shot <shot_id> --x1 .. --y1 .. --x2 .. --y2 ..
    python3 safe_tap.py key   --shot <shot_id> --code 4        # 返回键
    python3 safe_tap.py zones --shot <shot_id>                 # 看当前禁区
    python3 safe_tap.py audit                                   # 看最近操作记录

退出码：0 执行 / 2 被闸门拒绝 / 3 参数或凭据错误
"""
import argparse
import base64
import fcntl
import json
import os
import re
import shlex
import struct
import subprocess
import sys
import tempfile
import time

# 2026-10-01（P0-1 像素兜底）：容器侧 OCR（RapidOCR→tesseract 双引擎，见 skills/ocr.py）。
# 空树页（微信/淘宝系）控件树为空时：look 附带像素文本、G2 在像素文本上重建禁区、
# wait --text 改扫像素。OCR 较贵（~2-5s/次），可用 ocr.enabled=false 关闭。
try:
    from ocr import recognize_result as pixel_recognize_result
    OCR_OK = True
except Exception:
    pixel_recognize_result, OCR_OK = None, False

# 2026-10-20 虚拟屏：会话/显示器解析独立成 vd_session.py（safe_tap 只负责闸门与派发）。
# 缺模块时 VD 一律不可用（fail closed），主屏老路径不受影响。
try:
    import vd_session as vd
    VD_OK = True
except Exception:
    vd, VD_OK = None, False

# 2026-10-21（D2）：空树页非导航点击的视觉危险判断（只发目标周边裁剪图，见 vision_check.py）。
# 只会收紧；模块缺失/调用失败时维持 OCR 扫词规则并在审计里记 vision=unavailable。
try:
    import vision_check
    VISION_OK = True
except Exception:
    vision_check, VISION_OK = None, False
# 本进程这一次动作的闸门判定（ack 来源 + 视觉结果），供派发异常分支审计（一次 CLI 调用只派一个动作）。
_GATE_EVIDENCE = {}

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "safe_tap_state.json")
AUDIT = os.path.join(HERE, "safe_tap_audit.jsonl")
SHOTS = os.path.join(HERE, "safe_tap_shots")
try:
    from adb_endpoint import adb_target, transport_check, adb_command, EndpointUnavailable  # noqa: F401 - Probe the full endpoint API before selecting the legacy fallback.
except ImportError:
    # 旧真机按单文件同步时仍可运行；不把缺模块伪装成已验证的新安装器部署。
    class EndpointUnavailable(RuntimeError):
        pass
    def adb_command(binary='adb'):
        if os.path.exists('/data/data/com.termux/files/home/.dsb-installer-managed'):
            raise EndpointUnavailable('安装器模块缺失，未提交命令；请完整部署')
        return [binary, '-s', '127.0.0.1:5555']
    def transport_check():
        return 'warning', 'ADB 未加固：兼容单文件部署，需使用对应版本的独立检查'

SHOT_TTL = 90.0          # G1：截图凭据有效期（秒）
EDGE_X = 24              # G3：左右边缘禁带（px）—— tap 用；swipe 用实测的 systemGestures 宽度
EDGE_TOP = 40            # G3：顶部禁带（状态栏 / 返回箭头区）
# G3：底部禁带的**回退值**。取 84 而不是 56 —— 见 gesture_insets()：
# 实测本机 navigationBars=56 只是"视觉导航条"，真正的**强制手势区**是
# mandatorySystemGestures=84，取 56 会漏掉中间 28px。
EDGE_BOTTOM = 84
DANGER_PAD = 40          # G2：危险按钮周围禁区半径（px）
_INSETS = None           # gesture_insets() 的进程内缓存
REPEAT_LIMIT = 2         # G4：同点连击上限
REPEAT_RADIUS = 60       # 判定"同一点"的半径

# G2：危险词——出现在屏幕文字里就为其位置建禁区
DANGER_WORDS = [
    "退出登录", "退出", "注销", "删除", "卸载", "解绑", "移除",
    "清空", "格式化", "重置", "恢复出厂", "抹除",
    "转账", "支付", "付款", "确认支付", "立即支付", "下单", "提交订单",
    "发送", "举报", "拉黑", "停机",
]


# ---------------------------------------------------------------- 基础

class ActionDispatchError(Exception):
    def __init__(self, reason, status="unknown", returncode=None):
        super().__init__(reason)
        self.reason, self.status, self.returncode = reason, status, returncode


def sh(*a, t=90, checked=False):
    receipt_nonce = os.urandom(32).hex()
    try:
        result = subprocess.run(adb_command() + list(a), capture_output=True, timeout=min(t, 15) if checked else t,
                                env=dict(os.environ, DSB_ADB_RECEIPT_NONCE=receipt_nonce))
    except EndpointUnavailable as exc:
        raise ActionDispatchError(str(exc), 'not_submitted') from None
    except subprocess.TimeoutExpired:
        if checked:
            raise ActionDispatchError("adb_timeout") from None
        return ""
    except FileNotFoundError:
        if checked:
            raise ActionDispatchError("adb_start_failed", "not_submitted") from None
        return ""
    except Exception:
        if checked:
            raise ActionDispatchError("adb_transport_error") from None
        return ""
    if result.returncode == 78:
        diagnostic = result.stderr.decode("utf-8", "replace")
        policy_message = next((line for line in diagnostic.splitlines()
                               if line.startswith(("ADB_POLICY_PAUSED:", "ADB_POLICY_DENIED:"))), "")
        if policy_message and ('ADB_POLICY_RECEIPT:' + receipt_nonce) in diagnostic.splitlines():
            raise ActionDispatchError(policy_message, "not_submitted", returncode=78)
    output = result.stdout.decode("utf-8", "ignore")
    if checked:
        if result.returncode != 0:
            raise ActionDispatchError("adb_nonzero", returncode=result.returncode)
        diagnostic = output + "\n" + result.stderr.decode("utf-8", "ignore")
        if re.search(r"\b(?:error|[a-z]*exception)\b|unknown command|permission denied", diagnostic, re.I):
            raise ActionDispatchError("adb_error_output", returncode=0)
    return output



# ── 运行时配置（2026-10-01 借鉴 phone-harness config get/set：调参不改代码）──
CONFIG_FILE = os.path.join(HERE, "safe_tap_config.json")
CONFIG_DEFAULTS = {
    "look.ttl": 90.0,                  # G1 凭据 TTL（秒）
    "action.settle_budget": 2.5,       # 动作后稳定轮询预算（秒）
    "action.settle_budget_fast": 1.2,  # --fast 档预算
    "action.fast_default": False,      # 不加 --fast 也按 fast 跑
    "breaker.repeat_limit": 2,         # G4 同点连击上限
    "breaker.key_limit": 2,            # 按键熔断上限
    "breaker.gesture_limit": 16,       # 同一前台共享手势尝试上限
    "wait.interval": 0.5,              # wait 轮询间隔（秒）
}


def cfg_get(key, default=None):
    """优先级：SAFE_TAP_<KEY> 环境变量 > safe_tap_config.json > 默认值。"""
    env = "SAFE_TAP_" + key.replace(".", "_").upper()
    if env in os.environ:
        raw = os.environ[env]
        try:
            return json.loads(raw)
        except Exception:
            return raw
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            ov = json.load(f)
        if key in ov:
            return ov[key]
    except Exception:
        pass
    return CONFIG_DEFAULTS.get(key, default)


def foreground(display_id=0, dump=None):
    """某逻辑显示器的前台 Activity（dump：复用已取到的 dumpsys activity activities，省一次往返）。

    2026-10-20 修：存在虚拟屏时，`dumpsys activity activities` 里**第一处**
    topResumedActivity 可能是虚拟屏上的 App —— 原来全局取第一个会在有虚拟屏时
    把主屏判断成虚拟屏。现在按 `Display #<id>` 块解析（display_id=0 也走同一规则）。
    """
    if VD_OK:
        try:
            return vd.foreground(display_id, dump)
        except Exception:
            return ""
    d = dump if dump is not None else sh("shell", "dumpsys", "activity", "activities")
    m = re.search(r"topResumedActivity=ActivityRecord\{\S+ \S+ (\S+)", d)
    return m.group(1) if m else ""


def _foreground_on(display_id):
    """Keep the existing no-argument main-screen call contract."""
    return foreground(display_id) if display_id else foreground()


def _foregrounds(display_id):
    """(该屏前台, 主屏前台)：虚拟屏时一次 dumpsys 同时取两个（2026-10-21 提速，原来 look 里查 3 次）。"""
    if display_id and VD_OK:
        try:
            dump = vd.adb("shell", "dumpsys", "activity", "activities")
        except Exception:
            dump = ""         # 取不到 → 前台按空处理，后面的"前台必须是会话目标"会保守拒绝
        return foreground(display_id, dump=dump), foreground(0, dump=dump)
    f = _foreground_on(display_id)
    return f, f


def wakefulness():
    """亮屏状态（便宜查询，约 60ms）。用来区分「息屏黑图」和「安全页黑图」。

    2026-10-01 加：真正的 interactive 字段收进 App 快照要重编 APK + 重绑无障碍，
    先用这条**零成本替代**顶着 —— 审计里能直接看出当时是不是息屏。
    """
    d = sh("shell", "dumpsys", "power")
    m = re.search(r"mWakefulness=(\w+)", d)
    return m.group(1) if m else ""


def screen_size(display_id=0):
    """逻辑显示器的像素尺寸。主屏走 wm size；虚拟屏取会话记录（look 时用帧尺寸复核）。"""
    if int(display_id) != 0:
        if VD_OK:
            s = vd.load_session() or {}
            if int(s.get("displayId", -1)) == int(display_id):
                return (int(s.get("width") or 0), int(s.get("height") or 0))
        return (0, 0)
    d = sh("shell", "wm", "size")
    m = re.search(r"(\d+)x(\d+)", d)
    return (int(m.group(1)), int(m.group(2))) if m else (1260, 2800)


def _screen_size_on(display_id):
    """Keep the existing no-argument main-screen call contract."""
    return screen_size(display_id) if display_id else screen_size()


def gesture_insets(w, h):
    """从 dumpsys window 解析系统手势 inset（进程内缓存）。

    2026-10-01 加。为什么不再手工维护常量：inset 是**设备 / 导航模式派生**的
    （切三键导航，底部会明显变大）。实测本机（1260x2800 / 560dpi / navigation_mode=2 手势）：

        navigationBars          frame=[0,2744][1260,2800]   底 56   ← 只是"视觉导航条"
        mandatorySystemGestures frame=[0,2716][1260,2800]   底 84   ← **真正的强制手势区**
        systemGestures          frame=[0,0][70,2800]        左 70

    底部取前两者的**较大值**；左右取 systemGestures 的宽度。解析失败回落到常量。
    """
    global _INSETS
    if _INSETS is not None:
        return _INSETS
    left, right, bottom = EDGE_X, EDGE_X, EDGE_BOTTOM
    try:
        d = sh("shell", "dumpsys", "window")
        for line in d.splitlines():
            if "InsetsSource" not in line or "type=" not in line:
                continue
            mt = re.search(r"type=(\w+)", line)
            mf = re.search(r"frame=\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]", line)
            if not (mt and mf):
                continue
            typ = mt.group(1)
            l, t, r, b = (int(v) for v in mf.groups())
            if typ == "systemGestures":
                if r < w // 2:
                    left = max(left, r - l)
                elif l > w // 2:
                    right = max(right, w - l)
            elif typ in ("navigationBars", "mandatorySystemGestures"):
                if b >= h:
                    bottom = max(bottom, b - t)
    except Exception:
        pass
    _INSETS = {"left": left, "right": right, "bottom": bottom}
    return _INSETS


def load_state(strict=True):
    try:
        with open(STATE, encoding="utf-8") as stream:
            state = json.load(stream)
        if strict and not isinstance(state, dict):
            raise ValueError("invalid state object")
        if strict:
            for field in ("taps", "keys", "gestures"):
                if field in state and (not isinstance(state[field], list) or
                        any(not isinstance(item, dict) for item in state[field])):
                    raise ValueError("invalid state history: " + field)
            for gesture in state.get("gestures", []):
                if not isinstance(gesture.get("act_before", ""), str):
                    raise ValueError("invalid gesture foreground")
            if "last_shot" in state and not isinstance(state["last_shot"], dict):
                raise ValueError("invalid state credential")
        return state
    except FileNotFoundError:
        return {}
    except Exception:
        if strict:
            raise
        return {}


def save_state(s):
    # Never truncate the live ledger. Dispatch may proceed only after durability.
    payload = json.dumps(s, ensure_ascii=False, indent=1)
    directory = os.path.dirname(os.path.abspath(STATE))
    path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8",
                dir=directory, prefix=".safe-state-", delete=False) as stream:
            path = stream.name
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(path, STATE)
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        if path is not None:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass


def audit(rec):
    rec = {**rec, "ts": time.time(), "ts_local": time.strftime("%Y-%m-%d %H:%M:%S")}
    # 2026-10-01（Tier-1①）：任务上下文由 dsb_run 用环境变量注入，子进程自然继承；
    # 无任务上下文时字段缺省——现有审计消费方（stats 漏斗）不受影响。
    _tid = os.environ.get("DSB_TASK_ID")
    if _tid:
        rec["task_id"] = _tid
        rec["task_name"] = os.environ.get("DSB_TASK_NAME") or ""
        _step = os.environ.get("DSB_TASK_STEP")
        if _step:
            rec["step_no"] = int(_step)
    with open(AUDIT, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def lock():
    """串行：避免并发操作手机。"""
    os.makedirs(HERE, exist_ok=True)
    f = open(os.path.join(HERE, ".safe_tap.lock"), "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        t0 = time.time()
        while time.time() - t0 < 60:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                time.sleep(1)
        else:
            print("✗ 另一个 safe_tap 正在执行，拒绝并发", file=sys.stderr)
            sys.exit(3)
    return f




# ---------------------------------------------------------------- 球红绿灯
# 2026-09-22 用户指出：用 adb 直接操作时球不变橙，用户看不到"勿碰"警示。
# 这里在每次 look（任何 safe_tap 流程的第一步）时脉冲一次橙，
# App 侧 120 秒看门狗会在没有后续心跳时自动回蓝 —— 所以不会永久卡在橙色。
# 需要立刻回蓝时用：python3 safe_tap.py done
def _ball(cmd):
    try:
        subprocess.run(adb_command() + ["shell", "am", "broadcast", "-n",
                              "com.dsb.assistant/.CmdReceiver",
                              "-a", "com.dsb.assistant.CMD",
                              "--es", "token", "dsh-8f3a2c",
                              "--es", "cmd", cmd],
                       capture_output=True, timeout=20)
    except Exception:
        pass


# ------------------------------------------------ 虚拟屏强制策略（2026-10-20 用户要求）
# 规矩：**读 / 操作「别的 App」的界面必须发生在虚拟屏上**，不许占用主屏、不许切走前台 App。
# 这条不靠记忆，靠闸门：主屏上的动作默认一律拒绝，除非
#   ① 目标是控制面本身（逗沙包 App / 系统 UI / 桌面 / 权限弹窗），或
#   ② 调用方显式 --allow-main --why "原因"（进审计，可复盘）。
# 关掉/放宽：SAFE_TAP_VD_POLICY=off 或 safe_tap.py config set vd_policy off。
VD_STATE_FILE = os.path.join(HERE, "vd_session.json")
# 目标 App 自己拉起的系统级界面（文件选择器/授权框/分享面板）：同一块虚拟屏，属于流程一部分。
VD_ON_BEHALF_PKGS = {
    "com.android.documentsui",
    "com.google.android.documentsui",
    "com.android.permissioncontroller",
    "com.google.android.permissioncontroller",
    "com.android.intentresolver",
    "android",
}
# 控制面：这些包在主屏上活动是正常的（球、通知栏、桌面、权限框）
VD_EXEMPT_PKGS = {
    "com.dsb.assistant",        # 逗沙包 App（悬浮球本体）
    "com.dsh.canary",           # 自测 canary
    "com.dsh.emptytree",        # 空树夹具（smoke 回归用）
    "com.android.systemui",
    "android",
    "com.android.launcher3",
    "com.obric.feature.ObricLauncher",
}
VD_EXEMPT_PREFIXES = (
    "com.android.permissioncontroller",
    "com.google.android.permissioncontroller",
    "com.android.packageinstaller",
    "com.android.settings",     # 设置页属于系统，不强制虚拟屏
)


def vd_policy_mode():
    """require（默认）| off。env 优先，其次 safe_tap_config.json。"""
    if os.environ.get("SAFE_TAP_VD_POLICY"):
        raw = os.environ["SAFE_TAP_VD_POLICY"]
    else:
        raw = cfg_get("vd_policy", "require")
    return str(raw or "").strip().lower() or "require"


def vd_session_active():
    """读 vd_session.json，判断是否有**活的**虚拟屏会话（state=active + 未过期 + pid 在）。"""
    try:
        with open(VD_STATE_FILE, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return None
    if not isinstance(d, dict) or str(d.get("state")) != "active":
        return None
    try:
        if float(d.get("expiresAt") or 0) and time.time() > float(d["expiresAt"]):
            return None
    except Exception:
        pass
    pid = d.get("pid")
    if pid:
        try:
            os.kill(int(pid), 0)
        except Exception:
            return None
    return d


def vd_exempt(pkg):
    # 前台读不到时按「非豁免」处理：宁可拒绝（可 --allow-main 显式放行），不要放行后才发现
    if not pkg:
        return False
    return pkg in VD_EXEMPT_PKGS or any(pkg.startswith(p) for p in VD_EXEMPT_PREFIXES)


def vd_policy_block(kind, display_id, fg, allow_main=False, why=None):
    """返回 None=放行，或 (reason, hint) = 拒绝理由。

    只拦「主屏 + 目标是非控制面 App」。虚拟屏动作（display_id>0）一律放行。
    """
    if vd_policy_mode() in ("off", "0", "false", "warn"):
        return None
    if int(display_id or 0) != 0:
        return None
    if allow_main:
        return None
    pkg = (fg or "").split("/")[0]
    if vd_exempt(pkg):
        return None
    sess = vd_session_active()
    if sess:
        return ("主屏动作被 VD 策略拒绝：已有虚拟屏会话 %s（display %s），目标 App `%s` 应在该屏操作。"
                "请先 `look --display %s` 拿该屏凭据，再 `%s --display %s …`。"
                % (sess.get("sid"), sess.get("displayId"), pkg,
                   sess.get("displayId"), kind, sess.get("displayId")),
                "vd_active_elsewhere")
    return ("主屏动作被 VD 策略拒绝：目标 App `%s` 不允许占用主屏（用户 2026-10-20 定的规矩）。"
            "改用虚拟屏：`python3 %s/vd_session.py start --package %s --purpose '…'`，"
            "之后所有 look/%s 都带 `--display <N>`；收工 `vd_session.py stop`。"
            "确实无法虚拟屏（FLAG_SECURE / 必须系统级弹窗）时：加 `--allow-main --why '原因'`（会记审计）。"
            % (pkg, HERE, pkg, kind),
            "vd_required")


def vd_look_hint(display_id, fg):
    """look 只要没拒绝就仍可用，但主屏上看非控制面 App 会在结果里显式标 violation。"""
    if int(display_id or 0) != 0:
        return "ok_vd"
    pkg = (fg or "").split("/")[0]
    if vd_exempt(pkg):
        return "ok_exempt"
    if vd_policy_mode() in ("off", "0", "false", "warn"):
        return "disabled"
    return "violation_main_screen_view"


# ---------------------------------------------------------------- 截图 + 危险区

def grab(path, force=False, display_id=0):
    """截图。优先走逗沙包 App 的原子观测（快、JPEG），失败回退 adb screencap（慢、PNG）。

    返回**实际落盘的路径**（扩展名随所用通道变化），失败返回 None。
    每次调用都会刷新 LAST_GRAB = {"src","ms","reused","fallback"}，供上层做可观测性
    （回退 screencap 是会静默变慢的隐性悬崖，必须让它在日志/审计里看得见）。

    ⚠️ 虚拟屏（display_id != 0）**禁止回退 screencap**：本机 `screencap -d 36`
    直接报 "Display Id '36' is not valid"，真回退只会拿到主屏的图 —— 那正是
    "看错屏"最危险的一条路径。取不到就返回 None，让闸门拒绝。
    """
    global LAST_GRAB
    base = path
    for ext in (".png", ".jpg", ".jpeg"):
        if base.lower().endswith(ext):
            base = base[: -len(ext)]
            break
    t0 = time.time()
    obs = observe(with_image=True, force=force, display_id=display_id)
    if obs and obs.get("image") and not obs.get("err"):
        jp = base + ".jpg"
        if save_look_jpg(obs.get("jpg") or "look.jpg", jp):
            LAST_GRAB = {"src": "app", "ms": round(time.time() - t0, 3),
                         "reused": bool(obs.get("reused")), "fallback": False,
                         "frame_ts": (obs.get("ts") or 0) / 1000.0,
                         "age_ms": obs.get("ageMs", 0),
                         # 2026-10-01（App 批次一落的信号，现在开始往审计里记）：
                         # 这一帧本来就取回了观测图 JSON，这几项是**搭便车**，零额外往返。
                         "a_hash": obs.get("ahash") or "",
                         "tree_hash": obs.get("treeHash") or "",
                         "ev_count": obs.get("evCount"),
                         "window_id": obs.get("windowId", -1),
                         "interactive": obs.get("interactive")}
            return jp
    if int(display_id) != 0:
        # 虚拟屏没有 screencap 回退（会静默拿到主屏）；失败即失败。
        LAST_GRAB = {"src": "none", "ms": round(time.time() - t0, 3), "fallback": False,
                     "error": "virtual_display_capture_failed", "display_id": int(display_id),
                     "a_hash": "", "tree_hash": "", "ev_count": None,
                     "window_id": -1, "interactive": None}
        return None
    try:
        data = subprocess.run(adb_command() + ["exec-out", "screencap", "-p"],
                              capture_output=True, timeout=90).stdout
    except Exception:
        LAST_GRAB = {"src": "none", "ms": round(time.time() - t0, 3), "fallback": True,
                     "a_hash": "", "tree_hash": "", "ev_count": None,
                 "window_id": -1, "interactive": None}
        return None
    if len(data) < 1000:
        LAST_GRAB = {"src": "none", "ms": round(time.time() - t0, 3), "fallback": True,
                     "a_hash": "", "tree_hash": "", "ev_count": None,
                 "window_id": -1, "interactive": None}
        return None
    pn = base + ".png"
    open(pn, "wb").write(data)
    LAST_GRAB = {"src": "screencap", "ms": round(time.time() - t0, 3), "fallback": True,
                 "a_hash": "", "tree_hash": "", "ev_count": None,
                 "window_id": -1, "interactive": None}
    print("⚠ 截图快路径不可用，已回退 screencap（%.2fs）——频繁出现请检查无障碍是否还绑着"
          % (time.time() - t0), file=sys.stderr)
    return pn


LOOK_DIR = "/sdcard/Android/data/com.dsb.assistant/files"
LOOK_JSON = LOOK_DIR + "/look.json"          # 「观测」命令（只控件树）
LOOK_TOKEN = "dsh-8f3a2c"
OBS_TIMEOUT = 4.0
LAST_GRAB = {}                                # grab() 每次填充的可观测信息


def _nodes_from(d):
    """把 App 快照里的节点转成 build_zones 认的结构（text/cx/cy/rect）。"""
    out = []
    for n in (d or {}).get("nodes", []):
        try:
            txt = (n.get("text") or "").strip()
            r = n.get("rect")
            if not txt or not r or len(r) != 4:
                continue
            x1, y1, x2, y2 = (int(v) for v in r)
            out.append({"text": txt,
                        "cx": (x1 + x2) // 2, "cy": (y1 + y2) // 2,
                        "rect": [x1, y1, x2, y2]})
        except Exception:
            continue
    return out


def save_look_jpg(src_name, dest):
    """把 App 写好的 look-<nonce>.jpg 拷到 dest（exec-out，二进制安全）。"""
    try:
        data = subprocess.run(adb_command() + ["exec-out", "cat", LOOK_DIR + "/" + src_name],
                              capture_output=True, timeout=60).stdout
    except Exception:
        return False
    if len(data) < 1000:
        return False
    d = os.path.dirname(dest)
    if d:
        os.makedirs(d, exist_ok=True)
    try:
        open(dest, "wb").write(data)
    except Exception:
        return False
    return True


def observe(with_image=True, timeout=OBS_TIMEOUT, force=False, display_id=0):
    """向逗沙包 App 要一次**原子观测**：截图 + 控件树 + 前台包名 + 时间戳，同一次调用产出。

    返回 dict；失败/超时返回 None —— 主屏调用方回退老的 screencap / uiautomator。
    带图时读 look-<nonce>.json（**按 nonce 分文件**，消掉多调用方互相覆盖的竞态）；
    只取控件树时主屏读 look.json、虚拟屏读 look-<nonce>.json。
    App 侧对过密请求会**复用上一帧**并标 reused=true（复用缓存按 displayId 隔离）。

    虚拟屏（display_id != 0）用「观测屏图:<id>:<nonce>」；返回前校验 displayId 一致，
    不一致直接 None（串屏保护）——绝不把主屏观测当虚拟屏。

    ⚠️ 命令值里不能有空格：adb 本地拼串、设备端 shell 会再切一次。统一用冒号分隔。
    """
    did = int(display_id or 0)
    nonce = str(int(time.time() * 1000))
    # force=True → 带 :f，要求 App 不做"过密复用"，必须真拍一帧
    if did == 0:
        cmd = ("观测图:" if with_image else "观测:") + nonce + (":f" if force else "")
    else:
        cmd = ("观测屏图:" if with_image else "观测屏树:") + str(did) + ":" + nonce \
              + (":f" if force else "")
    try:
        subprocess.run(adb_command() + ["shell", "am", "broadcast", "-n",
                              "com.dsb.assistant/.CmdReceiver",
                              "-a", "com.dsb.assistant.CMD",
                              "--es", "token", LOOK_TOKEN,
                              "--es", "cmd", cmd],
                       capture_output=True, timeout=20)
    except Exception:
        return None
    path = (LOOK_DIR + "/look-" + nonce + ".json") if (with_image or did != 0) else LOOK_JSON
    t0 = time.time()
    while time.time() - t0 < timeout:
        raw = sh("shell", "cat", path, t=20)
        if nonce in raw:
            try:
                d = json.loads(raw)
            except Exception:
                return None
            if isinstance(d.get("nodesRaw"), str):
                try:
                    d["nodes"] = json.loads(d["nodesRaw"])
                except Exception:
                    d["nodes"] = []
            got = int(d.get("displayId", 0 if did == 0 else -1))
            if got != did:
                print("✗ 观测返回的 displayId=%s 与请求 %s 不一致（串屏保护）" % (got, did),
                      file=sys.stderr)
                return None
            d["_elapsed"] = time.time() - t0
            return d
        time.sleep(0.05)
    return None


def _settle_stable(budget=2.5, interval=0.35, min_polls=2, display_id=0):
    """动作后等屏幕稳定：借鉴 phone-harness scroll_screen 的判据
    （「连续两次读取完全相同 = 已稳定」），但用的是我们更便宜的无图**强刷**观测。

    为什么能替代写死的 sleep(3)：轮询的是 treeHash + evCount（App 原子观测），
    两个都停 = 界面不再变化；视频/动画页永远等不到，就用满预算——
    证据帧保证拍在稳定后，比盲等 3 秒质量更高（盲等可能拍在动画中）。

    返回 (settle_ms, polls, settled)。
    """
    t0 = time.time()
    deadline = t0 + budget
    prev = None
    polls = 0
    time.sleep(0.25)                      # 给动作本身一点生效时间
    while time.time() < deadline:
        d = observe(with_image=False, force=True, display_id=display_id)
        polls += 1
        if d:
            cur = (d.get("treeHash") or "", d.get("evCount"))
            if cur == prev and polls >= min_polls:
                return (round((time.time() - t0) * 1000), polls, True)
            prev = cur
        time.sleep(interval)
    return (round((time.time() - t0) * 1000), polls, False)


def dump_nodes_app():
    """只取控件树（不带图），比 uiautomator dump 快一个数量级。失败返回 None。"""
    d = observe(with_image=False)
    return _nodes_from(d) if d is not None else None


def dump_nodes_uiautomator():
    """老路子：uiautomator dump + cat（1~2 秒）。只在 App 快照不可用时用。"""
    sh("shell", "uiautomator", "dump", "/sdcard/_safe.xml", t=90)
    xml = sh("shell", "cat", "/sdcard/_safe.xml", t=90)
    out = []
    for n in re.findall(r"<node[^>]*>", xml):
        t = re.search(r'text="([^"]*)"', n)
        b = re.search(r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', n)
        if t and b and t.group(1).strip():
            x1, y1, x2, y2 = map(int, b.groups())
            out.append({"text": t.group(1).strip(),
                        "cx": (x1 + x2) // 2, "cy": (y1 + y2) // 2,
                        "rect": [x1, y1, x2, y2]})
    return out


def dump_nodes():
    """拿控件树文字及其位置。

    优先走逗沙包 App 的快照（毫秒级），失败回退 uiautomator dump（1~2 秒）。
    两条路径返回结构完全一致，调用方 build_zones / node_count 无感。
    """
    fast = dump_nodes_app()
    if fast is not None:
        return fast
    return dump_nodes_uiautomator()


EXTRA_ZONES = os.path.join(HERE, "safe_tap_zones.json")


def build_zones(shot_id, nodes, fg=None):
    """G2：把危险词的位置变成禁区；并叠加人工登记的额外禁区。

    ⚠️ 2026-09-23 修：带 _page 的「always」额外禁区现在**只在该页面前台时生效**。
    原实现里 always 是全局的 —— 微信「已登录设备」页登记的那条矩形禁区
    (x 445..1339, y 2253..2527) 于是外溢到所有 App：我在另一个 App 里
    点作曲器/模型面板那一横条，全被闸门以"屏幕上有「退出登录」"为由拒绝（实测踩到）。
    它本来是"该页整页都危险"的语义，就该跟页面绑定。
    """
    zones = []
    for nd in nodes:
        # 2026-10-21：输入框的**占位提示**（"发送给 张三, 李四"）不是按钮，不能建禁区。
        # 否则它外扩 40px 会盖住旁边的「+」附件按钮，导致只能点 3 像素宽的缝。
        _cls = (nd.get("cls") or "")
        _txt = (nd.get("text") or "").strip()
        if "EditText" in _cls or _txt.startswith("发送给"):
            continue
        for w in DANGER_WORDS:
            if w in nd["text"]:
                zones.append({"word": w, "text": nd["text"][:30],
                              "cx": nd["cx"], "cy": nd["cy"],
                              "rect": nd["rect"], "pad": DANGER_PAD})
                break
    try:
        extra = json.load(open(EXTRA_ZONES, encoding="utf-8"))
        for z in extra.get("zones", []):
            page = z.get("_page")
            if page and fg and page != fg:
                continue          # 这条禁区属于别的页面 → 不在本页生效
            if z.get("shot_id") in (None, shot_id) or z.get("always"):
                zones.append({**z, "pad": z.get("pad", DANGER_PAD), "word": z.get("word", "(手工登记)")})
    except Exception:
        pass
    return zones


def in_zone(x, y, z):
    r = z.get("rect")
    if r:
        x1, y1, x2, y2 = r
        p = z.get("pad", DANGER_PAD)
        if x1 - p <= x <= x2 + p and y1 - p <= y <= y2 + p:
            return True
    if z.get("cx") is not None:
        p = z.get("pad", DANGER_PAD)
        if abs(x - z["cx"]) <= p and abs(y - z["cy"]) <= p:
            return True
    return False


_DIALOG_WORDS = ["同意", "允许", "确定", "取消", "我知道了", "以后再说", "暂不", "跳过", "拒绝"]


def _detect_dialogs(nodes):
    """疑似弹窗检测（P1-1，借鉴 phone-harness interruption 的 Android 版）。
    只保留有信号的证据：Dialog/PopupWindow 类名（kind-1）、弹窗按钮文案+少节点（kind-2）。
    2026-10-01 复核修复：删掉 kind-3「新小窗口」——windowId 变化只是正常导航（新 Activity
    = 新窗口），稀疏页面（设置首页18/图库10/文件26节点）全被误报，实测误报率 75%。
    **只检测不自动点**，不改 G1-G5 判定；hint 必须写明命中的启发式与证据。"""
    dialogs = []
    dlg_cls = [n for n in nodes
               if any(k in (n.get('cls') or '').lower()
                      for k in ('dialog', 'popupwindow', 'alertdialog'))]
    if dlg_cls:
        cls_names = sorted({(n.get('cls') or '') for n in dlg_cls if n.get('cls')})[:2]
        dialogs.append({'kind': 'dialog-class', 'nodes': len(dlg_cls),
                        'evidence': cls_names,
                        'buttons': [(n.get('text') or '')[:10] for n in dlg_cls[:4]
                                    if (n.get('text') or '')]})
    texts = [(n.get('text') or n.get('desc') or '') for n in nodes]
    hits = sorted({w for w in _DIALOG_WORDS if any(w in t for t in texts)})
    if len(nodes) < 20 and hits:
        dialogs.append({'kind': 'consent-buttons', 'nodes': len(nodes), 'buttons': hits})
    hints = []
    for d in dialogs:
        if d['kind'] == 'dialog-class':
            hints.append('命中：Dialog 类名 %s' % '、'.join(d.get('evidence') or ['?']))
        elif d['kind'] == 'consent-buttons':
            hints.append('命中：弹窗按钮文案 %s' % '/'.join(d.get('buttons') or [])[:30])
    hint = ''
    if hints:
        btns = [b for d in dialogs for b in (d.get('buttons') or []) if b][:4]
        hint = ('⚠ 疑似弹窗（%s）：%s —— 授权类动作请交给人决定，不要自动点'
                % ('；'.join(hints),
                   '；'.join('【%s】' % b for b in btns) if btns else '未见按钮文本'))
    return dialogs, hint


# ---------------------------------------------------------------- 命令

def _ocr_danger_scan(last):
    """空控件树时的兜底：对凭据截图做 OCR，返回危险词禁区列表（每项 word/text/cx/cy/rect/pad，
    与 look 用像素文本建禁区同一口径：危险词所在行外扩 DANGER_PAD）；OCR 不可用或行缺几何返回 None。"""
    if not OCR_OK:
        return None
    path = os.path.join(SHOTS, "shot-%s.jpg" % ((last or {}).get("id") or "0"))
    if not os.path.isfile(path):
        path = (last or {}).get("path") or ""
    if not path or not os.path.isfile(path):
        return None
    try:
        res = pixel_recognize_result(path, float(cfg_get("ocr.min_confidence", 0.5)),
                                     timeout_s=15)
    except Exception:
        return None
    if (res or {}).get("status") not in ("ok", "empty"):
        return None
    zones = []
    for row in (res.get("rows") or []):
        text = row.get("text") or ""
        for w in DANGER_WORDS:
            if w in text:
                try:
                    x, y, rw, rh = int(row["x"]), int(row["y"]), int(row["w"]), int(row["h"])
                except (KeyError, TypeError, ValueError):
                    return None          # 定位不了危险词 → 按"OCR 不可用"保守处理
                zones.append({"word": w, "text": text[:30], "cx": x, "cy": y,
                              "rect": [x - rw // 2, y - rh // 2, x + rw // 2, y + rh // 2],
                              "pad": DANGER_PAD, "source": "ocr_gate"})
                break
    return zones


def _danger_bands(zones, w, h):
    """OCR 危险词行 → 贯穿全屏宽的横带（OCR 文字框不是按钮边界：整宽"确认付款"栏点边缘也要拦）。
    上下留白按屏高等比放大（720x1600 虚拟屏 40px，1260x2800 主屏 70px）。"""
    pad = max(DANGER_PAD, int(round(DANGER_PAD * h / 1600.0)))
    return [dict(z, rect=[0, z["rect"][1], w, z["rect"][3]], pad=pad) for z in zones]


def _ocr_cancel_check():
    flag = "/sdcard/Android/data/com.dsb.assistant/files/abort.flag"
    initial = sh("shell", "cat", flag, t=1).strip()
    next_check = [0.0]
    stopped = [False]
    def cancelled():
        if stopped[0]:
            return True
        now = time.monotonic()
        if now < next_check[0]:
            return False
        next_check[0] = now + .5
        current = sh("shell", "cat", flag, t=1).strip()
        stopped[0] = bool(current and current != initial)
        return stopped[0]
    return cancelled


def _observation_failure(stage, result):
    state = load_state()
    state.pop("last_shot", None)
    save_state(state)
    print(json.dumps({"ok": False, "found": False, "error": stage, "ocr": result}, ensure_ascii=False))
    return 3


def cmd_look(a):
    load_state()  # Refuse corrupt state before reading any device surface.
    cancel_check = _ocr_cancel_check()
    with lock():
        if cancel_check():
            return _observation_failure("cancelled", {"status": "cancelled", "rows": []})
        return _look_locked(a, cancel_check)


def _target_key(text):
    return "".join(text.split()).casefold()


def _look_locked(a, cancel_check):
    target = getattr(a, "target", None)
    if target is not None:
        target = target.strip()
        if not target:
            return _observation_failure("empty_target", {"status": "not_run", "rows": []})
    os.makedirs(SHOTS, exist_ok=True)
    sid = str(int(time.time() * 1000))
    t_look = time.time()
    display_id = int(getattr(a, "display", 0) or 0)
    # 虚拟屏：先验会话（显示器存在 / 未过期），再采一帧；任何一步失败都不产生可用凭据。
    vd_info = {}
    if display_id != 0:
        if not VD_OK:
            return _observation_failure("vd_module_unavailable", {"status": "not_run", "rows": []})
        vr = vd.verify()
        if not vr.get("ok"):
            return _observation_failure("vd_" + str(vr.get("reason")), {"status": "not_run", "rows": []})
        vd_info = vr.get("session") or {}
        if int(vd_info.get("displayId", -1)) != display_id:
            return _observation_failure("vd_display_mismatch", {"status": "not_run", "rows": []})
    # 上次动作之后紧接着的 look，必须强制真采 —— 复用帧可能是"动作前"的那一张
    last_action = load_state().get("last_action_ts", 0)
    force_look = ((time.time() - last_action) < 3.0 or getattr(a, "ocr", False)
                  or target is not None or display_id != 0)
    obs = observe(with_image=True, force=force_look, display_id=display_id)   # 一次原子观测：截图 + 控件树 同源同刻
    path, src = None, ""
    if obs and obs.get("image") and not obs.get("err"):
        cand = os.path.join(SHOTS, f"shot-{sid}.jpg")
        if save_look_jpg(obs.get("jpg") or "look.jpg", cand):
            path, src = cand, ("app-reused" if obs.get("reused") else "app")
    if path is None:
        if display_id != 0:
            # 虚拟屏禁止 screencap 回退（会拿到主屏 → 看错屏）；失败即失败。
            return _observation_failure("vd_capture_failed",
                                        {"status": "not_run", "rows": [], "display_id": display_id})
        path = grab(os.path.join(SHOTS, f"shot-{sid}.png"))
        if not path:
            return _observation_failure("capture_failed", {"status": "not_run", "rows": []})
        src, obs = "screencap", None
    # 虚拟屏：观测必须与请求同屏、帧尺寸等于会话尺寸、亮屏、非黑图、前台可确认。
    if display_id != 0:
        got_did = int((obs or {}).get("displayId", -1))
        if got_did != display_id:
            return _observation_failure("vd_observe_display_mismatch:%s" % got_did,
                                        {"status": "not_run", "rows": []})
        if bool(obs.get("black")):
            return _observation_failure("vd_black_frame", {"status": "not_run", "rows": []})
        if int(obs.get("w") or 0) != int(vd_info.get("width") or 0) or \
                int(obs.get("h") or 0) != int(vd_info.get("height") or 0):
            return _observation_failure("vd_frame_size_mismatch:%sx%s" % (obs.get("w"), obs.get("h")),
                                        {"status": "not_run", "rows": []})
        # App 侧必须真的拿到**该屏**的应用窗口。appWindows=0 说明无障碍没有这块屏的窗口，
        # 此时"页面内容"不可知 → 拒绝产出凭据（绝不回退主屏树或 OCR 猜测）。
        if int(obs.get("appWindows", -1) or 0) <= 0 or \
                str(obs.get("displayCoverage") or "") != "tree":
            return _observation_failure(
                "vd_no_app_windows:appWindows=%s,cov=%s,winErr=%s"
                % (obs.get("appWindows"), obs.get("displayCoverage"), obs.get("windowError") or "-"),
                {"status": "not_run", "rows": []})
        obs_pkg = (obs.get("fg") or "").split("/")[0]
        # 例外（2026-10-21）：目标 App 自己拉起的系统选择器/授权框（同一块虚拟屏）。
        # 不加这个例外，飞书「发文件」流程会在 documentsui 这一步被误判成串屏。
        if (obs_pkg not in VD_ON_BEHALF_PKGS
                and not vd_info.get("allowForeign")
                and (not obs_pkg or obs_pkg != vd_info.get("package"))):
            return _observation_failure("vd_observed_app_mismatch:%s(want %s)"
                                        % (obs_pkg or "-", vd_info.get("package")),
                                        {"status": "not_run", "rows": []})
    nodes = _nodes_from(obs) if obs else dump_nodes()
    # 闸门凭据的 fg 必须用 shell 版（格式 包名/Activity）；App 版只有包名，拿来比会永远不等
    fg, main_fg_now = _foregrounds(display_id)
    if display_id != 0:
        # shell 与 App 两个来源都必须指向会话里的目标 App，否则就是跳屏 / ID 复用。
        # 例外：目标 App 拉起的系统选择器/授权框（同一块屏）。
        _fg_pkg = (fg.split("/")[0] if fg else "")
        if (not fg or _fg_pkg != vd_info.get("package")) and _fg_pkg not in VD_ON_BEHALF_PKGS \
                and not vd_info.get("allowForeign"):
            return _observation_failure("vd_shell_target_mismatch:%s(want %s)"
                                        % (fg or "-", vd_info.get("package")),
                                        {"status": "not_run", "rows": []})
        # 2026-10-21（D1，owner 定）：虚拟屏空树页与主屏同规则——照常出凭据
        # （zone_coverage=none + OCR 像素文本），非导航动作仍须显式 ack，
        # 并在 check_gate 里过 OCR 扫词 + 视觉判断。原先这里直接 vd_tree_unavailable 失败，
        # 导致微信等主力 App 在虚拟屏上完全不可读。应用窗口仍必须存在（上面 appWindows/cov 检查）。
    zones = build_zones(sid, nodes, fg)
    # 2026-10-01（P0-1 像素兜底）：空树页 + OCR 可用 → 对截图跑容器侧 OCR。
    # ① pixel_nodes 附带进凭据（walk/wait/collect 在空树页复活）；
    # ② 危险词在像素文本上重建禁区（G2 不再空转——微信/淘宝系的安全盲区补上）。
    pixel_nodes = []
    ocr_result = {"status": "not_needed", "rows": []}
    _text_nodes = [n for n in nodes if (n.get("text") or n.get("desc") or "")]
    tree_matches = [n for n in nodes if target is not None and
                    _target_key(target) in (_target_key(n.get("text") or ""), _target_key(n.get("desc") or ""))]
    needs_ocr = not _text_nodes or getattr(a, "ocr", False) or (target is not None and not tree_matches)
    if needs_ocr and cfg_get("ocr.enabled", True) and path:
        try:
            ocr_result = (pixel_recognize_result(path, float(cfg_get("ocr.min_confidence", 0.5)),
                          timeout_s=15, cancel_check=cancel_check) if OCR_OK
                          else {"status": "unavailable", "rows": []})
        except Exception:
            ocr_result = {"status": "error", "rows": []}
        if ocr_result["status"] not in ("ok", "empty"):
            return _observation_failure("ocr_failed", ocr_result)
        pixel_nodes = ocr_result["rows"]
    elif needs_ocr:
        ocr_result = {"status": "disabled", "rows": []}
    if cancel_check():
        return _observation_failure("cancelled", {"status": "cancelled", "rows": []})
    for row in pixel_nodes:                   # G2：危险词 × 像素文本
        for w in DANGER_WORDS:
            if w in row["text"]:
                zones.append({"word": w, "text": row["text"][:30],
                              "cx": row["x"], "cy": row["y"],
                              "rect": [row["x"] - row["w"] // 2, row["y"] - row["h"] // 2,
                                       row["x"] + row["w"] // 2, row["y"] + row["h"] // 2],
                              "pad": DANGER_PAD, "source": "pixels"})
                break
    shot_black = bool(obs.get("black")) if obs else False
    _ball("橙")          # 脉冲：让用户知道我正在操作（看门狗 120s 自动回蓝）
    st = load_state()
    # 2026-10-01（App 批次一）：亮屏状态优先用 **App 快照里的 interactive** —— 省掉一次 dumpsys power 往返；
    # 只有走 screencap 回退（obs is None）时才去问 dumpsys。
    if obs is not None and "interactive" in obs:
        interactive = bool(obs.get("interactive"))
        wf = "Awake" if interactive else "Asleep"
    else:
        wf = wakefulness()
        interactive = (wf.lower() == "awake")
    # G2 覆盖率（2026-10-01 加）：控件树一个节点都读不到时，"危险词 → 禁区"根本没得可查（**空真**）。
    # 微信这类自绘 / FLAG_SECURE 页面恰恰如此 —— 必须让它在凭据里显式可见，别让闸门空转。
    # 三值（2026-10-01 修，复测者指出要把「有树但 0 禁区」与「空树」分开）：
    #   none  = 空树（0 个带文字节点）→ G2 空真，动手前必须显式 ack
    #   ok    = 有树，且识别到危险词
    #   clean = 有树，但没命中危险词（正常"干净页面"，**不是空转**）
    if len(nodes) == 0:
        zone_coverage = "none"
    elif zones:
        zone_coverage = "ok"
    else:
        zone_coverage = "clean"
    # ── 弹窗/新窗口检测（P1-1）：只检测不自动点，不改 G1-G5 语义 ──
    prev_win = (st.get("last_shot") or {}).get("window_id", -1)
    cur_win = (obs.get("windowId", -1)) if obs else -1
    window_changed = bool(cur_win != -1 and prev_win not in (-1, None) and cur_win != prev_win)
    dialogs, interruption_hint = _detect_dialogs(nodes)
    st["last_shot"] = {"id": sid, "path": path, "fg": fg,
                       # 2026-10-20 虚拟屏：凭据与显示器/会话/尺寸绑定；check_gate 逐项复核
                       "display_id": display_id,
                       "display_width": int((obs or {}).get("w") or 0) if display_id else 0,
                       "display_height": int((obs or {}).get("h") or 0) if display_id else 0,
                       "vd_sid": vd_info.get("sid") if display_id else None,
                       "display_coverage": (obs or {}).get("displayCoverage") if display_id else None,
                       "main_fg": (main_fg_now if display_id else fg),
                       "pixel_nodes": pixel_nodes,
                       "ocr": {k: v for k, v in ocr_result.items() if k != "rows"},
                       "window_changed": window_changed,
                       "dialogs": dialogs,
                       "interruption_hint": interruption_hint,
                       # ts = **真帧时间**（复用帧就是原始帧时间，不是观测时刻）；TTL 因此不会被复用骗过
                       "ts": ((obs.get("ts") or 0) / 1000.0) if obs else time.time(),
                       "zones": zones,
                       "node_count": len(nodes),
                       "shot_src": src, "shot_black": shot_black,
                       "shot_reused": bool(obs.get("reused")) if obs else False,
                       "shot_age_ms": obs.get("ageMs", 0) if obs else 0,
                       "look_forced": force_look,
                       "fg_app": (obs.get("fg") or "") if obs else "",
                       "wakefulness": wf, "interactive": interactive,
                       "zone_coverage": zone_coverage,
                       # 2026-10-01（App 批次一）：这三个是"零往返信号"，先**只记录不改判定**
                       #（第二批"按节点点击"才消费它们当"真成功判据"，复测者建议的切分）
                       "tree_hash": (obs.get("treeHash") or "") if obs else "",
                       "a_hash": (obs.get("ahash") or "") if obs else "",
                       "ev_count": (obs.get("evCount") or 0) if obs else 0,
                       # 2026-10-01（第二批 2a）：R1 守卫要 windowId；R3/R4 要原始节点字段
                       "window_id": (obs.get("windowId", -1)) if obs else -1,
                       "nodes": [{"viewId": n.get("viewId", ""), "text": n.get("text", ""),
                                  "desc": n.get("desc", ""), "norm": n.get("norm", ""),
                                  "cls": n.get("cls", ""), "ord": n.get("ord", 0),
                                  "qrect": n.get("qrect", ""), "rect": n.get("rect"),
                                  "digest": n.get("digest", ""), "handle": n.get("handle", ""),
                                  "clickable": n.get("clickable", False),
                                  "enabled": n.get("enabled", False),
                                  "selected": n.get("selected", False),
                                  "checked": n.get("checked", False),
                                  "visibleToUser": n.get("visibleToUser", True)}
                                 for n in (obs.get("nodes") or [])] if obs else []}
    # A new observation is not permission to repeat an exhausted action.
    save_state(st)
    w, h = _screen_size_on(display_id)
    if display_id and (w, h) != (int((obs or {}).get("w") or 0), int((obs or {}).get("h") or 0)):
        # 会话尺寸 / 截图帧尺寸必须一致（防止读到别的显示器尺寸的帧）
        return _observation_failure("vd_dimension_mismatch:%sx%s vs %sx%s" % (w, h, obs.get("w"), obs.get("h")),
                                    {"status": "not_run", "rows": []})
    target_result = {}
    if target is not None:
        # Normalized text equality returns raw evidence, never action permission.
        matches = tree_matches or [r for r in pixel_nodes if _target_key(r["text"]) == _target_key(target)]
        known = interactive and not shot_black and (bool(tree_matches) or ocr_result["status"] in ("ok", "empty"))
        target_result = {"target": {
            "text": target,
            "match": "casefold_no_whitespace",
            "status": ("unknown" if not known else "ambiguous" if len(matches) > 1 else
                       "found" if matches else "not_found"),
            "source": "tree" if tree_matches else "pixels" if ocr_result["status"] in ("ok", "empty") else "none",
            "candidates": matches if known else []}}
    print(json.dumps({
        **target_result,
        "shot_id": sid, "screenshot": path, "activity": fg,
        "display_id": display_id,
        "display_size": (f"{w}x{h}" if display_id else "main"),
        # 2026-10-20：主屏上看非控制面 App = violation（不拦，但每次都喊出来）
        "vd_policy": vd_look_hint(display_id, fg if not display_id else (main_fg_now or fg)),
        "vd_session": (vd_info.get("sid") if display_id else None),
        "node_count": len(nodes), "danger_zones": len(zones),
        "node_source": ("tree" if len(nodes) else "none"),
        "pixel_nodes": pixel_nodes,
        "ocr": {k: v for k, v in ocr_result.items() if k != "rows"},
        "window_changed": window_changed,
        "dialogs": dialogs,
        "interruption_hint": interruption_hint,
        "shot_src": src, "shot_black": shot_black,
        "shot_reused": bool(obs.get("reused")) if obs else False,
        "shot_age_ms": obs.get("ageMs", 0) if obs else 0,
        "look_forced": force_look,
        "fg_app": (obs.get("fg") or "") if obs else "",
        "look_ms": int((time.time() - t_look) * 1000),
        "wakefulness": wf, "interactive": interactive,
        "zone_coverage": zone_coverage,
        "tree_hash": (obs.get("treeHash") or "") if obs else "",
        "a_hash": (obs.get("ahash") or "") if obs else "",
        "ev_count": (obs.get("evCount") or 0) if obs else 0,
        "screen": f"{w}x{h}", "note": a.note or "",
        # 2026-10-01（复测者 §25⑤ 第①层，零闸门改动）：决策阶段就避开"已选中"的项
        "hints": {"already_active": [
            {"text": (n.get("text") or "")[:40], "digest": n.get("digest", ""),
             "handle": n.get("handle", ""),
             "state": ("checked" if n.get("checked") else "selected")}
            for n in ((obs or {}).get("nodes") or [])
            if n.get("selected") or n.get("checked")]},
        "next": f'python3 safe_tap.py tap --shot {sid} --x .. --y .. --expect "..."'
    }, ensure_ascii=False, indent=1))
    # 2026-10-01：读操作也入审计（借鉴 phone-harness 的 helper trace）——复盘时能回答"当时看到了什么"
    audit({"kind": "read", "op": "look", "note": a.note or "", "allowed": True,
           "node_count": len(nodes), "activity": fg,
           "display_id": display_id, "vd_sid": (vd_info.get("sid") if display_id else None),
           "look_ms": int((time.time() - t_look) * 1000)})
    # ⚠️ 以下提示一律走 stderr：stdout 只留那一个 JSON 对象，机器消费方才能直接 json.load。
    #    （复测者实测：原来严格 json.load(stdout) 会报 Extra data: line 21）
    sys.stdout.flush()
    if zone_coverage == "none":
        print("", file=sys.stderr)
        print("⛔ G2 危险区检查本次**空转**：控件树读到 0 个节点"
              "（游戏 / Canvas 自绘 / 显式关闭无障碍的页面常见）。", file=sys.stderr)
        print("   注意：FLAG_SECURE 只挡截图、不挡控件树 —— 那是「黑图 + 亮屏 → 以控件树为准」那一支，不是这里。", file=sys.stderr)
        print("   没有控件树就没有危险词，「退出登录 / 删除 / 解绑」这类按钮**不会被识别**。", file=sys.stderr)
        print("   非导航动作已拒绝，请人工接管；--ack-no-zones 不授予权限（HOME / BACK / 最近任务保留导航豁免）。", file=sys.stderr)
        print("   当前 zone_coverage=none；若只是「有树但没命中危险词」会是 clean（无需 ack）。", file=sys.stderr)
    elif zone_coverage == "clean":
        print("", file=sys.stderr)
        print("✓ G2 正常：本页读到 %d 个节点，只是没命中危险词 —— 这是「干净页面」，**不是空转**。" % len(nodes),
              file=sys.stderr)
    if shot_black:
        if not interactive:
            print("", file=sys.stderr)
            print("⚠ 截图是黑的，而且屏幕当前**是息屏状态**（%s）—— 先唤醒手机再操作，别拿黑图当依据。" % wf, file=sys.stderr)
        else:
            print("", file=sys.stderr)
            print("⚠ 截图是黑的，但屏幕是亮的 —— 这是 FLAG_SECURE / 安全页，**以控件树为准**（shot_black 只是提示）。", file=sys.stderr)
    elif not interactive:
        print("", file=sys.stderr)
        print("⚠ 屏幕当前是**息屏状态**（%s）—— 截图与点击都可能不生效，先唤醒手机。" % wf, file=sys.stderr)
    if zones:
        print("", file=sys.stderr)
        print("⚠ 检测到的禁区（这些位置会被拒绝）：", file=sys.stderr)
        for z in zones:
            print(f"   · 「{z.get('word')}」({z.get('text','')}) @ 中心({z.get('cx')},{z.get('cy')})", file=sys.stderr)
    return 0


def _fg_same(a, b):
    """两个"前台"标识是否算同一页。

    2026-10-01 加。闸门凭据的 fg 来自 shell（格式 包名/Activity），而 App 版只有包名 ——
    只做全等比较会在混合来源时误判。规则：
      全等必过；包名级相等**只在至少一侧没有 Activity 段**时才算过
      （两侧都带 Activity 且不同 = 真的换页了，仍然拒绝）。
    """
    if a == b:
        return True
    if not a or not b:
        return False
    if "/" in a and "/" in b:
        return False
    return a.split("/")[0] == b.split("/")[0]


_INPUT_D_HELP = None


def _input_supports_display():
    """`input` 是否支持 -d DISPLAY_ID（本机实测支持；启动时探测一次并缓存）。

    虚拟屏上的按键只能靠 input -d 绑定显示器（无障碍的 performGlobalAction 不能指定屏）。
    探测失败/不支持 → 虚拟屏按键一律拒绝（fail closed），不回退到"主屏按键"。
    """
    global _INPUT_D_HELP
    if _INPUT_D_HELP is None:
        try:
            r = subprocess.run(adb_command() + ["shell", "input"], capture_output=True, timeout=10)
            _INPUT_D_HELP = (r.stdout + r.stderr).decode("utf-8", "ignore")
        except Exception:
            _INPUT_D_HELP = ""
    return ("-d DISPLAY_ID" in _INPUT_D_HELP) or ("-d: specify the display ID" in _INPUT_D_HELP)


def _check_display_binding(last, kind):
    """虚拟屏凭据的额外前置：会话活着、帧来自 App、主屏没被动过、前台没变。

    全部 fail closed：任何一项无法确认都拒绝派发（不会"按主屏继续"）。
    """
    did = int((last or {}).get("display_id") or 0)
    if did == 0:
        return True, "ok"
    if not VD_OK:
        return False, "虚拟屏模块不可用（vd_session.py 缺失）"
    if (last.get("shot_src") or "") not in ("app", "app-reused"):
        return False, "虚拟屏凭据不是 App 按屏观测（%s），拒绝使用" % last.get("shot_src")
    if last.get("shot_black"):
        return False, "虚拟屏帧是黑图，不能作为点击依据"
    # 2026-10-21（D1）：只要求拿到同屏应用窗口；空树（nodes=0）交给 G2b 的 ack + OCR + 视觉判断。
    if str(last.get("display_coverage") or "") != "tree":
        return False, ("虚拟屏没有拿到同屏应用窗口（cov=%s, nodes=%s）→ 拒绝派发；"
                       "必须重新 look 且拿到同屏应用窗口。"
                       % (last.get("display_coverage"), last.get("node_count")))
    # App 观测到的前台包 vs shell 观测到的前台包，必须与凭据里的 fg 同属一个包。
    fg_pkg = (last.get("fg") or "").split("/")[0]
    app_pkg = (last.get("fg_app") or "").split("/")[0]
    if not fg_pkg or app_pkg != fg_pkg:
        return False, "App 观测前台(%s)与 shell 前台(%s)不一致，拒绝派发" % (
            last.get("fg_app") or "-", last.get("fg") or "-")
    v = vd.verify(last.get("vd_sid"))
    if not v.get("ok"):
        return False, "虚拟屏会话失效：%s" % v.get("reason")
    sess = v.get("session") or {}
    if int(sess.get("displayId", -1)) != did:
        return False, "虚拟屏会话显示器不匹配（会话 %s，凭据 %s）" % (sess.get("displayId"), did)
    if fg_pkg != sess.get("package") and fg_pkg not in VD_ON_BEHALF_PKGS:
        return False, "凭据前台(%s)不是会话目标 App(%s)，拒绝派发" % (fg_pkg, sess.get("package"))
    if (int(sess.get("width") or 0), int(sess.get("height") or 0)) != \
            (int(last.get("display_width") or 0), int(last.get("display_height") or 0)):
        return False, "虚拟屏尺寸与会话不一致（会话 %sx%s，凭据 %sx%s）" % (
            sess.get("width"), sess.get("height"), last.get("display_width"), last.get("display_height"))
    # 2026-10-21 用户明确要求：**去掉「主屏前台变了就拒绝虚拟屏动作」**。
    # 虚拟屏存在的意义就是"后台并行"——用户在主屏干什么都不影响这块屏。
    # 凭据绑定的是：同一块屏（displayId+uniqueId+尺寸）+ 该屏自己的前台没变（上面那段）。
    # main_fg 仍记录在凭据里，仅供事后复盘，不再参与放行判断。
    cur = v.get("foreground") or foreground(did)      # verify 刚取的同一时刻前台，不再重复查
    if not cur or not _fg_same(cur, last.get("fg")):
        cur2 = foreground(did)
        if not cur2 or not _fg_same(cur2, last.get("fg")):
            return False, "虚拟屏前台变了：%s → %s（复取 %s）。必须重新 look" % (
                last.get("fg"), cur, cur2)
        cur = cur2
    _GATE_EVIDENCE["vd_fg"] = cur     # 同一把锁内紧接着的 G1 前台核对复用（同一凭据、相隔毫秒）
    if kind in ("key", "type") and not _input_supports_display():
        return False, "本机 input 不支持 -d，虚拟屏按键无法绑定 displayId → 拒绝"
    return True, "ok"


NAV_KEYCODES = {3, 4, 187}   # HOME / BACK / APP_SWITCH：纯导航键，G2 空转时**豁免 ack**
KEYCODE_PASTE = 279           # 空树页唯一放行的非导航键（type 粘贴用；owner 2026-10-21）
KEY_REPEAT_LIMIT = 2      # 2026-10-01：同一 keycode 连续按超过这个次数 → 熔断（按键没有坐标，必须按 keycode 计数）
REPEAT_HISTORY_LIMIT = 64
GESTURE_KINDS = frozenset(("swipe", "scroll", "drag", "long_press"))
GESTURE_LIMIT = 16


def _display_gesture_insets(display_id, h, w=0):
    """虚拟屏自己的底部系统手势/导航 inset（2026-10-21 F2）。

    实测：scrcpy `--no-vd-system-decorations` 建的屏在 `dumpsys window displays` 里**没有任何
    InsetsSource**（主屏底部有 2716-2800 的强制手势区）。原先虚拟屏一律套主屏的 84px 底部禁带，
    把微信底部 tab（y=1541，屏高 1600）整排挡掉。现在按该屏自己的 inset 算；
    读不到/解析失败 → 回落 EDGE_BOTTOM（保守，等同旧行为）。
    """
    fallback = {"left": EDGE_X, "right": EDGE_X, "bottom": EDGE_BOTTOM, "source": "fallback"}
    try:
        out = sh("shell", "dumpsys", "window", "displays")
        for part in re.split(r"(?m)^\s*Display: mDisplayId=", out or "")[1:]:
            head = part.split(None, 1)
            if not head or head[0] != str(display_id):
                continue
            bottom = 0
            for line in part.splitlines():
                mt = re.search(r"InsetsSource.*?type=(\w+)", line)
                if not mt or mt.group(1) not in ("navigationBars", "mandatorySystemGestures"):
                    continue
                mf = re.search(r"frame=\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]", line)
                if not mf:
                    raise ValueError("truncated inset frame")   # 残缺行不能当"没有 inset"
                _l, t, _r, b = (int(v) for v in mf.groups())
                # 只有贴到屏幕底边、且是横条（宽≥屏宽一半）的才算底部 inset；横屏右侧竖条不算
                if b > t and b >= h - 1 and t > 0 and (not w or (_r - _l) >= w // 2):
                    bottom = max(bottom, b - t)
            return {"left": EDGE_X, "right": EDGE_X, "bottom": bottom, "source": "dumpsys"}
    except Exception:
        return fallback                               # 截断/异常输出一律回落保守值
    return fallback


R3_BAND = 0.035       # 比对横带半高 = 屏高 × 3.5%（1600 屏约 ±56px），覆盖目标所在的一行
R3_MAX_DIFF = 6.0     # 48×16 灰度缩略图平均差（0-255）。canary 虚拟屏实测（dsh-bridge/r3-cal）：
                      # 静态两帧 0.0；单个数字变化 2.6–2.9（角标级，放行）；换成另一行 51–58（重排，拒绝）


def _r3_verify(cred_img, display_id, pt):
    """派发前复核目标区域（R3）：现拍一帧，与凭据帧比对目标点所在的整行横带。

    行重排/内容替换 → 差异大 → 拒绝；拍不到/比不了 → 保守拒绝。只看目标附近一条，
    别处变化（状态栏时钟等）不影响。返回 {"ok", "diff", "why"}。"""
    try:
        from PIL import Image, ImageChops, ImageStat
    except Exception:
        return {"ok": False, "diff": None, "why": "pil_unavailable"}
    if not cred_img or not os.path.isfile(cred_img):
        return {"ok": False, "diff": None, "why": "no_credential_image"}
    os.makedirs(SHOTS, exist_ok=True)
    fresh = grab(os.path.join(SHOTS, "r3-%d.jpg" % int(time.time() * 1000)), force=True,
                 display_id=display_id)
    if not fresh or not os.path.isfile(fresh):
        return {"ok": False, "diff": None, "why": "capture_failed"}
    try:
        ia = Image.open(cred_img).convert("L")
        ib = Image.open(fresh).convert("L")
        if ia.size != ib.size:
            return {"ok": False, "diff": None, "why": "size %sx%s vs %sx%s" % (ia.size + ib.size)}
        w, h = ia.size
        _x, y = int(pt[0]), int(pt[1])
        hh = max(24, int(h * R3_BAND))
        box = (0, max(0, y - hh), w, min(h, y + hh))
        diff = ImageStat.Stat(ImageChops.difference(ia.crop(box).resize((48, 16)),
                                                    ib.crop(box).resize((48, 16)))).mean[0]
    except Exception as e:
        return {"ok": False, "diff": None, "why": "compare_failed:%s" % type(e).__name__}
    finally:
        try:
            os.remove(fresh)          # 只留差异值进审计，不多存一张屏
        except OSError:
            pass
    ok = diff <= R3_MAX_DIFF
    return {"ok": ok, "diff": round(diff, 2),
            "why": "" if ok else "diff=%.1f>%.1f" % (diff, R3_MAX_DIFF)}


def _long_press_ms(a):
    """实际派发的长按时长：须与派发端一致（主屏/--raw：≥200；虚拟屏手势：再截到 40..3000，
    见 _dispatch_display_gesture）。视觉判断必须按这个时长去问。"""
    ms = max(200, int(getattr(a, "ms", 800) or 800))
    if int(getattr(a, "display", 0) or 0) != 0 and not getattr(a, "raw_input", False):
        ms = max(40, min(3000, ms))
    return ms


def check_gate(a, kind, points):
    """所有硬性检查集中在这里。返回 (ok, reason, shot)"""
    _GATE_EVIDENCE.clear()        # dsb_run 同进程多次动作：不得沿用上一次的判定
    st = load_state()
    last = st.get("last_shot")
    if not last:
        return False, "没有截图凭据：请先运行 look", None
    if a.shot != last["id"]:
        return False, f"凭据不匹配：当前有效凭据是 {last['id']}，你给的是 {a.shot}", last

    # 2026-10-20 虚拟屏：凭据的 displayId 必须与本次动作请求完全一致（禁止跨屏复用）。
    display_id = int(getattr(a, "display", 0) or 0)
    if int(last.get("display_id", 0) or 0) != display_id:
        return False, ("凭据属于 displayId=%s，本次请求 displayId=%s —— 跨显示器复用凭据是禁止的。"
                       "请用对应 --display 重新 look。"
                       % (last.get("display_id", 0), display_id)), last

    if "consumed_at" in last:
        return False, "凭据已用于动作下发：请重新 look 并重新分析，不得重用或自动重试", last

    ok_bind, why_bind = _check_display_binding(last, kind)
    if not ok_bind:
        return False, why_bind, last

    # All frames must postdate the last action, including non-cached observations.
    _frame_ts = last.get("ts") or 0
    _la = st.get("last_action_ts", 0)
    if _la and _frame_ts <= _la:
        return False, ("这张凭据的画面不晚于上次动作（相差 %.2fs）—— 不能拿「动作前」的旧整屏决策。"
                       "必须重新 look。" % (_la - _frame_ts)), last

    age = time.time() - last["ts"]
    if age > float(cfg_get("look.ttl", SHOT_TTL)):         # G1（可配置）
        return False, f"截图凭据已过期（{age:.0f}s > {SHOT_TTL:.0f}s）：请重新 look", last

    fg_now = (_GATE_EVIDENCE.get("vd_fg") if display_id else None) or _foreground_on(display_id)
    if not _fg_same(fg_now, last["fg"]):                   # G1（按凭据所属显示器复核）
        fg_retry = _foreground_on(display_id)                  # 重取一次：吸收"恰好切页瞬间"的抖动
        if not _fg_same(fg_retry, last["fg"]):
            return False, (f"看完截图后前台变了：{last['fg']} → {fg_now}（复取 {fg_retry}）。"
                           f"你看的是旧页面，必须重新 look"), last

    w, h = _screen_size_on(display_id)
    if display_id and (w, h) == (0, 0):
        return False, "拿不到虚拟屏尺寸（会话可能已失效），拒绝派发", last
    ins = gesture_insets(w, h) if display_id == 0 else _display_gesture_insets(display_id, h, w)
    # swipe 的起点会被系统手势抢走，左右用**实测的 systemGestures 宽度**；tap 一般能穿过去，保持 EDGE_X。
    edge_l = max(EDGE_X, ins["left"]) if kind == "swipe" else EDGE_X
    edge_r = max(EDGE_X, ins["right"]) if kind == "swipe" else EDGE_X
    # 主屏底部至少 EDGE_BOTTOM；虚拟屏只按该屏自己的 inset（解析失败时为 EDGE_BOTTOM）
    edge_b = max(EDGE_BOTTOM, ins["bottom"]) if display_id == 0 else ins["bottom"]
    for pt in points:
        # 2026-10-01 修：key 动作只有一个整数码（cmd_key 传 [a.code]），没有坐标——
        # 原来写死 for (x, y) in points: 会对整数解包 → TypeError，key 子命令从来没跑通过。
        if not isinstance(pt, (tuple, list)) or len(pt) != 2:
            continue
        x, y = pt
        if x < edge_l or x > w - edge_r:                   # G3
            return False, (f"({x},{y}) 落在左/右边缘禁带（左<{edge_l} / 右<{edge_r}px）"
                           f"——边缘会被系统手势截获"), last
        if y < EDGE_TOP:                                   # G3
            return False, f"({x},{y}) 落在顶部禁带（<{EDGE_TOP}px）——状态栏/返回箭头区", last
        if y > h - edge_b:                                 # G3（2026-10-01 加，取实测强制手势区）
            return False, (f"({x},{y}) 落在**底部手势导航禁带**（屏幕高 {h}，禁带 {h - edge_b}~{h}）"
                           f"——底部会被系统手势截获（上滑回桌面 / 切换任务）"), last
        if not (0 <= x < w and 0 <= y < h):                # 像素坐标 0..w-1 / 0..h-1（半开区间）
            return False, f"({x},{y}) 超出屏幕 {w}x{h}", last
        for z in last.get("zones", []):                    # G2
            if in_zone(x, y, z):
                return False, (f"({x},{y}) 落在禁区：屏幕上有「{z.get('word')}」"
                               f"（{z.get('text','')}）@({z.get('cx')},{z.get('cy')}）。"
                               f"拒绝执行——这类按钮不可点。"), last

    # G2b: caller-supplied flags are not user approval. Unknown coverage fails
    # closed; existing navigation exceptions still obey every other gate.
    _is_nav_key = (kind == "key" and points and points[0] in NAV_KEYCODES)
    if last.get("zone_coverage") not in ("ok", "clean") and not _is_nav_key:
        # 2026-10-21 用户要求：空控件树不再一律拒绝。
        # 新规则：调用方显式 --ack-no-zones（= 我看过截图）时，先做 **OCR 危险词扫描**：
        #   扫到危险词 → 仍然拒绝；扫描干净 → 放行并审计 ack_source=ocr_clean；
        #   OCR 不可用    → 保守拒绝（无法确认就不点）。
        # 2026-10-21 owner 定：空树页上非导航按键只放行粘贴（279，type 用）——回车/空格等可能直接发送或确认，
        # 看不清页面就不按（白名单，不枚举危险键）。导航键 3/4/187 在上面已豁免。
        if kind == "key" and not (points and points[0] == KEYCODE_PASTE):
            return False, ("空控件树页上，非导航按键只允许粘贴（279）：回车等可能直接发送/确认，"
                           "看不见页面结构就不按。请人工接管。"), last
        if not getattr(a, "ack_no_zones", False):
            return False, ("空控件树，无法自动识别危险按钮。请先看截图确认没有「退出登录/删除/注销/解绑/转账」这类按钮，"
                           "再加 --ack-no-zones 重试（会 OCR 兜底扫描并记审计）。"
                           "HOME / BACK / 最近任务仍保留导航豁免。"), last
        ocr_zones = _ocr_danger_scan(last)
        if ocr_zones is None:
            return False, ("空控件树且 OCR 不可用，无法确认页面有无危险按钮 → 保守拒绝。"
                           "请人工查看屏幕后再操作。"), last
        words = "、".join(sorted({z["word"] for z in ocr_zones}))
        _GATE_EVIDENCE.update(ocr_danger=[{"word": z["word"], "cx": z["cx"], "cy": z["cy"]} for z in ocr_zones])
        # 2026-10-21 owner 批准：屏上有危险词时，**点按**不再一律整页拒绝（京东订单列表常驻
        # 「删除订单/待付款」，整页一个都点不了）：落点不在危险词行的整行横带里 + 视觉判断目标
        # impact=none（下面）才放行。其余动作照旧整页拒绝——Codex review 两轮否决了滑动类放宽：
        # 零位移滑动=点击；滑动确认/横滑删除/拖进删除区本身就能执行危险操作；纵向滚动整条轨迹落在
        # 高大的危险按钮里时，View 抬手仍可能触发点击，且这条路不经视觉。
        if ocr_zones:
            if kind not in ("tap", "long_press"):
                return False, ("空控件树 + OCR 扫到危险词：%s → 拒绝执行（这类按钮不可点）。" % words), last
            bands = _danger_bands(ocr_zones, w, h)
            for x, y in points:
                for b in bands:
                    if in_zone(x, y, b):
                        rows = "、".join("「%s」y=%d~%d" % (q["word"], q["rect"][1] - q["pad"], q["rect"][3] + q["pad"])
                                         for q in bands[:8])
                        return False, ("空控件树 + OCR：(%d,%d) 落在危险词「%s」所在横带里 → 拒绝执行。"
                                       "本屏危险横带（整行贯穿屏宽）：%s；请换一个不在横带里的点。"
                                       % (x, y, b["word"], rows)), last
            last["_ack_source"] = "ocr_zones_clear_ack"
        else:
            last["_ack_source"] = "ocr_clean_ack"
        # 2026-10-21（D2）：点按类再请看图模型判断目标本身（OCR 读不到图标、词表也列不全）。
        # impact≠none（含 unclear）→ 拒绝；不可用 → 维持上面的 OCR 规则，审计可查。
        if kind in ("tap", "long_press"):
            shot_path = (last.get("path") or
                         os.path.join(SHOTS, "shot-%s.jpg" % (last.get("id") or "0")))
            # 判断的必须是实际要执行的动作：长按不能按"点一下"去问
            action = ("长按约 %d 毫秒" % _long_press_ms(a)) if kind == "long_press" else "点击"
            verdicts, seen = [], set()
            for pt in points:
                if not isinstance(pt, (tuple, list)) or len(pt) != 2 or tuple(pt) in seen:
                    continue          # long_press 传两个相同端点：同一点只判一次
                seen.add(tuple(pt))
                vr = (vision_check.judge_tap(shot_path, pt[0], pt[1], getattr(a, "expect", "") or "",
                                             action=action)
                      if VISION_OK else {"status": "unavailable", "reason": "module_missing"})
                verdicts.append(vr)
                last["_vision"] = verdicts
                _GATE_EVIDENCE.update(vision=verdicts)
                if vr.get("status") == "ok" and vr.get("impact") != "none":
                    return False, ("空控件树 + 视觉判断：%s「%s」impact=%s（%s）→ 拒绝执行，请人工接管。"
                                   % (action, vr.get("target") or "?", vr.get("impact"), vr.get("reason") or "-")), last
            if verdicts and all(v.get("status") == "ok" for v in verdicts):
                last["_ack_source"] = "ocr_zones_vision_clean_ack" if ocr_zones else "ocr_vision_clean_ack"
            elif verdicts:
                if ocr_zones:
                    return False, ("空控件树 + 屏上有危险词（%s）且视觉判断不可用，无法确认目标本身安全"
                                   " → 保守拒绝（同原整页规则）。" % words), last
                last["_ack_source"] = "ocr_clean_ack_vision_unavailable"
        _GATE_EVIDENCE.update(ack_detail=last.get("_ack_source"))

    repeat_limit = None
    if kind in ("tap", "key"):
        setting = "breaker.key_limit" if kind == "key" else "breaker.repeat_limit"
        repeat_limit = cfg_get(setting, KEY_REPEAT_LIMIT if kind == "key" else REPEAT_LIMIT)
        if type(repeat_limit) is not int or not 1 <= repeat_limit <= REPEAT_HISTORY_LIMIT:
            return False, f"重复动作预算配置无效：{setting} 必须是1..64的整数", last

    if kind in GESTURE_KINDS:
        limit = cfg_get("breaker.gesture_limit", GESTURE_LIMIT)
        if type(limit) is not int or not 1 <= limit <= REPEAT_HISTORY_LIMIT:
            return False, "手势预算配置无效：breaker.gesture_limit 必须是1..64的整数", last
        # Shared across gesture aliases. UI change is not verified progress and
        # never refunds an attempt; unknown foreground matches conservatively.
        used = sum(1 for g in st.get("gestures", [])
                   if int(g.get("display_id", 0) or 0) == display_id
                   and (not g.get("act_before") or not last.get("fg")
                        or _fg_same(g["act_before"], last["fg"])))
        if used >= limit:
            return False, (f"同一前台手势预算用尽（{used}/{limit}次尝试），请人工接管。"
                           "这不代表此前动作没有进展；刷新观测或换手势不会清零。"), last

    # G4b: observations and non-key actions do not reset the key history.
    if kind == "key":
        code = points[0]
        # ⚠️ 2026-10-01 修：原实现是"最近 6 条里**累计**同 code"，于是
        #    HOME,BACK,HOME,BACK,HOME,BACK 这种交错序列会互相累计到 3 而被误判成"连按"。
        #    改成只数**尾部连续段**，与文案里的"连续"一致。
        run = 0
        for k in reversed(st.get("keys", [])):
            if int(k.get("display_id", 0) or 0) != display_id:
                break
            if k.get("code") == code:
                run += 1
            else:
                break
        if run >= repeat_limit:
            return False, (f"同一个按键（code={code}）已**连续**按了 {run} 次 → 熔断。"
                           f"刷新观测不会清零；请换一种操作或人工接管，不得重试相同按键。"), last

    # G4：同点连击熔断
    taps = [t for t in st.get("taps", [])
            if int(t.get("display_id", 0) or 0) == display_id
            and (not t.get("act_before") or _fg_same(t["act_before"], last["fg"]))]
    if kind == "tap":
        x, y = points[0]
        # 2026-10-01：加"成功判据" —— 上一次同点点击**已经把前台换掉**的不计入熔断，
        # 否则"连加 3 个条目"这类合法连点会被误拒。
        # ⚠️ 判据只有"前台/Activity 变了"这一档：同页内弹窗这类变化仍会被计入，
        #    所以文案据实写成"都没有造成页面跳转"，不再承诺"未确认成功"。
        dig = _digest_at(x, y, last)
        # 2026-10-01（复测者 §25⑤ 第③层·窄规则）：目标在 look 时已是**选中态**，
        # 且上一次点击**毫无反应** → 这是不可能产生新效果的无意义重复，第 2 次就熔断。
        # 不做硬拒绝（重开当前 tab / 刷新 / 再点确认 是合法的第 1 次点击）。
        if dig:
            sel_prev = [t for t in taps[-4:]
                        if t.get("digest") == dig and t.get("target_selected")
                        and not t.get("ui_reacted")]
            if sel_prev:
                return False, ("该控件在 look 时已是**选中态**，且上一次点击毫无反应"
                               "（无意义重复）→ 熔断。重复点击不会有新效果。"
                               "请重新 look 并换个目标。"), last
        near = [t for t in taps if not t.get("changed") and (
            (dig and t.get("digest") == dig) or
            (abs(t.get("x", 1e9) - x) <= REPEAT_RADIUS and
             abs(t.get("y", 1e9) - y) <= REPEAT_RADIUS))]
        what = "同一前台的同一控件或位置附近（60px半径）"
        if len(near) >= repeat_limit:
            return False, (f"{what}已连点 {len(near)} 次，且这几次**都没有造成页面跳转** → 熔断。"
                           f"刷新观测不会清零；请换个目标或人工接管，不要再试同一个目标。"), last
    # 闸门内的 dumpsys / OCR / 视觉调用都可能耗时：放行前对所有路径（含 clean 页与导航键）再核一次 G1 时效
    age = time.time() - last["ts"]
    if age > float(cfg_get("look.ttl", SHOT_TTL)):
        return False, ("凭据在闸门检查期间过期（%.0fs）：请重新 look，不要自动重试。" % age), last
    return True, "ok", last


def do_action(a, kind, points):
    with lock():
        try:
            return _do_action_locked(a, kind, points)
        except ActionDispatchError as error:
            rec = {"kind": kind, "points": points, "shot": getattr(a, "shot", None),
                   "display_id": int(getattr(a, "display", 0) or 0),
                   "allowed": True, "status": error.status, "reason": error.reason,
                   "dispatch_status": error.status, "returncode": error.returncode,
                   "ack_detail": _GATE_EVIDENCE.get("ack_detail"),
                   "vision": _GATE_EVIDENCE.get("vision"),
                   "ocr_danger": _GATE_EVIDENCE.get("ocr_danger"),
                   "clipboard_cleared": _GATE_EVIDENCE.get("clipboard_cleared"),
                   "r3": _GATE_EVIDENCE.get("r3"),
                   "goal_verified": False, "retryable": False}
            audit(rec)
            print(json.dumps(rec, ensure_ascii=False))
            return 3


def _refuse_action(a, kind, points, last, reason):
    print(f"✗ 闸门拒绝：{reason}", file=sys.stderr)
    audit({"kind": kind, "points": points, "expect": getattr(a, "expect", None),
           "shot": getattr(a, "shot", None), "allowed": False, "reason": reason,
           "shot_before": "", "dispatch_status": "not_submitted",
           "zone_coverage": (last or {}).get("zone_coverage"),
           "ack_source": "caller_untrusted" if getattr(a, "ack_no_zones", False) else None,
           "ack_detail": (last or {}).get("_ack_source"),
           "vision": (last or {}).get("_vision"),
           "ocr_danger": _GATE_EVIDENCE.get("ocr_danger"),
           "r3": _GATE_EVIDENCE.get("r3"),
           "ack_no_zones": bool(getattr(a, "ack_no_zones", False)),
           "approval_verified": False, "goal_verified": False})
    return 2


def _dispatch_display_gesture(display_id, kind, points, a):
    """把动作绑定到虚拟屏 displayId 并通过 App 下发；等**回执**才算成功。

    返回 {"channel":"accessibility-display","status":"completed",...}。
    - tap/swipe/long_press/drag/scroll 全走 AccessibilityService.GestureDescription.setDisplayId
      （坐标就是 takeScreenshot(displayId) 那张图的像素坐标）；
    - key 走 `input -d <id> keyevent`（无障碍 global action 不能指定屏），
      前置已由 _input_supports_display() 探测；
    - 回执超时 / cancelled / rejected 一律抛 ActionDispatchError → 审计记录真实原因，
      **不自动重试、不回退主屏 input**。
    """
    if kind == "key":
        sh("shell", "input", "-d", str(display_id), "keyevent", str(points[0]), checked=True)
        return {"channel": "input-d", "status": "completed"}
    if kind == "tap":
        x1, y1 = points[0]
        x2, y2 = x1, y1
        ms = 120
    elif kind == "long_press":
        x1, y1 = points[0]
        x2, y2 = x1, y1
        ms = max(200, int(getattr(a, "ms", 800) or 800))
    elif kind in ("swipe", "drag", "scroll"):
        (x1, y1), (x2, y2) = points[0], points[1]
        ms = 300 if kind == "swipe" else (max(200, int(getattr(a, "ms", 600) or 600))
                                          if kind == "drag" else 400)
    else:
        raise ActionDispatchError("unsupported_display_action", "not_submitted")
    ms = max(40, min(3000, int(ms)))
    nonce = str(int(time.time() * 1000))
    cmd = "手势屏:%d:%s:%d:%d:%d:%d:%d:%s" % (display_id, kind, int(x1), int(y1), int(x2), int(y2), ms, nonce)
    try:
        subprocess.run(adb_command() + ["shell", "am", "broadcast", "-n",
                              "com.dsb.assistant/.CmdReceiver",
                              "-a", "com.dsb.assistant.CMD",
                              "--es", "token", LOOK_TOKEN,
                              "--es", "cmd", cmd],
                       capture_output=True, timeout=20)
    except Exception:
        raise ActionDispatchError("gesture_broadcast_failed", "not_submitted")
    path = LOOK_DIR + "/gesture-" + nonce + ".json"
    deadline = time.time() + 6
    rec = {}
    while time.time() < deadline:
        raw = sh("shell", "cat", path, t=10)
        if nonce in raw:
            try:
                rec = json.loads(raw)
            except Exception:
                rec = {}
            if rec.get("status") in ("completed", "cancelled", "rejected"):
                break
        time.sleep(0.1)
    status = rec.get("status") or "timeout"
    if status != "completed":
        raise ActionDispatchError("gesture_" + status, status)
    if int(rec.get("displayId", -1)) != int(display_id):
        raise ActionDispatchError("gesture_receipt_display_mismatch", "unknown")
    return {"channel": "accessibility-display", "status": status,
            "receipt": {"displayId": rec.get("displayId"), "kind": rec.get("kind")}}


def _do_action_locked(a, kind, points):
    # ① 虚拟屏策略最先判（2026-10-20）：主屏上操作别的 App 直接拒绝，连凭据都不看。
    _did = int(getattr(a, "display", 0) or 0)
    _blk = vd_policy_block(kind, _did, _foreground_on(_did),
                           allow_main=bool(getattr(a, "allow_main", False)),
                           why=getattr(a, "why", None))
    if _blk:
        _reason, _tag = _blk
        rec = {"kind": kind, "points": points, "display_id": _did,
               "allowed": False, "vd_policy": _tag, "reason": _reason,
               "allow_main": bool(getattr(a, "allow_main", False)),
               "why": getattr(a, "why", None),
               "dispatch_status": "not_submitted", "shot_before": ""}
        audit(rec)
        print("✗ 闸门拒绝：%s" % _reason, file=sys.stderr)
        if _tag == "vd_required":
            print("   本次未下发任何动作。", file=sys.stderr)
        return 2
    ok, reason, last = check_gate(a, kind, points)
    if not ok:
        return _refuse_action(a, kind, points, last, reason)
    display_id = int(getattr(a, "display", 0) or 0)
    # 2026-10-01：fast 浏览模式 —— 跳过前后两张 JPEG 留证（省 ~1s/步），
    # 信号改由「凭据（look 时已存）+ 动作后观测」提供，ui_reacted 不含像素维度。
    # **闸门检查照常**：fast 只省证据开销，不减安全。
    fast = (bool(getattr(a, "fast", False)) or os.environ.get("SAFE_TAP_FAST") == "1"
            or bool(cfg_get("action.fast_default", False)))
    cred_img = os.path.join(SHOTS, "shot-%s.jpg" % ((last or {}).get("id") or "0"))
    if not os.path.isfile(cred_img):
        cred_img = (last or {}).get("path") or ""
    if fast:
        before = ""
        before_meta = {"src": "fast",
                       "tree_hash": (last or {}).get("tree_hash") or "",
                       "a_hash": (last or {}).get("a_hash") or "",
                       "ev_count": (last or {}).get("ev_count")}
    elif cred_img and os.path.isfile(cred_img):
        # 2026-10-01 选 B（借鉴 phone-harness 的动作/验证解耦）：凭据帧就是
        # "动作前的世界"——agent 决策依据的正是它，且 G1 已校验前台一致，
        # 不再重复现拍（省 ~1.3s/次）。
        # 口径注记：前后差分从此度量「自查凭据以来的变化」，失败率基线以本轮为界(v2)。
        before = cred_img
        before_meta = {"src": "credential",
                       "tree_hash": (last or {}).get("tree_hash") or "",
                       "a_hash": (last or {}).get("a_hash") or "",
                       "ev_count": (last or {}).get("ev_count"),
                       "window_id": (last or {}).get("window_id")}
    else:
        before = os.path.join(SHOTS, f"before-{int(time.time()*1000)}.jpg")
        os.makedirs(SHOTS, exist_ok=True)
        before = grab(before, display_id=display_id) or before
        before_meta = dict(LAST_GRAB)
    # R3（2026-10-21，owner 授权）：派发前现拍一帧，与凭据帧比对目标点所在横带。
    # 放在下面的前台/时效核对之前：拍帧要时间，核对必须发生在拍帧之后。
    # 真实事故：飞书发文件时群列表在 look 与 tap 之间重排，两次点错群——G1 只核前台，管不到同页内容变化。
    r3 = None
    if kind in ("tap", "long_press"):
        r3 = _r3_verify(cred_img, display_id, points[0])
        _GATE_EVIDENCE["r3"] = r3
        if not r3.get("ok"):
            return _refuse_action(a, kind, points, last,
                                  "派发前目标所在行与观测时不一致（%s）：页面在看完之后变了，动作未下发；"
                                  "请重新 look 并分析，不得自动重试。" % (r3.get("why") or "?"))
    fg_before = _foreground_on(display_id)
    # Evidence preparation can outlive the gate check. Reject a change already
    # observed here; this is not an atomic Android compare-and-inject guarantee.
    if not fg_before or not _fg_same(fg_before, last.get("fg")):
        return _refuse_action(a, kind, points, last,
                              f"派发前前台变化或不可确认：{last.get('fg')} → {fg_before}。"
                              "动作未下发；请重新 look 并分析，不得自动重试。")
    if time.time() - (last.get("ts") or 0) > float(cfg_get("look.ttl", SHOT_TTL)):
        return _refuse_action(a, kind, points, last,
                              "凭据在派发前准备（留证/R3 拍帧）期间过期：动作未下发；请重新 look，不得自动重试。")
    # Persist consumption before any dispatch. An uncertain/failed action cannot
    # make its old observation usable again, even if post-action capture fails.
    state = load_state()
    state["last_shot"]["consumed_at"] = time.time()
    state["last_action_ts"] = state["last_shot"]["consumed_at"]
    # Reserve before dispatch: an unknown outcome must still spend the budget.
    if kind == "tap":
        state.setdefault("taps", []).append({"x": points[0][0], "y": points[0][1],
            "ts": time.time(), "act_before": fg_before, "changed": False,
            "ui_reacted": False, "outcome": "unknown",
            "digest": _digest_at(points[0][0], points[0][1], last),
            "display_id": display_id,
            "target_selected": _sel_state(_digest_at(points[0][0], points[0][1], last), last)})
    elif kind == "key":
        state.setdefault("keys", []).append({"code": points[0], "ts": time.time(),
                                              "display_id": display_id})
    elif kind in GESTURE_KINDS:
        state.setdefault("gestures", []).append({"kind": kind,
            "act_before": last.get("fg") or "", "display_id": display_id, "ts": time.time()})
    for history in ("taps", "keys", "gestures"):
        state[history] = state.get(history, [])[-REPEAT_HISTORY_LIMIT:]
    save_state(state)
    gesture_info = {}
    if kind == "type":
        gesture_info = _dispatch_type(display_id, a)
    elif display_id != 0:
        # 虚拟屏：唯一派发通道是 App 的按屏手势（GestureDescription.setDisplayId）。
        # 返回回执后仍走同一套事后观测/审计；失败不自动重试、不回退 input。
        # 2026-10-21：`--raw` = 改用**真实注入触摸**（input -d）。有些系统界面
        # （实测 documentsui 的文件行）不响应无障碍手势，只认真实触摸；
        # 眼睛仍用无障碍截图，手换通道，闸门/留证/审计完全不变。
        if getattr(a, "raw_input", False):
            _ix = str(display_id)
            if kind == "tap":
                _x, _y = points[0]
                sh("shell", "input", "-d", _ix, "tap", str(_x), str(_y), checked=True)
            elif kind in ("swipe", "scroll", "drag"):
                (_x1, _y1), (_x2, _y2) = points[0], points[1]
                _ms = max(200, int(getattr(a, "ms", 300) or 300))
                sh("shell", "input", "-d", _ix, "swipe",
                   str(_x1), str(_y1), str(_x2), str(_y2), str(_ms), checked=True)
            elif kind == "long_press":
                _x, _y = points[0]
                _ms = max(200, int(getattr(a, "ms", 800) or 800))
                sh("shell", "input", "-d", _ix, "swipe",
                   str(_x), str(_y), str(_x), str(_y), str(_ms), checked=True)
            elif kind == "key":
                # 2026-10-21 F5：原先 key 在 raw 分支没有实现，0 次派发却报 completed 并消费凭据
                sh("shell", "input", "-d", _ix, "keyevent", str(points[0]), checked=True)
            else:
                raise ActionDispatchError("unsupported_raw_action:%s" % kind, "not_submitted")
            gesture_info = {"channel": "input-d-raw", "status": "completed"}
        else:
            gesture_info = _dispatch_display_gesture(display_id, kind, points, a)
    else:
        if kind == "tap":
            x, y = points[0]
            sh("shell", "input", "tap", str(x), str(y), checked=True)
        elif kind == "swipe":
            (x1, y1), (x2, y2) = points[0], points[1]
            sh("shell", "input", "swipe", str(x1), str(y1), str(x2), str(y2), "300", checked=True)
        elif kind == "long_press":
            # 借鉴 phone-harness 的 long_press：同点按下 → 保持 ms → 抬起。
            # input swipe 同坐标 + 时长即长按（Android 官方语义），纯 input 无需 App 通道。
            x, y = points[0]
            ms = max(200, int(getattr(a, "ms", 800) or 800))
            sh("shell", "input", "swipe", str(x), str(y), str(x), str(y), str(ms), checked=True)
        elif kind == "drag":
            # 借鉴 phone-harness 的 drag：按下 → 分步慢速移动（非甩）→ 终点 → 抬起。
            # 与 swipe（快速划过，用于翻页）的区别：drag 是「按住拖到某处」（排序/滑块）。
            (x1, y1), (x2, y2) = points[0], points[1]
            ms = max(200, int(getattr(a, "ms", 600) or 600))
            steps = max(2, int(getattr(a, "steps", 10) or 10))
            moves = " && ".join(
                "input motionevent MOVE %d %d" % (round(x1 + (x2 - x1) * i / steps),
                                                  round(y1 + (y2 - y1) * i / steps))
                for i in range(1, steps + 1))
            # UP is cleanup, not a second gesture. Preserve failure even if cleanup succeeds.
            seq = ("input motionevent DOWN %d %d && %s && sleep 0.2; rc=$?; "
                   "input motionevent UP %d %d; up_rc=$?; [ $rc -eq 0 ] && [ $up_rc -eq 0 ]"
                   % (int(x1), int(y1), moves, int(x2), int(y2)))
            sh("shell", seq, checked=True)
        elif kind == "key":
            sh("shell", "input", "keyevent", str(points[0]), checked=True)
        elif kind == "scroll":
            # 借鉴 phone-harness：按住→分步移动→停顿→抬手（content-space，防甩飞）。
            # 不支持 motionevent 或部分失败时拒绝，不自动重放 swipe。
            (x1, y1), (x2, y2) = points[0], points[1]
            steps = max(2, int(getattr(a, "steps", 8) or 8))
            moves = " && ".join(
                "input motionevent MOVE %d %d" % (round(x1 + (x2 - x1) * i / steps),
                                                  round(y1 + (y2 - y1) * i / steps))
                for i in range(1, steps + 1))
            seq = ("input motionevent DOWN %d %d && %s && sleep 0.2; rc=$?; "
                   "input motionevent UP %d %d; up_rc=$?; [ $rc -eq 0 ] && [ $up_rc -eq 0 ]"
                   % (int(x1), int(y1), moves, int(x2), int(y2)))
            sh("shell", seq, checked=True)

    # 2026-10-01 修：写死的"睡 0.5/3 秒再取证"换成「连续两次读取相同 = 已稳定」轮询
    # （借鉴 phone-harness scroll_screen；信号用我们更便宜的无图强刷观测）。
    # 快页面 ~0.5s 出头就稳定（原 3s），动画/跳转页自动多等到预算为止；
    # 证据帧必然拍在稳定后。--fast 只是把预算从 2.5s 降到 1.2s。
    settle_budget = float(cfg_get("action.settle_budget_fast" if fast else "action.settle_budget",
                                 1.2 if fast else 2.5))
    settle_ms, settle_polls, settled = _settle_stable(budget=settle_budget, display_id=display_id)
    fg_after = _foreground_on(display_id)
    if fast:
        ao = observe(with_image=False, display_id=display_id)
        after = ""
        after_meta = {"src": "fast",
                      "tree_hash": (ao or {}).get("treeHash") or "",
                      "ev_count": (ao or {}).get("evCount")}
    else:
        after = os.path.join(SHOTS, f"after-{int(time.time()*1000)}.jpg")
        after = grab(after, force=True, display_id=display_id) or after   # 动作后留证必须真拍
        after_meta = dict(LAST_GRAB)
    # type：输入框常只露出文字尾部（实测"文件传输助手"在微信搜索框只显示"输助手"），读回按尾部比对；
    # 只是提示，goal_verified 仍为 False。
    readback = (_type_readback(after, getattr(a, "text", "") or "",
                               [r.get("text") for r in (last.get("pixel_nodes") or [])] +
                               [n.get("text") for n in (last.get("nodes") or [])])
                if kind == "type" else None)

    exp_act = getattr(a, "expect_activity", None)
    act_ok = None
    if exp_act:
        act_ok = (fg_after == exp_act)
    # ⚠️ 判据只有"前台/Activity 变了"这一档：同页内的弹窗 / Toast / 滚动看不见。
    #    下次 App 批次可以**零往返**补两个更强的信号（复测者建议）：
    #      ① 用 do_action 本来就在拍的 before/after 帧算 16×16 ahash 比像素；
    #      ② 搭便车把 AgentAccessibilityService 本就在收的无障碍事件（lastEventTs / 计数）盖进同一个 JSON。
    #    彻底解是按节点 performAction(ACTION_CLICK) 拿真布尔。
    #    熔断取**或**（像素变 ∨ 事件 ∨ 前台变）—— 假失败会挡住该做的事，假成功只是让熔断弱一点。
    changed = bool(fg_before and fg_after and not _fg_same(fg_before, fg_after))

    # 2026-10-01（本轮 A）：把"UI 到底反应了没"从**只有前台**扩成**三路取或**。
    # 这三路信号都是 grab 前后帧时顺带拿到的，**零额外往返**：
    #   pixel_changed 比像素（16x16 ahash）  tree_changed 比控件树  ev_advanced 比无障碍事件计数
    # 熔断取**或**（复测者）：假失败会挡住该做的事，假成功只是让熔断弱一点。
    a_b = before_meta.get("a_hash") or ""
    a_a = after_meta.get("a_hash") or ""
    t_b = before_meta.get("tree_hash") or ""
    t_a = after_meta.get("tree_hash") or ""
    e_b = before_meta.get("ev_count")
    e_a = after_meta.get("ev_count")
    pixel_changed = bool(a_b and a_a and a_b != a_a)
    tree_changed = bool(t_b and t_a and t_b != t_a)
    ev_advanced = bool(e_b is not None and e_a is not None and e_a > e_b)
    ui_reacted = bool(changed or pixel_changed or tree_changed or ev_advanced)

    rec = {"kind": kind, "points": points, "expect": getattr(a, "expect", None),
           "shot": getattr(a, "shot", None), "allowed": True,
           "display_id": display_id, "display_kind": ("main" if display_id == 0 else "virtual"),
           "vd_sid": (last or {}).get("vd_sid"),
           "gesture_channel": gesture_info.get("channel") or ("input" if display_id == 0 else ""),
           "act_before": fg_before, "act_after": fg_after,
           "act_ok": act_ok, "screen_changed": changed,
           "dispatch_status": "command_completed", "goal_verified": False,
           "zone_coverage": (last or {}).get("zone_coverage"),
           "ack_source": ("caller_untrusted" if getattr(a, "ack_no_zones", False) else None),
           "ack_detail": (last or {}).get("_ack_source"),
           "vision": (last or {}).get("_vision"),
           "ocr_danger": _GATE_EVIDENCE.get("ocr_danger"),
           "r3": r3,
           "approval_verified": False,
           "ack_no_zones": bool(getattr(a, "ack_no_zones", False)),
           # 2026-10-01：量"坐标点击真实失败率"的数据源
           "a_hash_before": a_b, "a_hash_after": a_a, "pixel_changed": pixel_changed,
           "tree_hash_before": t_b, "tree_hash_after": t_a, "tree_changed": tree_changed,
           "ev_count_before": e_b, "ev_count_after": e_a, "ev_advanced": ev_advanced,
           "ui_reacted": ui_reacted, "fast": fast,
           "settle_ms": settle_ms, "settle_polls": settle_polls, "settled": settled,
           "shot_before": before, "shot_after": after,
           "shot_before_meta": before_meta, "shot_after_meta": after_meta,
           "resolved_by": getattr(a, "resolved_by", None),
           "handle": getattr(a, "node_handle", None),
           "target_digest": _digest_at(points[0][0], points[0][1], last) if kind == "tap" else None,
           "target_selected": _sel_state(_digest_at(points[0][0], points[0][1], last), last) if kind == "tap" else None,
           "readback": readback,
           "clipboard_cleared": gesture_info.get("clipboard_cleared") if kind == "type" else None}
    audit(rec)

    st = load_state()
    st["last_action_ts"] = time.time()       # 给 check_gate 的"旧帧拒绝"用
    if kind == "tap":
        st["taps"][-1].update({"act_after": fg_after, "changed": changed,
                              "ui_reacted": ui_reacted, "outcome": "command_completed"})
    save_state(st)

    _out = {k: rec[k] for k in
            ("kind", "points", "expect", "display_id", "vd_sid", "gesture_channel",
             "act_before", "act_after",
             "dispatch_status", "goal_verified",
             "act_ok", "screen_changed", "ui_reacted",
             "pixel_changed", "tree_changed", "ev_advanced",
             "a_hash_before", "a_hash_after",
             "tree_hash_before", "tree_hash_after",
             "ev_count_before", "ev_count_after",
             "shot_before", "shot_after",
             "shot_before_meta", "shot_after_meta",
             "resolved_by", "handle", "target_selected", "fast",
             "settle_ms", "settled")}
    if kind == "type":
        _out["readback"] = rec["readback"]
        _out["clipboard_cleared"] = rec["clipboard_cleared"]
        if rec["clipboard_cleared"] is False:
            print("⚠ 剪贴板清空失败：输入内容可能仍留在剪贴板里，请人工检查。", file=sys.stderr)
    print(json.dumps(_out, ensure_ascii=False, indent=1))
    if exp_act and act_ok is False:
        print(f"\n⚠ 未到预期页面（期望 {exp_act}，实际 {fg_after}）。"
              f"**不要连点**——先 look 重新分析。", file=sys.stderr)
        return 2
    return 0


def _iou(a, b):
    """两个 [l,t,r,b] 的交并比（R4 宽松匹配用）。"""
    try:
        ax1, ay1, ax2, ay2 = a
        bx1, by1, bx2, by2 = b
        ix1, iy1 = max(ax1, bx1), max(ay1, by1)
        ix2, iy2 = min(ax2, bx2), min(ay2, by2)
        inter = max(0, ix2 - ix1) * max(0, iy2 - iy1)
        ua = (ax2 - ax1) * (ay2 - ay1) + (bx2 - bx1) * (by2 - by1) - inter
        return (inter / ua) if ua > 0 else 0.0
    except Exception:
        return 0.0


def _qlist(q):
    """qrect 是逗号串（"l,t,r,b"）而 rect 是数组 —— 两种都容错。"""
    try:
        return [int(v) for v in str(q).split(",")]
    except Exception:
        return None


def _digest_at(x, y, last):
    """把屏幕坐标映射到"包含它的最小节点"的 digest（2026-10-01）。

    只用凭据里**本来就有**的 before 帧节点列表（look 时存的），**零额外往返**。
    找不到（自绘页 / 点在空白处）返回 None，调用方回落到 60px 半径规则。
    """
    best = None
    for n in ((last or {}).get("nodes") or []):
        r = n.get("rect")
        if not r or len(r) != 4:
            continue
        try:
            x1, y1, x2, y2 = (int(v) for v in r)
        except Exception:
            continue
        if x1 <= x <= x2 and y1 <= y <= y2:
            area = max(0, x2 - x1) * max(0, y2 - y1)
            if best is None or area < best[0]:
                best = (area, n.get("digest") or "")
    return (best[1] or None) if best else None


def _sel_state(dig, last):
    """digest → 该节点在 look 时是否已是选中态（selected/checked）。"""
    for n in ((last or {}).get("nodes") or []):
        if dig and n.get("digest") == dig:
            return bool(n.get("selected") or n.get("checked"))
    return False


def resolve_handle(handle):
    """把节点句柄解析成"现在"的点击坐标（第二批 2a；按复测者冻死的 R1–R4）。

    handle 格式：h1.<treeHash8>.<index>.<digest8>
    返回 {"ok","x","y","by","why","candidates"} —— **任何"不唯一"一律拒绝**（绝不赌一个）。
    ⚠️ R1 必须带 node_count>0 守卫：空树页面的 treeHash 是常量（sha1("") 前缀），
       否则两个不同的空树页会被误判成"同世界"。
    """
    out = {"ok": False, "why": "", "by": "", "candidates": []}
    parts = (handle or "").split(".")
    if len(parts) != 4 or parts[0] != "h1":
        out["why"] = "句柄格式不对（应为 h1.<treeHash8>.<index>.<digest8>）：%r" % handle
        return out
    h_hash, h_idx, h_dig = parts[1], parts[2], parts[3]
    try:
        h_index = int(h_idx)
    except Exception:
        out["why"] = "句柄里的 index 不是数字：%r" % h_idx
        return out

    stt = load_state()
    last = stt.get("last_shot") or {}
    ns_old = last.get("nodes") or []
    orig = None
    for n in ns_old:
        if n.get("handle") == handle:
            orig = n
            break
    if orig is None and 0 <= h_index < len(ns_old) and ns_old[h_index].get("digest") == h_dig:
        orig = ns_old[h_index]
    if orig is None:
        out["why"] = "这张凭据里找不到该句柄（可能来自更早的一次 look）：%s" % handle
        return out

    obs = observe(with_image=False)
    if not obs:
        out["why"] = "取不到当前控件树"
        return out
    ns_new = obs.get("nodes") or []
    th_new = obs.get("treeHash") or ""
    win_new = obs.get("windowId", -1)
    fg_new = foreground()
    win_old = last.get("window_id", -1)

    def _c(n):
        r = n.get("rect") or [0, 0, 0, 0]
        return (int(r[0]) + int(r[2])) // 2, (int(r[1]) + int(r[3])) // 2

    # R1 同世界：树哈希一致 ∧ 节点数>0（空树哈希是常量！）∧ 前台未变 ∧ windowId 未变
    if (th_new[:8] == h_hash and len(ns_new) > 0
            and last.get("fg") and fg_new == last["fg"]
            and win_old in (-1, win_new)):
        if 0 <= h_index < len(ns_new) and ns_new[h_index].get("digest") == h_dig:
            x, y = _c(ns_new[h_index])
            out.update(ok=True, by="r1", x=x, y=y)
            return out

    # R2 身份：digest 精确命中
    hit = [n for n in ns_new if n.get("digest") == h_dig]
    if len(hit) == 1:
        x, y = _c(hit[0])
        out.update(ok=True, by="r2", x=x, y=y)
        return out
    if len(hit) >= 2:
        out["why"] = "digest 命中 %d 个（**不唯一**，拒绝）—— 重新 look 并给更明确的目标" % len(hit)
        out["candidates"] = [{"text": n.get("text"), "viewId": n.get("viewId"), "rect": n.get("rect")}
                             for n in hit[:3]]
        return out

    # R3 锚定：viewId 非空且相等 ∧ norm 相等 ∧ ord 相等（**精确锚用 text，容错锚才用 norm**）
    if orig.get("viewId"):
        anc = [n for n in ns_new
               if n.get("viewId") == orig.get("viewId")
               and n.get("norm") == orig.get("norm")
               and n.get("ord") == orig.get("ord")]
        if len(anc) == 1:
            x, y = _c(anc[0])
            out.update(ok=True, by="r3", x=x, y=y)
            return out
        if len(anc) >= 2:
            out["why"] = "锚定命中 %d 个（不唯一，拒绝）" % len(anc)
            return out

    # R4 宽松：norm 相等 ∧ windowId 未变 ∧ IoU(qrect) >= 0.8
    q0 = _qlist(orig.get("qrect"))
    loose = []
    if q0 and win_old in (-1, win_new):
        for n in ns_new:
            if n.get("norm") != orig.get("norm"):
                continue
            q1 = _qlist(n.get("qrect"))
            if q1 and _iou(q0, q1) >= 0.8:
                loose.append(n)
    if len(loose) == 1:
        x, y = _c(loose[0])
        out.update(ok=True, by="loose", x=x, y=y)
        return out
    out["why"] = "宽松匹配命中 %d 个（要求恰好 1 个）→ 拒绝（stale_handle），请重新 look" % len(loose)
    return out


def cmd_tap(a):
    node = getattr(a, "node", None)
    if node:
        r = resolve_handle(node)
        if not r.get("ok"):
            print("✗ 节点解析失败：%s" % r.get("why"), file=sys.stderr)
            for c in r.get("candidates", []):
                print("   候选：%s" % json.dumps(c, ensure_ascii=False), file=sys.stderr)
            audit({"kind": "tap", "handle": node, "allowed": False,
                   "reason": r.get("why"), "resolved_by": r.get("by")})
            return 2
        a.resolved_by = r.get("by")
        a.node_handle = node
        print("→ 句柄解析 [%s] → (%d,%d)" % (r.get("by"), r["x"], r["y"]), file=sys.stderr)
        return do_action(a, "tap", [(r["x"], r["y"])])
    if a.x is None or a.y is None:
        print("✗ 需要 --x/--y 或 --node 二者之一", file=sys.stderr)
        return 3
    return do_action(a, "tap", [(a.x, a.y)])


def cmd_swipe(a):
    return do_action(a, "swipe", [(a.x1, a.y1), (a.x2, a.y2)])


def cmd_long_press(a):
    node = getattr(a, "node", None)
    if node:
        r = resolve_handle(node)
        if not r.get("ok"):
            print("✗ 节点解析失败：%s" % r.get("why"), file=sys.stderr)
            audit({"kind": "long_press", "handle": node, "allowed": False,
                   "reason": r.get("why"), "resolved_by": r.get("by")})
            return 2
        a.resolved_by = r.get("by")
        print("→ 句柄解析 [%s] → (%d,%d)" % (r.get("by"), r["x"], r["y"]), file=sys.stderr)
        return do_action(a, "long_press", [(r["x"], r["y"]), (r["x"], r["y"])])
    if a.x is None or a.y is None:
        print("✗ 需要 --x/--y 或 --node 二者之一", file=sys.stderr)
        return 3
    return do_action(a, "long_press", [(a.x, a.y), (a.x, a.y)])


def cmd_drag(a):
    node = getattr(a, "node", None)
    if node:
        r = resolve_handle(node)
        if not r.get("ok"):
            print("✗ 节点解析失败：%s" % r.get("why"), file=sys.stderr)
            audit({"kind": "drag", "handle": node, "allowed": False,
                   "reason": r.get("why"), "resolved_by": r.get("by")})
            return 2
        a.resolved_by = r.get("by")
        print("→ 句柄解析 [%s] → (%d,%d)" % (r.get("by"), r["x"], r["y"]), file=sys.stderr)
        return do_action(a, "drag", [(r["x"], r["y"]), (a.x2, a.y2)])
    if a.x1 is None or a.y1 is None or a.x2 is None or a.y2 is None:
        print("✗ 需要 --x1/--y1/--x2/--y2 或 --node(起点)+--x2/--y2(终点)", file=sys.stderr)
        return 3
    return do_action(a, "drag", [(a.x1, a.y1), (a.x2, a.y2)])


def cmd_key(a):
    return do_action(a, "key", [a.code])


TYPE_MAX_CHARS = 500


def _clip_nonce():
    return "t%d_%s" % (int(time.time() * 1000), os.urandom(3).hex())     # App 只收 [0-9A-Za-z_-]{1,64}


def _clip_receipt(nonce, op, deadline, want_len=None):
    """读 App 写的 clip-<nonce>.json（2026-10-21 App 侧新增：只记 op/ok/len，不含原文）。
    回执 ok=false → 立即 False；截止时间绝对、每次读取有硬超时，过截止才读到的不算。"""
    path = LOOK_DIR + "/clip-" + nonce + ".json"
    while True:
        left = deadline - time.time()
        if left <= 0:
            return False
        raw = sh("shell", "cat", path, t=max(0.3, min(3.0, left)))
        if time.time() <= deadline and nonce in raw:
            try:
                rec = json.loads(raw)
            except Exception:
                rec = {}
            if rec.get("nonce") == nonce and rec.get("op") == op:
                return bool(rec.get("ok")) and (want_len is None or rec.get("len") == want_len)
        time.sleep(0.1)


def _dispatch_type(display_id, a):
    """输入文字（2026-10-21 R4）：App 写剪贴板（带回执）→ 可选全选删除 → KEYCODE_PASTE(279) → 清空剪贴板（带回执）。

    实测虚拟屏可用：微信搜索框获焦（mServedView=ActionBarEditText、IME 在该屏）时粘贴生效。
    - 剪贴板 #写:<nonce>:<base64>（挂在老"剪贴板 "前缀下，旧版 App 只会当文字写进剪贴板、不会当任务跑）；
      base64 无空格，不受 App 侧 cmd.trim() 和 adb/shell 二次切分影响；
      App 写回执 clip-<nonce>.json，只记成败与字数（不再把原文写进 logcat）。
    - 没拿到 ok 回执就不发任何按键：否则 --clear 会删掉原内容、PASTE 会贴进用户剪贴板里的旧东西。
    - 绝不按回车/发送：换行在 cmd_type 已拒绝，提交必须是单独一次、过闸门的点击。
    - 已知限制（owner 2026-10-21 接受）：会覆盖用户原剪贴板（后台读不到，无法恢复），与用户同时复制有约 1s 竞态。
    """
    text = getattr(a, "text", "") or ""
    # 一律显式 -d：不带时按键跟随系统焦点显示器（建虚拟屏后焦点可能在虚拟屏），主屏也要钉 -d 0
    pre = ["input", "-d", str(int(display_id or 0))]
    cb = ("am broadcast -n com.dsb.assistant/.CmdReceiver -a com.dsb.assistant.CMD "
          "--es token %s --es cmd %s")
    b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
    utf16_len = len(text.encode("utf-16-le")) // 2          # 对齐 App 侧 Java String.length()
    cleared, attempted = False, False
    try:
        nonce = _clip_nonce()
        attempted = True      # 从这里起剪贴板可能已被改写，finally 需要清空
        sh("shell", cb % (LOOK_TOKEN, shlex.quote("剪贴板 #写:%s:%s" % (nonce, b64))), checked=True)
        if not _clip_receipt(nonce, "write", time.time() + 3.0, utf16_len):
            raise ActionDispatchError("clipboard_write_unconfirmed", "not_submitted")
        if getattr(a, "clear", False):
            sh("shell", *pre, "keycombination", "113", "29", checked=True)   # CTRL_LEFT + A
            sh("shell", *pre, "keyevent", "67", checked=True)                # DEL
        sh("shell", *pre, "keyevent", "279", checked=True)                   # PASTE
    finally:
        if attempted:
            try:   # clearPrimaryClip（App 侧），以回执为准
                n2 = _clip_nonce()
                sh("shell", cb % (LOOK_TOKEN, shlex.quote("剪贴板 #清:" + n2)), checked=True)
                cleared = _clip_receipt(n2, "clear", time.time() + 3.0)
            except Exception:
                cleared = False
        _GATE_EVIDENCE["clipboard_cleared"] = cleared if attempted else None
    return {"channel": "clipboard-paste-d", "status": "completed", "clipboard_cleared": cleared}


def _type_readback(after_path, text, before_texts=()):
    """动作后截图 OCR：**动作前没有的**文字行里，整段出现或可见片段是输入文字的尾部/头部 → seen。
    只是提示，不说明输入一定正确（goal_verified 仍 False）。"""
    want = "".join(text.split())
    before = {"".join((t or "").split()) for t in before_texts}
    if not after_path or not os.path.isfile(after_path) or not OCR_OK or not want:
        return {"status": "unknown", "matches": 0}
    try:
        res = pixel_recognize_result(after_path, float(cfg_get("ocr.min_confidence", 0.5)), timeout_s=15)
    except Exception:
        return {"status": "unknown", "matches": 0}
    if (res or {}).get("status") not in ("ok", "empty"):
        return {"status": "unknown", "matches": 0}
    def _common(a_, b_, rev):
        a_, b_ = (a_[::-1], b_[::-1]) if rev else (a_, b_)
        n_ = 0
        while n_ < min(len(a_), len(b_)) and a_[n_] == b_[n_]:
            n_ += 1
        return n_
    need = min(2, len(want))
    n = 0
    for row in res.get("rows") or []:
        got = "".join((row.get("text") or "").split())
        if not got or got in before:      # 页面原有文字（如"搜索"标签）不算输入的证据
            continue
        # 输入框常只露尾部，且框边会裁掉半个字、前面混进图标（实测"测试 一下"读成"Q式一下"）：
        # 按与输入文字的共同后缀/前缀 ≥2 字判断
        if want in got or _common(got, want, True) >= need or _common(got, want, False) >= need:
            n += 1
    return {"status": "seen" if n else "not_seen", "matches": n}


def cmd_type(a):
    text = getattr(a, "text", "") or ""
    if not text.strip():
        print("✗ --text 为空", file=sys.stderr); return 2
    if "\n" in text or "\r" in text:
        print("✗ 文字里有换行：输入与发送必须分开，换行/回车不在 type 里做", file=sys.stderr); return 2
    if len(text) > TYPE_MAX_CHARS:
        print("✗ 文字超过 %d 字" % TYPE_MAX_CHARS, file=sys.stderr); return 2
    return do_action(a, "type", [279])


def cmd_zones(a):
    st = load_state()
    last = st.get("last_shot")
    if not last:
        print("没有有效截图凭据", file=sys.stderr); return 3
    print(f"凭据 {last['id']}  display={last.get('display_id',0)}  Activity={last['fg']}  "
          f"节点={last['node_count']}  禁区={len(last.get('zones',[]))}")
    for z in last.get("zones", []):
        print(f"  ·「{z.get('word')}」{z.get('text','')} @({z.get('cx')},{z.get('cy')}) rect={z.get('rect')}")
    return 0


def cmd_audit(a):
    if not os.path.exists(AUDIT):
        print("暂无记录"); return 0
    rows = [json.loads(x) for x in open(AUDIT, encoding="utf-8") if x.strip()]
    for r in rows[-a.n:]:
        mark = "✓" if r.get("allowed") else "✗"
        print(f"{mark} {r.get('ts_local')} {r.get('kind')} {r.get('points')} "
              f"expect={r.get('expect')!r} {r.get('act_before','').split('/')[-1]}"
              f"→{r.get('act_after','').split('/')[-1]}"
              + ("" if r.get("allowed") else f"  拒绝理由: {r.get('reason')}"))
    return 0


def cmd_scroll(a):
    """内容方向滚动：motionevent 按住→分步移动→停顿→抬手（防甩飞）。
    --dy 为手指位移：负=指尖向上=露出下方内容。G1/G2/G3 闸门照常。"""
    return do_action(a, "scroll", [(a.x, a.y), (a.x, a.y + a.dy)])


def cmd_wait(a):
    """等待原语（只读）：--text 等文本出现；--stable 等屏幕稳定；--app 等 App 到前台。"""
    t0 = time.time()
    if getattr(a, "app", None):
        if a.text or a.stable:
            print("✗ --app 与 --text/--stable 互斥", file=sys.stderr)
            return 3
        frags = list(_APP_IDS.get(a.app, [a.app])) + [a.app.lower()]
        deadline = time.time() + a.timeout
        fg = ""
        while True:
            fg = foreground()
            if fg and any(f and f.lower() in fg.lower() for f in frags if f):
                res = {"app": a.app, "foreground": fg, "waited_s": round(time.time() - t0, 2)}
                print(json.dumps(res, ensure_ascii=False))
                audit({"kind": "read", "op": "wait.app", "allowed": True, **res})
                return 0
            if time.time() >= deadline:
                res = {"found": False, "app": a.app, "foreground": fg,
                       "waited_s": round(time.time() - t0, 2)}
                print(json.dumps(res, ensure_ascii=False))
                audit({"kind": "read", "op": "wait.app", "allowed": True, **res})
                return 1
            time.sleep(a.interval)
    if a.stable:
        ms, polls, settled = _settle_stable(budget=a.timeout, interval=a.interval)
        print(json.dumps({"stable": settled, "settle_ms": ms, "polls": polls,
                          "waited_s": round(time.time() - t0, 2)}, ensure_ascii=False))
        audit({"kind": "read", "op": "wait.stable", "allowed": True,
               "settled": settled, "settle_ms": ms, "polls": polls})
        return 0 if settled else 1
    if not a.text:
        print(json.dumps({"found": False, "error": "需要 --text 或 --stable"}), file=sys.stderr)
        return 3
    deadline = time.monotonic() + a.timeout
    cancel_check = _ocr_cancel_check()
    while True:
        d = observe(with_image=False)
        if d:
            q = a.text.lower()
            for n in (d.get("nodes") or []):
                t = (n.get("text") or n.get("desc") or "").strip()
                if t and q in t.lower():
                    r = n.get("rect") or [0, 0, 0, 0]
                    print(json.dumps({"found": True, "text": t[:60],
                                      "x": (int(r[0]) + int(r[2])) // 2,
                                      "y": (int(r[1]) + int(r[3])) // 2,
                                      "waited_s": round(time.time() - t0, 2)},
                                     ensure_ascii=False))
                    audit({"kind": "read", "op": "wait.text", "allowed": True,
                           "found": True, "text": t[:60],
                           "waited_s": round(time.time() - t0, 2)})
                    return 0
        # 空树页兜底（P0-1）：树空 + OCR 可用 → 对最新截图跑像素 OCR 再找（OCR 较贵，间隔放大）
        if cfg_get("ocr.enabled", True) and not (d.get("nodes") if d else []):
            p = os.path.join(SHOTS, "wait-%d.jpg" % int(time.time() * 1000))
            p = grab(p, force=True) or p
            try:
                result = (pixel_recognize_result(p, float(cfg_get("ocr.min_confidence", 0.5)),
                          timeout_s=max(0, deadline - time.monotonic()), cancel_check=cancel_check)
                          if OCR_OK else {"status": "unavailable", "rows": []})
            except Exception:
                result = {"status": "error", "rows": []}
            if result["status"] not in ("ok", "empty"):
                return _observation_failure("ocr_failed", result)
            for row in result["rows"]:
                t = row.get("text", "")
                if t and a.text.lower() in t.lower():
                    print(json.dumps({"found": True, "text": t[:60], "x": row["x"], "y": row["y"],
                                      "waited_s": round(time.time() - t0, 2), "source": "pixels"},
                                     ensure_ascii=False))
                    audit({"kind": "read", "op": "wait.text.ocr", "allowed": True,
                           "found": True, "text": t[:60], "source": "pixels"})
                    return 0
        if time.monotonic() >= deadline:
            print(json.dumps({"found": False, "waited_s": round(time.time() - t0, 2)},
                             ensure_ascii=False))
            audit({"kind": "read", "op": "wait.text", "allowed": True,
                   "found": False, "text": a.text, "waited_s": round(time.time() - t0, 2)})
            return 1
        time.sleep(a.interval)


_APP_IDS = {
    "微信": ["com.tencent.mm"], "wechat": ["com.tencent.mm"],
    "京东": ["com.jingdong.app.mall"], "jd": ["com.jingdong.app.mall"],
    "美团": ["com.sankuai.meituan"], "meituan": ["com.sankuai.meituan"],
    "点评": ["com.dianping.v1"], "大众点评": ["com.dianping.v1"],
    "拼多多": ["com.xunmeng.pinduoduo"], "pdd": ["com.xunmeng.pinduoduo"],
    "设置": ["com.android.settings"], "settings": ["com.android.settings"],
    "高德": ["com.autonavi.minimap"], "百度地图": ["com.baidu.BaiduMap"],
    "滴滴": ["com.sdu.didi.psnger"], "淘宝": ["com.taobao.taobao"],
}


def cmd_open(a):
    """Resolve one launcher, submit once, and verify the foreground package."""
    pkg = comp = None
    submitted = False

    def finish(rc, status, reason, **extra):
        record = {"kind": "open", "ok": rc == 0, "status": status, "reason": reason,
                  "pkg": pkg, "component": comp, "launch_verified": rc == 0,
                  "goal_verified": False, "retryable": False,
                  "dispatch_status": "submitted" if submitted else "not_submitted", **extra}
        try:
            audit(record)
        except Exception:
            rc = 3
            record.update(ok=False, status="unknown", reason="audit_failed", launch_verified=False)
        print(json.dumps(record, ensure_ascii=False))
        return rc

    cancel_check = _ocr_cancel_check()
    with lock():
        try:
            if cancel_check():
                return finish(130, "cancelled", "cancelled")
            name = a.app.strip()
            if not name:
                return finish(2, "refused", "empty_app")
            out = sh("shell", "pm", "list", "packages", t=30, checked=True)
            pkgs = sorted(set(line[8:].strip() for line in out.splitlines()
                              if re.fullmatch(r"package:[A-Za-z][A-Za-z0-9_.]*", line)))
            want = re.sub(r"[^a-z0-9一-鿿]+", "", name.lower())
            hits = [name] if name in pkgs else sorted(set(
                p for p in (_APP_IDS.get(name) or []) + (_APP_IDS.get(want) or []) if p in pkgs))
            if not hits:
                hits = [p for p in pkgs if want and want in p.lower().replace(".", "")]
            if len(hits) != 1:
                return finish(2, "refused", "ambiguous_app" if hits else "app_not_found")
            pkg = hits[0]
            # 2026-10-20：`open --vd` = 一步在**虚拟屏**上打开（推荐入口，让正确的路最好走）
            if getattr(a, "vd", False):
                import subprocess as _sp
                _cmd = [sys.executable, os.path.join(HERE, "vd_session.py"), "start",
                        "--package", pkg, "--purpose", (a.why or a.app or "safe_tap open --vd")]
                try:
                    _out = _sp.run(_cmd, capture_output=True, text=True, timeout=240)
                except Exception as exc:
                    return finish(3, "failed", "vd_start_error:%s" % type(exc).__name__)
                if _out.returncode != 0:
                    return finish(3, "failed", "vd_start_failed",
                                  vd_rc=_out.returncode,
                                  stderr=(_out.stderr or "")[-300:],
                                  stdout=(_out.stdout or "")[-300:])
                try:
                    _d = json.loads(_out.stdout[_out.stdout.find("{"):])
                except Exception:
                    return finish(3, "unknown", "vd_start_unparsable",
                                  stdout=(_out.stdout or "")[-300:])
                _did = ((_d.get("display") or {}).get("id"))
                return finish(0, "completed", "vd_started", display_id=_did,
                              vd=((_d.get("session") or {}).get("sid")),
                              fg=(_d.get("foregroundOnDisplay") or "")[:80],
                              hint="接着 `look --display %s`" % _did)
            # 2026-10-20 虚拟屏策略：在主屏上「打开别的 App」= 抢占用户前台，默认禁止
            _open_blk = vd_policy_block("open", 0, pkg,
                                        allow_main=bool(getattr(a, "allow_main", False)))
            if _open_blk:
                return finish(2, "refused", "vd_required:" + _open_blk[1],
                              vd_policy=_open_blk[1],
                              hint="改用 vd_session.py start --package %s --purpose '…'" % pkg)
            resolved = sh("shell", "cmd", "package", "resolve-activity", "--brief",
                          "-c", "android.intent.category.LAUNCHER", pkg, t=30, checked=True)
            components = [line.strip() for line in resolved.splitlines()
                          if re.fullmatch(re.escape(pkg) + r"/[A-Za-z_.$][A-Za-z0-9_.$]*", line.strip())]
            if len(components) != 1:
                return finish(3, "failed", "invalid_launcher")
            comp = components[0]
            if cancel_check():
                return finish(130, "cancelled", "cancelled")
            audit({"kind": "open_attempt", "pkg": pkg, "component": comp, "goal_verified": False})
            submitted = True
            sh("shell", "am", "start", "-W", "-n", comp, t=60, checked=True)
            fg = ""
            for _ in range(6):
                if cancel_check():
                    return finish(130, "cancelled", "cancelled_after_submission")
                fg = foreground()
                if fg.partition("/")[0] == pkg:
                    return finish(0, "completed", "foreground_matched", foreground=fg)
                time.sleep(0.25)
            return finish(3, "unknown", "foreground_unverified", foreground=fg)
        except ActionDispatchError as exc:
            return finish(3, exc.status, exc.reason, returncode=exc.returncode,
                          dispatch_status=exc.status if submitted else "not_submitted")
        except Exception as exc:
            return finish(3, "unknown" if submitted else "failed", type(exc).__name__)


def cmd_doctor(a):
    """体检：adb/授权/亮屏锁屏/无障碍服务/观测通道——每项附下一步该做什么。"""
    rows = []
    def add(name, ok, msg=""):
        rows.append((name, bool(ok), str(msg)[:100]))
    transport, detail = transport_check()
    if transport == "warning":
        print("⚠ %s" % detail)
    else:
        add("ADB 私有传输", transport == "ok", detail)
        if transport == "fail":
            print("✗ ADB 私有传输  %s" % detail)
            print("DOCTOR_FAIL")
            return 2
    out = sh("devices", t=20)
    line = next((l for l in out.splitlines()
                 if l.strip().endswith("\tdevice") or l.strip().endswith(" device")), "")
    add("adb", True, "adb 可用")
    add("设备已连接", bool(line.strip()),
        line.strip()[:60] or "无设备（插 USB 或无线配对）")
    fg = foreground()
    add("前台可读", bool(fg), fg or "dumpsys 无 topResumedActivity")
    pw = sh("shell", "dumpsys", "power | grep -m1 mWakefulness=")
    add("亮屏", "Awake" in pw, "灭屏 → input keyevent 224" if "Awake" not in pw else "")
    kg = sh("shell", "dumpsys", "window | grep -m1 isKeyguardShowing")
    locked = "isKeyguardShowing=true" in kg
    add("未锁屏", not locked, "锁着 → 请解锁（绝不代输 PIN）" if locked else "")
    acc = sh("shell", "dumpsys accessibility | grep -E 'Bound services|Crashed' -A1")
    a11y_ok = ("逗沙包" in acc or "大烧饼" in acc) and ("Crashed services:{}" in acc)   # 2026-10-21 改名，旧名兼容
    add("无障碍服务绑定", a11y_ok,
        "" if a11y_ok else "未绑定 → 请在系统无障碍设置里开启逗沙包")
    d = observe(with_image=False, timeout=8)
    add("观测通道", d is not None,
        "nodes=%s" % len((d or {}).get("nodes") or []) if d else "App 观测超时（OverlayService 在跑吗）")
    ok_all = all(r[1] for r in rows)
    for name, ok, msg in rows:
        print("%s %s  %s" % ("✓" if ok else "✗", name, msg))
    print("DOCTOR_%s" % ("PASS" if ok_all else "FAIL"))
    return 0 if ok_all else 2


def cmd_capabilities(a):
    """当前页面能力协商（借鉴 phone-harness supports(op)）：先问再做，避免白跑。"""
    d = observe(with_image=False, timeout=8)
    nodes = (d.get("nodes") or []) if d else []
    last = load_state().get("last_shot") or {}
    caps = {
        "activity": (d.get("fg") if d else "") or last.get("fg_app") or last.get("fg") or "",
        "tree": {"available": len(nodes) > 0, "nodes": len(nodes)},
        "pixel_ocr": {"available": bool(OCR_OK),
                      "engine": "container(ocr.py)",
                      "note": "空树页对最新截图跑容器侧 OCR（skills/ocr.py）"},
        "danger_zone_coverage": last.get("zone_coverage") or "",
        "scroll": {"supported": "unknown",
                   "note": "需试探：scroll 一步看 ui_reacted；部分页面会吞手势"},
        "input": {"tap": True, "swipe": True, "key": True,
                  "text_ascii": "input text", "text_cjk": "剪贴板+KEYCODE_PASTE(279)"},
        "gates": "armed (G1-G5)",
        "hints": last.get("hints") or {},
    }
    print(json.dumps(caps, ensure_ascii=False, indent=1))
    return 0


def _img_size(path):
    """读截图尺寸（PNG IHDR / JPEG SOF），给 coord 的 --img-w/h 缺省用。"""
    if not path or not os.path.isfile(path):
        return None
    with open(path, "rb") as f:
        head = f.read(262144)
    if head[:8] == b"\x89PNG\r\n\x1a\n" and len(head) >= 24:
        w, h = struct.unpack(">II", head[16:24])
        return int(w), int(h)
    i = 2
    while i < len(head) - 9:
        if head[i] != 0xFF:
            i += 1
            continue
        m = head[i + 1]
        if m in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            hh, ww = struct.unpack(">HH", head[i + 5:i + 9])
            return int(ww), int(hh)
        if m in (0xD8, 0x01) or 0xD0 <= m <= 0xD7:
            i += 2
            continue
        i += 2 + struct.unpack(">H", head[i + 2:i + 4])[0]
    return None


def cmd_coord(a):
    """图像坐标 → 设备坐标（借鉴 phone-harness 的 image_point；Android 无窗口偏移版）。
    复用 G2/G3 判定报告安全性（zone_hit/edge_hit）；只读不改状态。人类提示走 stderr。"""
    w, h = screen_size()
    iw, ih = a.img_w, a.img_h
    if iw is None or ih is None:
        last = load_state().get("last_shot") or {}
        got = _img_size(last.get("path"))
        if not got:
            print("✗ 未给 --img-w/--img-h，且最近 look 的截图尺寸读不到 —— 请显式给", file=sys.stderr)
            return 3
        iw, ih = got
    if iw <= 0 or ih <= 0:
        print("✗ --img-w/--img-h 必须为正", file=sys.stderr)
        return 3
    dx, dy = round(a.img_x * w / iw), round(a.img_y * h / ih)
    out = {"device": {"x": dx, "y": dy}, "screen": "%dx%d" % (w, h),
           "scale": round(w / iw, 4), "img": {"w": iw, "h": ih}}
    if not (0 <= dx <= w and 0 <= dy <= h):
        out["ok"] = False
        out["error"] = ("换算后 (%d,%d) 超出屏幕 %dx%d —— 检查 --img-w/h 是否与截图实际尺寸一致"
                        % (dx, dy, w, h))
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return 2
    ins = gesture_insets(w, h)
    edge_l = edge_r = EDGE_X
    edge_b = max(EDGE_BOTTOM, ins["bottom"])
    edge_hit = (dx < edge_l or dx > w - edge_r or dy < EDGE_TOP or dy > h - edge_b)
    zones = (load_state().get("last_shot") or {}).get("zones") or []
    hits = [z for z in zones if in_zone(dx, dy, z)]
    out.update({"ok": True, "zone_hit": bool(hits),
                "zones": [{"word": z.get("word"), "cx": z.get("cx"), "cy": z.get("cy")}
                          for z in hits],
                "edge_hit": edge_hit,
                "edge": {"left": edge_l, "right": edge_r, "top": EDGE_TOP, "bottom": edge_b}})
    if a.note:
        out["note"] = a.note
    if edge_hit:
        print("⚠ 该点落在边缘禁带（左<%d / 右<%d 起，顶<%d / 底<%d 起）——会被系统手势截获"
              % (edge_l, w - edge_r, EDGE_TOP, h - edge_b), file=sys.stderr)
    if hits:
        print("⚠ 该点落在危险禁区：%s" % "、".join(str(z.get("word")) for z in hits), file=sys.stderr)
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0


def cmd_inbox(a):
    """统一收件箱（Tier-2）：盯梢结果的汇报入口。"""
    from inbox import items, mark_read
    if a.mark_read:
        n = mark_read(a.mark_read)
        print("已标记 %d 条已读" % n)
        return 0
    if a.drop_test is not None:
        from inbox import drop_test
        kw = a.drop_test or "测试"
        n = drop_test(kw)
        print(json.dumps({"dropped": n, "keyword": kw,
                          "note": "删除含关键词的条目（验收/清理用；指纹保留不会重入箱）"},
                         ensure_ascii=False))
        return 0
    rows = items(all=a.all)
    if a.json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        return 0
    if not rows:
        print("收件箱：无未读")
        return 0
    for r in rows:
        print("* [%s] %s | %s — %s (%s)"
              % (r.get("priority"), r.get("kind"), r.get("title"),
                 r.get("summary"), r.get("ts_local")))
    print("共 %d 条；--mark-read all 全部已读" % len(rows))
    return 0


def _save_watch_subs(subs):
    """写后回读校验（复核遗留④）：写不进去要报错，不许静默。"""
    SUBS = os.path.join(HERE, "watch_subs.json")
    with open(SUBS, "w", encoding="utf-8") as f:
        json.dump(subs, f, ensure_ascii=False, indent=1)
    back = json.load(open(SUBS, encoding="utf-8"))
    if len(back) != len(subs):
        raise RuntimeError("watch 订阅写后回读不一致（%d→%d）" % (len(subs), len(back)))


def cmd_watch(a):
    """盯梢订阅管理（Tier-2）：watcher.py 按此调度。"""
    try:
        subs = json.load(open(os.path.join(HERE, "watch_subs.json"), encoding="utf-8"))
    except Exception:
        subs = []
    if a.add:
        subs = [s for s in subs if s.get("id") != a.add]
        subs.append({"id": a.add, "every": a.every or "30m",
                     "hours": a.hours or "", "last_run": 0})
        _save_watch_subs(subs)
        print(json.dumps({"added": a.add, "every": a.every or "30m",
                          "hours": a.hours or ""}, ensure_ascii=False))
        return 0
    if a.remove:
        subs = [s for s in subs if s.get("id") != a.remove]
        _save_watch_subs(subs)
        print(json.dumps({"removed": a.remove}, ensure_ascii=False))
        return 0
    if not subs:
        print("无订阅；watch --add wechat --every 30m --hours 7-23")
        return 0
    for s in subs:
        last = time.strftime("%m-%d %H:%M", time.localtime(s["last_run"])) if s.get("last_run") else "未跑过"
        print("  %-10s 每 %-6s 时段 %-6s 上次: %s%s"
              % (s.get("id"), s.get("every", "30m"), s.get("hours", "-") or "-",
                 last, (" rc=%s" % s.get("last_rc")) if s.get("last_rc") is not None else ""))
    return 0


def cmd_recover(a):
    """恢复处置 CLI（Tier-1②）：实现在 skills/recovery.py，此处只做入口。"""
    try:
        from recovery import recover, detect_situation, RecoveryFailure
    except Exception as e:
        print("✗ recovery 模块不可用: %s" % e, file=sys.stderr)
        return 3
    try:
        if a.situation == "detect":
            sit, info = detect_situation(expected_app=a.app or "")
            print(json.dumps({"situation": sit, **info}, ensure_ascii=False, indent=1))
            return 3 if sit == "unknown" else 0
        r = recover(a.situation, {"app": a.app or "",
                                   "expected_activity": getattr(a, "expect_activity", None)})
    except RecoveryFailure as e:
        print(json.dumps({"ok": False, "rc": e.rc, "reason": str(e),
                          "goal_verified": False}, ensure_ascii=False))
        return e.rc
    except (TypeError, ValueError) as e:
        print("✗ 参数非法: %s" % e, file=sys.stderr)
        return 2
    print(json.dumps(r, ensure_ascii=False, indent=1))
    return r.get("rc", 3)


def cmd_config(a):
    """config list / get KEY / set KEY VALUE —— 调参不改代码（VALUE 按 JSON 解析）。"""
    if a.op == "list":
        ov = {}
        try:
            ov = json.load(open(CONFIG_FILE, encoding="utf-8"))
        except Exception:
            pass
        print(json.dumps({"defaults": CONFIG_DEFAULTS, "overrides": ov,
                          "hint": "set 后写 safe_tap_config.json；env SAFE_TAP_<KEY> 单次覆盖"},
                         ensure_ascii=False, indent=1))
        return 0
    if a.op == "get":
        print(json.dumps({a.key: cfg_get(a.key)}, ensure_ascii=False))
        return 0
    if a.op == "set":
        try:
            val = json.loads(a.value)
        except Exception:
            val = a.value
        ov = {}
        try:
            ov = json.load(open(CONFIG_FILE, encoding="utf-8"))
        except Exception:
            pass
        ov[a.key] = val
        json.dump(ov, open(CONFIG_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print(json.dumps({"ok": True, a.key: cfg_get(a.key)}, ensure_ascii=False))
        return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="点击闸门：所有手机点击必须走这里")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("look", help="截图并获取凭据（点击前必须先做）")
    p.add_argument("--note", default="")
    p.add_argument("--ocr", action="store_true", help="即使有部分控件文字，也对新鲜截图补充 OCR")
    p.add_argument("--target", default=None, help="只读找文字（忽略空白及大小写）：树未命中补一次 OCR，不自动点击")
    p.add_argument("--display", type=int, default=0,
                   help="目标逻辑显示器 ID（0=主屏；>0=虚拟屏，须与 vd_session 会话一致）")
    p.set_defaults(fn=cmd_look)

    for name, fn in (("tap", cmd_tap), ("swipe", cmd_swipe), ("key", cmd_key), ("type", cmd_type)):
        q = sub.add_parser(name, help=f"{name}（需要 --shot 凭据）")
        q.add_argument("--shot", required=True, help="look 返回的 shot_id")
        if name == "tap":
            q.add_argument("--x", type=int)
            q.add_argument("--y", type=int)
            q.add_argument("--node", default=None,
                           help="节点句柄 h1.<treeHash8>.<index>.<digest8>（给了它就不用 --x/--y）")
            q.add_argument("--fast", action="store_true",
                           help="浏览模式：0.5s 稳定、跳过前后 JPEG 留证（信号改由凭据/观测提供，不含像素维度）")
            q.add_argument("--expect", default="", help="你以为点的是什么（用于留证）")
        elif name == "swipe":
            for k in ("x1", "y1", "x2", "y2"):
                q.add_argument(f"--{k}", type=int, required=True)
        elif name == "type":
            q.add_argument("--text", required=True,
                           help="要输入的文字（支持中文；不得含换行——发送必须另行点击）。输入框需已获焦")
            q.add_argument("--clear", action="store_true", help="先全选删除输入框原有内容")
            q.add_argument("--expect", default="", help="你以为在往哪个输入框里写（留证）")
            q.add_argument("--fast", action="store_true", help="跳过前后 JPEG 留证（同时没有读回）")
        else:
            q.add_argument("--code", type=int, required=True, help="keyevent 码，如 4=返回")
        q.add_argument("--expect-activity", default=None)
        q.add_argument("--display", type=int, default=0,
                       help="目标逻辑显示器 ID（0=主屏；>0=虚拟屏，须与该屏凭据一致）")
        q.add_argument("--ack-no-zones", dest="ack_no_zones", action="store_true",
                       help="已废弃的调用方自述，不授予权限；覆盖不足时请人工接管")
        q.set_defaults(fn=fn)

    q = sub.add_parser("zones", help="看当前禁区"); q.set_defaults(fn=cmd_zones)

    q = sub.add_parser("capabilities", help="能力协商：当前页支持什么（树/像素/危险区/滚动）")
    q.set_defaults(fn=cmd_capabilities)

    q = sub.add_parser("config", help="运行时配置：list / get KEY / set KEY VALUE")
    q.add_argument("op", choices=["list", "get", "set"])
    q.add_argument("key", nargs="?", default=None)
    q.add_argument("value", nargs="?", default=None)
    q.set_defaults(fn=cmd_config)

    q = sub.add_parser("scroll", help="非甩飞滚动：--x/--y 锚点 + --dy 手指位移（负=向上滑，露出下方内容）")
    q.add_argument("--shot", required=True, help="look 返回的 shot_id")
    q.add_argument("--x", type=int, required=True)
    q.add_argument("--y", type=int, required=True)
    q.add_argument("--dy", type=int, default=-420)
    q.add_argument("--steps", type=int, default=8)
    q.add_argument("--expect", default="", help="你期望滚动后看到什么（留证用）")
    q.add_argument("--expect-activity", default=None)
    q.add_argument("--ack-no-zones", dest="ack_no_zones", action="store_true",
                   help="已废弃的调用方自述，不授予权限；覆盖不足时请人工接管")
    q.add_argument("--display", type=int, default=0)
    q.set_defaults(fn=cmd_scroll)

    q = sub.add_parser("wait", help="等待原语（只读）：--text X 等文本出现 / --stable 等屏幕稳定")
    q.add_argument("--text", default=None)
    q.add_argument("--stable", action="store_true")
    q.add_argument("--app", default=None, help="等某 App 到前台（包名或名字，复用 open 映射；与 --text/--stable 互斥）")
    q.add_argument("--timeout", type=float, default=10.0)
    q.add_argument("--interval", type=float, default=0.5)
    q.set_defaults(fn=cmd_wait)

    q = sub.add_parser("inbox", help="统一收件箱（Tier-2 汇报入口）")
    q.add_argument("--all", action="store_true")
    q.add_argument("--mark-read", default=None, help="<id|all>")
    q.add_argument("--drop-test", nargs="?", const="测试", default=None,
                   help="删除含关键词的条目（验收/清理用；默认'测试'）")
    q.add_argument("--json", action="store_true")
    q.set_defaults(fn=cmd_inbox)

    q = sub.add_parser("watch", help="盯梢订阅管理（watcher.py 按此调度）")
    q.add_argument("--list", action="store_true")
    q.add_argument("--add", default=None, help="订阅 id：wechat|express")
    q.add_argument("--every", default="30m", help="间隔（30m/1h…，最小 10m）")
    q.add_argument("--hours", default="", help="限定时段（如 7-23）")
    q.add_argument("--remove", default=None)
    q.set_defaults(fn=cmd_watch)

    q = sub.add_parser("long_press", help="长按（同点按下保持 --ms，默认 800ms）")
    q.add_argument("--shot", required=True, help="look 返回的 shot_id")
    q.add_argument("--x", type=int)
    q.add_argument("--y", type=int)
    q.add_argument("--node", default=None, help="节点句柄（给了它就不用 --x/--y）")
    q.add_argument("--ms", type=int, default=800, help="按住时长（毫秒）")
    q.add_argument("--expect", default=None)
    q.add_argument("--expect-activity", default=None)
    q.add_argument("--ack-no-zones", action="store_true",
                   help="已废弃的调用方自述，不授予权限；覆盖不足时请人工接管")
    q.add_argument("--fast", action="store_true")
    q.add_argument("--display", type=int, default=0)
    q.set_defaults(fn=cmd_long_press)

    q = sub.add_parser("drag", help="按住拖拽（分步慢速移动到终点，与 swipe 的快速划过不同）")
    q.add_argument("--shot", required=True, help="look 返回的 shot_id")
    q.add_argument("--x1", type=int)
    q.add_argument("--y1", type=int)
    q.add_argument("--x2", type=int)
    q.add_argument("--y2", type=int)
    q.add_argument("--node", default=None, help="起点节点句柄（终点仍需 --x2/--y2）")
    q.add_argument("--ms", type=int, default=600, help="总时长（毫秒）")
    q.add_argument("--steps", type=int, default=10)
    q.add_argument("--expect", default=None)
    q.add_argument("--expect-activity", default=None)
    q.add_argument("--ack-no-zones", action="store_true",
                   help="已废弃的调用方自述，不授予权限；覆盖不足时请人工接管")
    q.add_argument("--fast", action="store_true")
    q.add_argument("--display", type=int, default=0)
    q.set_defaults(fn=cmd_drag)

    q = sub.add_parser("recover", help="失败恢复处置（Tier-1②）：--situation detect|dialog|loading|page_drift|empty_tree|target_missing|app_dead|login_required")
    q.add_argument("--situation", required=True)
    q.add_argument("--app", default=None, help="目标包名（app_dead 重启用）")
    q.add_argument("--expect-activity", default=None, help="page_drift恢复后必须精确匹配的Activity")
    q.set_defaults(fn=cmd_recover)

    q = sub.add_parser("coord", help="图像坐标→设备坐标（复用 G2/G3 判定，只读）")
    q.add_argument("--img-x", type=int, required=True)
    q.add_argument("--img-y", type=int, required=True)
    q.add_argument("--img-w", type=int, default=None)
    q.add_argument("--img-h", type=int, default=None)
    q.add_argument("--note", default=None)
    q.set_defaults(fn=cmd_coord)

    q = sub.add_parser("open", help="按名字打开 App（内置映射 + 包名片段匹配）")
    q.add_argument("--app", required=True)
    q.add_argument("--vd", action="store_true",
                   help="【推荐】直接建虚拟屏并在其上打开（返回 display_id，之后 all look/tap --display N）")
    q.set_defaults(fn=cmd_open)

    q = sub.add_parser("doctor", help="体检：adb/授权/亮屏锁屏/无障碍服务/观测通道")
    q.set_defaults(fn=cmd_doctor)

    def _c_orange(a):
        _ball("橙"); print("🟠 球→橙（含分析期间；20 秒无心跳自动回蓝）"); return 0
    def _c_blue(a):
        _ball("蓝"); print("🔵 球→蓝（交还手机）"); return 0
    q = sub.add_parser("busy", help="球变橙（开始操作/继续分析，续心跳）"); q.set_defaults(fn=_c_orange)
    q = sub.add_parser("keep", help="同 busy：分析期间续心跳，别让球提前回蓝"); q.set_defaults(fn=_c_orange)
    q = sub.add_parser("done", help="球变蓝（操作结束，交还手机）"); q.set_defaults(fn=_c_blue)
    q = sub.add_parser("audit", help="看操作记录")
    q.add_argument("-n", type=int, default=15)
    q.set_defaults(fn=cmd_audit)

    # 2026-10-20 虚拟屏策略的统一逃生口：所有子命令都接受
    #   --allow-main --why "原因"   → 显式放行主屏动作（进审计）
    for _p in sub.choices.values():
        if "--allow-main" in _p._option_string_actions:
            continue
        _p.add_argument("--allow-main", dest="allow_main", action="store_true",
                        help="【逃生口】允许在主屏操作非控制面 App；必须同时给 --why，会记审计")
        _p.add_argument("--why", default=None,
                        help="配合 --allow-main：为什么必须占用主屏（如 FLAG_SECURE）")
        _p.add_argument("--raw", dest="raw_input", action="store_true",
                        help="用真实注入触摸（input -d）代替无障碍手势；某些系统界面只认真实触摸")

    args = ap.parse_args()
    sys.exit(args.fn(args))
