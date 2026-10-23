#!/data/data/com.termux/files/usr/bin/bash
# 看手机侧 dsh 状态（给 App/脚本/人看）
#
# adb 状态这一段 2026-09-22 经真重启实测后修正（第一版判据是错的）：
#   ❌ 旧判据"找不到 adbd 进程" → 从 Termux 的 ps 看不到别的命名空间，会误报"adbd未运行"
#   ✅ 正确判据：看 init.svc.adbd（adbd 本体）+ service.adb.tcp.port（有没有 TCP 监听）
#
# 实测事实：本 ROM **开机 adbd 是 running 的，但没有 TCP 监听**
#   · 5555 只在 USB 侧跑过 `adb tcpip 5555` 之后才存在，重启即失效
#   · adb_wifi_enabled 设成 1 也没用 —— 开机被系统重置，且 adbd 不按它开无线调试
#   → 重启后要恢复 adb，只能插一次 USB
PS=/system/bin/ps
P=/data/data/com.termux/files/usr/bin
GP=/system/bin/getprop

echo -n "3097="; "$P/curl" -s -m 4 http://127.0.0.1:3097/health | head -c 24
echo -n "  3080="; "$P/curl" -s -m 4 -o /dev/null -w '%{http_code}' http://127.0.0.1:3080/
echo -n "  3096="; "$P/curl" -s -m 4 http://127.0.0.1:3096/health | head -c 24
echo
N=$($PS -A -o PID,ARGS | grep -E 'dsh web|bridge\.mjs|url-server' | grep -v grep | wc -l)
SUP=$($PS -A -o PID,ARGS | grep -E 'wx/supervisor\.sh' | grep -v grep | wc -l)
echo "dsh相关进程=$N  看门狗=$SUP"

# ---- adb 状态 ----
ADBD=$("$GP" init.svc.adbd 2>/dev/null)
TCPPORT=$("$GP" service.adb.tcp.port 2>/dev/null)
if [ -n "$TCPPORT" ]; then
  echo "adb=UP(adbd=$ADBD, TCP端口=$TCPPORT)  → adb connect 127.0.0.1:$TCPPORT"
else
  echo "adb=DOWN(adbd=$ADBD, 无TCP监听) ← 本 ROM 重启后 adbd 无 TCP 端口；"
  echo "     恢复方式：插一次 USB 跑 'adb tcpip 5555'（无线调试无效：adb_wifi_enabled 开机被重置）"
  echo "     不影响 App 自更新（走 HTTP 3097，不需要 adb）"
fi
