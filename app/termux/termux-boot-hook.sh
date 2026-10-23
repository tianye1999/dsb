#!/data/data/com.termux/files/usr/bin/bash
# Termux:Boot 开机钩子：App 万一被系统拦住，这里也能把手机侧服务拉起来
setsid nohup /data/data/com.termux/files/home/dsb/boot.sh >>/data/data/com.termux/files/home/dsb/boot.log 2>&1 </dev/null &
