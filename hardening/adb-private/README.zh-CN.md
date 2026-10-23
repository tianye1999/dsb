# ADB 私有传输

[English](README.md) · **简体中文**

## 是什么

将受管 ADB server 从共享 TCP 5037 改为 Termux 的 **0700 私有目录与 0600 filesystem socket**，统一 Termux/Ubuntu 的受管入口。安装时由 Termux clang 编译 launcher，路径和 UID 现场探测。

设备标识、连接目标与固定本机默认值的区别，见[安装说明：设备相关数值](../../docs/INSTALL.zh-CN.md#设备相关数值哪些需要调整)。

这不是 Android 沙箱。已有 Termux UID、RUN_COMMAND、ADB 授权或 root 的主体仍在信任边界内；无线调试端口仍需保管授权，不用时关闭。具体边界见[威胁模型](../../docs/THREAT-MODEL.zh-CN.md)。

## 为什么默认尝试

普通 App 可接触的 TCP server 端口与 App 私有目录是不同边界，不能用 APK 命令鉴权代替 server 隔离。安装器从第一次 start/pair/connect 就使用私有 socket，配对码仅走 stdin。外来或无法识别的 IPv4/IPv6 5037 监听是红色冲突，不得降级成跳过。

预检不满足不替换入口，doctor 明确显示原因和「ADB 未加固」警告；已加固状态检查失败则严格失败。候选版的连接身份与并行会话仍须完成验收，不能把预检跳过或有限测试宣传成加固可用。旧工具只在一台真机验证过，通用版本只在 arm64 模拟器验证；不同 ROM、重启、mDNS 与 x86_64 Android 尚需实机验收。

设备地址只是候选，不是身份。每个新连接经私有 filesystem 通道与 shell 跨 UID 挑战后，凭据绑定 server 代次、transport ID 和通道进程。通道只承载一次实际连接，断线后拒绝复用；失败的新连接断开、凭据失效。普通客户端共享执行锁；启动/清单/回滚独占。完整清单检查或回滚前应先结束长期虚拟屏会话；guard 无法取得独占锁时只续做文件审计回执，不会把未执行的进程检查写成健康。

冷启动独占锁内双检，多个首调共用一个 server。启动环境强制 `ADB_EMU=0`，关闭本地模拟器扫描，不把本机连接注册成模拟器别名。guard 只在完整核验 exe、socket、UID、starttime 与运行镜像后，借助 pidfd 终止重复受管 server 并起一个；未知进程或不支持 pidfd 的旧内核拒绝自动终止，需本人维护，不使用数值 PID 盲杀。

## 怎么回滚

```bash
# 原生 Termux 中执行，不在 Ubuntu 容器执行
bash ~/dsb-installer/install.sh check
bash ~/dsb-installer/install.sh rollback
```

私有切换一旦发布完整事务，中断或自检失败优先从已验证的 manifest、运行时和 launcher 补完私有启动，不强制退回 TCP；发布前失败则普通入口未改。普通回滚校验本 UID server、监听 inode、IPv6 关闭、转发表及在线设备身份；后半段失败会尝试重新应用私有代，重应用也失败则保留现场并报告真实阶段，不能宣称 ADB 可用。成功回滚恢复普通 TCP，仅是较弱的可用性回退；App、Key、网页和任务桥不删除。

若回滚已到终结态、收尾前中断，重启后又先运行了普通 `adb`，该新 server 没有本工具的 5037 凭据，`recover` 会拒绝归档。先在原生 Termux 运行 `adb kill-server`，确认没有其它 5037 使用者，再运行 `bash ~/dsb-installer/install.sh recover`；不要删除 journal 绕过归属检查。

旧版 R6-0b journal/diversion 或未知格式在任何写操作前拒绝，提示「旧版部署，需 owner 在场迁移」；公开工具不能回滚该部署。不要删除 journal 绕过检查。

## 共享库升级与私有代

**公共库可以正常 `pkg upgrade`，不必先回滚。** 安装时用 `readelf --dynamic` 递归计算 backend 的 NEEDED 闭包，用 `dpkg-query -S` 记录每个非系统库及链接目标的包属主与版本；不维护库名或包名枚举表。实体和必要 SONAME 别名复制到 `R/runtime-<代次>/lib`（目录0700，实体0600），backend 放在同代目录；逐文件 SHA-256、权限、属主和链接锁定，完整代准备后才发布 manifest。

只 hold 被 diversion 的 Termux `android-tools` 和 Ubuntu `adb`，记录并在普通回滚后恢复原 hold，不新增公共共享库 hold，也不改无关或用户预先设置的 hold。系统 bionic、`/system`/APEX 库不复制。`libc++_shared.so` 随完整闭包私有化，仅在独立 backend 进程内使用。

静态 launcher 在启动 Python 前清理调用者 `LD_*`。普通客户端、身份挑战、version 探针和 server 启动统一仅为已验证的 backend 设置固定私有 `LD_LIBRARY_PATH`，不传给 Python/proot/系统命令。部署时锁定逐文件哈希及 dev/inode/size/mtime_ns/ctime_ns；日常调用走元数据快检，任一变化即复核并拒绝，原生 doctor 和定期 guard 完整哈希及 version 探针。server 的 `/proc/PID/maps` 核对私有映射；系统库按当前映射的可信根目录、属主、权限及实时 dev/inode 检查，不锁住 APEX 的版本路径或开机设备号。失败明确报告 `ADB_PRIVATE_RUNTIME_DRIFT`、`ADB_PRIVATE_RUNTIME_LOAD_PATH` 或 `ADB_BACKEND_LOAD_FAILED`，不会降级 TCP。

**私有副本不会随 `pkg upgrade` 自动获得安全更新。** 定期更新安装器源码及仓库索引，结束长期 ADB 会话后刷新完整一代：

```bash
pkg update
bash ~/dsb-installer/install.sh refresh
bash ~/dsb-installer/install.sh recover  # 仅当上一步提示事务未完成时运行
bash ~/dsb-installer/install.sh check
bash ~/dsb-installer/install.sh doctor
```

重跑默认 `install.sh` 也会刷新已部署的私有代。刷新通过 APT 的现有仓库索引解析并校验下载完整包集合（不安装、不执行包脚本，不修改公共库）；从暂存包动态计算闭包，验证 backend 加载后整代切换，不能把旧 backend 配新库后重新记哈希。仓库本身若不一致，刷新拒绝，原私有代不变。使用第三方/本地源者须自行保证源的一致性；无网络时保留原代，但不能宣称已获得安全更新。刷新后 `check` 的设备身份阶段需要无线调试设备在线或重新连接；离线时可先运行 `doctor` 的本地审计，不能把旧 binding 当作新连接。

ROOT、HOME helpers、四入口与容器内单个 `adb_endpoint.py` 同事务更新。doctor 先在原生 Termux 下完整审计并发布短期回执；proot 侧只读该回执、执行元数据快检和实际进程/映射检查，不再逐库全量哈希。回执缺失或过期严格失败。若提示未完成刷新，运行 `bash ~/dsb-installer/install.sh recover` 先恢复私有事务，再重试刷新或普通回滚。

刷新有独立可恢复事务，不借普通 TCP 回滚。先离线切换并自检本地 server/runtime，无线设备离线不阻止刷新；旧 binding 文件保留，但新 server 代次不会把它当作已认证连接，只有实际重连时才失效并重建。激活失败或断电后恢复旧私有代。私有新代已健康提交后，若 hold 恢复被其它 apt/dpkg 操作阻挡，则保留新代并提示重跑安装器完成恢复。失败新代在恢复后清理，历史刷新备份限留五份；运行中的代次不清理。

普通 `rollback` 在停 server、改入口/日志/hold 之前，先以公共库验证两侧原 vendor ELF 可加载。若公共库已升级、旧 vendor 不能加载，报 `ADB_ROLLBACK_REFUSED`；只有 journal 为 applied 且本地私有 server/maps 健康，才称私有服务仍可用。请准备与普通 ADB 匹配的依赖包，或由维护者审查普通 ADB 包及回滚基线更新，再重试；不能删除私有目录、修改哈希或强行解除 ADB hold。刷新私有代不等于更新普通 vendor 包，故不保证普通回滚总能立即完成。

新 journal/manifest 为 `tool_version=3`。旧公开 v2 可显式 `refresh` 迁移：同事务更新 ROOT 与 HOME helpers、入口和 manifest，新代健康后仅一次将旧版新增的共享库 hold 恢复为各包原状态。历史 hold 另存审计，后续刷新与普通回滚只管理两侧 ADB，不重放共享库 hold 清理。旧公开 v1 仍支持显式回滚，之后重新安装；不提供隐式 v1 改写。历史 R6/手机私有 journal 继续在所有公开写操作前拒绝，必须使用另行审查的私有增量事务，不得补 schema 绕过。

若中断发生在旧 v1/v2 切换途中、尚无完整私有代，`recover` 会先执行普通回滚预检，再尝试恢复旧入口；若普通 `android-tools` 已升级到与日志基线不同的版本，预检会拒绝，不会强制覆盖新包。先由维护者恢复与日志匹配的包及公共库，再重跑 `recover`，不要手动删除 journal 或解除 hold。v3 的已回滚终结态可由 `recover` 幂等完成目录归档。

Android OTA、Python/Termux 自身损坏，以及已有同 UID/root 控制权仍不在私有库隔离的保证内。维护态只暂停新 ADB 调用，不撤回已提交动作，也不停止网页、桥或 SSH：

```bash
bash ~/dsb-installer/install.sh maintenance
bash ~/dsb-installer/install.sh resume
```
