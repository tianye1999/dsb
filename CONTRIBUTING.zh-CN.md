# 贡献指南

[English](CONTRIBUTING.md) · **简体中文**

感谢你有兴趣参与。逗沙包卷的是「可信」而不是「聪明」，所以这份指南里关于**安全闸门改动**的规矩比一般项目严。

## 开发环境

| 部分 | 需要什么 | 测试怎么跑 |
|---|---|---|
| `agent/`（执行层，Python 3.11+） | 只需 Python；离线测试不碰设备 | `cd agent && for t in test_*.py tests/test_*.py; do python3 $t; done` |
| `bridge/`（任务桥接，Node 20+） | Node | `cd bridge && node --test test-*.mjs` |
| `app/`（Android App，无 Gradle） | JDK 17 + Android build-tools 35 | `cd app && DSB_KEY_PASS=... ./build.sh`（见 `docs/INSTALL.zh-CN.md`） |
| `termux/`、`dsh-config/`、`dsh-web-tweaks/` | Termux + proot Ubuntu（真机） | 真机验证 |

离线测试在 CI 里跑；真机验证没法在 CI 跑，所以见下一节。

新增的主要功能必须同时在自动化测试套件中加入相应测试。

## 本地静态检查

在隔离的 Python 3.11+ 环境中安装开发依赖（不改变用户安装依赖），并准备 Node 22、JDK 17、Android API 35 和 ShellCheck：

```bash
python3 -m venv /tmp/dsb-lint-venv
source /tmp/dsb-lint-venv/bin/activate
python3 -m pip install --require-hashes --only-binary=:all: -r requirements-dev.txt
bash scripts/lint.sh
bash app/tests/lint-java.sh
```

`scripts/lint.sh` 检查所有 Git 跟踪的 Python、`.sh`、`.js` 和 `.mjs` 文件及 App manifest；新文件需先纳入 Git。Ruff 固定为 0.17.2，wheel 哈希来自 PyPI 对应版本的 JSON 元数据；仅启用 `F`、`E9`、`B006/B012/B016/B018/B023/B025/B033` 缺陷规则，不做格式改写。ShellCheck 按 Bash 解析全部 `.sh`（包括 Termux shebang），`warning` 及以上失败；CI 使用 `ubuntu-latest` 预装版本并输出版本号，本地基线为 0.10.0，不额外下载 CI 二进制或引入 Action。

Java 的全部 App／instrumentation 源码、离线 Java 测试和发布构建均使用 `--release 11 -Xlint:all -Werror`。`--release 11` 保持 Java 11 字节码目标，同时避免 `-source/-target` 未设置系统模块路径的工具链警告；没有全局排除 lint 类别。发布构建也严格失败，因为其 JDK 17／API 35 工具链已明确；确需保留的 Android 兼容 API 只在具体方法用带理由的 `@SuppressWarnings("deprecation")` 标注，不更改运行逻辑。设置 `ANDROID_HOME`（或 `ANDROID_SDK_ROOT`）可指定 SDK；macOS 默认使用 `~/Library/Android/sdk`。这些检查不会运行手机、模拟器或浏览器。

## 真机门

凡是改到这些路径的 PR，合并前必须有**真机**证据（审计日志片段 / `safe_tap.py doctor` 输出 / 端到端计时），写在 PR 描述里：

- `agent/safe_tap.py`、`agent/vd_session.py`、`agent/act.py`、`agent/vision_check.py`、`agent/ocr.py`
- `bridge/bridge.mjs`、`bridge/task-ledger.mjs`
- `app/src/**`

「离线测试全绿」不等于「能用」——这个项目的历史里，好几次假阳性都是这么来的。

## 改安全闸门的规矩

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
