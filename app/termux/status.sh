#!/data/data/com.termux/files/usr/bin/bash
# 看手机侧 dsh 状态（给 App/脚本/人看）
PS=/system/bin/ps
P=/data/data/com.termux/files/usr/bin
echo -n "3097="; "$P/curl" -s -m 4 http://127.0.0.1:3097/health | head -c 24
echo -n "  3080="; "$P/curl" -s -m 4 -o /dev/null -w '%{http_code}' http://127.0.0.1:3080/
echo -n "  3096="; "$P/curl" -s -m 4 http://127.0.0.1:3096/health | head -c 24
echo
N=$($PS -A -o PID,ARGS | grep -E 'dsh web|bridge\.mjs|url-server' | grep -v grep | wc -l)
SUP=$($PS -A -o PID,ARGS | grep -E 'wx/supervisor\.sh' | grep -v grep | wc -l)
echo "dsh相关进程=$N  看门狗=$SUP"
