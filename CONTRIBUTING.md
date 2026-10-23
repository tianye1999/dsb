# 贡献指南 / Contributing

感谢你有兴趣参与。逗沙包卷的是「可信」而不是「聪明」，所以这份指南里关于**安全闸门改动**的规矩比一般项目严。

## 开发环境

| 部分 | 需要什么 | 测试怎么跑 |
|---|---|---|
| `agent/`（执行层，Python 3.11+） | 只需 Python；离线测试不碰设备 | `cd agent && for t in test_*.py tests/test_*.py; do python3 $t; done` |
| `bridge/`（任务桥接，Node 20+） | Node | `cd bridge && node --test test-*.mjs` |
| `app/`（Android App，无 Gradle） | JDK 17 + Android build-tools 35 | `cd app && DSB_KEY_PASS=... ./build.sh`（见 `docs/INSTALL.md`） |
| `termux/`、`dsh-config/`、`dsh-web-tweaks/` | Termux + proot Ubuntu（真机） | 真机验证 |

离线测试在 CI 里跑；真机验证没法在 CI 跑，所以见下一节。

## 真机门（Real-device gate）

凡是改到这些路径的 PR，合并前必须有**真机**证据（审计日志片段 / `safe_tap.py doctor` 输出 / 端到端计时），写在 PR 描述里：

- `agent/safe_tap.py`、`agent/vd_session.py`、`agent/act.py`、`agent/vision_check.py`、`agent/ocr.py`
- `bridge/bridge.mjs`、`bridge/task-ledger.mjs`
- `app/src/**`

「离线测试全绿」不等于「能用」——这个项目的历史里，好几次假阳性都是这么来的。

## 改安全闸门的规矩（Changing a safety gate）

闸门是产品的核心，不是可以随手调的参数。新增、放宽、删除任何一道闸门，PR 必须同时满足：

1. **有真实事故或真实需求**：说清楚是哪次任务、哪个页面、为什么现有闸门不够或过严。
2. **有行为测试**：至少一个会在改动前失败、改动后通过的测试，以及一个证明「该拒的仍然拒」的测试。
3. **有独立 review**：作者不能自己批自己；审稿人要专门找「新的绕过路径」。
4. **不用枚举式守卫**：别靠加关键词、加黑名单来「修」问题（这类补丁历史上每次都被下一个案例绕过）。要改就改机制。
5. **默认关闭**：新守卫默认不启用，除非维护者明确批准。

放宽闸门的 PR（让更多动作通过）比收紧的 PR 审得更严，请预留时间。

## 提交与 PR

- 提交信息中文或英文都行，第一行说清「改了什么、为什么」。
- 一个 PR 只做一件事。
- PR 模板里的勾选项不是走过场：没有的证据就写「没有」，不要勾。
- 不要往仓库里提交任何截图、审计日志、会话记录、聊天或订单内容、密钥库、口令。CI 的密钥扫描会拦，但请在本地先 `gitleaks dir .`。

## 行为准则

参见 `CODE_OF_CONDUCT.md`（中文版 `CODE_OF_CONDUCT.zh-CN.md`）。

---

*English summary:* Offline tests run in CI (`agent/`: `python3 test_*.py`; `bridge/`: `node --test`). Changes to the execution gates, the bridge or the app additionally require real-device evidence in the PR. Any change to a safety gate needs a real incident/need, a behaviour test (fail-before/pass-after plus a still-refuses test), an independent review hunting for bypasses, no keyword-list guards, and default-off. Never commit screenshots, audit logs, sessions, chat/order content, keystores or secrets.
