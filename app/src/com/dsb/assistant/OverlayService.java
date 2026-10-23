package com.dsb.assistant;

import android.Manifest;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.graphics.Bitmap;
import android.graphics.Color;
import android.graphics.PixelFormat;
import android.graphics.drawable.GradientDrawable;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.speech.RecognitionListener;
import android.speech.RecognizerIntent;
import android.speech.SpeechRecognizer;
import android.util.Base64;
import android.util.Log;
import android.view.Gravity;
import android.view.MotionEvent;
import android.view.View;
import android.view.WindowManager;
import android.view.accessibility.AccessibilityNodeInfo;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;
import android.widget.Toast;

import org.json.JSONObject;
import org.json.JSONArray;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileOutputStream;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;

/**
 * 通用手机代理的「手和眼」。
 *
 * 设计原则（重要）：
 *   本 App 不含任何 App 专用逻辑：没有深链、没有某个 App 的技能、没有包名白名单。
 *   它只提供两件通用能力：
 *     1) 感知：截屏 + 无障碍元素列表 + 前台包名 + 通知摘要
 *     2) 执行：tap / longpress / swipe / type / open_app / back / home / recents / wait
 *   所有「该做什么」的决定都交给 DSH（经 dsh-bridge），本 App 不猜、不硬编码。
 */
public class OverlayService extends Service {
    public static final String ACTION_CMD = "com.dsb.assistant.RUN";
    /** 兼容老 adb 脚本/老版本的 action 字符串 */
    public static final String ACTION_CMD_LEGACY = "com.dsh.assistant.RUN";
    public static final String EXTRA_CMD = "cmd";
    public static final String TOKEN = "dsh-8f3a2c";   // 兼容老脚本的命令参数，不是秘密；鉴权靠 CmdReceiver 的 INJECT_EVENTS 接收权限
    public static final int MAX_STEPS = 12;            // 一个目标最多走多少步

    /** 网页界面 ↔ 蓝色小球：服务要"操作别的 App"时通知网页收起来 */
    public static final String ACTION_COLLAPSE = "com.dsb.assistant.COLLAPSE";
    public static final String ACTION_EXPAND = "com.dsb.assistant.EXPAND";
    /** 小球直径（dp）。2026-09-22 晚：用户要求"半径减半、透明度尽量提升"——
     *  40dp → 20dp（半径 20 → 10），配 0.5 透明度，几乎不挡界面但还看得见、点得到。 */
    public static final int BUBBLE_DP = 20;

    static volatile OverlayService INSTANCE;

    /** 到手机侧 3098 的"存活连接"（见 aliveTask） */
    private java.net.Socket aliveSock;

    /**
     * 保持一条到 127.0.0.1:3098 的长连接 —— 这是"App 还活着"的信号。
     * 本进程一死（用户在最近任务里划掉 = 系统 3ms 内 SIGKILL），连接立刻断，
     * 手机侧据此马上执行 shutdown.sh 把 dsh web / 桥 / URL 服务 / 看门狗都停掉。
     *
     * 为什么不直接在 onTaskRemoved 里发命令：实测那条路根本来不及（进程 3 毫秒就没了）。
     */
    private final Runnable aliveTask = new Runnable() {
        @Override public void run() {
            new Thread(() -> {
                try {
                    if (aliveSock == null || aliveSock.isClosed() || !aliveSock.isConnected()) {
                        java.net.Socket s = new java.net.Socket();
                        s.connect(new java.net.InetSocketAddress("127.0.0.1", 3098), 1500);
                        s.setKeepAlive(true);
                        aliveSock = s;
                        Log.i("DSH", "存活连接已建立（3098）");
                    }
                } catch (Throwable t) {
                    try { if (aliveSock != null) aliveSock.close(); } catch (Throwable ignored) {}
                    aliveSock = null;
                }
            }, "dsb-alive").start();
            main.postDelayed(this, 10000);
        }
    };
    /** 用户划掉任务后置 true：避免 START_STICKY 把服务又粘回来 */
    private volatile boolean quitting;

    /**
     * "用户已经把 App 划掉"的标记。用**文件**而不是 SharedPreferences：
     * 划掉时系统会杀掉进程，随后又按 START_STICKY 把服务拉起来（新进程，
     * 内存里的标志早没了）——只有落盘的东西能跨进程/跨重启活下来。
     * 用户重新打开 App 或手机重启时清掉它（那两个场景球应该回来）。
     */
    static java.io.File closedFlag(android.content.Context c) {
        return new java.io.File(c.getFilesDir(), "user_closed.flag");
    }

    /** 系统开机计数（读全局设置即可，不需要权限；读不到返回 -1） */
    static int bootCount(android.content.Context c) {
        try {
            return android.provider.Settings.Global.getInt(c.getContentResolver(),
                    android.provider.Settings.Global.BOOT_COUNT);
        } catch (Throwable t) { return -1; }
    }

    /** 标记"用户已关闭"，并记下当时是第几次开机（用来分辨真开机 vs ROM 补发） */
    static void markUserClosed(android.content.Context c) {
        try {
            java.io.FileOutputStream fo = new java.io.FileOutputStream(closedFlag(c));
            fo.write(String.valueOf(bootCount(c)).getBytes("UTF-8"));
            fo.close();
        } catch (Throwable t) { Log.i("DSH", "写关闭标记失败：" + t.getClass().getSimpleName()); }
    }

    /** 读回标记里的开机计数（空/读不到返回 -1） */
    static int closedFlagBootCount(android.content.Context c) {
        try {
            java.io.File f = closedFlag(c);
            if (!f.exists()) return -1;
            java.io.BufferedReader br = new java.io.BufferedReader(new java.io.InputStreamReader(new java.io.FileInputStream(f), "UTF-8"));
            String s = br.readLine();
            br.close();
            return s == null || s.trim().isEmpty() ? -1 : Integer.parseInt(s.trim());
        } catch (Throwable t) { return -1; }
    }

    /**
     * "App 正在运行"的落盘标记：内容是**当前的开机计数**。
     *
     * 为什么需要它（2026-09-22 实测的 bug）：用户从最近任务划掉 App 后，
     * ① 系统 3ms 内 SIGKILL 进程，onTaskRemoved 常常来不及把 user_closed.flag 写完；
     * ② 部分 ROM 紧接着补发一次 BOOT_COMPLETED（开发验证机实测：bootCount 没变、uptime 5 小时），
     * BootReceiver 因为读不到关闭标记，把假开机当成真开机 → 球和 dsh 全被拉回来。
     * 有了这个标记，判断"是不是同一次开机会话"不再依赖那个抢写失败的 flag。
     */
    static java.io.File runMark(android.content.Context c) {
        return new java.io.File(c.getFilesDir(), "app_run_boot.count");
    }

    /** App 每次起来（服务 onCreate / 主界面 onCreate）都写一次：bootCount + 启动时刻(ms) */
    static void markAppRunning(android.content.Context c) {
        try {
            java.io.FileOutputStream fo = new java.io.FileOutputStream(runMark(c));
            fo.write((bootCount(c) + " " + android.os.SystemClock.elapsedRealtime()).getBytes("UTF-8"));
            fo.close();
        } catch (Throwable t) { Log.i("DSH", "写运行标记失败：" + t.getClass().getSimpleName()); }
    }

    /** 读运行标记的两个字段；返回 {bootCount, elapsedMs}，读不到返回 null */
    static long[] readRunMark(android.content.Context c) {
        try {
            java.io.File f = runMark(c);
            if (!f.exists()) return null;
            java.io.BufferedReader br = new java.io.BufferedReader(new java.io.InputStreamReader(new java.io.FileInputStream(f), "UTF-8"));
            String s = br.readLine();
            br.close();
            if (s == null || s.trim().isEmpty()) return null;
            String[] p = s.trim().split("\\s+");
            long b = Long.parseLong(p[0]);
            long t = p.length > 1 ? Long.parseLong(p[1]) : -1;
            return new long[]{b, t};
        } catch (Throwable t) { return null; }
    }

    /** App 上次运行时的开机计数（没运行过返回 -1） */
    static int appRunBootCount(android.content.Context c) {
        long[] r = readRunMark(c);
        return r == null ? -1 : (int) r[0];
    }

    /** App 上次起来的时刻（SystemClock.elapsedRealtime()，读不到返回 -1） */
    static long appRunElapsedMs(android.content.Context c) {
        long[] r = readRunMark(c);
        return r == null ? -1 : r[1];
    }

    private WindowManager wm;
    private TextView bubble;
    private LinearLayout panel;
    private TextView log;
    private ScrollView logScroll;
    private LinearLayout voiceBox;
    private EditText voiceInput;
    private boolean voiceShown;
    private TextView statusView;
    private String lastStatusText = "待命";
    private Button stopButton;
    private ScrollView logBox;
    private Button logToggleBtn;
    private boolean logExpanded;
    private LinearLayout confirmBar;
    private boolean confirmShown;
    private WindowManager.LayoutParams bubbleLp;
    private boolean panelShown;
    // 2026-09-20 删：全屏"锁屏"浮层。可触摸的浮层会把 adb 注入的点击也一起吃掉，
    // 与"我能继续操作"互斥（原理上必然二选一）。
    // 独占信号改为悬浮球红绿灯：橙=忙（勿碰）、蓝=闲，见 setBubbleBusy()。
    private volatile boolean bubbleBusy;
    /** 橙球看门狗：这么久没有「橙」心跳就自动回蓝（用户要求"停手后立刻变蓝"） */
    private static final long BUSY_WATCHDOG_MS = 20000L;

    private final Runnable busyWatchdog = new Runnable() {
        @Override public void run() {
            if (agentRunning || taskWorkerRunning || cancelInFlight || cancelUnconfirmed) {
                main.postDelayed(this, BUSY_WATCHDOG_MS);
                return;
            }
            if (bubbleBusy) { setBubbleBusy(false); log("⏱ 橙色看门狗超时（>20s 无心跳），已自动回蓝"); }
        }
    };
    private SpeechRecognizer asr;

    private final Handler main = new Handler(Looper.getMainLooper());
    private final ExecutorService exec = Executors.newSingleThreadExecutor();
    private final Object taskLock = new Object();
    private volatile boolean agentRunning;
    // P1 修1（2026-10-01）：停止必须即时——不再排 exec 队列；代数号让迟到结果作废。
    private volatile boolean taskCancelled = false;
    private volatile long taskGeneration = 0;
    private volatile boolean taskWorkerRunning, cancelInFlight, cancelUnconfirmed;
    private static final java.util.concurrent.atomic.AtomicInteger ACTIVE_WORKERS = new java.util.concurrent.atomic.AtomicInteger();
    private static final java.util.concurrent.atomic.AtomicInteger ACTIVE_CANCELS = new java.util.concurrent.atomic.AtomicInteger();
    private TaskRecoveryStore recoveryStore;
    private String taskRecoveryId = "";
    private boolean taskTransportSettled;
    private long workerGeneration;
    private Thread taskThread;
    private String taskBridgeBase;
    private volatile String lastCmd = "";
    private long createdAt;
    private String pendingDecision;
    private final Object decisionLock = new Object();

    /** 把网页界面收起来（App 变身为蓝色小球）：广播给 MainActivity */
    private void collapseWebUi() {
        try { sendBroadcast(new Intent(ACTION_COLLAPSE).setPackage(getPackageName())); } catch (Throwable ignored) {}
    }

    /** 把网页界面拉回前台（小球变回网页形态） */
    void openWebUi() {
        try {
            showPanel(false);
            Intent i = new Intent(this, MainActivity.class);
            i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_REORDER_TO_FRONT);
            i.putExtra("expand", true);
            startActivity(i);
        } catch (Throwable t) { log("❌ 打开网页界面失败：" + t); }
    }

    /** 当前前台应用的包名（拿不到就返回 null）。 */
    // Deliberately retain the own-task fallback when accessibility has no foreground package.
    @SuppressWarnings("deprecation")
    private String topPackage() {
        // 首选：无障碍服务记录的当前窗口包名（全系统可见，无需权限）
        // 次选：ActivityManager.getRunningTasks —— 但 Android 5+ 对第三方应用
        //       只返回自己的任务，拿不到别的 App（实测一直 null）
        try {
            String p = AgentAccessibilityService.lastTopPackage;
            if (p != null && !p.isEmpty()) return p;
        } catch (Throwable ignored) {}
        try {
            android.app.ActivityManager am =
                    (android.app.ActivityManager) getSystemService(ACTIVITY_SERVICE);
            java.util.List<android.app.ActivityManager.RunningTaskInfo> ts =
                    am.getRunningTasks(1);
            if (ts != null && !ts.isEmpty()) {
                android.content.ComponentName cn = ts.get(0).topActivity;
                if (cn != null) return cn.getPackageName();
            }
        } catch (Throwable ignored) {}
        return null;
    }

    /** 判断"要不要在朗读结束后把界面带回逗沙包"。 */
    private boolean shouldReturnAfterTts() {
        if (!ttsAutoReturn) return false;
        String top = topPackage();
        if (top == null) return false;                  // 判不出前台就不动，避免误抢屏
        if (top.equals(getPackageName())) return false; // 已在 DSB
        // 注：曾想用 PowerManager.getLastUserActivityTime() 判断"用户是否刚碰过屏幕"，
        //     但它是 @hide 方法（编译不过）。改用无需新权限的等效思路：
        //     只要前台**不是**逗沙包，就认为用户在看别处 → 朗读结束带回来。
        //     （用户明确要这个行为："如果我在其他 APP 界面的话"，故不加更多迟疑条件。）
        return true;
    }

    /** 朗读结束后回到逗沙包界面。 */
    private void returnToDsb() {
        try {
            String top = topPackage();
            if (top != null && top.equals(getPackageName())) return;
            if (!shouldReturnAfterTts()) {
                log("（朗读结束；检测到你在操作或判不出前台，不切界面）");
                return;
            }
            log("🔙 朗读结束，回到逗沙包界面");
            openWebUi();
        } catch (Throwable t) {
            log("❌ 回逗沙包失败：" + t);
        }
    }

    /** TtsSpeaker 在朗读结束（或出错）时回调这里。 */
    void onTtsFinished(boolean ok) {
        if (!ttsPendingReturn) return;      // 只对自动朗读生效
        ttsPendingReturn = false;
        if (ok) returnToDsb();
    }

    public static void dispatch(android.content.Context ctx, String cmd) {
        OverlayService s = INSTANCE;
        if (s != null) { s.handleCommand(cmd); return; }
        Intent i = new Intent(ctx, OverlayService.class).setAction(ACTION_CMD).putExtra(EXTRA_CMD, cmd);
        if (Build.VERSION.SDK_INT >= 26) ctx.startForegroundService(i); else ctx.startService(i);
    }

    private int dp(float v) { return (int) (v * getResources().getDisplayMetrics().density); }

    // ── 朗读结束后自动回到逗沙包（2026-09-23 用户要求）──────────────────────
    // 用户："当任务完成、弹出卡片、TTS 朗读结束后，你要把手机界面返回到 DSB APP，
    //        如果我在其他 APP 界面的话。"
    // 保护：① 已在 DSB 前台就不动；② 用户 3 秒内碰过屏幕就不抢（别打断正在操作的人）；
    //       ③ 只对**自动朗读**生效 —— 手动发「朗读」命令时不跳（那会打断用户自己看）。
    private boolean ttsAutoReturn = true;
    private boolean ttsPendingReturn = false;

    String bridgeUrl() {
        return "termux://dsb-v1";
    }

    /** 读上次记住的球位置（dp）。2026-09-20 加：用户要求"默认放屏幕中间，不放左边"，
     *  并且挪过之后要记住——否则每次服务重启又跳回默认点。 */
    private int[] loadBubblePos() {
        SharedPreferences sp = getSharedPreferences("dsh", MODE_PRIVATE);
        return new int[]{ sp.getInt("bubble_x", 180), sp.getInt("bubble_y", 320) };
    }

    private void saveBubblePos() {
        try {
            float d = getResources().getDisplayMetrics().density;
            getSharedPreferences("dsh", MODE_PRIVATE).edit()
                    .putInt("bubble_x", Math.round(bubbleLp.x / d))
                    .putInt("bubble_y", Math.round(bubbleLp.y / d))
                    .apply();
        } catch (Throwable ignored) {}
    }

    // Retain the existing notification-removal path when a user-closed service restarts.
    @SuppressWarnings("deprecation")
    @Override public void onCreate() {
        super.onCreate();
        markAppRunning(this);   // 记下"App 在这次开机会话里跑过"（分辨 ROM 补发的假开机）
        // 用户划掉过 App：这次是被 START_STICKY 拉起来的，别再冒头
        if (closedFlag(this).exists()) {
            Log.i("DSH", "🚪 已被用户关闭过：不显示小球（打开 App 或重启手机后恢复）");
            startForeground(1, buildNotification());   // 先满足前台服务契约，再立刻收掉
            try { stopForeground(true); } catch (Throwable ignored) {}
            stopSelf();
            return;
        }
        INSTANCE = this;
        restoreTaskRecovery();
        createdAt = System.currentTimeMillis();
        wm = (WindowManager) getSystemService(WINDOW_SERVICE);
        startForeground(1, buildNotification());
        ensureAccessibility(this);
        new Thread(() -> ensureAccessibilityReady(this, 25000), "dsb-accessibility-ready").start();
        addBubble();
        TermuxRunner.boot(this);    // 球一出现就确保手机侧服务在跑
        main.post(aliveTask);       // 并维持"我还活着"的长连接
    }

    @Override public int onStartCommand(Intent intent, int flags, int startId) {
        if (closedFlag(this).exists()) { stopSelf(); return START_NOT_STICKY; }
        if (intent != null && ACTION_CMD.equals(intent.getAction())) {
            final String cmd = intent.getStringExtra(EXTRA_CMD);
            if (cmd != null) {
                // 冷启动时悬浮窗还没真正显示，startActivity 会被后台启动限制挡掉，等窗口就绪
                long wait = Math.max(0, 1200 - (System.currentTimeMillis() - createdAt));
                main.postDelayed(new Runnable() { @Override public void run() { handleCommand(cmd); } }, wait);
            }
        }
        if (quitting) { stopSelf(); return START_NOT_STICKY; }
        return START_STICKY;
    }

    /**
     * 用户从"最近任务"里把 App 划掉 = 关闭 App。
     * 悬浮球是前台服务，系统默认会让它继续活着——那样球会"阴魂不散"。
     * 这里明确收摊：移除球和面板、撤掉前台通知、停服务，也不让 START_STICKY 再把它拉起来。
     * （手机侧的 dsh 不动，它只是服务端；下次打开 App 球会再回来。）
     */
    // The legacy stopForeground overload is confined to the existing API <24 fallback.
    @SuppressWarnings("deprecation")
    @Override public void onTaskRemoved(Intent rootIntent) {
        markUserClosed(this);   // ⚠️ 先落盘：进程可能 3ms 后就被 SIGKILL（写晚了标记就丢）
        Log.i("DSH", "🧹 任务被划掉 → DSB 退出（收球、停服务、停手机侧 dsh）");
        quitting = true;
        TermuxRunner.shutdown(this);   // 先把"停 dsh"的命令发出去（Termux 那边异步执行）
        try { if (bubble != null) wm.removeView(bubble); } catch (Throwable ignored) {}
        try { if (panel != null && panelShown) wm.removeView(panel); } catch (Throwable ignored) {}
        try { if (confirmBar != null && confirmShown) wm.removeView(confirmBar); } catch (Throwable ignored) {}
        try { if (voiceBox != null && voiceShown) wm.removeView(voiceBox); } catch (Throwable ignored) {}
        try {
            if (Build.VERSION.SDK_INT >= 24) stopForeground(STOP_FOREGROUND_REMOVE);
            else stopForeground(true);
        } catch (Throwable ignored) {}
        stopSelf();
        super.onTaskRemoved(rootIntent);
    }

    @Override public void onDestroy() {
        if (agentRunning || taskWorkerRunning || cancelUnconfirmed) stopTask();
        INSTANCE = null;
        synchronized (taskLock) {
            agentRunning = false;
            taskCancelled = true;
            taskGeneration++;
            if (taskThread != null) taskThread.interrupt();
        }
        setDecision("deny");
        try { main.removeCallbacks(aliveTask); } catch (Throwable ignored) {}
        try { if (aliveSock != null) aliveSock.close(); } catch (Throwable ignored) {}
        try { if (bubble != null) wm.removeView(bubble); } catch (Throwable ignored) {}
        try { if (panel != null && panelShown) wm.removeView(panel); } catch (Throwable ignored) {}
        if (asr != null) { asr.destroy(); asr = null; }
        super.onDestroy();
    }

    @Override public IBinder onBind(Intent intent) { return null; }

    /**
     * 前台服务通知（2026-09-24 按用户要求改成"零打扰"）。
     *
     * 用户原话："DSB 每次启动都会给我系统做一个推送通知，说什么点击小球、会停止任务，这个通知不需要。"
     * 为什么不能直接删：Android 硬性要求——前台服务（悬浮球要常驻）**必须有**一条通知，
     * 没有它 startForeground 会抛异常，服务活不下来。
     * 所以改成**最大限度不打扰**：
     *   ① 用**新通道** dsh_assistant_quiet + IMPORTANCE_MIN（通道一旦建好就不可改，
     *      老的 dsh_assistant 实测 importance=4 会响/会横幅，所以必须换 id 并删掉老的）；
     *   ② setSilent(true) + 禁振动 + 不显示角标 + 锁屏不显示内容；
     *   ③ 文案改成中性的"悬浮球运行中"，不再教用户"点小球会中止任务"。
     * 效果：不响、不弹横幅、状态栏无图标，只在你主动下拉通知栏时能看到一条，
     * 而且 Android 13+ 允许直接划掉它（划掉不影响服务）。
     */
    // The channel-less builder is confined to the existing API <26 fallback.
    @SuppressWarnings("deprecation")
    private Notification buildNotification() {
        String ch = "dsh_assistant_quiet";
        NotificationManager nm = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
        if (Build.VERSION.SDK_INT >= 26) {
            NotificationChannel c = new NotificationChannel(ch, "运行状态（静默）", NotificationManager.IMPORTANCE_MIN);
            c.setShowBadge(false);
            c.enableVibration(false);
            c.setSound(null, null);
            c.setLockscreenVisibility(Notification.VISIBILITY_SECRET);
            nm.createNotificationChannel(c);
            // 老通道（importance=4，带提示音）留着只会继续响 → 删掉
            try { nm.deleteNotificationChannel("dsh_assistant"); } catch (Throwable ignored) { }
        }
        PendingIntent pi = PendingIntent.getActivity(this, 0, new Intent(this, MainActivity.class), PendingIntent.FLAG_IMMUTABLE);
        Notification.Builder b = Build.VERSION.SDK_INT >= 26 ? new Notification.Builder(this, ch) : new Notification.Builder(this);
        return b.setContentTitle("逗沙包")
                .setContentText("后台服务运行中")   // 中性文案：不教用户"点小球会中止任务"
                .setSmallIcon(android.R.drawable.ic_menu_compass)
                .setContentIntent(pi)
                .setOnlyAlertOnce(true)   // 通道已是 IMPORTANCE_MIN（静默），这里再保险一次：只提醒一次
                .setOngoing(false)        // 允许用户划掉（不影响服务继续跑）
                .build();
    }

    // ---------------- 悬浮球 ----------------

    private void addBubble() {
        bubble = new TextView(this);
        // 2026-09-20 改：从"DSH"字样的 52dp 球 缩小到 17dp（≈1/3，13dp 实测偏小）的纯色小点。
        // 原因：原尺寸+点击弹面板会挡住微信聊天内容，且一碰就把微信切走，
        // 导致自动化操作被反复打断。现在它只当"服务在跑"的指示灯，不承载交互。
        bubble.setText("");
        bubble.setTextColor(Color.WHITE);
        bubble.setTextSize(6);
        bubble.setGravity(Gravity.CENTER);
        // 2026-09-22：蓝色小球 = 本 App 收起后的样子。用径向渐变做出"有高光的球"，
        // 36dp 可点可拖，静止时不透明（要让用户一眼看到它），拖动/任务中另有颜色。
        GradientDrawable bg = new GradientDrawable(GradientDrawable.Orientation.TL_BR,
                new int[]{0xFF7FC0FF, 0xFF2F7FF0, 0xFF12408F});
        bg.setShape(GradientDrawable.OVAL);
        bg.setStroke(dp(1), 0xE6FFFFFF);
        bubble.setBackground(bg);
        bubble.setAlpha(0.28f);         // 2026-09-22 用户要求「非常透明」（原 0.5 → 0.8 → 0.28）
        bubble.setElevation(dp(4));

        bubbleLp = new WindowManager.LayoutParams(dp(BUBBLE_DP), dp(BUBBLE_DP),
                WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY,
                WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE | WindowManager.LayoutParams.FLAG_LAYOUT_NO_LIMITS,
                PixelFormat.TRANSLUCENT);
        bubbleLp.gravity = Gravity.TOP | Gravity.START;
        // 2026-09-20 改：默认不再贴左边缘（dp(4)）——左边缘被系统的 edge-swipe
        // 手势监视器优先截获，触摸送不到球上。改到屏幕中间区域，并读上次记住的位置。
        int[] pos = loadBubblePos();
        // 初始放置也要夹进屏幕内：实测卸载重装会保留 SharedPreferences 里的旧坐标，
        // 若旧值是别的分辨率/被拖到边缘，球会落在屏幕外（我踩过：x=1260 屏幕只有 1260 宽）。
        android.util.DisplayMetrics dm0 = getResources().getDisplayMetrics();
        bubbleLp.x = Math.max(0, Math.min(dp(pos[0]), dm0.widthPixels - dp(BUBBLE_DP)));
        bubbleLp.y = Math.max(0, Math.min(dp(pos[1]), dm0.heightPixels - dp(BUBBLE_DP)));

        bubble.setOnTouchListener(new View.OnTouchListener() {
            float downX, downY;
            int startX, startY;
            boolean moved;
            @Override public boolean onTouch(View v, MotionEvent e) {
                switch (e.getActionMasked()) {
                    case MotionEvent.ACTION_DOWN:
                        downX = e.getRawX(); downY = e.getRawY();
                        startX = bubbleLp.x; startY = bubbleLp.y;
                        moved = false;
                        return true;
                    case MotionEvent.ACTION_MOVE:
                        int dx = (int) (e.getRawX() - downX), dy = (int) (e.getRawY() - downY);
                        if (Math.abs(dx) > dp(6) || Math.abs(dy) > dp(6)) moved = true;
                        bubbleLp.x = startX + dx; bubbleLp.y = startY + dy;
                        try { wm.updateViewLayout(bubble, bubbleLp); } catch (Throwable ignored) {}
                        return true;
                    case MotionEvent.ACTION_UP:
                        // Only an explicit tap opens recovery; failures do not steal focus.
                        if (!moved) {
                            if (cancelUnconfirmed && !cancelInFlight) {
                                showPanel(true);
                            } else if (bubbleBusy) {
                                // 橙球=任务在跑：点一下=紧急中止（用户明确要的"打断"入口）
                                touchAbortFlag();
                                if (agentRunning || taskWorkerRunning || cancelInFlight || cancelUnconfirmed) stopTask();
                                else setBubbleBusy(false);
                                log("⛔ ABORT 用户点击悬浮球，中止任务");
                            } else {
                                openWebUi();                 // 点球 = 变回网页形态（交互都在网页里做）
                            }
                        } else { clampBubblePosition(); saveBubblePos(); }
                        return true;
                }
                return false;
            }
        });
        wm.addView(bubble, bubbleLp);
    }

    private Button btn(String text, View.OnClickListener l) {
        Button b = new Button(this);
        b.setText(text);
        b.setAllCaps(false);
        b.setTextSize(13);
        b.setOnClickListener(l);
        return b;
    }

    /** 极简交互：一个输入框 + 状态 + 停止 + 收起；过程按需展开。其余全交给 DSH。 */
    private void buildPanel() {
        panel = new LinearLayout(this);
        panel.setOrientation(LinearLayout.VERTICAL);
        panel.setBackgroundColor(0xF2FFFFFF);
        int p = dp(10);
        panel.setPadding(p, p, p, p);

        // 标题行：DSH …… 收起
        LinearLayout head = new LinearLayout(this);
        head.setOrientation(LinearLayout.HORIZONTAL);
        head.setGravity(Gravity.CENTER_VERTICAL);
        TextView title = new TextView(this);
        title.setText("逗沙包");
        title.setTextSize(16);
        title.setTextColor(0xFF111111);
        head.addView(title, new LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f));
        head.addView(btn("收起", v -> showPanel(false)));
        panel.addView(head);

        // 唯一入口：说 / 打 一条指令（用底部输入框，能拉起输入法麦克风）
        panel.addView(btn("🎙 说 / 打指令…", v -> showVoiceBox()));
        panel.addView(btn("🌐 回到网页界面", v -> { showPanel(false); openWebUi(); }));

        // 状态行 + 停止
        LinearLayout statusRow = new LinearLayout(this);
        statusRow.setOrientation(LinearLayout.HORIZONTAL);
        statusRow.setGravity(Gravity.CENTER_VERTICAL);
        statusView = new TextView(this);
        statusView.setTextSize(12);
        statusView.setTextColor(0xFF666666);
        statusView.setMaxLines(4);
        statusView.setEllipsize(android.text.TextUtils.TruncateAt.END);
        statusView.setText(lastStatusText);
        statusRow.addView(statusView, new LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f));
        stopButton = btn("⏹ 停止", v -> stopTask());
        stopButton.setTextSize(14);
        stopButton.setPadding(dp(4), 0, dp(4), 0);
        statusRow.addView(stopButton, new LinearLayout.LayoutParams(dp(100), dp(52)));
        updateStopControl();
        panel.addView(statusRow);

        // 过程（默认收起，点开才看细节）
        logToggleBtn = btn("看过程 ▾", v -> toggleLog());
        panel.addView(logToggleBtn);

        log = new TextView(this);
        log.setTextSize(11);
        log.setTextColor(0xFF333333);
        log.setText("（还没有执行记录）");
        ScrollView sc = new ScrollView(this);
        sc.addView(log);
        logScroll = sc;
        logBox = sc;
        sc.setVisibility(View.GONE);
        panel.addView(sc, new LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, dp(220)));

        WindowManager.LayoutParams lp = new WindowManager.LayoutParams(
                Math.min(dp(320), getResources().getDisplayMetrics().widthPixels - dp(8)), WindowManager.LayoutParams.WRAP_CONTENT,
                WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY,
                WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE | WindowManager.LayoutParams.FLAG_LAYOUT_NO_LIMITS,
                PixelFormat.TRANSLUCENT);
        lp.gravity = Gravity.TOP | Gravity.START;
        lp.x = dp(4);
        lp.y = dp(380);
        panel.setTag(lp);
    }

    private void toggleLog() {
        logExpanded = !logExpanded;
        if (logBox != null) logBox.setVisibility(logExpanded ? View.VISIBLE : View.GONE);
        if (logToggleBtn != null) logToggleBtn.setText(logExpanded ? "收起过程 ▴" : "看过程 ▾");
    }

    private void stopTask() {
        final long stoppedGen;
        final String base;
        final String stoppedId;
        synchronized (taskLock) {
            if (!agentRunning && !taskWorkerRunning && !cancelInFlight) restoreTaskRecovery();
            if (cancelInFlight) { log("正在确认停止，请稍候"); return; }
            if (!agentRunning && !taskWorkerRunning && !cancelUnconfirmed) {
                setStatus("当前没有 App 任务在跑"); return;
            }
            if (taskBridgeBase == null || taskBridgeBase.isEmpty() || taskRecoveryId.isEmpty()) {
                setStatus("停止未确认：恢复记录不可用，需人工核对原任务；新任务已暂停");
                return;
            }
            if (!ACTIVE_CANCELS.compareAndSet(0, 1)) {
                setStatus("停止确认仍在进行，请稍后重试；新任务已暂停");
                return;
            }
            agentRunning = false;
            taskCancelled = true;
            stoppedGen = ++taskGeneration;
            cancelInFlight = true;
            cancelUnconfirmed = true;
            base = taskBridgeBase;
            stoppedId = taskRecoveryId;
            if (taskThread != null) taskThread.interrupt();
        }
        setDecision("deny");
        showConfirmBar(false);
        setStatus("正在停止，等待执行线程与桥确认…");
        updateStopControl();
        log("（你按了停止）");
        new Thread(new Runnable() { @Override public void run() {
            boolean confirmed = false;
            String error = "执行线程尚未退出";
            try {
                JSONObject cancellation = new JSONObject().put("task_id", stoppedId);
                DshClient.post(OverlayService.this, base, "/cancel", cancellation, 15000);
                // Drain any request admitted just before stop, then cancel once more.
                // A fixed sleep is not evidence that the old worker has exited.
                long deadline = System.nanoTime() + java.util.concurrent.TimeUnit.SECONDS.toNanos(15);
                synchronized (taskLock) {
                    while (ACTIVE_WORKERS.get() != 0) {
                        long left = deadline - System.nanoTime();
                        if (left <= 0) break;
                        taskLock.wait(Math.max(1, Math.min(100, java.util.concurrent.TimeUnit.NANOSECONDS.toMillis(left))));
                    }
                }
                if (ACTIVE_WORKERS.get() == 0) {
                    DshClient.Result r = DshClient.post(OverlayService.this, base, "/cancel", cancellation, 15000);
                    confirmed = r.ok;
                    error = cancellationFailure(r);
                }
            } catch (Throwable t) { error = "取消确认异常，连接恢复后重试"; }
            finally { ACTIVE_CANCELS.set(0); }
            final boolean ok = confirmed;
            final String detail = error;
            taskUi(stoppedGen, () -> {
                boolean durable;
                synchronized (taskLock) {
                    durable = ok && taskRecovery().clear(taskRecoveryId);
                    cancelInFlight = false;
                    cancelUnconfirmed = !durable;
                }
                updateStopControl();
                if (durable) {
                    setBubbleRunning(false);
                    setStatus("已停止");
                    log("已停止当前会话（执行线程退出，桥已确认）");
                } else {
                    setStatus("停止未确认：" + (ok ? "恢复状态未能落盘" : detail) + "；新任务已暂停");
                    toast("停止未确认，点橙球查看并重试");
                    log("停止未确认：" + detail + "；暂不接受新任务");
                }
            });
        }}, "task-cancel").start();
    }

    private TaskRecoveryStore taskRecovery() {
        if (recoveryStore == null)
            recoveryStore = new TaskRecoveryStore(new java.io.File(getFilesDir(), "task-recovery.json"));
        return recoveryStore;
    }

    private void restoreTaskRecovery() {
        synchronized (taskLock) {
            if (agentRunning || taskWorkerRunning || cancelInFlight) return;
            TaskRecoveryStore.State saved = taskRecovery().read();
            cancelUnconfirmed = saved.pending;
            taskRecoveryId = saved.id;
            taskBridgeBase = saved.bridge;
            if (saved.pending) {
                taskCancelled = true;
                setStatus("停止未确认：上次任务结果未知，请重试停止；新任务已暂停");
            }
        }
        updateStopControl();
    }

    private String cancellationFailure(DshClient.Result r) {
        switch (r.failureKind) {
            case "transport": return "无法连接原任务的桥，恢复连接后重试";
            case "timeout": return "桥响应超时，稍后重试";
            case "http": return "桥返回 HTTP " + r.statusCode + "，修复服务后重试";
            case "response": return "桥响应无效，检查服务后重试";
            case "rejected": return "桥未确认取消，检查服务后重试";
            default: return "取消尚未确认，请重试";
        }
    }

    private void updateStopControl() {
        runOnMain(() -> {
            if (stopButton == null) return;
            stopButton.setText(cancelInFlight ? "确认中…" : cancelUnconfirmed ? "重试停止" : "⏹ 停止");
            stopButton.setEnabled(!cancelInFlight);
        });
    }

    private long beginTask() {
        synchronized (taskLock) {
            if (!agentRunning && !taskWorkerRunning && !cancelInFlight) restoreTaskRecovery();
            if (agentRunning || taskWorkerRunning || cancelInFlight || cancelUnconfirmed
                    || ACTIVE_CANCELS.get() != 0 || !ACTIVE_WORKERS.compareAndSet(0, 1)) {
                log("⚠️ 已有任务或停止尚未确认，请等待或重试停止");
                return -1;
            }
            String base = bridgeUrl();
            TaskRecoveryStore.State saved = taskRecovery().begin(base);
            if (saved == null) {
                ACTIVE_WORKERS.set(0);
                cancelUnconfirmed = true;
                setStatus("停止未确认：任务状态未能保存，未发出任务；请检查存储");
                updateStopControl();
                return -1;
            }
            taskRecoveryId = saved.id;
            taskTransportSettled = false;
            synchronized (decisionLock) { pendingDecision = null; }
            taskCancelled = false;
            agentRunning = true;
            taskWorkerRunning = true;
            taskBridgeBase = base;
            return workerGeneration = ++taskGeneration;
        }
    }

    private boolean taskActive(long gen) {
        return agentRunning && !taskCancelled && gen == taskGeneration;
    }

    private boolean enterWorker(long gen) {
        synchronized (taskLock) {
            if (!taskActive(gen)) return false;
            taskThread = Thread.currentThread();
            return true;
        }
    }

    private void taskUi(long gen, Runnable update) {
        main.post(() -> {
            synchronized (taskLock) {
                if (gen == taskGeneration) update.run();
            }
        });
    }

    private void taskStatus(long gen, String text) { taskUi(gen, () -> setStatus(text)); }

    private void finishTask(long gen, boolean show) {
        synchronized (taskLock) {
            if (workerGeneration != gen || !taskWorkerRunning) return;
            if (gen == taskGeneration) {
                cancelUnconfirmed = !taskTransportSettled || !taskRecovery().clear(taskRecoveryId);
            }
            taskWorkerRunning = false;
            taskThread = null;
            if (gen == taskGeneration) agentRunning = false;
            ACTIVE_WORKERS.set(0);
            taskLock.notifyAll();
        }
        taskUi(gen, () -> {
            updateStopControl();
            if (cancelUnconfirmed) setStatus("停止未确认：任务结果未知或状态未落盘；新任务已暂停");
            setBubbleRunning(false);
            showConfirmBar(false);
            if (show) showPanel(true);
        });
        Thread.interrupted(); // Do not leak this task's interrupt into the executor's next task.
    }

    private boolean dispatchTaskAction(long gen, JSONObject action) throws InterruptedException {
        ActionReceipt receipt;
        synchronized (taskLock) {
            if (!taskActive(gen)) return false;
            receipt = executeAction(action);
        }
        // Never wait under taskLock: stop must be able to interrupt this worker.
        String state = receipt.await(5000);
        if (!taskActive(gen)) return false;
        log("动作回执：" + action.optString("action") + " / " + state);
        if (ActionReceipt.succeeded(state)) return true;
        taskStatus(gen, "操作未完成：" + state);
        return false;
    }

    private void runOnMain(Runnable action) {
        if (Looper.myLooper() == Looper.getMainLooper()) action.run();
        else main.post(action);
    }

    private void setStatus(final String s) {
        runOnMain(new Runnable() {
            @Override public void run() {
                lastStatusText = s;
                if (statusView != null) statusView.setText(s);
            }
        });
    }

    /** 把红绿灯状态写进 App 外部目录，给 adb 侧脚本读（比 logcat 快且稳）。
     *  文件：/sdcard/Android/data/com.dsb.assistant/files/bubble_state.txt */
    private void writeBubbleState(String s) {
        try {
            java.io.File d = getExternalFilesDir(null);
            if (d == null) return;
            java.io.FileOutputStream fo = new java.io.FileOutputStream(new java.io.File(d, "bubble_state.txt"), false);
            fo.write((s + " " + System.currentTimeMillis() + "\n").getBytes("UTF-8"));
            fo.close();
        } catch (Throwable ignored) {}
    }

    /** 用户单击橙色球中止任务时留下的标志；脚本在每次动作前检查它。 */
    private void touchAbortFlag() {
        try {
            java.io.File d = getExternalFilesDir(null);
            if (d == null) return;
            java.io.FileOutputStream fo = new java.io.FileOutputStream(new java.io.File(d, "abort.flag"), false);
            fo.write(("ABORT " + System.currentTimeMillis() + "\n").getBytes("UTF-8"));
            fo.close();
        } catch (Throwable ignored) {}
    }

    /** 红绿灯（2026-09-20 需求）：橙=我正在独占操作屏幕（用户勿碰）；蓝=空闲。
     *  由脚本经 CmdReceiver 的「橙 / 蓝」命令控制；每次「橙」都是一次心跳，
     *  超过 120 秒没有心跳自动回蓝（脚本崩溃/被杀时的兜底）。 */
    private void setBubbleBusy(final boolean busy) {
        runOnMain(new Runnable() {
            @Override public void run() {
                if (!busy && (agentRunning || taskWorkerRunning || cancelInFlight || cancelUnconfirmed)) return;
                bubbleBusy = busy;
                main.removeCallbacks(busyWatchdog);
                // 2026-09-22 用户要求：停手后"立刻"回蓝。
                // 从 120 秒改到 20 秒 —— 取舍依据：
                //   · 我干活时是连续的 adb 操作，间隔通常 <10 秒 → 20 秒够续上，全程保持橙
                //   · 我一停手，20 秒内自动回蓝（不用等我显式发"蓝"，因为我无法在
                //     "推理结束"那一刻执行代码）
                if (busy) main.postDelayed(busyWatchdog, BUSY_WATCHDOG_MS);
                if (bubble == null) return;
                GradientDrawable bg = new GradientDrawable();
                bg.setShape(GradientDrawable.OVAL);
                bg.setColor(busy ? 0xFFF08C00 : 0xE61F6FEB);
                bg.setStroke(dp(busy ? 2 : 1), 0xDDFFFFFF);
                bubble.setBackground(bg);
                bubble.setAlpha(busy ? 0.60f : 0.28f);   // 2026-09-22 非常透明（静止0.28/任务0.60）
                writeBubbleState(busy ? "BUSY" : "IDLE");
                int[] loc = new int[2];
                try { bubble.getLocationOnScreen(loc); } catch (Throwable ignored) {}
                log("球状态：" + (busy ? "橙（忙）" : "蓝（闲）") + " @(" + loc[0] + "," + loc[1] + ")");
            }
        });
    }

    /** 任务在跑时给悬浮球一点状态反馈（旧入口，统一走红绿灯） */
    private void setBubbleRunning(final boolean running) { setBubbleBusy(running); }

    private void showPanel(final boolean show) {
        runOnMain(new Runnable() { @Override public void run() { showPanelInternal(show); } });
    }

    /** 横竖屏切换后保证面板还在可见区域（否则 y=380dp 可能落到横屏屏幕外） */
    // Overlay coordinates use the full default display, not activity-window bounds.
    @SuppressWarnings("deprecation")
    private void clampPanelPosition() {
        try {
            android.util.DisplayMetrics dm = new android.util.DisplayMetrics();
            wm.getDefaultDisplay().getRealMetrics(dm);
            WindowManager.LayoutParams lp = (WindowManager.LayoutParams) panel.getTag();
            int maxY = Math.max(0, dm.heightPixels - dp(340));
            if (lp.y > maxY || lp.y < 0) lp.y = Math.min(dp(180), maxY);
            int maxX = Math.max(0, dm.widthPixels - dp(300));
            if (lp.x > maxX || lp.x < 0) lp.x = dp(4);
        } catch (Throwable ignored) {}
    }

    /** 悬浮球拖动结束后夹回屏幕内（横竖屏切换也不会丢） */
    // Keep the same physical-display bounds as the overlay drag/gesture coordinates.
    @SuppressWarnings("deprecation")
    private void clampBubblePosition() {
        try {
            android.util.DisplayMetrics dm = new android.util.DisplayMetrics();
            wm.getDefaultDisplay().getRealMetrics(dm);
            bubbleLp.x = Math.max(0, Math.min(bubbleLp.x, dm.widthPixels - dp(BUBBLE_DP)));
            bubbleLp.y = Math.max(0, Math.min(bubbleLp.y, dm.heightPixels - dp(BUBBLE_DP)));
            wm.updateViewLayout(bubble, bubbleLp);
        } catch (Throwable ignored) {}
    }

    private void showPanelInternal(boolean show) {
        if (show && panel == null) buildPanel();
        if (panel == null) return;
        try {
            if (show) clampPanelPosition();
            if (show && !panelShown) { wm.addView(panel, (WindowManager.LayoutParams) panel.getTag()); panelShown = true; }
            else if (!show && panelShown) { wm.removeView(panel); panelShown = false; }
        } catch (Throwable t) { Log.i("DSH", "panel err " + t.getClass().getSimpleName()); }
    }

    private void togglePanel() { showPanel(!panelShown); }

    private void log(final String s) {
        // 面板保留原文；logcat 不记录任务、屏幕、通知、请求/响应或异常消息。
        Log.i("DSH", "panel event chars=" + (s == null ? 0 : s.length()));
        main.post(new Runnable() {
            @Override public void run() {
                if (log == null) return;
                log.append("\n" + s);
                String t = log.getText().toString();
                if (t.length() > 6000) log.setText(t.substring(t.length() - 6000));
                if (logScroll != null) logScroll.fullScroll(View.FOCUS_DOWN);
            }
        });
    }

    private void toast(String s) { try { Toast.makeText(this, s, Toast.LENGTH_SHORT).show(); } catch (Throwable ignored) {} }

    /**
     * 响一声提示音（2026-09-24 加）。
     * 为什么：任务完成时把界面强行切回逗沙包会打断用户正在做的事 → 改成"只提醒、不动屏幕"。
     * 优先用系统「通知」铃声（跟随用户的提示音设置、静音模式自然不响）；
     * 拿不到就退回 ToneGenerator 的两声短音。**不碰系统音量、不碰界面。**
     */
    private void playAlertTone() {
        try {
            android.media.Ringtone r = android.media.RingtoneManager.getRingtone(
                    this, android.media.RingtoneManager.getDefaultUri(android.media.RingtoneManager.TYPE_NOTIFICATION));
            if (r != null) {
                r.play();
                log("🔔 提示音（系统通知音）");
                return;
            }
        } catch (Throwable t) {
            log("提示音：系统通知音不可用（" + t + "），改用 ToneGenerator");
        }
        try {
            final android.media.ToneGenerator tg =
                    new android.media.ToneGenerator(android.media.AudioManager.STREAM_NOTIFICATION, 95);
            tg.startTone(android.media.ToneGenerator.TONE_PROP_BEEP2, 260);
            new Handler(Looper.getMainLooper()).postDelayed(new Runnable() {
                @Override public void run() {
                    try { tg.stopTone(); tg.release(); } catch (Throwable ignored) { }
                }
            }, 700);
            log("🔔 提示音（ToneGenerator）");
        } catch (Throwable t) {
            log("❌ 提示音失败：" + t);
        }
    }

    // ---------------- 确认（敏感动作） ----------------

    private void setDecision(String d) { synchronized (decisionLock) { pendingDecision = d; decisionLock.notifyAll(); } }

    private String waitForDecision(long gen, long timeoutMs) {
        synchronized (decisionLock) {
            long deadline = System.currentTimeMillis() + timeoutMs;
            while (pendingDecision == null && taskActive(gen)) {
                long left = deadline - System.currentTimeMillis();
                if (left <= 0) return "timeout";
                try { decisionLock.wait(left); } catch (InterruptedException e) { return "deny"; }
            }
            String d = taskActive(gen) ? pendingDecision : "deny";
            pendingDecision = null;
            return d;
        }
    }

    private void showConfirmBar(final boolean show) {
        runOnMain(new Runnable() {
            @Override public void run() {
                try {
                    if (show && confirmBar == null) {
                        LinearLayout bar = new LinearLayout(OverlayService.this);
                        bar.setOrientation(LinearLayout.HORIZONTAL);
                        bar.setBackgroundColor(0xF2FFF3CD);
                        int p = dp(6);
                        bar.setPadding(p, p, p, p);
                        TextView t = new TextView(OverlayService.this);
                        t.setText("⚠️ DSH 认为这步有风险，要执行吗？");
                        t.setTextSize(13);
                        t.setTextColor(0xFF7A4B00);
                        bar.addView(t, new LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f));
                        bar.addView(btn("✅ 允许", v -> { log("（你点了允许）"); setDecision("allow"); }));
                        bar.addView(btn("⛔ 拒绝", v -> { log("（你点了拒绝）"); setDecision("deny"); }));
                        WindowManager.LayoutParams lp = new WindowManager.LayoutParams(
                                WindowManager.LayoutParams.MATCH_PARENT, WindowManager.LayoutParams.WRAP_CONTENT,
                                WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY,
                                WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE | WindowManager.LayoutParams.FLAG_LAYOUT_NO_LIMITS,
                                PixelFormat.TRANSLUCENT);
                        lp.gravity = Gravity.TOP;
                        lp.y = dp(180);
                        bar.setTag(lp);
                        confirmBar = bar;
                    }
                    if (show && !confirmShown) { wm.addView(confirmBar, (WindowManager.LayoutParams) confirmBar.getTag()); confirmShown = true; }
                    else if (!show && confirmShown) { wm.removeView(confirmBar); confirmShown = false; }
                } catch (Throwable t) { Log.i("DSH", "confirmBar err " + t.getClass().getSimpleName()); }
            }
        });
    }

    // ---------------- 输入框（拉得起输入法，含键盘麦克风） ----------------

    private void showVoiceBox() {
        main.post(new Runnable() { @Override public void run() { showVoiceBoxInternal(); } });
    }

    private void showVoiceBoxInternal() {
        try {
            if (voiceBox == null) {
                LinearLayout row = new LinearLayout(this);
                row.setOrientation(LinearLayout.HORIZONTAL);
                row.setBackgroundColor(0xF2FFFFFF);
                int p = dp(8);
                row.setPadding(p, p, p, p);
                voiceInput = new EditText(this);
                voiceInput.setHint("说/打一条指令，例如：把屏幕上看到的内容念出来");
                voiceInput.setTextSize(14);
                row.addView(voiceInput, new LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f));
                row.addView(btn("执行", v -> {
                    String t = voiceInput.getText().toString().trim();
                    voiceInput.setText("");
                    hideVoiceBox();
                    if (!t.isEmpty()) handleCommand(t);
                }));
                row.addView(btn("取消", v -> hideVoiceBox()));
                WindowManager.LayoutParams lp = new WindowManager.LayoutParams(
                        WindowManager.LayoutParams.MATCH_PARENT, WindowManager.LayoutParams.WRAP_CONTENT,
                        WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY,
                        WindowManager.LayoutParams.FLAG_NOT_TOUCH_MODAL,
                        PixelFormat.TRANSLUCENT);
                lp.gravity = Gravity.BOTTOM;
                lp.y = dp(8);
                row.setTag(lp);
                voiceBox = row;
            }
            if (!voiceShown) { wm.addView(voiceBox, (WindowManager.LayoutParams) voiceBox.getTag()); voiceShown = true; }
            voiceInput.requestFocus();
            voiceInput.postDelayed(new Runnable() {
                @Override public void run() {
                    try {
                        android.view.inputmethod.InputMethodManager imm =
                                (android.view.inputmethod.InputMethodManager) getSystemService(INPUT_METHOD_SERVICE);
                        imm.showSoftInput(voiceInput, android.view.inputmethod.InputMethodManager.SHOW_IMPLICIT);
                    } catch (Throwable ignored) {}
                }
            }, 300);
            setStatus("在下面输入，然后点「执行」");
        } catch (Throwable t) { log("弹出输入框失败：" + t); }
    }

    private void hideVoiceBox() {
        main.post(new Runnable() {
            @Override public void run() {
                try {
                    if (voiceBox != null && voiceShown) { wm.removeView(voiceBox); voiceShown = false; }
                    android.view.inputmethod.InputMethodManager imm =
                            (android.view.inputmethod.InputMethodManager) getSystemService(INPUT_METHOD_SERVICE);
                    if (imm != null && voiceInput != null) imm.hideSoftInputFromWindow(voiceInput.getWindowToken(), 0);
                } catch (Throwable ignored) {}
            }
        });
    }

    // ---------------- 指令入口（通用） ----------------

    void handleCommand(String cmd) {
        final String c = cmd == null ? "" : cmd.trim();
        if (c.isEmpty()) return;
        if (c.matches("(?s)(点击|长按|滑动|无障碍做|盲操)(?:[\\s:：].*)?")) {
            log("⛔ 旧执行入口未接入安全闸门，已停用；不会自动转发或重试");
            setStatus("旧执行入口已停用，请使用普通任务入口");
            return;
        }
        lastCmd = c;
        // 2026-09-20 改：**不再自动弹面板**。
        // 原来这里 showPanel(true)，导致每次用剪贴板等命令都把面板弹出来——
        // 面板一出来就抢焦点、把前台 App（微信）切走，自动化操作被反复打断。
        // 现在命令静默执行；要看面板请自己点小圆点。
        // 剪贴板命令里是用户要输入的原文：不进 logcat / 面板日志（2026-10-21）
        log("🗣 " + (c.startsWith("剪贴板") ? "剪贴板（内容不记日志）" : c));
        if (c.equals("截图") || c.equals("截屏")) { shot(); return; }
        if (c.startsWith("问DSH") || c.startsWith("问 DSH")) { askDsh(c.replaceFirst("^(问DSH|问 DSH)[：: ]*", "")); return; }
        if (c.equals("界面元素")) { dumpNodes(); return; }
        // ---- 剪贴板：给"发中文"用（input text 不支持非 ASCII）----
        // 2026-10-21：带回执的写/清，供 safe_tap type 判定"真的写进/清掉了"。
        //   剪贴板 #写:<nonce>:<base64(UTF-8)>   剪贴板 #清:<nonce>   → clip-<nonce>.json（只记成败与字数）
        //   挂在老命令"剪贴板 "前缀下：旧版 App 收到只会把这串当普通文字写进剪贴板，不会落到 runGoal 当任务跑。
        //   base64 不含空格，绕开 trim 吃掉首尾空白、adb/shell 二次切分。
        if (c.startsWith(CLIP_WRITE)) { clipboardWriteWithReceipt(c.substring(CLIP_WRITE.length())); return; }
        if (c.startsWith(CLIP_CLEAR)) { clipboardClearWithReceipt(c.substring(CLIP_CLEAR.length())); return; }
        if (c.startsWith("剪贴板 ")) { setClipboard(c.substring(4)); return; }
        if (c.equals("剪贴板")) { log("剪贴板内容（" + clipboardText().length() + " 字，不记原文）"); return; }
        // ===== 悬浮球红绿灯（2026-09-20 需求：橙=独占操作中，勿碰；蓝=空闲）=====
        // 每次「橙」= 一次心跳，刷新 App 侧 120 秒看门狗。
        if (c.equals("橙") || c.equals("忙") || c.equals("红灯")) { setBubbleBusy(true); return; }
        if (c.equals("蓝") || c.equals("闲") || c.equals("绿灯")) { setBubbleBusy(false); return; }
        if (c.equals("球状态")) { log("球状态：" + (bubbleBusy ? "橙（忙）" : "蓝（闲）")); return; }
        // P1 修1 补充：停止必须有文字命令（悬浮面板不在控件树里，自动化无法点）。
        // 主线程即时调用 stopTask()——绝不能排 exec 队列（否则排在任务后面 = 停止无效）。
        if (c.equals("停止") || c.equals("取消任务") || c.equals("停止任务") || c.equals("重试停止")) {
            stopTask();
            return;
        }
        // ===== 本地 TTS 朗读（2026-09-23 需求：结论卡显示完后念出来）=====
        // 为什么做在 App 里：容器（proot）没有 /dev/snd，放不出声；
        // Android 音频必须由 App 进程调用。用系统语音引擎 → 本地、离线、中文。
        // 详见 TtsSpeaker.java 的注释。
        if (c.equals("停止朗读") || c.equals("别念了")) {
            TtsSpeaker.get(this).stop();
            log("🔇 已停止朗读");
            return;
        }
        if (c.equals("语音状态") || c.equals("TTS状态")) {
            log("语音引擎：" + TtsSpeaker.get(this).info());
            return;
        }
        // ⚠️ 顺序要紧：具体命令必须排在通配式 startsWith("念"/"朗读") 之前，
        //    否则「列出语音」会被当成「念出 出语音…」（2026-09-23 差点踩到）。
        // 列出可选语音（用户要挑男声）——内容长，用多行日志输出
        if (c.equals("列出语音") || c.equals("语音列表") || c.equals("选声音")) {
            log("🎙 可用语音：" + TtsSpeaker.get(this).listVoices());
            return;
        }
        // 切换语音：用语音 <引擎里的 name>
        if (c.startsWith("用语音") || c.startsWith("换声音")) {
            String name = c.replaceFirst("^(用语音|换声音)[：: ]*", "").trim();
            if (name.isEmpty()) { log("❌ 用法：用语音 <名字>（先用「列出语音」看）"); return; }
            log(TtsSpeaker.get(this).useVoice(name));
            return;
        }
        // 音量 / 语速（2026-09-23 用户要求"声音调大一点、速度调快一点"）
        //   音量只作用于朗读，**不改系统媒体音量**（免得影响听音乐/看视频）
        if (c.startsWith("音量")) {
            String a = c.replaceFirst("^音量[：: ]*", "").trim();
            if (a.isEmpty()) {
                log("🔊 当前朗读音量 = " + TtsSpeaker.get(this).volume() + "（1.0 最大）");
                return;
            }
            try { log(TtsSpeaker.get(this).setVolume(Float.parseFloat(a))); }
            catch (Throwable t) { log("❌ 用法：音量 0.0~1.0"); }
            return;
        }
        if (c.startsWith("语速") || c.startsWith("速度")) {
            String a = c.replaceFirst("^(语速|速度)[：: ]*", "").trim();
            if (a.isEmpty()) {
                log("⏩ 当前语速 = " + TtsSpeaker.get(this).rate() + "（1.0 正常）");
                return;
            }
            try { log(TtsSpeaker.get(this).setRate(Float.parseFloat(a))); }
            catch (Throwable t) { log("❌ 用法：语速 0.5~3.0"); }
            return;
        }
        // 🔔 提示音：任务完成/卡片出现时**只响一声，不抢屏幕**。
        //   2026-09-24 用户要求：不要在任务完成时强行把界面切回逗沙包（他在用别的 App 时太打断），
        //   响个提醒音就行。daemon 侧改调这条命令。
        if (c.equals("提示音") || c.equals("响铃") || c.equals("提醒音") || c.equals("叮")) {
            playAlertTone();
            return;
        }
        // 兼容多种写法：「朗读 xxx」「念 xxx」「TTS xxx」
        if (c.startsWith("朗读") || c.startsWith("念") || c.startsWith("TTS")) {
            String text = c.replaceFirst("^(朗读|念|TTS)[：: ]*", "");
            if (text.isEmpty()) { log("❌ 用法：朗读 要念的文字"); return; }
            // 带上 "auto" 前缀的调用（来自 tts_reader.py 的自动朗读）才会在念完后回逗沙包；
            // 手动朗读不跳，免得打断用户自己看。
            boolean auto = text.startsWith("auto ") || text.startsWith("auto：");
            if (auto) text = text.replaceFirst("^auto[ ：]*", "");
            ttsPendingReturn = auto;
            TtsSpeaker.get(this).speak(text, auto ? () -> onTtsFinished(true) : null);
            return;
        }
        // 自动回逗沙包开关（2026-10-21 改名；旧命令词「自动回大烧饼」「回大烧饼开关」照旧可用）
        if (c.equals("自动回逗沙包") || c.equals("回逗沙包开关")
                || c.equals("自动回大烧饼") || c.equals("回大烧饼开关")) {
            ttsAutoReturn = !ttsAutoReturn;
            log("🔙 朗读结束自动回逗沙包：" + (ttsAutoReturn ? "开" : "关"));
            return;
        }
        // 网页形态 ↔ 蓝色小球（也能被 adb/脚本调用）
        if (c.equals("回网页") || c.equals("网页")) { openWebUi(); return; }
        if (c.equals("收起网页") || c.equals("收网页") || c.equals("变身")) { collapseWebUi(); return; }

        // 2026-09-20 加：把球位置复位到默认（屏幕中间区域）。
        // 因为"卸载重装"不会清 SharedPreferences，旧坐标会一直生效，需要一个重置入口。
        // 2026-09-20 加：把无障碍手势派发暴露成命令。
        // 为什么需要：`adb shell input tap` 是合成事件，微信的自绘界面（尤其搜索页）不接受；
        // 而 AccessibilityService.dispatchGesture 是真正的手势注入，路径不同，实测可能能穿过。
        // 用法：点击 X Y   /  长按 X Y   /  滑动 X1 Y1 X2 Y2 [毫秒]
        AgentAccessibilityService acc0 = AgentAccessibilityService.INSTANCE;
        if (acc0 == null && (c.startsWith("点击 ") || c.startsWith("长按 ") || c.startsWith("滑动 "))) {
            log("❌ 无障碍未开启，无法派发手势"); return;
        }
        if (c.startsWith("点击 ")) {
            String[] a1 = c.substring(3).trim().split("\\s+");
            if (a1.length >= 2) {
                try {
                    int gx = Integer.parseInt(a1[0].trim()), gy = Integer.parseInt(a1[1].trim());
                    // 用 120ms 而不是默认 80ms：太短的点击在部分自绘控件上会被当成无有效按压
                    acc0.tapAt(gx, gy, 120);
                    log("✅ 已派发点击 (" + gx + "," + gy + ")");
                } catch (Throwable t) { log("❌ 点击参数错误：" + t); }
            } else log("❌ 用法：点击 X Y");
            return;
        }
        if (c.startsWith("长按 ")) {
            String[] a1 = c.substring(3).trim().split("\\s+");
            if (a1.length >= 2) {
                try {
                    int gx = Integer.parseInt(a1[0].trim()), gy = Integer.parseInt(a1[1].trim());
                    acc0.tapAt(gx, gy, 900);
                    log("✅ 已派发长按 (" + gx + "," + gy + ")");
                } catch (Throwable t) { log("❌ 长按参数错误：" + t); }
            } else log("❌ 用法：长按 X Y");
            return;
        }
        if (c.startsWith("滑动 ")) {
            String[] a1 = c.substring(3).trim().split("\\s+");
            if (a1.length >= 4) {
                try {
                    int x1 = Integer.parseInt(a1[0].trim()), y1 = Integer.parseInt(a1[1].trim());
                    int x2 = Integer.parseInt(a1[2].trim()), y2 = Integer.parseInt(a1[3].trim());
                    int ms = a1.length >= 5 ? Integer.parseInt(a1[4].trim()) : 300;
                    acc0.swipeAt(x1, y1, x2, y2, ms);
                    log("✅ 已派发滑动");
                } catch (Throwable t) { log("❌ 滑动参数错误：" + t); }
            } else log("❌ 用法：滑动 X1 Y1 X2 Y2 [毫秒]");
            return;
        }
        // 诊断命令（2026-09-22 加，用于验证微信气泡可读性）
        //   找id bkl    → 只看 viewId 含 "bkl" 的节点（微信气泡 id 是 com.tencent.mm:id/bkl）
        //   节点        → 全部带文字的节点，不过滤
        // 观测 [nonce]：机器可读的控件树快照 → 外部文件目录的 look.json
        //   2026-10-01 加：给手机侧 safe_tap.py 用。走文件而不是 logcat（logcat 单行 ~4KB 会截断）；
        //   进程内读 AccessibilityNodeInfo，比 `uiautomator dump` 快一个数量级。
        //   **同步执行**（不丢线程）：调用方的 `am broadcast` 返回时文件已写好，省掉轮询。
        if (c.equals("观测") || c.startsWith("观测 ") || c.startsWith("观测:")) {
            final AgentAccessibilityService accS = AgentAccessibilityService.INSTANCE;
            if (accS == null) { log("❌ 无障碍未开启"); return; }
            // nonce 支持「观测:123」与「观测 123」两种写法；冒号写法无空格，专为 adb shell 的调用方准备
            final String nonce = c.length() > 3 ? c.substring(3).trim() : String.valueOf(System.currentTimeMillis());
            long t0 = System.currentTimeMillis();
            try {
                org.json.JSONObject snapS = accS.snapshot(800);
                String nodesJson = snapS.optJSONArray("nodes") == null ? "[]" : snapS.optJSONArray("nodes").toString();
                String fgp = "";
                try { fgp = foregroundPackage(); } catch (Throwable ignored) {}
                String json = "{\"nonce\":\"" + nonce + "\",\"ts\":" + System.currentTimeMillis()
                        + ",\"fg\":\"" + fgp + "\",\"treeHash\":\"" + snapS.optString("treeHash", "")
                        + "\",\"windowId\":" + snapS.optInt("windowId", -1)
                        + ",\"nodes\":" + nodesJson + "}";
                java.io.File dir = getExternalFilesDir(null);
                if (dir == null) { log("❌ 没有外部文件目录"); return; }
                java.io.File tmp = new java.io.File(dir, "look.json.tmp");
                java.io.File out = new java.io.File(dir, "look.json");
                java.io.FileOutputStream fo = new java.io.FileOutputStream(tmp);
                fo.write(json.getBytes("UTF-8"));
                fo.close();
                tmp.renameTo(out);
                log("👁 观测快照 " + nonce + " → look.json（" + json.getBytes("UTF-8").length + " 字节，" + (System.currentTimeMillis() - t0) + " ms）");
            } catch (Throwable t) {
                log("❌ 观测失败：" + t);
            }
            return;
        }
        // 观测图 [nonce]：**同一次观测**里给出 截图 + 控件树 + 前台包名 + 时间戳
        //   第三轮：走系统 takeScreenshot + JPEG，替代 adb exec-out screencap（7.3MB PNG / 1.6s）
        //   第四轮：加**单飞互斥 + 过密复用上一帧**；文件按 nonce 分开写，避免多个调用方互相覆盖
        if (c.equals("观测图") || c.startsWith("观测图:") || c.startsWith("观测图 ")) {
            final AgentAccessibilityService accI = AgentAccessibilityService.INSTANCE;
            if (accI == null) { log("❌ 无障碍未开启"); return; }
            String ncI = "";
            if (c.startsWith("观测图:") || c.startsWith("观测图 ")) ncI = c.substring(4).trim();
            final boolean forceI = ncI.endsWith(":f");   // 「:f」= 强制真采，不接受复用帧（动作后的观测必须用它）
            if (forceI) ncI = ncI.substring(0, ncI.length() - 2);
            final String nonceI = ncI.isEmpty() ? String.valueOf(System.currentTimeMillis()) : ncI;
            observeGraph(android.view.Display.DEFAULT_DISPLAY, nonceI, true, forceI);
            return;
        }
        // ── 虚拟屏观测（2026-10-20）：必须按**逻辑 displayId** 取树+取帧 ──────────────
        //   观测屏图:<displayId>:<nonce>[:f]   → 截图 + 该显示器控件树
        //   观测屏树:<displayId>:<nonce>       → 只控件树（快）
        //   ⚠️ 显示器不存在 / 取不到窗口 / 截图失败 → 文件里显式写 error，
        //      绝不回退主屏的树或图（那会让 safe_tap 核对错页面）。
        if (c.startsWith("观测屏图:") || c.startsWith("观测屏树:")) {
            final AgentAccessibilityService accV = AgentAccessibilityService.INSTANCE;
            if (accV == null) { log("❌ 无障碍未开启"); return; }
            final boolean withImage = c.startsWith("观测屏图:");
            String[] parts = c.substring(5).split(":");
            if (parts.length < 2) { log("❌ 用法：观测屏图:<displayId>:<nonce>[:f]"); return; }
            int did;
            try { did = Integer.parseInt(parts[0].trim()); }
            catch (Throwable t) { log("❌ 观测屏：displayId 非法 " + parts[0]); return; }
            if (did <= 0) { log("❌ 观测屏：displayId 必须 >0（主屏请用「观测图」）"); return; }
            String nc = parts[1].trim();
            boolean forceV = parts.length >= 3 && "f".equals(parts[2].trim());
            if (nc.isEmpty()) nc = String.valueOf(System.currentTimeMillis());
            observeGraph(did, nc, withImage, forceV);
            return;
        }
        // ── 虚拟屏手势（2026-10-20）：displayId 与坐标**同源绑定** ─────────────────
        //   手势屏:<displayId>:<kind>:<x1>:<y1>:<x2>:<y2>:<ms>:<nonce>
        //   kind = tap|swipe|long_press|drag|scroll
        //   派发结果写回执文件 gesture-<nonce>.json，供 safe_tap 判定"真的下发成功"。
        if (c.startsWith("手势屏:")) {
            final AgentAccessibilityService accG = AgentAccessibilityService.INSTANCE;
            String[] p = c.split(":");
            if (p.length < 9) { log("❌ 用法：手势屏:<id>:<kind>:<x1>:<y1>:<x2>:<y2>:<ms>:<nonce>"); return; }
            final String kind = p[2].trim();
            final int did, x1, y1, x2, y2, ms;
            final String nonce;
            try {
                did = Integer.parseInt(p[1].trim());
                x1 = Integer.parseInt(p[3].trim()); y1 = Integer.parseInt(p[4].trim());
                x2 = Integer.parseInt(p[5].trim()); y2 = Integer.parseInt(p[6].trim());
                ms = Integer.parseInt(p[7].trim());
                nonce = p[8].trim();
            } catch (Throwable t) { log("❌ 手势屏参数非法：" + t); return; }
            if (did <= 0 || nonce.isEmpty() || ms < 40 || ms > 3000
                    || x1 < 0 || y1 < 0 || x2 < 0 || y2 < 0) { log("❌ 手势屏参数越界"); return; }
            if (accG == null) { log("❌ 无障碍未开启，无法派发手势屏"); return; }
            if (!("tap".equals(kind) || "swipe".equals(kind) || "long_press".equals(kind)
                    || "drag".equals(kind) || "scroll".equals(kind))) {
                log("❌ 手势屏：未知 kind=" + kind); return;
            }
            dispatchDisplayGesture(accG, did, kind, x1, y1, x2, y2, ms, nonce);
            return;
        }
        if (c.equals("探测")) {
            final AgentAccessibilityService accP = AgentAccessibilityService.INSTANCE;
            if (accP == null) { log("❌ 无障碍未开启"); return; }
            new Thread(new Runnable() { @Override public void run() {
                for (String line : accP.probeRoot().split("\n")) log("🧪 " + line);
            }}).start();
            return;
        }
        if (c.startsWith("找id ") || c.equals("节点")) {
            final AgentAccessibilityService accD = AgentAccessibilityService.INSTANCE;
            if (accD == null) { log("❌ 无障碍未开启"); return; }
            final String filter = c.startsWith("找id ") ? c.substring(4).trim() : "";
            new Thread(new Runnable() { @Override public void run() {
                String out = accD.dumpAllNodes(filter, 6000);
                for (String line : out.split("\n")) log("🔍 " + line);
            }}).start();
            return;
        }
        if (c.equals("复位球") || c.equals("球复位")) {
            try { getSharedPreferences("dsh", MODE_PRIVATE).edit()
                    .remove("bubble_x").remove("bubble_y").apply(); } catch (Throwable ignored) {}
            int[] d0 = loadBubblePos();
            android.util.DisplayMetrics dmr = getResources().getDisplayMetrics();
            bubbleLp.x = Math.max(0, Math.min(dp(d0[0]), dmr.widthPixels - dp(BUBBLE_DP)));
            bubbleLp.y = Math.max(0, Math.min(dp(d0[1]), dmr.heightPixels - dp(BUBBLE_DP)));
            try { wm.updateViewLayout(bubble, bubbleLp); } catch (Throwable ignored) {}
            log("✅ 球已复位到 (" + d0[0] + "," + d0[1] + ")dp");
            return;
        }
        if (c.equals("粘贴")) { pasteClipboard(); return; }
        if (c.startsWith("无障碍做 ") || c.startsWith("盲操 ")) {
            runGoalByAccessibility(c.replaceFirst("^(无障碍做|盲操)[：: ]*", ""));
            return;
        }
        runGoal(c);   // 其它一切：交给 DSH 决定的通用循环
    }

    private void setClipboard(String text) {
        AgentAccessibilityService acc = AgentAccessibilityService.INSTANCE;
        if (acc == null) { log("❌ 无障碍未开启"); return; }
        boolean ok = acc.setClipboard(text);
        log(ok ? ("✅ 已写入剪贴板（" + text.length() + " 字）") : "❌ 写剪贴板失败");
    }

    static final String CLIP_WRITE = "剪贴板 #写:";
    static final String CLIP_CLEAR = "剪贴板 #清:";

    private static boolean validNonce(String n) {
        return n != null && n.matches("[0-9A-Za-z_-]{1,64}");   // 进文件名，挡住 ../ 之类
    }

    private static String nonceForLog(String n) { return validNonce(n) ? n : "<invalid>"; }

    /** 剪贴板 #写:<nonce>:<base64(UTF-8)> → 写入（标记敏感）并写回执 clip-<nonce>.json。args = 前缀之后的部分。 */
    private void clipboardWriteWithReceipt(String args) {
        String[] p = args.split(":", 2);
        String nonce = p[0].trim();
        boolean ok = false; int len = -1; String err = "";
        try {
            if (p.length < 2 || !validNonce(nonce)) throw new IllegalArgumentException("usage");
            String b64 = p[1].trim();
            // Base64.DEFAULT 会跳过非字母表字符：先严格校验，免得乱码"解码成功"写进错误内容
            if (b64.length() % 4 != 0 || !b64.matches("[A-Za-z0-9+/]*={0,2}")) throw new IllegalArgumentException("bad_base64");
            String text = new String(Base64.decode(b64, Base64.NO_WRAP), "UTF-8");
            len = text.length();
            AgentAccessibilityService acc = AgentAccessibilityService.INSTANCE;
            if (acc == null) throw new IllegalStateException("a11y_off");
            ok = acc.setClipboardSensitive(text);
            if (!ok) err = "set_failed";
        } catch (Throwable t) { err = t.getClass().getSimpleName(); }
        writeClipReceipt(nonce, "write", ok, len, err);
        log("📋 剪贴板写 nonce=" + nonceForLog(nonce) + " ok=" + ok + (len >= 0 ? (" " + len + " 字") : "")
                + (err.isEmpty() ? "" : (" err=" + err)));
    }

    /** 剪贴板 #清:<nonce> → clearPrimaryClip 并写回执 clip-<nonce>.json。args = 前缀之后的部分。 */
    private void clipboardClearWithReceipt(String args) {
        String nonce = args.trim();
        boolean ok = false; String err = "";
        try {
            if (!validNonce(nonce)) throw new IllegalArgumentException("usage");
            AgentAccessibilityService acc = AgentAccessibilityService.INSTANCE;
            if (acc == null) throw new IllegalStateException("a11y_off");
            ok = acc.clearClipboard();
            if (!ok) err = "clear_failed";
        } catch (Throwable t) { err = t.getClass().getSimpleName(); }
        writeClipReceipt(nonce, "clear", ok, -1, err);
        log("📋 剪贴板清 nonce=" + nonceForLog(nonce) + " ok=" + ok + (err.isEmpty() ? "" : (" err=" + err)));
    }

    private void writeClipReceipt(String nonce, String op, boolean ok, int len, String err) {
        if (!validNonce(nonce)) return;             // 没有合法 nonce 就没有回执，调用方按"未确认"处理
        java.io.File dir = getExternalFilesDir(null);
        if (dir == null) { log("❌ 没有外部文件目录，无法写剪贴板回执"); return; }
        try {
            org.json.JSONObject o = new org.json.JSONObject();
            o.put("nonce", nonce); o.put("op", op); o.put("ok", ok); o.put("len", len);
            o.put("err", err); o.put("ts", System.currentTimeMillis());
            writeAtomically(new java.io.File(dir, "clip-" + nonce + ".json"), o.toString());
        } catch (Throwable t) { log("❌ 写剪贴板回执失败：" + t.getClass().getSimpleName()); }
        pruneReceipts(dir, "clip-");
    }

    private String clipboardText() {
        AgentAccessibilityService acc = AgentAccessibilityService.INSTANCE;
        return acc == null ? "（无障碍未开启）" : acc.getClipboard();
    }

    /** 粘贴：先试无障碍 ACTION_PASTE，失败再退回 KEYCODE_PASTE(279) */
    private void pasteClipboard() {
        AgentAccessibilityService acc = AgentAccessibilityService.INSTANCE;
        if (acc == null) { log("❌ 无障碍未开启"); return; }
        if (acc.pasteIntoFocused()) { log("✅ 已粘贴（无障碍 ACTION_PASTE）"); return; }
        try {
            Runtime.getRuntime().exec(new String[]{"sh", "-c", "input keyevent 279"});
            log("⚠ 无障碍粘贴失败，已退回 KEYCODE_PASTE(279)");
        } catch (Throwable t) {
            log("❌ 粘贴失败：" + t);
        }
    }

    private void dumpNodes() {
        AgentAccessibilityService acc = AgentAccessibilityService.INSTANCE;
        if (acc == null) { log("❌ 无障碍未开启"); return; }
        String nodes = acc.dumpNodes(60);
        if (nodes.isEmpty()) log("（这个界面读不到控件元素，只能靠截图）");
        else { log("界面元素："); for (String line : nodes.split("\n")) log(line); }
    }

    /** 当前前台应用的包名（通用上下文，不针对任何 App） */
    private String foregroundPackage() {
        try {
            AgentAccessibilityService acc = AgentAccessibilityService.INSTANCE;
            if (acc == null) return "";
            AccessibilityNodeInfo root = acc.getRootInActiveWindow();
            return root == null ? "" : String.valueOf(root.getPackageName());
        } catch (Throwable t) { return ""; }
    }

    /** 通用能力：按名字打开 App（系统级，对任何 App 一视同仁，不做包名白名单） */
    private boolean openApp(String name) {
        if (name == null || name.trim().isEmpty()) { log("❌ open_app 缺名字"); return false; }
        name = name.trim();
        try {
            PackageManager pm = getPackageManager();
            // 2026-10-21 改名：旧名「大烧饼」仍指向本 App（老脚本、熟路笔记里还在用）——按包名直接启动，不走标签搜索
            if (name.equals("大烧饼")) {
                Intent self = pm.getLaunchIntentForPackage(getPackageName());
                if (self == null) { log("❌ 本 App 没有启动入口"); return false; }
                self.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                startActivity(self);
                log("已提交启动请求：逗沙包");
                return true;
            }
            Intent main = new Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_LAUNCHER);
            List<android.content.pm.ResolveInfo> list = pm.queryIntentActivities(main, 0);
            String pkg = null, labelHit = null;
            for (android.content.pm.ResolveInfo ri : list) {
                String label = String.valueOf(ri.loadLabel(pm));
                if (label.equalsIgnoreCase(name)) { pkg = ri.activityInfo.packageName; labelHit = label; break; }
                if (pkg == null && (label.contains(name) || name.contains(label))) { pkg = ri.activityInfo.packageName; labelHit = label; }
            }
            if (pkg == null) { log("❌ 没找到叫「" + name + "」的应用"); return false; }
            Intent i = pm.getLaunchIntentForPackage(pkg);
            if (i == null) { log("❌ " + labelHit + " 没有启动入口"); return false; }
            i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            startActivity(i);
            log("已提交启动请求：" + labelHit);
            return true;
        } catch (Throwable t) { log("❌ 启动失败：" + t); return false; }
    }

    // ---------------- 截屏 ----------------

    private void shot() {
        final AgentAccessibilityService acc = AgentAccessibilityService.INSTANCE;
        if (acc == null) { log("❌ 请到 设置→无障碍 里手动开启「逗沙包」"); return; }
        showPanel(false);
        log("📸 截屏中…");
        acc.shot((path, bmp, err) -> {
            if (err != null) { log("❌ " + err); return; }
            try {
                File dir = new File(getExternalFilesDir(null), "shots");
                if (!dir.exists()) dir.mkdirs();
                File f = new File(dir, "shot_" + System.currentTimeMillis() + ".png");
                FileOutputStream out = new FileOutputStream(f);
                bmp.compress(Bitmap.CompressFormat.PNG, 100, out);
                out.close();
                pruneShots(dir);
                log("✅ 已保存：" + f.getAbsolutePath());
            } catch (Throwable t) { log("❌ 保存失败：" + t); }
        });
    }

    private void pruneShots(File dir) {
        File[] files = dir.listFiles();
        if (files == null || files.length <= 20) return;
        java.util.Arrays.sort(files, (a, b) -> Long.compare(a.lastModified(), b.lastModified()));
        for (int i = 0; i < files.length - 20; i++) files[i].delete();
    }

    /** 粗判"整屏基本全黑"（息屏/某些 FLAG_SECURE 页面）：24x24 网格采样，亮度均值<8 且峰值<24 */
    private boolean isMostlyBlack(Bitmap b) {
        try {
            int w = b.getWidth(), h = b.getHeight();
            int step = Math.max(1, Math.min(w, h) / 24);
            long sum = 0; int n = 0, mx = 0;
            for (int y = 0; y < h; y += step) {
                for (int x = 0; x < w; x += step) {
                    int p = b.getPixel(x, y);
                    int lum = ((p >> 16 & 0xff) * 299 + (p >> 8 & 0xff) * 587 + (p & 0xff) * 114) / 1000;
                    sum += lum; n++;
                    if (lum > mx) mx = lum;
                }
            }
            return n > 0 && (sum / n) < 8 && mx < 24;
        } catch (Throwable t) { return false; }
    }

    // ── 观测图的单飞互斥 / 过密复用（2026-10-01 第四轮）────────────────────
    //   为什么要：takeScreenshot 约 1 次/秒。多个调用方（safe_tap / App 自己的兜底循环 /
    //   别的 adb 路径）同时来，会把对方的截图挤成"频率限制失败"，然后各自重试 → 又慢又乱。
    //   现在：同一时刻只有一次真实采集；过密的请求**复用上一帧**并标 reused=true。
    //   文件也从固定名改成 look-<nonce>.json/.jpg —— 消掉"读到别人的图"的竞态。
    private static final Object OBS_LOCK = new Object();
    private static volatile org.json.JSONObject obsLastObj = null;
    private static volatile long obsLastFrameTs = 0L;
    private static volatile boolean obsBusy = false;
    private static final long OBS_MIN_INTERVAL_MS = 900L;   // 间隔小于它就复用上一帧
    private static final int OBS_KEEP_FILES = 40;           // look-* 最多留 40 个（约 32MB）

    private void writeAtomically(java.io.File out, String text) throws Exception {
        out = checkedOutputFile(out);
        java.io.File tmp = new java.io.File(out.getParentFile(), out.getName() + ".tmp");
        try (java.io.OutputStream fo = openOutput(tmp)) {
            fo.write(text.getBytes("UTF-8"));
        }
        java.nio.file.Files.move(tmp.toPath(), out.toPath(),
                java.nio.file.StandardCopyOption.ATOMIC_MOVE,
                java.nio.file.StandardCopyOption.REPLACE_EXISTING);
    }

    private void copyFile(java.io.File src, java.io.File dst) throws Exception {
        try (java.io.InputStream in = java.nio.file.Files.newInputStream(
                    checkedOutputFile(src).toPath(), java.nio.file.LinkOption.NOFOLLOW_LINKS);
             java.io.OutputStream out = openOutput(dst)) {
            byte[] buf = new byte[65536];
            int n;
            while ((n = in.read(buf)) > 0) out.write(buf, 0, n);
        }
    }

    private java.io.OutputStream openOutput(java.io.File out) throws Exception {
        return java.nio.file.Files.newOutputStream(checkedOutputFile(out).toPath(),
                java.nio.file.StandardOpenOption.CREATE, java.nio.file.StandardOpenOption.TRUNCATE_EXISTING,
                java.nio.file.StandardOpenOption.WRITE, java.nio.file.LinkOption.NOFOLLOW_LINKS);
    }

    // All observation/receipt files must be direct children of the App's external files directory.
    private java.io.File checkedOutputFile(java.io.File file) throws java.io.IOException {
        java.io.File root = getExternalFilesDir(null);
        if (root == null || file == null) throw new java.io.IOException("output_directory_unavailable");
        for (java.nio.file.Path part : file.toPath()) {
            if ("..".equals(part.toString())) throw new java.io.IOException("invalid_output_path");
        }
        java.nio.file.Path base = root.getCanonicalFile().toPath();
        java.nio.file.Path target = file.getCanonicalFile().toPath();
        java.io.File parent = file.getAbsoluteFile().getParentFile();
        if (parent == null || !parent.toPath().normalize()
                    .equals(root.getAbsoluteFile().toPath().normalize())
                || !target.startsWith(base) || !base.equals(target.getParent())
                || java.nio.file.Files.isSymbolicLink(file.toPath())) {
            throw new java.io.IOException("invalid_output_path");
        }
        return file;
    }

    /** 只保留最近 OBS_KEEP_FILES 个 look-* 文件（每次观测约 0.8MB，不清会撑大） */
    private void pruneLooks(java.io.File dir) {
        try {
            java.io.File[] fs = dir.listFiles(new java.io.FilenameFilter() {
                @Override public boolean accept(java.io.File d, String name) {
                    return name.startsWith("look-") && !name.endsWith(".tmp");
                }
            });
            if (fs == null || fs.length <= OBS_KEEP_FILES) return;
            java.util.Arrays.sort(fs, new java.util.Comparator<java.io.File>() {
                @Override public int compare(java.io.File a, java.io.File b) {
                    return Long.compare(b.lastModified(), a.lastModified());
                }
            });
            for (int i = OBS_KEEP_FILES; i < fs.length; i++) fs[i].delete();
        } catch (Throwable ignored) {}
    }

    /** 复用上一帧：只换 nonce；图与控件树仍是那一帧的，标 reused=true / frameTs=原始帧时间 */
    private void writeReusedObservation(String nonce, org.json.JSONObject lastObj, long t0) {
        try {
            java.io.File dir = getExternalFilesDir(null);
            if (dir == null) return;
            String lastJpg = lastObj.optString("jpg", "");
            String newJpg = "look-" + nonce + ".jpg";
            java.io.File src = new java.io.File(dir, lastJpg);
            if (!src.isFile()) { log("❌ 复用时找不到上一帧 " + lastJpg); return; }
            copyFile(src, new java.io.File(dir, newJpg));
            org.json.JSONObject o = new org.json.JSONObject(lastObj.toString());
            long frameTs = lastObj.optLong("ts", 0L);
            o.put("nonce", nonce);
            // ⚠️ ts 保持"真帧时间"，绝不洗白成现在 —— 否则只读 ts 的消费者会把旧帧当新帧。
            o.put("ts", frameTs);
            o.put("servedAt", System.currentTimeMillis());
            o.put("ageMs", System.currentTimeMillis() - frameTs);
            o.put("reused", true);
            o.put("frameTs", frameTs);
            o.put("jpg", newJpg);
            o.put("ms", System.currentTimeMillis() - t0);
            writeAtomically(new java.io.File(dir, "look-" + nonce + ".json"), o.toString());
            pruneLooks(dir);
            log("♻️ 观测图复用 " + nonce + "（上一帧 "
                    + (System.currentTimeMillis() - lastObj.optLong("ts", 0L)) + " ms 前，"
                    + (new java.io.File(dir, newJpg).length() / 1024) + "KB，"
                    + (System.currentTimeMillis() - t0) + " ms）");
        } catch (Throwable t) { log("❌ 复用观测失败：" + t); }
    }

    /** 16x16 平均哈希（64bit → 16 hex）。复用**已经在手**的 Bitmap，零额外 adb 往返。 */
    private String aHash64(Bitmap b) {
        try {
            Bitmap s = Bitmap.createScaledBitmap(b, 16, 16, true);
            int[] px = new int[256];
            s.getPixels(px, 0, 16, 0, 0, 16, 16);
            int[] lum = new int[256];
            long sum = 0;
            for (int i = 0; i < 256; i++) {
                int pp = px[i];
                lum[i] = ((pp >> 16 & 0xff) * 29 + (pp >> 8 & 0xff) * 150 + (pp & 0xff) * 77) >> 8;
                sum += lum[i];
            }
            int avg = (int) (sum / 256);
            StringBuilder sb = new StringBuilder(16);
            for (int i = 0; i < 256; i += 4) {
                int nib = 0;
                for (int j = 0; j < 4; j++) nib = (nib << 1) | (lum[i + j] >= avg ? 1 : 0);
                sb.append("0123456789abcdef".charAt(nib));
            }
            return sb.toString();
        } catch (Throwable t) { return ""; }
    }

    /** 亮屏状态（App 侧直读）——替掉 safe_tap 里那次 dumpsys power 往返 */
    private boolean isInteractiveNow() {
        try {
            android.os.PowerManager pm = (android.os.PowerManager) getSystemService(POWER_SERVICE);
            return pm != null && pm.isInteractive();
        } catch (Throwable t) { return false; }
    }

    // ── 虚拟屏观测 / 手势（2026-10-20）─────────────────────────────────────────
    //   设计要点：
    //   1) 观测输出里**必须**带 displayId；safe_tap 用它和凭据、会话三方对齐。
    //   2) 复用缓存按 displayId 隔离 —— 否则主屏请求可能拿到虚拟屏的旧帧（或反过来）。
    //   3) 任何一步失败都写 err，绝不"顺手"回退主屏。
    private static volatile int obsLastDisplayId = -1;

    /** 统一观测入口：displayId=0 走主屏老路径；>0 走 AccessibilityService 的按屏 API。 */
    private void observeGraph(final int displayId, final String nonceI,
                              final boolean withImage, final boolean forceI) {
        if (!validNonce(nonceI)) { log("invalid_observation_nonce"); return; }
        new Thread(new Runnable() { @Override public void run() {
            long t0I = System.currentTimeMillis();
            org.json.JSONObject reuseObj = null;
            boolean owner = false;
            synchronized (OBS_LOCK) {
                if (withImage && !forceI && obsLastObj != null && obsLastDisplayId == displayId
                        && (obsBusy || (t0I - obsLastFrameTs) < OBS_MIN_INTERVAL_MS)) {
                    reuseObj = obsLastObj;
                } else if (!obsBusy) {
                    obsBusy = true; owner = true;
                }
            }
            try {
                if (reuseObj != null) { writeReusedObservation(nonceI, reuseObj, t0I); return; }
                AgentAccessibilityService acc = AgentAccessibilityService.INSTANCE;
                if (acc == null) {
                    writeObservationError(displayId, nonceI, "accessibility_not_connected");
                    return;
                }
                org.json.JSONObject snap = displayId == android.view.Display.DEFAULT_DISPLAY
                        ? acc.snapshot(800) : acc.snapshotOnDisplay(displayId, 800);
                String nodesJson = snap.optJSONArray("nodes") == null
                        ? "[]" : snap.optJSONArray("nodes").toString();
                String fgp = displayId == android.view.Display.DEFAULT_DISPLAY
                        ? foregroundPackage() : snap.optString("rootPkg", "");
                Bitmap bmp = withImage ? capture(displayId, 8000) : null;
                java.io.File dir = getExternalFilesDir(null);
                if (dir == null) { log("❌ 没有外部文件目录"); return; }
                String jpgName = "look-" + nonceI + ".jpg";
                String err = "";
                int w = 0, h = 0, bytes = 0;
                boolean black = false;
                String ahash = "";
                if (bmp != null) {
                    w = bmp.getWidth(); h = bmp.getHeight();
                    black = isMostlyBlack(bmp);
                    ahash = aHash64(bmp);
                    try (java.io.OutputStream fo = openOutput(new java.io.File(dir, jpgName))) {
                        bmp.compress(Bitmap.CompressFormat.JPEG, 82, fo);
                    }
                    bytes = (int) new java.io.File(dir, jpgName).length();
                } else if (withImage) {
                    err = "capture_failed";
                }
                org.json.JSONObject o = new org.json.JSONObject();
                o.put("nonce", nonceI);
                o.put("ts", System.currentTimeMillis());
                o.put("displayId", displayId);
                o.put("fg", fgp);
                o.put("treeHash", snap.optString("treeHash", ""));
                o.put("windowId", snap.optInt("windowId", -1));
                o.put("windowsOnDisplay", snap.optInt("windowsOnDisplay", -1));
                o.put("appWindows", snap.optInt("appWindows", -1));
                o.put("displayCoverage", snap.optString("coverage",
                        displayId == android.view.Display.DEFAULT_DISPLAY ? "tree" : "none"));
                o.put("interactive", isInteractiveNow());
                o.put("ahash", ahash);
                o.put("evTs", AgentAccessibilityService.lastEventTs);
                o.put("evType", AgentAccessibilityService.lastEventType);
                o.put("evCount", AgentAccessibilityService.eventCount);
                o.put("nodesRaw", nodesJson);
                o.put("image", bmp != null ? jpgName : org.json.JSONObject.NULL);
                o.put("jpg", bmp != null ? jpgName : "");
                o.put("w", w);
                o.put("h", h);
                o.put("black", black);
                o.put("jpgBytes", bytes);
                o.put("err", err);
                o.put("reused", false);
                o.put("ms", System.currentTimeMillis() - t0I);
                writeAtomically(new java.io.File(dir, "look-" + nonceI + ".json"), o.toString());
                pruneLooks(dir);
                if (bmp != null) {
                    synchronized (OBS_LOCK) {
                        obsLastObj = o; obsLastFrameTs = System.currentTimeMillis(); obsLastDisplayId = displayId;
                    }
                }
                log("📸 观测" + (displayId == 0 ? "图" : "屏图[display=" + displayId + "]") + " " + nonceI
                        + " → " + (bmp != null ? (w + "x" + h + " JPEG " + (bytes / 1024) + "KB") : ("无图（" + err + "）"))
                        + (black ? " ⚠全黑" : "") + " tree=" + snap.optInt("count", -1)
                        + " cov=" + snap.optString("coverage", "-") + "，" + (System.currentTimeMillis() - t0I) + " ms");
            } catch (Throwable t) {
                log("❌ 观测失败(display=" + displayId + ")：" + t);
                try { writeObservationError(displayId, nonceI, "observe_exception:" + t); } catch (Throwable ignored) {}
            } finally {
                if (owner) { synchronized (OBS_LOCK) { obsBusy = false; } }
            }
        }}).start();
    }

    /** 观测失败也写一份**带 nonce 与 displayId** 的文件，让调用方看到真实原因而不是超时。 */
    private void writeObservationError(int displayId, String nonce, String err) {
        try {
            java.io.File dir = getExternalFilesDir(null);
            if (dir == null) return;
            org.json.JSONObject o = new org.json.JSONObject();
            o.put("nonce", nonce);
            o.put("ts", System.currentTimeMillis());
            o.put("displayId", displayId);
            o.put("err", err);
            o.put("image", org.json.JSONObject.NULL);
            o.put("jpg", "");
            o.put("nodes", new org.json.JSONArray());
            o.put("nodesRaw", "[]");
            o.put("count", 0);
            o.put("treeHash", "0000000000000000");
            o.put("coverage", "none");
            writeAtomically(new java.io.File(dir, "look-" + nonce + ".json"), o.toString());
        } catch (Throwable ignored) {}
    }

    /** 手势回执：pending → completed/cancelled/rejected。safe_tap 只认 completed。 */
    private void dispatchDisplayGesture(final AgentAccessibilityService acc, final int displayId,
                                        final String kind, final int x1, final int y1,
                                        final int x2, final int y2, final int ms, final String nonce) {
        if (!validNonce(nonce)) { log("invalid_gesture_nonce"); return; }
        final java.io.File dir = getExternalFilesDir(null);
        if (dir == null) { log("❌ 没有外部文件目录，无法写手势回执"); return; }
        final java.io.File out = new java.io.File(dir, "gesture-" + nonce + ".json");
        try {
            org.json.JSONObject o = new org.json.JSONObject();
            o.put("nonce", nonce); o.put("displayId", displayId); o.put("kind", kind);
            o.put("x1", x1); o.put("y1", y1); o.put("x2", x2); o.put("y2", y2); o.put("ms", ms);
            o.put("accepted", true); o.put("status", "pending"); o.put("ts", System.currentTimeMillis());
            writeAtomically(out, o.toString());
        } catch (Throwable t) { log("❌ 写手势回执失败：" + t); return; }
        boolean accepted = acc.gestureOnDisplay(displayId, x1, y1, x2, y2, ms,
                new AgentAccessibilityService.GestureDone() {
                    @Override public void done(boolean ok, String status) {
                        try {
                            org.json.JSONObject r = new org.json.JSONObject();
                            r.put("nonce", nonce); r.put("displayId", displayId); r.put("kind", kind);
                            r.put("x1", x1); r.put("y1", y1); r.put("x2", x2); r.put("y2", y2); r.put("ms", ms);
                            r.put("accepted", ok); r.put("status", status);
                            r.put("completed", "completed".equals(status));
                            r.put("ts", System.currentTimeMillis());
                            writeAtomically(out, r.toString());
                            log("🖐 手势屏 display=" + displayId + " " + kind + " (" + x1 + "," + y1 + ")"
                                    + (("tap".equals(kind) || "long_press".equals(kind)) ? "" : "→(" + x2 + "," + y2 + ")")
                                    + " status=" + status);
                        } catch (Throwable t) { log("❌ 写手势回执失败：" + t); }
                    }
                });
        if (!accepted) {
            try {
                org.json.JSONObject r = new org.json.JSONObject();
                r.put("nonce", nonce); r.put("displayId", displayId); r.put("kind", kind);
                r.put("accepted", false); r.put("status", "rejected"); r.put("completed", false);
                r.put("ts", System.currentTimeMillis());
                writeAtomically(out, r.toString());
            } catch (Throwable ignored) {}
            log("❌ 手势屏被系统拒绝 display=" + displayId + " kind=" + kind);
        }
        pruneGestures(dir);
    }

    /** 只保留最近 40 个 gesture-* 回执（每个几百字节，仍要防无限增长）。 */
    private void pruneGestures(java.io.File dir) { pruneReceipts(dir, "gesture-"); }

    /** 只保留某类回执最近 40 个（gesture-* / clip-*）。 */
    private void pruneReceipts(java.io.File dir, final String prefix) {
        try {
            java.io.File[] fs = dir.listFiles(new java.io.FilenameFilter() {
                @Override public boolean accept(java.io.File d, String name) {
                    return name.startsWith(prefix) && name.endsWith(".json");
                }
            });
            if (fs == null || fs.length <= 40) return;
            java.util.Arrays.sort(fs, new java.util.Comparator<java.io.File>() {
                @Override public int compare(java.io.File a, java.io.File b) {
                    return Long.compare(b.lastModified(), a.lastModified());
                }
            });
            for (int i = 40; i < fs.length; i++) fs[i].delete();
        } catch (Throwable ignored) {}
    }

    private Bitmap capture(int displayId, int timeoutMs) {
        AgentAccessibilityService acc = AgentAccessibilityService.INSTANCE;
        long waitUntil = System.currentTimeMillis() + 6000;
        while (acc == null && System.currentTimeMillis() < waitUntil) {
            try { Thread.sleep(200); } catch (InterruptedException ignored) {}
            acc = AgentAccessibilityService.INSTANCE;
        }
        if (acc == null) { log("❌ 请到 设置→无障碍 里手动重开「逗沙包」"); return null; }
        for (int attempt = 1; attempt <= 3; attempt++) {
            final Bitmap[] box = new Bitmap[1];
            final String[] errBox = new String[1];
            final CountDownLatch latch = new CountDownLatch(1);
            acc.shot(displayId, (path, bmp, err) -> { box[0] = bmp; errBox[0] = err; latch.countDown(); });
            try { latch.await(timeoutMs, TimeUnit.MILLISECONDS); } catch (InterruptedException ignored) {}
            if (box[0] != null) return box[0];
            log("（截图失败 display=" + displayId + " " + (errBox[0] != null ? errBox[0] : "超时")
                    + "，重试 " + attempt + "/3）");
            try { Thread.sleep(800); } catch (InterruptedException ignored) {}
        }
        return null;
    }

    private Bitmap capture(int timeoutMs) {
        AgentAccessibilityService acc = AgentAccessibilityService.INSTANCE;
        long waitUntil = System.currentTimeMillis() + 6000;
        while (acc == null && System.currentTimeMillis() < waitUntil) {
            try { Thread.sleep(200); } catch (InterruptedException ignored) {}
            acc = AgentAccessibilityService.INSTANCE;
        }
        if (acc == null) { log("❌ 请到 设置→无障碍 里手动重开「逗沙包」"); return null; }
        for (int attempt = 1; attempt <= 3; attempt++) {
            final Bitmap[] box = new Bitmap[1];
            final String[] errBox = new String[1];
            final CountDownLatch latch = new CountDownLatch(1);
            acc.shot((path, bmp, err) -> { box[0] = bmp; errBox[0] = err; latch.countDown(); });
            try { latch.await(timeoutMs, TimeUnit.MILLISECONDS); } catch (InterruptedException ignored) {}
            if (box[0] != null) return box[0];
            log("（截图失败" + (errBox[0] != null ? "：" + errBox[0] : "：超时") + "，重试 " + attempt + "/3）");
            try { Thread.sleep(800); } catch (InterruptedException ignored) {}
        }
        return null;
    }

    private String jpegBase64(Bitmap bmp) {
        ByteArrayOutputStream bos = new ByteArrayOutputStream();
        bmp.compress(Bitmap.CompressFormat.JPEG, 72, bos);
        return Base64.encodeToString(bos.toByteArray(), Base64.NO_WRAP);
    }

    // ---------------- 通用循环：感知 → 问 DSH → 执行 ----------------

    /** 只做「明显高危」的兜底（DSH 自己也会给 sensitive），与具体 App 无关 */
    private static final String[] SENSITIVE_WORDS = {
            "确认支付", "立即支付", "付款码", "提交订单", "立即购买", "确认订单", "确认删除",
            "删除数据", "清空数据", "退出登录", "解绑银行卡", "转账", "提现", "免密支付", "授权扣款"};

    private boolean looksSensitive(String s) {
        if (s == null) return false;
        for (String w : SENSITIVE_WORDS) if (s.contains(w)) return true;
        return false;
    }

    /** 兜底模式：App 当眼睛和手，DSH 只回结构化动作（现在默认不用，除非 /do 不通） */

    /**
     * 默认执行方式（和网页里的 DSH 完全一样）：
     *   把目标交给 DSH，它自己用 bash + adb 动手（截屏、点按、输入、读通知…），
     *   本 App 只负责显示进度和结果，不再限制它的动作种类。
     *   /do 失败时明确报错，不隐式切换执行模式；无障碍模式由用户单独发起。
     */
    private void runGoal(final String goal) {
        if (goal == null || goal.trim().isEmpty()) { log("❌ 目标为空"); return; }
        final long gen = beginTask();
        if (gen < 0) return;
        // Admission must precede boot: an unconfirmed stop must not restart services.
        collapseWebUi();            // 要动别的 App 了：网页形态先收起，别挡住界面
        TermuxRunner.boot(this);    // 顺手确保手机侧 dsh / 桥都活着
        setBubbleRunning(true);     // 球变橙；交互都在网页里，不再弹面板
        setStatus("已交给 DSH…");
        log("🎯 目标：" + goal);
        log("   （DSH 自己用 bash+adb 动手，权限与网页会话一致：danger-full-access）");
        exec.execute(() -> {
            final long start = System.currentTimeMillis();
            try {
                if (!enterWorker(gen)) return;
                main.post(new Runnable() {
                    @Override public void run() {
                        synchronized (taskLock) {
                            if (!taskActive(gen) || taskTransportSettled) return;
                            long s = (System.currentTimeMillis() - start) / 1000;
                            // Already on the main thread: another post can outlive the final reply.
                            setStatus("DSH 正在做… 已 " + s + " 秒");
                        }
                        main.postDelayed(this, 2000);
                    }
                });
                JSONObject body = new JSONObject();
                body.put("goal", goal);
                body.put("task_id", taskRecoveryId);
                String fg = foregroundPackage();
                if (!fg.isEmpty()) body.put("notes", "执行前的前台应用包名：" + fg);
                if (!taskActive(gen)) return;
                DshClient.Result r = DshClient.post(this, taskBridgeBase, "/do", body, 900000);
                if (!taskActive(gen)) { log("🛑 已停止——丢弃迟到结果"); return; }
                long secs = (System.currentTimeMillis() - start) / 1000;
                if (r.ok && r.text != null && !r.text.trim().isEmpty()) {
                    synchronized (taskLock) { if (taskActive(gen)) taskTransportSettled = true; }
                    log("💡 " + r.text);
                    log("   （耗时 " + secs + " 秒）");
                    taskStatus(gen, "已回复，结果未核验（" + secs + " 秒）：" + (r.text.length() > 60 ? r.text.substring(0, 60) + "…" : r.text));
                } else {
                    String e = r.error == null ? "" : r.error;
                    if (e.contains("ConnectException") || e.contains("Failed to connect")) {
                        log("❌ 可信任务通道未就绪；任务状态需核验");
                        log("   修：Termux 里执行 /root/dsh-bridge/watchdog.sh（看门狗会自动拉起）");
                        taskStatus(gen, "❌ 桥没起来：先跑 watchdog.sh");
                    } else {
                        log("⚠️ /do 失败：" + e + "；未自动重试或切换执行模式");
                        taskStatus(gen, "⚠️ /do 失败，请检查后重新发起任务");
                    }
                    return;
                }
            } catch (Throwable t) {
                log("❌ 任务异常：" + t);
                taskStatus(gen, "❌ 异常：" + t);
            } finally {
                finishTask(gen, false);
            }
        });
    }

    static void ensureAccessibility(android.content.Context c) {
        AccessibilityRecovery.enable(c);
    }

    static boolean ensureAccessibilityReady(android.content.Context c, long waitMs) {
        return AccessibilityRecovery.await(c, waitMs, false);
    }

    private boolean ensureAccessibility(int waitMs) {
        boolean ready = AccessibilityRecovery.await(this, waitMs, true);
        if (!ready) {
            log("无障碍未就绪，请到 设置→无障碍 手动开启「逗沙包」");
            setStatus("请到 设置→无障碍 手动开启「逗沙包」");
        }
        return ready;
    }

    private void runGoalByAccessibility(final String goal) {
        if (goal == null || goal.trim().isEmpty()) { log("❌ 目标为空"); return; }
        final long gen = beginTask();
        if (gen < 0) return;
        collapseWebUi();
        setBubbleRunning(true);
        setStatus("开始：" + goal);
        log("🎯 目标：" + goal + "（最多 " + MAX_STEPS + " 步，每步都由 DSH 决定）");
        exec.execute(() -> {
            StringBuilder history = new StringBuilder();
            boolean wantShot = true;   // ② 元素树优先：只在第 1 步、以及大脑明确索取时才截图
            int frames = 0, shots = 0;
            try {
                if (!enterWorker(gen)) return;
                if (!ensureAccessibility(25000)) {
                    log("❌ 无障碍不可用，请到 设置→无障碍 里手动开启「逗沙包」");
                    setStatus("请到 设置→无障碍 里手动开启「逗沙包」");
                    return;
                }
                for (int step = 1; step <= MAX_STEPS && taskActive(gen); step++) {
                    log("—— 第 " + step + " 步 ——");
                    taskStatus(gen, "第 " + step + " 步：正在看屏幕…");
                    // ② 无障碍节点树在进程内读，毫秒级、不花视觉 token
                    String nodes = AgentAccessibilityService.INSTANCE.dumpNodes(60);
                    boolean treeEmpty = (nodes == null || nodes.trim().isEmpty());
                    Bitmap bmp = null;
                    if (wantShot || treeEmpty) {
                        boolean wasShown = panelShown;   // 截屏时把面板让开，截完立刻还回来
                        taskUi(gen, () -> showPanel(false));
                        Thread.sleep(300);
                        if (!taskActive(gen)) return;
                        bmp = capture(8000);
                        if (wasShown) taskUi(gen, () -> showPanel(true));
                    }
                    wantShot = false;
                    frames++;
                    if (bmp != null) shots++;
                    JSONObject body = new JSONObject();
                    body.put("goal", goal);
                    body.put("task_id", taskRecoveryId);
                    body.put("history", history.length() == 0 ? "(无)" : history.toString());
                    if (bmp != null) { body.put("imageBase64", jpegBase64(bmp)); body.put("ext", "jpg"); }
                    body.put("shot", bmp != null);
                    body.put("nodes", nodes == null ? "" : nodes);
                    body.put("foregroundApp", foregroundPackage());
                    body.put("notifications", recentNotifications());
                    log("👁 观测 #" + frames + "：元素树" + (treeEmpty ? "空" : "有") + "，截图" + (bmp != null ? "有" : "无"));
                    if (!taskActive(gen)) return;
                    DshClient.Result r = DshClient.post(this, taskBridgeBase, "/step", body, 600000);
                    if (!taskActive(gen)) { log("🛑 已停止——丢弃迟到结果"); return; }
                    if (!r.ok || r.action == null) {
                        taskStatus(gen, "❌ DSH 没给出动作");
                        log("❌ DSH 没给出动作：" + r.error + (r.text == null || r.text.isEmpty() ? "" : "\n    它说：" + r.text));
                        break;
                    }
                    JSONObject a = r.action;
                    // ③ 批量动作：大脑一次可以给 {"actions":[...]}，减少 LLM 往返次数
                    JSONArray batch = a.optJSONArray("actions");
                    if (batch != null && batch.length() > 0) {
                        boolean stop = false, anySensitive = false;
                        for (int i = 0; i < batch.length(); i++) {
                            if (taskCancelled || gen != taskGeneration) { log("🛑 已停止——批内中止"); stop = true; break; }
                            JSONObject one = batch.optJSONObject(i);
                            if (one == null) continue;
                            String act = one.optString("action", "?");
                            if ("done".equals(act) || "ask".equals(act)) stop = true;
                            if (one.optBoolean("sensitive", false) || looksSensitive(one.optString("reason", ""))) anySensitive = true;
                        }
                        log("🤖 批量动作 ×" + batch.length() + (a.has("reason") ? "（" + a.optString("reason") + "）" : ""));
                        taskStatus(gen, "第 " + step + " 步：批量 " + batch.length() + " 个动作");
                        if (anySensitive) {
                            taskUi(gen, () -> { showPanel(true); showConfirmBar(true); });
                            log("⚠️ 这批动作里有风险步骤，等你点顶部「✅ 允许」或「⛔ 拒绝」（180 秒不点=拒绝）");
                            String d = waitForDecision(gen, 180000);
                            taskUi(gen, () -> showConfirmBar(false));
                            if (!"allow".equals(d)) { log("⛔ 未允许（" + d + "），停止"); break; }
                        }
                        for (int i = 0; i < batch.length(); i++) {
                            if (taskCancelled || gen != taskGeneration) { log("🛑 已停止——批内中止"); stop = true; break; }
                            JSONObject one = batch.optJSONObject(i);
                            if (one == null) continue;
                            String act = one.optString("action", "?");
                            String why = one.optString("reason", "");
                            if ("done".equals(act)) { log("✅ 完成：" + why); taskStatus(gen, "✅ " + why); stop = true; break; }
                            if ("ask".equals(act)) { log("❓ DSH 需要你补充：" + why); taskStatus(gen, "❓ " + why); stop = true; break; }
                            if ("screenshot".equals(act)) { wantShot = true; log("👁 大脑要求下一步带截图"); continue; }
                            StringBuilder line = new StringBuilder("    " + (i + 1) + ") " + act);
                            if (one.has("x")) line.append(" (").append(one.optInt("x")).append(",").append(one.optInt("y")).append(")");
                            if (one.has("text")) line.append(" 文本=“").append(one.optString("text")).append("”");
                            log(line.toString());
                            if (i > 0) Thread.sleep(400);   // 批内只等界面反应，不等模型
                            if (!dispatchTaskAction(gen, one)) { stop = true; break; }
                            history.append(step).append(".").append(i + 1).append(". ").append(act).append(" — ").append(why).append("\n");
                        }
                        if (stop) break;
                        Thread.sleep(1200);
                        continue;
                    }
                    // 单动作（旧协议，保持兼容）
                    String action = a.optString("action", "?");
                    String reason = a.optString("reason", "");
                    boolean sensitive = a.optBoolean("sensitive", false) || looksSensitive(reason);
                    StringBuilder line = new StringBuilder("🤖 " + action);
                    if (a.has("x")) line.append(" (").append(a.optInt("x")).append(",").append(a.optInt("y")).append(")");
                    if (a.has("text")) line.append(" 文本=“").append(a.optString("text")).append("”");
                    log(line.toString());
                    log("    理由：" + reason);
                    taskStatus(gen, "第 " + step + " 步：" + action
                            + (a.has("text") ? " \"" + a.optString("text") + "\"" : "")
                            + (reason.isEmpty() ? "" : " · " + reason));
                    if ("done".equals(action)) { log("✅ 完成：" + reason); taskStatus(gen, "✅ " + reason); break; }
                    if ("ask".equals(action)) { log("❓ DSH 需要你补充：" + reason); taskStatus(gen, "❓ " + reason); break; }
                    if ("screenshot".equals(action)) { wantShot = true; log("👁 大脑要求下一步带截图"); continue; }
                    if (sensitive) {
                        taskUi(gen, () -> { showPanel(true); showConfirmBar(true); });
                        log("⚠️ DSH 标了这一步有风险，等你点顶部「✅ 允许」或「⛔ 拒绝」（180 秒不点=拒绝）");
                        String d = waitForDecision(gen, 180000);
                        taskUi(gen, () -> showConfirmBar(false));
                        if (!"allow".equals(d)) { log("⛔ 未允许（" + d + "），停止"); break; }
                    }
                    if (!dispatchTaskAction(gen, a)) return;
                    history.append(step).append(". ").append(action).append(" — ").append(reason).append("\n");
                    Thread.sleep(1200);
                }
            } catch (Throwable t) {
                log("❌ 任务异常：" + t);
            } finally {
                finishTask(gen, true);
                log("—— 任务结束（观测 " + frames + " 次，截图 " + shots + " 张）——");
            }
        });
    }

    /** 通用设备上下文：最近的几条通知（谁发的 + 内容），不给任何 App 开小灶 */
    private String recentNotifications() {
        try {
            List<String[]> all = NotificationWatcher.snapshot();
            if (all.isEmpty()) return "";
            StringBuilder sb = new StringBuilder();
            for (int i = 0; i < Math.min(all.size(), 5); i++) {
                String[] r = all.get(i);
                String text = r[2] == null ? "" : (r[2].length() > 60 ? r[2].substring(0, 60) : r[2]);
                sb.append("- [").append(r[0]).append("] ").append(r[1]).append("：").append(text).append("\n");
            }
            return sb.toString();
        } catch (Throwable t) { return ""; }
    }

    private ActionReceipt executeAction(JSONObject a) {
        AgentAccessibilityService acc = AgentAccessibilityService.INSTANCE;
        if (acc == null) return ActionReceipt.of("accessibility_unavailable");
        switch (a.optString("action")) {
            case "tap": case "longpress": case "swipe":
                boolean swipe = "swipe".equals(a.optString("action"));
                if (!integerField(a, "x") || !integerField(a, "y") || (swipe && (!integerField(a, "x2") || !integerField(a, "y2")))
                        || (swipe && a.has("ms") && !integerField(a, "ms")))
                    return ActionReceipt.of("invalid_coordinates");
                return acc.gestureWithReceipt(a.optInt("x", -1), a.optInt("y", -1),
                    swipe ? a.optInt("x2", -1) : a.optInt("x", -1), swipe ? a.optInt("y2", -1) : a.optInt("y", -1),
                    swipe ? a.optInt("ms", 400) : ("longpress".equals(a.optString("action")) ? 800 : 60));
            case "type": {
                if (!a.has("text")) return ActionReceipt.of("missing_text");
                if (!(a.opt("text") instanceof String)) return ActionReceipt.of("invalid_text");
                return acc.typeWithReceipt(a.optString("text", ""));
            }
            case "open_app": return ActionReceipt.of(openApp(a.optString("text", "")) ? "accepted" : "launch_failed");
            case "back": return ActionReceipt.of(acc.performGlobalAction(android.accessibilityservice.AccessibilityService.GLOBAL_ACTION_BACK) ? "accepted" : "rejected");
            case "home": return ActionReceipt.of(acc.performGlobalAction(android.accessibilityservice.AccessibilityService.GLOBAL_ACTION_HOME) ? "accepted" : "rejected");
            case "recents": return ActionReceipt.of(acc.performGlobalAction(android.accessibilityservice.AccessibilityService.GLOBAL_ACTION_RECENTS) ? "accepted" : "rejected");
            case "wait": return ActionReceipt.of("completed");
            default: return ActionReceipt.of("unsupported_action");
        }
    }

    private static boolean integerField(JSONObject action, String key) {
        Object value = action.opt(key);
        if (!(value instanceof Number)) return false;
        double n = ((Number) value).doubleValue();
        return !Double.isNaN(n) && !Double.isInfinite(n) && n >= Integer.MIN_VALUE && n <= Integer.MAX_VALUE && n == Math.rint(n);
    }

    private void askDsh(final String question) {
        if (question == null || question.trim().isEmpty()) return;
        final long gen = beginTask();
        if (gen < 0) return;
        showPanel(true);
        setBubbleRunning(true);
        log("❓ 问 DSH：" + question);
        exec.execute(() -> {
            try {
                if (!enterWorker(gen)) return;
                Bitmap bmp = capture(8000);
                JSONObject body = new JSONObject();
                body.put("task_id", taskRecoveryId);
                body.put("prompt", question);
                if (bmp != null) {
                    body.put("imageBase64", jpegBase64(bmp));
                    body.put("ext", "jpg");
                }
                body.put("foregroundApp", foregroundPackage());
                body.put("notifications", recentNotifications());
                AgentAccessibilityService acc2 = AgentAccessibilityService.INSTANCE;
                if (acc2 != null) body.put("nodes", acc2.dumpNodes(60));
                log("… 已把" + (bmp != null ? "当前屏幕 + " : "") + "问题交给 DSH");
                if (!taskActive(gen)) return;
                DshClient.Result r = DshClient.post(this, taskBridgeBase, "/ask", body, 600000);
                if (!taskActive(gen)) return;
                if (!r.ok) log("❌ DSH 返回错误：" + (r.error.isEmpty() ? r.text : r.error));
                else if (r.text != null && !r.text.trim().isEmpty()) {
                    synchronized (taskLock) { if (taskActive(gen)) taskTransportSettled = true; }
                    log("💡 DSH：" + r.text);
                    taskStatus(gen, "已回复，结果未核验");
                }
            } catch (Throwable t) { log("❌ 问 DSH 失败：" + t); }
            finally { finishTask(gen, true); }
        });
    }

    // ---------------- 系统语音识别（本机若无识别服务则不可用） ----------------

    private void startVoice() {
        if (!SpeechRecognizer.isRecognitionAvailable(this)) {
            log("❌ 本机没有可用的语音识别服务，用面板里的「语音/打字说目标」");
            return;
        }
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            log("❌ 没有麦克风权限（跑一次 adb 授权脚本即可恢复）");
            return;
        }
        if (asr == null) {
            asr = SpeechRecognizer.createSpeechRecognizer(this);
            asr.setRecognitionListener(new RecognitionListener() {
                @Override public void onReadyForSpeech(Bundle p) { log("🎙 在听…"); }
                @Override public void onBeginningOfSpeech() {}
                @Override public void onRmsChanged(float v) {}
                @Override public void onBufferReceived(byte[] b) {}
                @Override public void onEndOfSpeech() {}
                @Override public void onError(int e) { log("🎙 识别出错 code=" + e); }
                @Override public void onResults(Bundle r) {
                    ArrayList<String> list = r.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION);
                    if (list != null && !list.isEmpty()) handleCommand(list.get(0));
                    else log("🎙 没听清");
                }
                @Override public void onPartialResults(Bundle r) {}
                @Override public void onEvent(int t, Bundle b) {}
            });
        }
        Intent i = new Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH);
        i.putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM);
        i.putExtra(RecognizerIntent.EXTRA_LANGUAGE, "zh-CN");
        i.putExtra(RecognizerIntent.EXTRA_PARTIAL_RESULTS, true);
        try { asr.startListening(i); } catch (Throwable t) { log("🎙 启动失败：" + t); }
    }
}
