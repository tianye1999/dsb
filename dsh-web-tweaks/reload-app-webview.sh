#!/data/data/com.termux/files/usr/bin/bash
# 让「逗沙包」App 的 WebView 重新加载页面（改完网页微调后必须跑一次）
#
# 为什么需要：DSH 是单页应用，App 的 WebView 不会自己重新拉取 index.html。
#   改完 index.html 后：
#     · Chrome 里 → 刷新页面就生效
#     · App 里    → **不会生效**，WebView 还跑着旧脚本（实测踩到：
#                    App 里 dsh-composer-gesture 是旧版 1685 字节，
#                    Chrome 里已是新版 3248 字节）
#
# ⚠️ WebView 在独立进程 com.dsb.assistant:web（不是主进程），pidof 必须带 :web（2026-09-25 修）
# 仅供可调试构建：MainActivity 按 FLAG_DEBUGGABLE 开启 WebView 调试，
#   才有 @webview_devtools_remote_<pid> socket，可以通过 CDP 发 Page.reload。
#   公开发布构建默认关闭调试口，此脚本不可用于该构建。
set -u
if [ -d "${PREFIX:-/data/data/com.termux/files/usr}/var/lib/dsb-adb" ]; then
  echo '拒绝 reload：私有 ADB 已部署；adb forward tcp: 会让 ADB 自持 TCP 监听，策略将判为冲突并暂停 ADB。此调试工具不支持私有传输。' >&2
  exit 2
fi
source /data/data/com.termux/files/home/dsb/adb-target.sh || exit $?
PORT=9334
PID=$("${DSB_ADB_COMMAND[@]}" "${DSB_ADB_SELECTOR[@]}" shell pidof com.dsb.assistant:web 2>/dev/null | tr -d '\r')
if [ -z "$PID" ]; then echo "✗ 逗沙包 App 没在跑"; exit 1; fi
"${DSB_ADB_COMMAND[@]}" "${DSB_ADB_SELECTOR[@]}" forward "tcp:$PORT" "localabstract:webview_devtools_remote_$PID" >/dev/null 2>&1 || {
  echo '✗ ADB forward 失败，未尝试 reload' >&2
  exit 1
}
node - <<'JS' 2>/dev/null || echo "✗ reload 失败（node 或 CDP 不可用）"
const port = process.env.PORT || 9334;
const list = await (await fetch(`http://127.0.0.1:${port}/json`, {signal:AbortSignal.timeout(8000)})).json();
const p = list.find(t => t.type === 'page');
if (!p) { console.log('✗ 没找到 WebView 页面'); process.exit(1); }
const ws = new WebSocket(p.webSocketDebuggerUrl);
await new Promise((res, rej) => { ws.addEventListener('open', res); ws.addEventListener('error', rej); setTimeout(() => rej(new Error('t')), 6000); });
ws.send(JSON.stringify({id: 1, method: 'Page.reload', params: {ignoreCache: true}}));
await new Promise(r => setTimeout(r, 1500));
ws.close();
console.log('✓ 已让 App 的 WebView 重新加载页面');
JS
