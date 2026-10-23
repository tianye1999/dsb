## 改了什么 / What

<!-- 一句话：改了什么、为什么 -->

## 怎么验证的 / How it was verified

- [ ] 离线测试全绿（`agent/`：`python3 test_*.py tests/test_*.py`；`bridge/`：`node --test`）
- [ ] **真机证据**（改到 `agent/safe_tap.py` `vd_session.py` `act.py` `vision_check.py` `ocr.py`、`bridge/`、`app/src/` 时必填）：doctor 输出 / 审计片段（已打码）/ 端到端计时
- [ ] 没有提交截图、审计日志、会话、聊天或订单内容、密钥库、口令（本地跑过 `gitleaks dir .`）

## 如果改了安全闸门 / If a safety gate changed

- [ ] 真实事故或需求：<!-- 哪次任务、哪个页面 -->
- [ ] 行为测试：一个改前失败、改后通过；一个证明「该拒的仍然拒」
- [ ] 不是枚举式守卫（没有靠加关键词/黑名单）
- [ ] 新守卫默认关闭
- [ ] 请求独立 review（审稿人请专门找新的绕过路径）

## 备注 / Notes
