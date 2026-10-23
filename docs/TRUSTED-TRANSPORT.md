# App 与 Termux 的大负载传输

## 是什么

任务仍由 Termux 官方 RUN_COMMAND 接收小型固定元数据。请求与回复保留在 App 私有 spool；Termux 经私有 ADB 服务访问受 INJECT_EVENTS 接收权限保护的 CmdReceiver，随后只访问 Termux 私有 Unix 套接字。不新增 TCP 写入口、ContentProvider 或 URI 授权。

两个方向分别限长：READ 最多 64 KiB 原始字节，通过广播 result data 返回；WRITE 最多 32 KiB 原始字节，通过 base64 命令参数送入。READ 必须显式请求长度并收到恰好对应的字节数，短读或超长立即拒绝，不自动减小块或重试任务。缺少 length 的旧调用仍读 12 KiB，便于旧接收脚本与新 App 共存。

完整请求先核对长度、SHA256 和 JSON 路由，然后再做一次零负载 READ 检查 nonce/有效期，才允许执行。回复仍按长度、摘要和偏移收齐后原子发布；超时不等于停止确认，任务账本、task_id 幂等及停止回执语义不变。

## 为什么

12 KiB 对称分块把每次 shell/广播启动的固定成本重复太多次。广播不是通用大文件总线；但在约 1 MiB 请求、有限小回复的目标范围，固定小块仍能避免新增导出组件。

| 路线 | 普通 App 能否取得内容 | 必须成立的条件与风险 | 1 MiB 请求的开销预估 |
| --- | --- | --- | --- |
| READ 64 KiB / WRITE 32 KiB，保留受保护广播 | 不持有 INJECT_EVENTS 不能访问接收器；知道 request_id 也仍需 nonce。root、系统、持该权限的平台签名 App 仍在信任边界内 | 大 result data 必须完整；WRITE 的完整命令须低于设备参数上限；多个在途 Binder 事务共享缓冲，不能把单次上限当并发保证 | 16 次 READ + 1 次有效期检查 + 小回复 1 次 WRITE，共 18 次；每调用 60 ms 时约 1.08 秒固定成本，不含启动/调度。实际目标仍需设备验证 |
| 仅 shell UID 的 ContentProvider，以 content read/write 流式传输 | 必须在 Binder 入口核对实际调用 UID=2000，禁 URI grants，不信 URI 自报身份。普通 App、root/system 均不因路径已知而放行 | FD/pipe 可避开大 Binder 参数，但新增导出入口；必须限制 URI/模式/长度、关闭超时 FD、验证摘要并原子提交。CLI 写成功退出并不证明后台 pipe 已收齐；跨线程前须保存经验证的 UID，不能在线程中重新读取调用者身份 | 请求和回复各一次流式命令，加一次执行前存活检查，常数 3 次；预计固定成本低于广播分块，但依赖 OEM content 命令和 pipe 实测 |
| 把完整请求放 RUN_COMMAND stdin，或放公共文件再通知 | 前者仍受 RUN_COMMAND 权限保护；后者普通 App 可能读取/替换，不能作为明文替代 | stdin 是 Intent 字符串，并不是跨 UID 的私有文件流；大 JSON 仍进入 Binder。公共文件需要额外加密、认证和生命周期协议，或不再满足原边界 | 不能可靠处理约 1 MiB 的 base64 截图；不是本轮备选 |

采用第一条，不以调高到 Binder 极限为目标。32 KiB WRITE 的 base64 是 43,692 个字符，完整 argv 另做 56 KiB 上界断言；64 KiB READ 的 base64 是 87,384 个字符，不作为 adb 命令参数。Android 文档说明 Binder 的约 1 MiB 缓冲由进程内在途事务共享，返回值也可能因缓冲不足失败，因此不得把大返回视为无限制。[TransactionTooLargeException](https://developer.android.com/reference/android/os/TransactionTooLargeException)

ContentProvider 的只读/只写 FD 可以是 pipe；其调用者身份应从真实 Binder UID 取得。它是后续超出当前负载范围时的候选，不在本版注册生产入口。[ContentProvider.openFile](https://developer.android.com/reference/android/content/ContentProvider#openFile(android.net.Uri,%20java.lang.String))、[Binder.getCallingUid](https://developer.android.com/reference/android/os/Binder#getCallingUid())

现路线依赖以下边界，任何一条不成立均应拒绝而非降级到 HTTP：RUN_COMMAND 的权限与 allow-external-apps 已正确配置；ADB_SERVER_SOCKET 固定到私有文件套接字、目标身份是 shell；Manifest 的接收权限未被移除；am 有序广播返回格式完整；spool 的 nonce、长度、有效期及摘要校验仍成功。App 被强停、Termux/ADB 不可用、ROM 截断结果或 Binder 并发不足，都可能使通道失败，不能据此声称任务未执行。

本边界不防 root、已授权 ADB、持 INJECT_EVENTS 的平台代码、Termux 同 UID/proot 内的恶意代码，或已获用户 RUN_COMMAND 权限的其它 App 利用 Termux 执行任意命令。私有 ADB 套接字防普通本机 App 抢连，不把所有可能获得 shell 的来源变为 DSB 专属。

## 怎么验证

- `python3 termux/test-trusted-entry.py` 验证实际读写函数、精确分块、摘要、固定命令和调用次数。1 MiB JSON 请求加小回复最多 18 次广播；1,224,867 B 请求最多 21 次；1 MiB 图片经 base64 包装并附至多 1 KiB JSON 元数据，最多 24 次。回复超过 32 KiB 时另按 ceil(reply_bytes / 32768) 计数，不伪称全尺寸回复也仅一次。
- `node app/tests/run-trusted-files-tests.mjs` 编译并执行真实 TrustedFiles，验证 64 KiB READ、旧 12 KiB 默认、长度/nonce/有效期拒绝、32 KiB WRITE、重放一致性和原子发布。
- 在独立临时模拟器测试 result data 的完整长度与摘要、WRITE 参数边界及 content 二进制读写/普通 UID 拒绝；探针不打进产品 APK，模拟器结论不能冒充 OEM 真机上限。
- 设备窗口仍以 /ask、/step 各 2 次预热和 10 次正式样本验收：median <= 4 秒、p95 <= 8 秒、单次 <= 10 秒且无错误。失败回滚，不临时加权限、开 HTTP 或放宽门槛。
- 真机结果（Android 16 开发验证机，约 1.2 MB 请求）：12 KiB 对称分块时 median 约 6.0 秒（不达标、已回滚）；本版 /ask median 1.49 秒、p95 1.59 秒，/step median 1.52 秒、p95 1.63 秒。
