#!/data/data/com.termux/files/usr/bin/bash
# 开发用：干净重启 3097 上的 URL 小服务（杀掉旧实例再起新的）
P=/data/data/com.termux/files/usr/bin
H=/data/data/com.termux/files/home
ps -A -o PID,ARGS | grep 'url-server\.py' | grep -v grep | awk '{print $1}' | while read pid; do kill -9 "$pid" 2>/dev/null; done
sleep 1
setsid nohup "$P/python3" "$H/dsb/url-server.py" >>"$H/dsb/url-server.log" 2>&1 </dev/null &
sleep 3
echo -n "health: "; "$P/curl" -s -m 10 http://127.0.0.1:3097/health; echo
echo -n "dsh-url: "; "$P/curl" -s -m 15 http://127.0.0.1:3097/dsh-url; echo
