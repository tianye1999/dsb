package com.dsb.assistant;

import android.app.Notification;
import android.os.Bundle;
import android.util.Log;
import android.service.notification.NotificationListenerService;
import android.service.notification.StatusBarNotification;

import java.text.SimpleDateFormat;
import java.util.ArrayList;
import java.util.Date;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;

/** 通用通知采集：给 DSH 提供设备上下文（任何 App 的通知一视同仁）。需要「通知使用权」。 */
public class NotificationWatcher extends NotificationListenerService {
    public static volatile boolean connected = false;
    private static final LinkedHashMap<String, String[]> RECENT = new LinkedHashMap<>();

    @Override public void onListenerConnected() {
        connected = true;
        Log.i("DSH", "onListenerConnected");
        seedFromActive();
    }

    @Override public void onListenerDisconnected() { connected = false; }

    /** 授权瞬间，把通知栏里已经存在的通知也吃进来 */
    private void seedFromActive() {
        try {
            StatusBarNotification[] arr = getActiveNotifications();
            if (arr == null) return;
            for (StatusBarNotification sbn : arr) record(sbn);
        } catch (Throwable ignored) {}
    }

    @Override public void onNotificationPosted(StatusBarNotification sbn) {
        Log.i("DSH", "notification posted");
        record(sbn);
    }

    private void record(StatusBarNotification sbn) {
        if (sbn == null || sbn.getNotification() == null) return;
        Bundle ex = sbn.getNotification().extras;
        if (ex == null) return;
        String title = str(ex.getCharSequence(Notification.EXTRA_TITLE));
        String text = str(ex.getCharSequence(Notification.EXTRA_TEXT));
        if (text.isEmpty()) text = str(ex.getCharSequence(Notification.EXTRA_BIG_TEXT));
        if (text.isEmpty()) text = str(ex.getCharSequence(Notification.EXTRA_SUMMARY_TEXT));
        String sub = str(ex.getCharSequence(Notification.EXTRA_SUB_TEXT));
        if (!sub.isEmpty() && !text.contains(sub)) text = text + " " + sub;
        if (title.isEmpty() && text.isEmpty()) return;
        String pkg = sbn.getPackageName();
        String time = new SimpleDateFormat("HH:mm", Locale.getDefault()).format(new Date(sbn.getPostTime()));
        synchronized (RECENT) {
            String key = pkg + "|" + title;
            RECENT.remove(key);
            RECENT.put(key, new String[]{pkg, title, text, time});
            while (RECENT.size() > 80) {
                String k = RECENT.keySet().iterator().next();
                RECENT.remove(k);
            }
        }
        // Tier-2 事件驱动盯梢（2026-10-01）：通知 → 落盘 notifications.jsonl
        //（getExternalFilesDir，手机侧 adb shell cat 读，同 look.json 通道）。
        // 后台线程写 + 异常全吞——绝不影响 NotificationListenerService 稳定性。
        final String fPkg = pkg, fTitle = title, fText = text;
        final long postTs = sbn.getPostTime();
        new Thread(new Runnable() { @Override public void run() {
            try {
                java.io.File dir = getExternalFilesDir(null);
                if (dir == null) return;
                java.io.File nf = new java.io.File(dir, "notifications.jsonl");
                java.util.ArrayList<String> lines = new java.util.ArrayList<>();
                if (nf.exists()) {
                    java.io.BufferedReader br = new java.io.BufferedReader(
                            new java.io.InputStreamReader(new java.io.FileInputStream(nf), "UTF-8"));
                    String ln;
                    while ((ln = br.readLine()) != null) {
                        if (ln.trim().length() > 0) lines.add(ln);
                    }
                    br.close();
                }
                String eTitle = fTitle.replace("\\", "\\\\").replace("\"", "\\\"");
                String eText = fText.replace("\\", "\\\\").replace("\"", "\\\"");
                String ePkg = fPkg.replace("\"", "");
                lines.add("{\"ts\":" + postTs + ",\"pkg\":\"" + ePkg
                        + "\",\"title\":\"" + eTitle + "\",\"text\":\"" + eText + "\"}");
                while (lines.size() > 240) lines.remove(0);   // 滚动裁剪：留最近 ~200 条 + 余量
                java.io.PrintWriter pw = new java.io.PrintWriter(
                        new java.io.OutputStreamWriter(new java.io.FileOutputStream(nf), "UTF-8"));
                for (String l : lines) pw.println(l);
                pw.close();
            } catch (Throwable ignored) {}
        }}, "notif-disk").start();
    }

    private static String str(CharSequence cs) { return cs == null ? "" : cs.toString().trim(); }

    public static int size() { synchronized (RECENT) { return RECENT.size(); } }

    /** 最近的在前 */
    public static List<String[]> snapshot() {
        List<String[]> out = new ArrayList<>();
        synchronized (RECENT) {
            List<String[]> all = new ArrayList<>(RECENT.values());
            for (int i = all.size() - 1; i >= 0; i--) out.add(all.get(i));
        }
        return out;
    }
}
