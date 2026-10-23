#!/data/data/com.termux/files/usr/bin/bash
# 真实就绪探测：客户端插件 combo 包必须能 200 才算就绪。
#   exit 0 = 已就绪（可以安全交给 App 加载）
#   exit 1 = 还没就绪（认证路由通了但插件注册表没装完 → 此时加载会白屏）
#   exit 2 = 参数缺失
# 用法: ready-probe.sh "http://127.0.0.1:3080/?token=..."
#
# 背景（2026-09-25）：dsh 先监听端口、后增量装载插件树。GET /?token= 返回 303
# 只证明认证/首页路由挂上了，dsh-client-modules 的插件注册表可能还是空的，
# 此时 WebView 加载页面会让插件 combo 请求 404 → 页面只剩空壳。
P=/data/data/com.termux/files/usr/bin
H=/data/data/com.termux/files/home
URL="$1"
[ -z "$URL" ] && exit 2

JAR="$H/dsb/.rp-jar.$$"
IDX="$H/dsb/.rp-idx.$$"
cleanup() { rm -f "$JAR" "$IDX"; }
trap cleanup EXIT

# 1) 用 token 换 cookie，跟随 303 拿到首页
"$P/curl" -s -L -c "$JAR" -b "$JAR" -o "$IDX" -m 8 "$URL" || exit 1
[ -s "$IDX" ] || exit 1

# 2) 取第一个插件 combo 包（boot 载荷里的 "url":"plugins/??..."，兼容 HTML 属性形式）
PU="$("$P/grep" -oE 'plugins/\?\?[^"]+client\.js[^"]*' "$IDX" 2>/dev/null | "$P/head" -1 | "$P/sed" 's/&amp;/\&/g')"
[ -z "$PU" ] && exit 1

# 3) 该包必须真的能 200
C="$("$P/curl" -s -b "$JAR" -o /dev/null -m 8 -w '%{http_code}' "http://127.0.0.1:3080/$PU")"
[ "$C" = "200" ] && exit 0
exit 1
