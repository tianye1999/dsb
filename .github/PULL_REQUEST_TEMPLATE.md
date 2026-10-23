## What / 改了什么

<!-- One sentence: what changed and why. 一句话：改了什么、为什么 -->

## How it was verified / 怎么验证的

- [ ] Offline tests pass / 离线测试全绿 (`agent/`: `python3 test_*.py tests/test_*.py`; `bridge/`: `node --test`)
- [ ] **Real-device evidence / 真机证据** (required when touching `agent/safe_tap.py` `vd_session.py` `act.py` `vision_check.py` `ocr.py`, `bridge/` or `app/src/` / 改到这些路径时必填): doctor output / redacted audit excerpt / end-to-end timings（doctor 输出 / 审计片段（已打码）/ 端到端计时）
- [ ] No screenshots, audit logs, sessions, chat or order content, keystores or passwords committed / 没有提交截图、审计日志、会话、聊天或订单内容、密钥库、口令 (ran `gitleaks dir .` locally)

## If a safety gate changed / 如果改了安全闸门

- [ ] Real incident or need / 真实事故或需求: <!-- which task, which page / 哪次任务、哪个页面 -->
- [ ] Behaviour tests: one fails before and passes after; one proves what should be refused is still refused / 行为测试：一个改前失败、改后通过；一个证明「该拒的仍然拒」
- [ ] Not an enumeration guard (no added keywords/blocklists) / 不是枚举式守卫
- [ ] New guards are off by default / 新守卫默认关闭
- [ ] Independent review requested (reviewers: hunt for new bypass paths) / 已请求独立 review（审稿人请专门找新的绕过路径）

## Notes / 备注
