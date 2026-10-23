# Security Policy / 安全策略

逗沙包的核心卖点是「可信委托」：AI 只做杂务，决定权留给人。所以凡是能让这个承诺落空的问题，我们都当安全问题处理。

## 什么算安全问题（What counts）

- 绕过执行闸门（G1–G5、空树页 OCR + 视觉判断、派发前复核），让 AI 能点到付款、发送、删除、授权这类按钮
- 绕过本地控制面鉴权：手机上其他 App 能给逗沙包下指令、往大脑会话注入消息、冒充桥接服务
- 任务账本失效：同一任务被重复执行，或「停止」被确认了但实际没停
- 隐私泄露：截图、聊天内容、订单内容、验证码、剪贴板原文出现在日志、审计、回执或仓库里
- 威胁模型（`docs/THREAT-MODEL.md`）里声明的边界与实际行为不符

## 怎么报告（How to report）

**请不要公开开 issue。** 使用 GitHub 的私密漏洞报告（仓库页 → Security → Report a vulnerability）。
请附上：复现步骤、影响范围、涉及的设备与 Android 版本。如果你有修复建议或 PoC，一并附上。

这是个人维护的项目：我会尽量在 **7 天内** 确认收到，并在修复发布前与你同步进度；修复后在发布说明中致谢（除非你不希望）。

## 范围外（Out of scope）

- 需要物理接触手机、或已经取得 root / 已授权 ADB 调试 / 已拥有 Termux 执行权限的攻击者（这些主体在威胁模型里被视为与逗沙包同等信任）
- 第三方 App（微信、淘宝等）自身的漏洞
- 大模型输出错误本身（除非它导致闸门被绕过）

## Supported versions

Only the latest release on the `main` branch receives security fixes.

---

*English summary:* Report vulnerabilities privately via GitHub's "Report a vulnerability". In scope: execution-gate bypass, local control-plane auth bypass, task-ledger idempotency/stop failures, privacy leaks, and any mismatch between `docs/THREAT-MODEL.md` and real behaviour. Out of scope: attackers with physical access, root, authorized ADB, or Termux execution rights. Best-effort acknowledgement within 7 days.
