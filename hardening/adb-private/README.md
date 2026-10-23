# ADB 私有传输

## 是什么

将受管 ADB server 从共享 TCP 5037 改为 Termux 的 **0700 私有目录与 0600 filesystem socket**，统一 Termux/Ubuntu 的受管入口。安装时由 Termux clang 编译 launcher，路径和 UID 现场探测。

设备标识、连接目标与固定本机默认值的区别，见[安装说明：设备相关数值](../../docs/INSTALL.md#设备相关数值哪些需要调整)。

这不是 Android 沙箱。已有 Termux UID、RUN_COMMAND、ADB 授权或 root 的主体仍在信任边界内；无线调试端口仍需保管授权，不用时关闭。具体边界见[威胁模型](../../docs/THREAT-MODEL.md)。

## 为什么默认尝试

普通 App 可接触的 TCP server 端口与 App 私有目录是不同边界，不能用 APK 命令鉴权代替 server 隔离。安装器从第一次 start/pair/connect 就使用私有 socket，配对码仅走 stdin。外来或无法识别的 IPv4/IPv6 5037 监听是红色冲突，不得降级成跳过。

预检不满足不替换入口，doctor 明确显示原因和「ADB 未加固」警告；已加固状态检查失败则严格失败。候选版的连接身份与并行会话仍须完成验收，不能把预检跳过或有限测试宣传成加固可用。旧工具只在一台真机验证过，通用版本只在 arm64 模拟器验证；不同 ROM、重启、mDNS 与 x86_64 Android 尚需实机验收。

设备地址只是候选，不是身份。每个新连接经私有 filesystem 通道与 shell 跨 UID 挑战后，凭据绑定 server 代次、transport ID 和通道进程。通道只承载一次实际连接，断线后拒绝复用；失败的新连接断开、凭据失效。普通客户端共享执行锁；启动/清单/回滚独占。完整清单检查或回滚前应先结束长期虚拟屏会话；guard 无法取得独占锁时延期检查，不会写成健康。

冷启动独占锁内双检，多个首调共用一个 server。启动环境强制 `ADB_EMU=0`，关闭本地模拟器扫描，不把本机连接注册成模拟器别名。guard 只在完整核验 exe、socket、UID、starttime 与运行镜像后，借助 pidfd 终止重复受管 server 并起一个；未知进程或不支持 pidfd 的旧内核拒绝自动终止，需本人维护，不使用数值 PID 盲杀。

## 怎么回滚

```bash
# 原生 Termux 中执行，不在 Ubuntu 容器执行
bash ~/dsb-installer/install.sh check
bash ~/dsb-installer/install.sh rollback
```

切换自检失败会自动回滚。回滚校验本 UID server、监听 inode、IPv6 关闭、转发表及在线设备身份；冲突时保留现场并报告真实失败阶段，不保证仍在维护态，也不宣称已经恢复。成功回滚恢复普通 TCP，仅是较弱的可用性回退，不具有私有 socket 的原子地址隔离；App、Key、网页和任务桥不删除。

旧版 R6-0b journal/diversion 或未知格式在任何写操作前拒绝，提示「旧版部署，需 owner 在场迁移」；公开工具不能回滚该部署。不要删除 journal 绕过检查。

加固时 hold 两侧 adb 包，回滚恢复原 hold。升级前先回滚，升级后重跑安装器；强制包/库漂移需维护窗口人工核验，v1 不提供自动 rebase。维护态只暂停新 ADB 调用，不撤回已提交动作，也不停止网页、桥或 SSH：

```bash
bash ~/dsb-installer/install.sh maintenance
bash ~/dsb-installer/install.sh resume
```
