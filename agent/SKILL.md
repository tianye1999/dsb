---
name: dsb-phone
description: 操控这台 Android 手机时必读（触发词：点/滑/截图/打开App/微信/设置/查订单/快递/作业/手机坏了/屏幕没反应）。一切屏幕动作必须走 safe_tap 闸门；中文输入四步法、坐标换算、弹窗只检测不代点、危险红线都在这条技能里。
---

# DSB 手机操控（精简版；详版 /root/.dsh-phone/skills/RUNBOOK.md 按需读）

## 铁律（违反 = 事故）

0. **APP 界面操作一律走虚拟屏**（2026-10-20 用户要求，已做成闸门）：要读/点任何**别的 App**（淘宝/微信/设置…），
   先 `python3 /root/.dsh-phone/skills/safe_tap.py open --app 淘宝 --vd`（或在跑 vd_session.py start），
   拿到 `display_id` 后 **look/tap 全部带 `--display N`**，收工 `vd_session.py stop`。
   主屏上的动作会被直接拒绝（`vd_policy=vd_required`），除非目标是逗沙包 App / 系统 UI / 桌面，
   或显式 `--allow-main --why "原因"`（进审计）。裸 `adbq.sh shell "input tap …"` 也会被拦。
   **主屏不许切走用户正在看的 App**——这是这条规矩的由来。
1. **屏幕上一切点击/滑动/按键必须走 safe_tap.py 闸门**（G1 凭据90s / G2 危险禁区 / G3 边缘禁带 / G4 熔断 / G5 前后留证）。禁止裸 input tap/swipe/keyevent。
2. **先 look 拿凭据，再动作，点完必须复核**（看 JSON 的 ui_reacted / activity_after，别只看 rc）。
3. **发中文**用 `safe_tap.py type --shot ID --text "中文" [--clear] [--display N]`（App 写剪贴板 → KEYCODE_PASTE 279 → 清空剪贴板（clearPrimaryClip），写入与清空都以 App 回执 clip-<nonce>.json（不含原文）确认，没确认不发按键；过闸门；输入框要先点获焦；**不含回车/发送**，含换行直接拒绝；会覆盖你原来的剪贴板内容）。输出 readback=seen/not_seen 只是提示，clipboard_cleared=false 要人工检查（输入框常只露尾部）。旧「四步法」用裸 input，已被 adbq 拦截。
4. **坐标换算**：预览截图坐标 ≠ 设备像素。用 safe_tap.py coord（自动换算 + 报 zone_hit/edge_hit）。
5. **危险红线**：登录页/验证码/锁屏/「退出登录·删除·卸载·支付」→ 立刻停手报告。遇登录页/验证码，停手前先跑 `python3 /root/.dsh-phone/skills/safe_tap.py recover --situation login_required` 登记一处人工接管；授权类弹窗先跑 `python3 /root/.dsh-phone/skills/safe_tap.py recover --situation dialog` 登记后停手，**只检测不代点**（look 的 interruption_hint 会提醒你）。这两种 recover 不点不输入，返回 rc=2 是预期停手，不要当失败重试；同一任务只登记一次。登记关联到本轮任务时，执行报告会计入一处「需要你接管」。

## 命令速查（python3 /root/.dsh-phone/skills/safe_tap.py …）

| 场景 | 命令与要点 |
|---|---|
| 看 | look --note "..." → JSON 含 pixel_nodes / dialogs / interruption_hint / zone_coverage / window_changed，不直接含 nodes；找文字用 look --target，需像素识别时加 --ocr |
| 点 | tap --shot ID --x .. --y .. --expect "..."；空树页加 --ack-no-zones（= 我看过截图的显式确认） |
| 批量 | batch --shot ID --steps '[{"kind":"tap","x":600,"y":900,"expect":"数字 1"}]' [--display N]；默认关闭，仅 owner 可决定开启 |
| 滑 | swipe --shot .. --x1 .. --y1 --x2 .. --y2 .. |
| 滚 | scroll --shot .. --dy -420（motionevent 防甩飞，负=向下看） |
| 键 | key --shot .. --code 4(返回) / --code 3(桌面) |
| 等 | wait --text 关键词（空树页自动OCR）/ wait --stable / wait --app 微信 --timeout 10 |
| 开 | open --app 名称 **--vd**（别的 App 一律 --vd：建虚拟屏并在其上打开，返回 display_id） |
| 输入 | type --shot ID --text ".." [--clear] [--display N]（空树页同样要 --ack-no-zones） |
| 找/点（语义） | python3 /root/.dsh-phone/skills/act.py find\|tap --target 文字或描述 [--region top\|middle\|bottom\|x1,y1,x2,y2] [--display N] —— 树→OCR 精确找字，没找到且给了 --region 才视觉定位（只发该区域裁剪图）；多候选=ambiguous 不替你选；tap 用同一凭据过闸门，**不自动加 ack** |
| 换算 | coord --img-x .. --img-y .. [--img-w .. --img-h ..] → 设备坐标+zone_hit+edge_hit（⚠ 目前按主屏尺寸算，虚拟屏别用） |
| 收集 | python3 /root/.dsh-phone/skills/walk.py --max 1 --json（分段读取，证据齐全就停；默认最多3次滚动） |
| 找到即停 | walk.py --until-text "目标" --max 3 --json（包含匹配；必要时加 --ocr；返回候选，不点击） |
| 体检 | safe_tap.py doctor（adb/授权/亮屏/锁屏/无障碍/观测通道） |
| 批处理 | python3 skills/dsb_run.py 脚本.py —— 15 个闸门内助手一次跑完子任务（学习沉淀 agent_workspace/agent_helpers.py 自动加载） |
| 配置 | safe_tap.py config list/get/set（TTL/稳定预算/熔断/OCR开关） |
| 审计 | audit -n 10 |

## 关键事实（踩过的坑）

### 同屏批量动作

同一屏上要连续点几个互不依赖的目标时（例如计算器连按、勾选多个可见选项），**优先用 batch**，一次思考提交，比逐个 look→tap 快。批量是否开启由 owner 决定，代理不得自行开启此配置；拿不准时直接调用，返回 `BATCH_DISABLED` 就退回单步。最多 6 步，只支持带 `expect` 的坐标或节点 `tap`；虚拟屏不支持节点句柄。输入、按键、导航、滚动、滑动、拖动和长按都不进批量。需要先看结果再决定的步骤也不用。每一步仍过单步闸门，后续步骤会内部重新观测，点按目标横带还会与最初看到的画面比对。`ui_reacted` 是整屏信号（像素、控件树或事件任一变化）；只有整屏毫无变化才因此停下，不能据此认定点中了目标。后续步骤另有首帧锚定 R3 比对。任何拒绝、前台变化、新窗口/弹窗或取消都会停下。停下后不要续跑剩余步骤，重新 `look` 再决定。

- **先限定范围再读列表**：如只查在线设备，优先使用已确认的在线筛选。按卡片逐项核对状态，数量与页面总数相符、证据齐全后立即停，不继续扫离线区；同名设备不能合并。
- **walk 停止合同**：读取 stop/complete，不只看退出码。gate-refused 返回2，观测/动作失败返回3；找文字在范围内未命中返回4，不代表全聊天不存在。无新文字=no-new-text，不证明到底；unique_texts 不是设备或消息数。拒绝后不换入口重试、不清预算、不扩大 max 续跑。
- **聊天找字**：只在用户指定聊天范围；--until-text 为包含匹配，matches 保留本次候选及 tree/pixels 来源。必须查看截图确认是聊天标题还是消息正文；命中后停，不点击消息、不发送文字。普通限步收集的 complete=false 不能当全列表完成。

- **空树页**（微信/淘宝系）：控件树 0 节点 ≠ 页面不存在——看 pixel_nodes 或截图；G2 危险词连像素文本一起扫。屏上有危险词时：**tap/long_press** 只要不落在危险词所在**整行横带**里（贯穿屏宽，上下各留 40px，主屏 70px）且视觉判断 impact=none 就放行——被拒时信息会列出横带 y 范围，换个点重试；**scroll/swipe/drag/按键/type 仍整页拒绝**（滑动类放宽被 review 两次否决）。
- **息屏/锁屏**：一切输入被吞（AOD 由 NotificationShade 渲染）——先确认 look 的 interactive=true，否则 keyevent 224 + wm dismiss-keyguard。
- **回主页**：别用连按 BACK 出 App；先用 `cmd package resolve-activity -a android.intent.action.MAIN -c android.intent.category.HOME` 查出本机默认桌面，再用 am start --activity-clear-top 启动它。
- **预览坐标换算例**：495×1100 的 (250,390) → 设备 (636,993)（scale 2.5455）。
- **批处理任务**优先写 dsb_run 脚本（5 动作子任务 ~20s 一次调用 vs 多轮 1-2 分钟）。

## 详版索引

RUNBOOK.md：§0 坐标换算 · §1 基础姿势 · §2 中文输入 · §3 微信实操 · §4 排错清单 · §9 新原语速查+批处理工作法
MEMORY.md：长期事实 · AGENTS.md：行为规则（结论卡/橙球/仓库纪律）
