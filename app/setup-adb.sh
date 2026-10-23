#!/bin/bash
# DSB App 手动安装和一次性授权；WRITE_SECURE_SETTINGS 授权可选自愈，不直接写 settings。
set -u
ADB=${ADB:-$HOME/Library/Android/sdk/platform-tools/adb}
PKG=com.dsb.assistant
APK=${1:-$(dirname "$0")/DSB-1.0.apk}

echo "== 安装 =="
$ADB install -r -g "$APK" | tail -1

echo "== 悬浮窗 =="
$ADB shell appops set $PKG SYSTEM_ALERT_WINDOW allow

echo "== 运行时权限 =="
$ADB shell pm grant $PKG android.permission.POST_NOTIFICATIONS 2>/dev/null || true
$ADB shell pm grant $PKG android.permission.RECORD_AUDIO 2>/dev/null || true
echo "== 可选无障碍自愈：用户主动执行本脚本才授予开发权限 =="
$ADB shell pm grant $PKG android.permission.WRITE_SECURE_SETTINGS 2>/dev/null || true
echo "== 无障碍：请到系统设置 → 无障碍，手动开启「逗沙包」 =="

echo "== 通知使用权 =="
$ADB shell cmd notification allow_listener $PKG/$PKG.NotificationWatcher | tail -1

echo "== 电池白名单 =="
$ADB shell dumpsys deviceidle whitelist +$PKG | tail -1

echo "== 结果 =="
$ADB shell "dumpsys package $PKG | grep -E 'RUN_COMMAND|POST_NOTIFICATIONS' | head -5"
$ADB shell "settings get secure enabled_accessibility_services; settings get secure enabled_notification_listeners"
