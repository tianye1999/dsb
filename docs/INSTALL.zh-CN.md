# 安装逗沙包（安装器 v1，候选版）

[English](INSTALL.md) · **简体中文**

DSH 插件包的本地安装与旧前端补丁迁移见 [dsh-dsb 说明](../dsh-plugin/README.zh-CN.md)。

不需要 root，不需要电脑常连。Android 11+，64 位 arm64 / x86_64，建议预留至少 8 GB 空间，首次下载与解包也会临时占用空间。你唯一需要准备的服务配置是 **DeepSeek API Key**。无线配对码是 Android 的一次性授权，不是第二个服务 Key。

> v1.0.3 安装器为候选版：验证范围目前限于维护者的一台真机与 arm64 模拟器，冻结首装和不同 ROM 验收尚待完成。下面的下载命令内联了发布包的 SHA-256（与 Release 附带的 `SHA256SUMS` 一致），校验不通过不会解包执行。APK 由维护者现有发布密钥签名，安装器按固定的证书 SHA-256 验签；安装器的模拟器测试仅用 debug 签名，不使用发布密钥。

> [!WARNING]
> **逗沙包只从本项目安装。** 官方命令只有项目 README 和本安装说明里这一条：先把发布包从 `github.com/tianye1999/dsb/releases/download/…` 下载成文件，核对它的 SHA-256 与本页写的值一致（也就是该 Release 里 `SHA256SUMS` 的值），然后才解包；它从不把下载内容直接交给 `bash` 或 `sh` 执行。Issues、讨论区、评论、私信或其他消息里贴出的安装、升级、修复命令和 APK 链接，一律不要运行或打开，即使对方自称维护者。Termux 本身按安装说明从 F-Droid 或 Termux GitHub Release 安装。如果已经运行了别处的命令或装过别处的 APK，请看[怎么处理](../SECURITY.zh-CN.md#冒充的安装命令)。

## 1. 装 Termux 和逗沙包

在手机上安装官方 [Termux F-Droid 版](https://f-droid.org/packages/com.termux/)或 [Termux GitHub Release 版](https://github.com/termux/termux-app/releases)，版本 0.118.x 或更新。Termux 插件必须与主程序同源。安装器 v1 不支持 Play 分支或 32 位系统。

逗沙包使用本项目固定版本 Release 的 APK，由你在系统安装器里点「安装」。可以先安装；还没装时，第 2 步的脚本会下载并打开 APK，不会代点安装。允许侧载来源时只选择你信任的来源。

装好后先不要点「打开」；请先完成脚本的授权引导，特别是悬浮窗。安装器确认悬浮窗授权后才打开 App。现有 App 在缺少悬浮窗授权时直接打开可能启动服务失败。

## 2. Termux 里跑一条命令

打开 Termux，等待首次初始化，然后执行：

```bash
curl -fL https://github.com/tianye1999/dsb/releases/download/v1.0.3/dsb-installer.tar.gz -o ~/dsb-installer.tar.gz && printf '%s  %s\n' 'af9d85a726f939c89261eed755ff063ae3274a732aacfa899eb46e2ba453b3c6' "$HOME/dsb-installer.tar.gz" | sha256sum -c - && tar -xzf ~/dsb-installer.tar.gz -C ~ && bash ~/dsb-installer/install.sh
```

首次会更新 Termux 的基础库、安装 Ubuntu 24.04、Node/dsh、Python/OCR、scrcpy，下载量较大。网络需可访问 Termux、GitHub、Ubuntu、nodejs.org、npm、PyPI。`Ctrl-C` 可以中断；重跑 `bash ~/dsb-installer/install.sh` 继续，不删除容器、会话或 Key，也不重复添加 SSH 授权。

> **已解决**：Termux proot 5.1.107.99（2026-10-23）在容器里安装 Ubuntu 软件包时可能报 `proot warning: signal 11 received from process -1` 而停止。proot 5.1.107.100 已修复（[termux/proot#420](https://github.com/termux/proot/issues/420)），我们已在 arm64 模拟器上复测原来崩溃的安装。如果你之前为规避它 hold 了 proot，请运行 `apt-mark unhold proot && pkg upgrade`。

Ubuntu 官方软件源使用 HTTPS；最小镜像缺少 CA 时，先使用 Termux 已受信任的 CA，再由 Ubuntu 的 `ca-certificates` 包接管。不关闭 TLS 或 APT 签名校验，保留原源配置备份和自定义源。

安装器同步当前公开版的网页调整和逗沙包品牌包。品牌通过 web profile 的 bundles 与本地包链接接入，不重装已有 profile 依赖；保留本地自定义。web、phone、headless 的会话日志上传默认关闭，写入后用实际 dsh `--dump-config` 读回核验。工作区使用 `/root/dsb-workspaces`，不把存有 Key/SSH 私钥的 `/root` 当作工作区。

安装器会说明并开启 Termux 的 `allow-external-apps`。这是逗沙包通过官方 `RUN_COMMAND` 接口启动本机服务的前提；系统仍要求你给逗沙包单独授权。**不要给不信任的 App 运行 Termux 命令的权限**，获得它就能读写同一 Termux 用户的数据和 Key。

## 3. 填 DeepSeek API Key

脚本只询问一次，输入时不显示字符。保存到 Ubuntu 的 `/root/.dsh/.deepseek-key`，目录 0700、文件 0600。重跑保留原 Key，不再询问。安装日志 `~/dsb-install.log` 只记状态，不记录 Key，也不收集命令的原始输出。

输错时运行 `bash ~/dsb-installer/install.sh set-key` 隐藏输入并原子替换，重启 DSH 后使用新值。只有开始真实任务才会产生模型调用。环境安装和 doctor 不调用模型。

## 4. 按提示点几下

按脚本逐步引导完成，能够直接打开的系统设置页会自动打开：

1. 开发者选项 → 无线调试 → 使用配对码配对。尚无开发者选项时，先到「关于手机」连续点版本号/Build number 七次，按系统要求确认本人身份，再返回。将配对页的地址和六位配对码输入脚本，再输入无线调试主页的**连接端口**。两种端口不同。建议分屏查看，避免配对页关闭后端口失效；系统不允许从后台打开页面时，回到 Termux 前台再按回车，或手动进入对应设置页。
2. 如果尚未安装逗沙包，打开方式选「软件包安装程序 / Package installer」（不是 Termux），在系统安装器中确认安装。允许 Termux 安装未知来源后若只回到终端，按回车重新打开安装器；若系统结束了 Termux，重新打开 Termux、重跑同一安装命令即可续装，不用再填 Key。
3. 允许逗沙包悬浮窗、无障碍服务、通知使用权、通知权限，以及「运行 Termux 命令」权限。侧载限制开启时，需要你在应用详情菜单中确认「允许受限设置」。部分系统要先在被禁用的权限页点一次、看到拒绝提示，应用详情右上菜单才会出现此项；确认后返回权限页开启。脚本不会写系统设置替你授权。
4. 在 ROM 的自启动/后台管理中允许 Termux 和逗沙包，电池设为不受限。ROM 名称不同；安装器不能证明白名单已经生效。
5. 回到 Termux 按回车，脚本起服务并跑 doctor。按失败项完成剩余操作，之后执行 `bash ~/dsb-installer/install.sh doctor`。
6. 解锁并亮屏，打开逗沙包；首次确认 DSH Preview 提示。如果显示 Choose workspace，选择安装器自动创建的 Default workspace，不需要填写目录。确认网页、悬浮球和无障碍观测正常。先下一个低风险只读任务，例如「查看系统计算器当前显示，不输入、不修改」。假 Key 测试无法完成真实模型任务。

**doctor 结果要分开看**：`DOCTOR_FAIL` 表示有必需项未满足；`ADB 未加固` 是预检跳过/已回滚的安全警告，不会单独让其余正常的 DSB 失败。出现“已加固但检查失败”则严格失败，不降级为普通 ADB。

`safe_tap.py doctor` 也显示截图目录占用。在安装后存放 `safe_tap.py` 的目录（维护者手机上例如 `/root/.dsh-phone/skills`），运行 `python3 safe_tap.py prune-shots --dry-run` 查看或 `python3 safe_tap.py prune-shots` 清理过期截图；`--days N` 可覆盖默认的 3 天保留期（1–365 天）。审计日志不会被清理，但历史截图路径可能失效。

## 设备相关数值：哪些需要调整

**使用安装器时，不需要手改设备标识或 ADB 选择器。** 以下值由本机探测或现场生成，不从维护者设备复制：

- **ADB 连接目标**：首次按提示输入配对地址和连接端口，核验后保存到 `~/dsb/adb-target`；之后重连会尝试保存地址或匹配本机标识的 mDNS 服务。地址和服务名都只是候选，每个新连接必须通过本机 shell 身份挑战，失败即断开；mDNS 不可用且端口改变时，重跑安装器更新端口。
- **实际 transport serial**：不是保存的 IP:端口，而是每个新连接生成的私有 `localfilesystem:` 通道。凭据在 `~/dsb/adb-transport.json`，同时绑定 server 代次、transport ID 和通道；脚本通过统一入口选择已验证的 transport，不要手填或复用旧通道名。
- **Termux UID、路径与 ADB 入口**：安装时核验本机 UID、实际 Ubuntu rootfs 路径和两侧 ADB 入口，加固 launcher 由本机 clang 现场编译。当前只支持官方 Termux 私有目录，不是任意路径均可配置；无需照抄维护者的 UID 或路径。

下面是所有安装共用的固定默认值，不是设备标识；TCP 服务只监听本机回环，socket 则是本机私有文件套接字：

| 项目 | 默认值与调整边界 |
|---|---|
| SSH | `127.0.0.1:8022`；由 `installer/ssh_config.py` 生成配置，`termux/tsh.sh` 与安装器校验也固定使用该端口 |
| DSH 网页 | `127.0.0.1:3080`；启动命令在 `termux/start-dsh.sh`，就绪检查、可信入口与 App 也依赖该端口 |
| 私有 socket | ADB server 为 `$PREFIX/var/run/dsb-adb/server.sock`，任务桥为 `~/.dsb-ipc/bridge.sock`；路径由部署及调用代码约定，不是上面的随机 transport 通道名 |

当前安装器**不提供端口或 socket 路径配置项**。调整需要修改源码并同步调用方与校验，不能只改 `sshd_config` 或一条启动命令。

`127.0.0.1:5555` 只是**未使用安装器的旧手动安装默认值**，不是新安装要填写的端口。`DSB_ADB_SERIAL` 没有代码读取，设置它不会改变连接。文档、注释或示例中的维护者设备数值不适用于其他设备，不要照抄；按本机安装提示操作即可。

实现依据：[安装器](../install.sh)、[重连脚本](../termux/wx_adb_connect.sh)、[私有传输](../hardening/adb-private/README.zh-CN.md)、[agent ADB 入口](../agent/adb_endpoint.py)。

## 默认 ADB 加固

从第一次配对起就使用私有 filesystem socket，配对码不经 TCP 5037。IPv4/IPv6 5037 若有外来或无法识别的监听，红色退出，绝不归入预检跳过。设备身份使用短时 shell 跨 UID 挑战，不相信公开的 boot ID 或序列号。

安装器默认尝试把 ADB server 从共享 TCP 5037 改为 Termux 私有目录下的 filesystem socket。安装前探测本机 UID、容器路径、ADB 入口、进程和设备身份；不满足则不改入口，显示原因。切换后检查私有 socket、关键文件、入口 shell UID、IPv4/IPv6 5037 和 ADB 进程持有的 TCP 监听；中断后优先补完已验证的私有部署，不强制退回可能已不能加载的普通 ADB。

这是 server 传输加固，不是把 Android 无线调试或 `RUN_COMMAND` 变成沙箱。已获得 ADB 授权、Termux 执行权或 root 的主体仍在信任边界内。详见 [加固说明和回滚](../hardening/adb-private/README.zh-CN.md)。

```bash
# 原生 Termux 中运行，不在 Ubuntu 里运行
python3 ~/dsb-installer/hardening/adb-private/deploy.py check
bash ~/dsb-installer/install.sh rollback
```

回滚恢复普通 ADB TCP 传输，保留 Key、App、容器、会话和其它服务。若普通 vendor ADB 已不兼容升级后的公共库，回滚会在停服和修改前拒绝；只有已证实私有服务健康，才说私有 ADB 仍可用。

### 软件包升级与私有 ADB 运行库

- **安装器做了什么**：加固后的 ADB server 运行的是一份冻结的 `adb` 副本。安装器动态计算它实际需要的非系统共享库（ELF 依赖闭包），复制到私有目录，并逐文件锁定哈希。只 hold 两侧的 ADB 包，不新增任何公共共享库的 hold。
- **`pkg upgrade` 是安全的**：Termux 的公共库可以照常升级，不需要先回滚；私有副本保证 ADB server 照常工作。
- **安全更新要靠刷新**：私有副本不会自动获得库的安全更新。更新仓库索引后运行 `bash ~/dsb-installer/install.sh refresh`（或重跑默认安装器），把 backend 和依赖库作为一代整体刷新，再运行 `check` / `doctor`。刷新先验证本地服务，不要求无线设备在线；原有设备 binding 保留，下次实际重连时重新认证。
- **出问题时会看到什么**：doctor 在原生侧做完整审计，容器里只读一份短期审计回执并做元数据快检。私有运行库缺失、被改动或加载失败时，ADB 调用会被明确拒绝（guard 也会暂停 ADB），doctor 报出原因和下一步（例如 `ADB_PRIVATE_RUNTIME_DRIFT`、`ADB_BACKEND_LOAD_FAILED`），绝不回落公共库或 TCP。

旧版兼容、刷新失败后的恢复和安全更新边界见 [ADB 私有传输说明](../hardening/adb-private/README.zh-CN.md)。

无线端口变化时，保存地址和匹配名称的 mDNS 服务只是候选；连接后仍须重新通过 shell 身份挑战，失败即断开，不能仅凭地址或名称称为已核验本机。凭据同时绑定 server 代次、transport ID 和私有 filesystem 通道；通道只转发一次实际连接，断开后不能在旧 ID 上接入新端点。部分 Termux adb 不支持 mDNS，此时打开无线调试并重跑安装器更新连接端口，不必重复填写 Key。候选版仍须完成冻结首装和不同 ROM 验收。

回滚到普通 TCP 是较弱的可用性回退：doctor 会校验监听归属并在冲突时失败，但不能把独立的检查与后续 TCP 连接称为原子身份保证。需要私有传输边界时，应先恢复加固，不要忽略「ADB 未加固」警告。

首次新建 Ubuntu 时，安装器暂缓网页/任务桥，只运行 SSH 和 ADB 守护；完成授权引导后才释放首装标记并起服务。这样不会在 Android 的来源授权结束 Termux 之前提前建立任务归属。**任务桥已经运行后**若发生崩溃或归属不明，原 A6 保护仍会拒绝重新接收任务。安装器不会删除旧 socket、pid 或账本来冒充恢复；doctor 报出实际失败，需要人工核对旧任务。这与安装步骤续装是不同的验收层。

## 开发与测试

从完整仓库运行 `bash install.sh`。开发覆盖项不属于普通用户配置：

| 环境变量 | 用途 |
|---|---|
| `DSB_APK_PATH` | 使用本地 APK 路径；模拟器只用 debug 签名 |
| `DSB_TEST_APK_SHA256` | 本地 debug APK 的实测 SHA-256，缺失/不符均不打开 APK |
| `DSB_TEST_APK_CERT_SHA256` | 仅与本地 APK 测试路径一起使用的 debug 签名证书 SHA-256；已有包也必须完整验签 |
| `DEEPSEEK_API_KEY` | 预置假 Key 进行测试；脚本立即取消导出，不传给安装依赖命令 |
| `DSB_ADB_TARGET` | 测试中已配对的本机 IPv4:连接端口；仍核验设备身份，不绕过授权 |
| `DSB_NONINTERACTIVE=1` | 不等待手动步骤；不假定授权已完成，doctor 仍返回真实结果 |
| `DSB_MIGRATE_MANUAL=1` | 显式迁移旧手动安装（先备份脚本）；旧版 R6-0b 仍无条件拒绝 |

DSH 版本唯一来源为 `installer/release.json`，安装器和 CI 都读此文件。release 包用 `installer/build_release.py` 装配，固定 APK hash，并携带两架构固定 NDK 对象/NOTICE，手机不必访问 googlesource。直接跑开发源码但材料缓存为空时才需要该域名。

不要在 shell 命令行输入真实 Key，也不要把带真实 Key 的环境打印到 issue。普通用户应使用隐藏交互输入。

## 附录：手动部署、构建和升级

手动维护也应先使用本页入口完成私有 bootstrap、配对和加固，不要将旧脚本里的固定端口、旧 supervisor 或 `cp agent/*` 用作新部署。安装器部署的目标位置为：

| 内容 | 位置 |
|---|---|
| agent 与技能 | Ubuntu `/root/.dsh-phone/skills`、`/root/.dsh/skills/dsb-phone` |
| bridge / dsh-config | Ubuntu `/root/dsh-bridge`、`/root/dsh-config` |
| Termux 原生脚本 / 容器副本 | `~/dsb`、Ubuntu `/root/termux-dsb` |
| SSH 容器私钥 / Termux 授权 | Ubuntu `/root/.ssh`、Termux `~/.ssh/authorized_keys` |
| Key / 专用工作目录 | Ubuntu `/root/.dsh/.deepseek-key`、`/root/dsb-workspaces` |

更新代码前先结束虚拟屏会话并运行 `check`；需要升级 adb 包时先 `rollback`，保留 journal 与备份，不手工覆盖受管入口。旧手动环境必须显式 `DSB_MIGRATE_MANUAL=1`，安装器会备份 Termux 脚本及容器 skills/bridge/dsh-config；旧版 R6-0b 仍需 owner 在场迁移。

手动启动与自检：

```bash
bash ~/dsb/boot.sh
proot-distro login ubuntu -- python3 /root/.dsh-phone/skills/safe_tap.py doctor
```

从源码构建 App 用 `app/build.sh`，在手机上签名构建用 `app/build-phone.sh`。正式 APK 使用 owner 已有发布密钥，不能重新生成密钥代替；升级仍由用户确认安装。`sync-dsh-config.sh` 是运行目录同步到副本，不能初始化新环境。

安装器修改 SSH 前会备份 `$PREFIX/etc/ssh/sshd_config.pre-dsb`。已有 sshd 进程不会自动重载，必须本人重启并核对监听。需要还原时停止当前 sshd，将该备份恢复为 `sshd_config`，运行 `sshd -t` 后重新启动；确认影响现有远程会话后再操作。`~/.termux/termux.properties.pre-dsb` 同样保留首次修改前内容。

公开 APK 的升级必须用**同一签名密钥**重新构建，并由用户确认安装；不会后台下载或代点更新。发现已有 `com.dsb.assistant` 时，安装器先完整验签并比对固定证书 SHA-256；不符即停止，不凭包名接受或覆盖。

划掉逗沙包代表退出，手机侧服务按现有退出协议停止。卸载 App 与删除 Termux/Ubuntu 数据是不同操作；删除后会丢失本地 Key、任务和会话，请自行确认备份后再操作。
