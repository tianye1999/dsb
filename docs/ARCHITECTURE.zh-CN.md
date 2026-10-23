# 架构

[English](ARCHITECTURE.md) · **简体中文**

## 三层部署

| 层 | 跑在哪 | 目录 | 作用 |
|---|---|---|---|
| **App**（`com.dsb.assistant`，显示名「逗沙包」） | Android | `app/` | 眼和手：无障碍服务（读控件树、派发手势/输入）、悬浮球（橙/蓝）、WebView 里显示 dsh 网页、通知监听、命令入口 `CmdReceiver` |
| **Termux 宿主** | Termux App | `termux/` | sshd、adb 自连、看门狗（`supervisor.sh`）、开机链（`boot.sh`/`shutdown.sh`）、球红绿灯（`adbq.sh`/`ball.sh`）、屏幕常亮守护 |
| **容器** | Termux 里的 proot Ubuntu | `agent/` `bridge/` `dsh-config/` `dsh-web-tweaks/` | 大脑（dsh + DeepSeek）、任务桥接、执行层原语、OCR/视觉 |

## 一个任务的生命周期

1. 你在 App 网页里下任务 → App 带 `task_id`（UUID）调桥接 `/do`。
2. **桥接**（`bridge/bridge.mjs` + `task-ledger.mjs`）：账本检查 `task_id` 幂等、当前无执行中任务、运行时状态已知 → 把原话交给 dsh 运行时（`phone` 配置，挂着危险动作钩子 `agent/check_gate.py`）。
3. **大脑**读 `docs/internal` 里给它的技能说明（发布版见 `agent/SKILL.md`），用 bash 调 `agent/` 里的原语：
   - `safe_tap.py open --app X --vd`：建 scrcpy 虚拟屏并在上面打开 App
   - `safe_tap.py look --display N`：拍一帧 + 控件树 + （空树时）OCR → 发一张**凭据**（shot_id）
   - `act.py tap --target "文字或描述"`：树 → OCR 精确找字 → 没找到且给了区域才视觉定位
   - `safe_tap.py tap/swipe/scroll/key/type --shot <id>`：全部先过闸门再派发
   - `vd_session.py stop`：收屏、删录像
4. 每个动作：闸门 → （R3 派发前复核）→ 经 App 无障碍派发到指定显示器 → 等回执 → 动作后截图 → 审计。
5. 大脑写结论卡 → 桥接回 App；账本记 `reply_received`。「停止」走 `/cancel`，只有确认所有子进程退出才记 `cancelled`。

## 闸门（`agent/safe_tap.py`）

| 闸门 | 管什么 |
|---|---|
| G1 | 凭据 90 秒有效；前台 App 不变；虚拟屏凭据绑定 displayId / uniqueId / 尺寸 / 会话 |
| G2 | 控件树危险词周围禁区（pad 40px） |
| G2b | 空控件树：调用方确认 + OCR 危险词（整行横带）+ 点按视觉判断；滑动类在危险页整页拒绝；按键只放行导航键和粘贴 |
| G3 | 系统手势区（左右边、顶部、底部导航区）禁点 |
| G4 | 同点重复、同前台手势预算，用尽即熔断 |
| G5 | 动作前后截图、审计记录（`safe_tap_audit.jsonl`） |
| R3 | 派发前现拍一帧，与凭据帧比对目标所在行（48×16 灰度均差 > 6 拒绝） |

不能将整条链路描述成“所有判定 fail-closed”：空树页的 OCR 不可用时拒绝；视觉成功返回 `impact≠none`（含 `unclear`）时拒绝，但**视觉服务异常时，若 OCR 未检出危险词、调用方已确认截图且其他闸门条件满足，仍可能仅凭 OCR 放行**，记为 `ocr_clean_ack_vision_unavailable`；已检出危险词则拒绝。依据：[OCR 分支](../agent/safe_tap.py#L1477)、[视觉判断与回退](../agent/safe_tap.py#L1516)。

命令层的 TypeSafe 钩子另有服务不可用时的 fail-open 分支（[代码](../agent/check_gate.py#L162)）。大脑、闸门和凭据在同一个 Termux Linux 用户下运行，闸门不能防住被攻破的大脑改写或绕过它；运行权限见[大脑进程启动](../bridge/bridge.mjs#L122)。

## 虚拟屏（`agent/vd_session.py`）

`scrcpy --new-display=720x1600/320 --no-vd-system-decorations --no-window --no-audio` 建一块只有目标 App 的屏。`verify()` 一次 adb 往返批量核对：屏还在、uniqueId、尺寸、类型、overlay 设置、scrcpy 存活、目标 App 在该屏前台、App 进程 pid；任何一项不符就拒绝。主屏上的第三方 App 动作默认拒绝（`vd_policy`）。

## 空树页（微信、淘宝系、京东 Taro 页）

控件树读不到时：`look` 自动 OCR 生成 `pixel_nodes`；G2b 用 OCR 行建危险横带；点按前把目标附近 ±240px 裁图 + 红圈发给视觉模型问「点这里会发生什么」。成功返回时 `impact≠none` 拒绝；服务异常适用上面的 OCR 回退规则，不等于所有视觉失败都拒绝。依据：[裁剪与请求](../agent/vision_check.py#L36)、[判定与回退](../agent/safe_tap.py#L1516)。

## 数据流与留存

- **裁剪范围是路径级约束**：闸门视觉复核只发目标附近裁剪图，视觉定位只发指定区域裁剪图（[复核](../agent/vision_check.py#L36)、[定位](../agent/vision_check.py#L114)），不是整个大脑只收裁剪图。
- **整屏问答另有完整截图路径**：App 的「问 DSH」将整屏截图、控件树和最近通知交给 `/ask`；API 模式直接把整屏上传到默认的 DeepSeek API；dsh 模式被引导 `read_image` 完整截图。dsh 内部图片传输实现不在本仓库，不能承诺只外发裁剪图。任务和页面文本也可能进入云端模型，手机上运行 dsh 不代表离线推理。依据：[App 输入](../app/src/com/dsb/assistant/OverlayService.java#L2273)、[问答桥接](../bridge/bridge.mjs#L548)、[API 上传](../bridge/bridge.mjs#L144)、[dsh 看图入口](../bridge/bridge.mjs#L170)。
- **可选 TypeSafe 判定**：配置 Key 后，将可疑 shell 命令片段（最多 1500 字符）、工具名称与命中特征送往 TypeSafe 云端服务；不可用时命令钩子 fail-open。依据：[判定输入](../agent/check_gate.py#L151)、[服务与密钥](../agent/jev_client.py#L23)、[不可用分支](../agent/check_gate.py#L162)。
- **截图落盘，审计未统一脱敏**：动作前后截图和问答截图保存在本机；`audit()` 直接序列化记录，`note` / `expect` / 视觉解释可能带入聊天、订单等原文，不能承诺零原文审计。屏幕动作审计也不覆盖大脑的每一条 bash 命令。依据：[动作记录与截图](../agent/safe_tap.py#L1943)、[审计写入](../agent/safe_tap.py#L369)、[问答截图落盘](../bridge/bridge.mjs#L87)。

## 中文输入（`type`）

App 命令 `剪贴板 #写:<nonce>:<base64>` 写入剪贴板 → App 落回执 `files/clip-<nonce>.json`（操作、结果、长度等元数据，不含剪贴板原文）→ 核对回执后才发粘贴键 → 尝试清空并核验回执 → OCR 读回核验。清空失败会记录 `clipboard_cleared=false`，不能写成必然清空；更不能由回执无原文推导出所有审计都已脱敏。依据：[回执核验](../agent/safe_tap.py#L2233)、[粘贴 / 清空](../agent/safe_tap.py#L2252)。

## 控制面

| 通道 | 鉴权 / 方案 | 当前状态与依据 |
|---|---|---|
| `CmdReceiver` 广播（球色、剪贴板、停止等） | 接收权限 `INJECT_EVENTS`（shell/root/系统） | [Manifest 已声明权限](../app/AndroidManifest.xml#L99) |
| adb server | Termux 私有文件套接字、无 TCP 的收口方案（R6-0） | 安装器默认尝试私有传输加固，第一次配对即走私有 bootstrap；维护者设备 2026-10-23 迁到同一布局并通过重启验收。预检跳过或回滚加固时，普通 TCP 5037 作为较弱回退保留并明确警告，此时其他 App 可能取得 shell 身份。以上针对新安装：公开工具不迁移旧版部署，软件包升级已在 arm64 模拟器上验收（真实 `pkg upgrade` 后原版 adb 无法加载，私有 ADB 与 doctor 正常），维护者真机上的软件包升级验收尚未做。见[威胁模型](THREAT-MODEL.zh-CN.md#4-控制权--本地控制面鉴权) |
| App ↔ 桥接任务通道 | Termux `RUN_COMMAND` + 私有套接字（R6-1） | [任务传输代码](../app/src/com/dsb/assistant/DshClient.java#L31)与[私有监听](../bridge/private-listener.mjs#L5)已改，维护者设备 2026-10-22 切换并验收 |

边界见 [THREAT-MODEL.zh-CN.md](THREAT-MODEL.zh-CN.md)。

## 测试

- `agent/`：离线单测（闸门、凭据、并发、虚拟屏绑定、R3、OCR、walk、dsb_run……），全部不碰设备
- `bridge/`：账本、取消、重启审计
- `installer/`、`hardening/adb-private/`：安装器与私有 ADB 传输的离线测试
- `app/tests/`：真机门脚本与说明
- 真机门：改闸门/桥接/App 必须附真机证据（见 [CONTRIBUTING.zh-CN.md](../CONTRIBUTING.zh-CN.md)）
