# dsh-dsb

[English](README.md) | 简体中文

这是逗沙包的可安装 DSH 组合包。逗沙包基于
[DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness)，是独立项目，
不是 DeepSeek 官方项目，也不暗示官方背书。本包不包含 Android App 或手机执行器。

## 包含内容

| Cordis 条目 | 内容 |
| --- | --- |
| `dsb-brand` | 官方侧栏槽位中的逗沙包插画与名称 |
| `dsb-report` | 带鉴权、白名单过滤的执行报告，以及主动下载的 ZIP 材料 |
| `dsb-ui` | 侧栏、输入框、字号、主题、手机布局与干净反馈导航 |

三个条目共用 `dsh-dsb` 加载入口，用 `component` 配置分派；客户端只有一个
`./client` 懒加载入口，组合原来的两个槽位插件，避免同包多来源冲突。
与原 DSB profile 一致，组合包同时停用 `ui-brand-official`（避免槽位冲突）和
`ui-message-feedback`。`dsh-recap` 独立保留，不安装、不删除、不改配置。
对第三方用户，这意味着官方品牌位被替换，消息赞/踩按钮与上游反馈表单消失。
要恢复全部上游界面，卸载本包、清除 profile 自己对这两个上游条目的停用覆盖后重启。
若保留报告但恢复上述控件，在 web profile 本地 patch 中停用 `dsb-brand`、`dsb-ui`，
并显式启用 `ui-brand-official`、`ui-message-feedback`，然后重启。

## 版本要求

**仅支持 DSH `0.2.1-rc.2`。** 清单使用精确的
`peerDependencies["@deepseek-ai/dsh"]`；不是 `^`，也不靠 `engines.dsh` 猜测兼容性。
app-boot 会在组合包与插件加载边界检查 peer，不匹配时跳过整个包并报告
`skippedBundles`。不要添加版本豁免：CSS Module 选择器与此版本绑定。
必须先清除旧 dist 补丁，否则跳过插件也不能还原原版界面。

## 安装

在已安装 DSH 的环境里，从可信源码目录安装到 `web` profile：

```sh
dsh plugin --profile web add /absolute/path/to/dsb/dsh-plugin
```

包内已带客户端、插画和报告运行文件，不需要发布到 npm 或批准构建脚本。
报告/导出仍需 Python 3 与仓库 `installer/requirements.txt` 中的相应依赖；
包含截图的导出需要 Pillow。本命令不安装手机执行器、不创建 Key、不连接设备。
安装或更换链接后重启 DSH，再刷新网页。

当包含本目录的已核对提交发布后，可用 pnpm 12 的 GitHub 子目录写法：

```sh
dsh plugin --profile web add 'github:tianye1999/dsb#<reviewed-commit>&path:/dsh-plugin'
```

把 `<reviewed-commit>` 替换为实际提交哈希。这里只是文档确认的语法模板，
不表示本次未提交的改动已经在远端；本任务没有发布或实际执行远端安装。
依据：[pnpm 参数组合规则](https://pnpm.io/package-sources#install-from-a-git-repository-combining-different-parameters)
以及 DSH plugin-manager 的 Git spec → GitHub 连通性检查 → `pnpm add` 流程。

## 已部署机器迁移

安装器把 `/root/dsh-plugin` 链为唯一的 `dsh-dsb` 组合包，并登记 `link:` 依赖以便
DSH 包管理器正常卸载；仅自动迁移指向同级
`dsh-web-tweaks/dsb-brand`、`dsh-web-tweaks/dsb-report` 的受管旧符号链接。
其它 bundles、依赖及 `node_modules` 保留。实体目录、外来链接、手动声明的旧依赖、
没有链接归属证据的旧 bundle 名都拒绝覆盖，要求人工核对。
清单写入及安装后验证失败会回滚原字节和权限，保留旧链接，包括 Python 的
KeyboardInterrupt / SystemExit。迁移成功后只删除安装器明确部署过的旧补丁脚本清单；
保留 restore/reload 工具、recap 与未知文件。安装器自有的 `/root/dsh-plugin`
先复制到 staging、校验名称与必需文件，再通过 rename 发布，并保留可恢复的旧目录
回滚槽；复制失败不会先删掉已安装版本，成功替换也不会残留旧包的废弃文件。

安装器在切换 profile 前检查旧标记；命中时进入现有维护态、停止 DSH，再用随包
纯净首页恢复。恢复成功立即移除本次带归属标识的维护标记，不跨越后续交互或可能失败的
步骤。重跑时，安装器自己的遗留标记且首页已无旧补丁，就保存证据后清理标记，
包括不是随包 pristine 的未打补丁首页，不会覆盖该首页。外来或无标识标记不删除。
部署或修改 profile 前持久化 `~/.dsb-ipc/restart-required`，中断后重跑、profile 未变化、
仅更新插件内容也会请求重启。恢复失败保留标记并打印恢复命令，不能报告成功。
现有 `start-dsh.sh --restart` 返回后还必须确认旧 PID 消失、新 PID 出现、认证地址及
客户端插件就绪，才能清除重启请求并报告成功。未命中旧补丁则不改首页、
不因恢复而停服。插件自身也会在旧首页上撤下本包的 UI 注入并告警一次，避免两套脚本。

手动迁移时，**先停止 DSH**。安装器的 `/opt/dsb-dsh` dist 不是纯净版：升级机器上
它就是被旧补丁修改、待恢复的文件。使用本包 `runtime/` 随附的固定纯净首页，
npm / Git 安装形态也包含它。安装器管理的手机在 Ubuntu 内运行：

```sh
python3 /root/dsh-plugin/runtime/restore-index.py \
  --index /opt/dsb-dsh/node_modules/@deepseek-ai/dsh-web-frontend/dist/index.html
```

工具检查纯净 SHA-256、旧标记、资源名及资源存在性，独占保存
`.before-dsh-dsb` 备份，重试使用唯一后缀，不覆盖已有证据；恢复前清理自己的残留普通
临时文件。处理报出的磁盘、权限或版本问题后可重试该命令，再重跑安装器自动恢复自有
维护标记。不猜路径、不下载、不重打补丁，未知/混合版本拒绝。
恢复会替换完整首页，先人工确认没有需要保留的其它定制。`--pristine` 默认使用
随包 `runtime/pristine-0.2.1-rc.2.html`；其它安装布局替换包与目标路径即可。
恢复后重启 DSH。日常插件渲染不写 dist；安装器只做一次旧补丁恢复。

报告改为导入包内 bridge 辅助文件，不依赖手机旧目录中单独复制的
`task-report-store.mjs` 或仓库相对位置。继续保留 `report-paths.sh` 提供的
`BRIDGE_AGENT_DIR`、`BRIDGE_PYTHON`、`DSB_AUDIT_FILE`、`DSB_ACTIVE_TASK_FILE`
以及原 `.dsh-phone` 数据。安装器设置 `BRIDGE_AGENT_DIR=/root/.dsh-phone/skills`，
实际使用该处的 `task_report.py`、`feedback_export.py` 与词表，而不是 `runtime/agent`。
无环境覆盖时，已有 `~/.dsh-phone/skills/task_report.py` 仍优先；否则使用包内只读脚本。
默认审计、账本、报告写入可写的 `~/.dsh-phone`，分别为
`skills/safe_tap_audit.jsonl`、`active-task.json`、`reports/`，不会写入可能只读的包目录；
显式数据路径覆盖仍有效。不会凭空产生手机执行证据。agent/词表损坏只使 report
组件失败并告警，brand / ui 独立加载。

报告 bridge 辅助代码始终使用 `runtime/bridge`，不使用旧 `/root/dsh-bridge`。
只更新插件不会同步手机 skills；只更新 `/root/dsh-bridge` 也不会更新报告的 bridge。
应在同一源码版本运行 `node dsh-plugin/build.mjs`，再重跑安装器，一起部署 `agent/`
到 skills 和重建后的插件；不要只复制某一个 Python helper，也不要手改 runtime 副本。

## 卸载

```sh
dsh plugin --profile web remove dsh-dsb
```

重启 DSH、刷新网页即可撤销包的注入；profile 自己的覆盖仍属于 profile。
历史磁盘补丁需先按上文恢复。卸载不删除报告或审计，不要并存安装两个旧包。

## 隐私

反馈仅在当前视图打开 `https://github.com/tianye1999/dsb/issues`，不附账号/设备参数。
报告仅返回白名单字段；材料下载保留鉴权，截图只在主动选择时导出。
本插件没有新增遥测。

DSH 自带的插件清单功能**默认启用**：向 DeepSeek 官方 API 发请求时，
`dsh_plugin_packages` 会包含启用且活跃的插件包名和版本，不包含对话、报告或设备内容。
这不表示模型请求本身不含对话内容。上游该功能的 `enabled: false` 可关闭清单；
具体见锁定版 `@deepseek-ai/dsh-plugin-package-inventory-deepseek/README.md`。

## 开发与验证

原客户端或公共报告逻辑改变后运行 `node dsh-plugin/build.mjs`；
`node dsh-plugin/build.mjs --check` 校验客户端、插画及运行文件与仓库源文件一致。
不要直接编辑生成的 `runtime/` 和 `client.js`。
迁移块对照、离线夹具来源与浏览器验收见 [MIGRATION.md](MIGRATION.md)。
