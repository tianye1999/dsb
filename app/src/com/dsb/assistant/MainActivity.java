package com.dsb.assistant;

import android.app.Activity;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.pm.ApplicationInfo;
import android.graphics.Bitmap;
import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.Path;
import android.graphics.drawable.Drawable;
import android.graphics.drawable.GradientDrawable;
import android.widget.ImageView;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.SystemClock;
import android.util.Log;
import android.util.TypedValue;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.webkit.CookieManager;
import android.webkit.URLUtil;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.TextView;

import org.json.JSONObject;

/**
 * DSH 的"网页形态"：一个全屏 WebView 显示 dsh web UI。
 *
 * 设计要点（2026-09-22）：
 *   * 没有权限向导、没有设置页——打开就是网页；权限由一次性 adb 脚本授予。
 *   * 打开时若 dsh 没在跑，先让 Termux 侧把它拉起来，并显示启动进度。
 *   * 悬浮球服务要操作别的 App 时会广播 COLLAPSE，本页立刻收起到蓝色小球，
 *     避免挡住被操作的界面；长按小球再变回网页形态。
 *   * 本页跑在独立进程 :web，网页崩了也不会带走悬浮球/无障碍服务。
 */
public class MainActivity extends Activity {
    private static final String TAG = "DSB";

    private WebView web;
    private LinearLayout splash;
    private TextView splashText;
    private Button retryBtn;

    private String loadedUrl;
    private volatile boolean ensuring;
    private long lastUrlCheck;
    private final Handler recoveryHandler = new Handler(Looper.getMainLooper());
    private Runnable retryReensure;
    private Runnable recoveryProgress;
    private static final long RECOVERY_WINDOW_MS = 90000;
    private static final long STARTUP_RETRY_MS = 1000;
    private long recoveryStartedAt = -1;
    private long lastRecoveryBoot = -1;
    private int recoveryGeneration;
    private final java.util.Set<String> failedUrls = new java.util.HashSet<>();
    private boolean mainFrameFailed;
    private volatile boolean destroyed;

    /** 网页里的"选择文件"（附件上传）需要 Activity 帮忙起系统选择器 */
    private ValueCallback<Uri[]> filePathCallback;
    private static final int FILE_CHOOSER_REQ = 1001;

    private final BroadcastReceiver collapseReceiver = new BroadcastReceiver() {
        @Override public void onReceive(Context c, Intent i) {
            String a = String.valueOf(i.getAction());
            if (OverlayService.ACTION_COLLAPSE.equals(a)) {
                Log.i(TAG, "COLLAPSE -> 收起成蓝色小球");
                moveTaskToBack(true);
            }
        }
    };

    private int dp(float v) { return (int) (v * getResources().getDisplayMetrics().density); }

    // Keep WebSQL enabled for older WebView engines; newer engines ignore this setting.
    @SuppressWarnings("deprecation")
    @Override protected void onCreate(Bundle s) {
        super.onCreate(s);

        FrameLayout root = new FrameLayout(this);
        root.setBackgroundColor(0xFF0B1220);

        web = new WebView(this);
        web.setBackgroundColor(0xFF0B1220);
        WebSettings ws = web.getSettings();
        ws.setJavaScriptEnabled(true);
        ws.setDomStorageEnabled(true);
        ws.setDatabaseEnabled(true);
        ws.setUseWideViewPort(true);
        ws.setLoadWithOverviewMode(false);
        ws.setMediaPlaybackRequiresUserGesture(false);
        ws.setMixedContentMode(WebSettings.MIXED_CONTENT_COMPATIBILITY_MODE);
        ws.setTextZoom(75);          // 网页正文缩到原来的 3/4（用户要求；A/B 实测 19.2→14.4px）
        ws.setJavaScriptCanOpenWindowsAutomatically(true);
        ws.setSupportMultipleWindows(false);
        WebView.setWebContentsDebuggingEnabled((getApplicationInfo().flags & ApplicationInfo.FLAG_DEBUGGABLE) != 0);

        web.setWebChromeClient(new WebChromeClient() {
            @Override public boolean onShowFileChooser(WebView v, ValueCallback<Uri[]> cb, FileChooserParams params) {
                if (filePathCallback != null) { filePathCallback.onReceiveValue(null); filePathCallback = null; }
                filePathCallback = cb;
                try {
                    startActivityForResult(params.createIntent(), FILE_CHOOSER_REQ);
                    return true;
                } catch (Throwable t) {
                    filePathCallback = null;
                    return false;
                }
            }
        });
        // 网页里的下载（导出会话等）：交给系统下载器
        web.setDownloadListener((url, ua, contentDisposition, mime, len) -> {
            try {
                android.app.DownloadManager.Request req = new android.app.DownloadManager.Request(Uri.parse(url));
                String cookie = CookieManager.getInstance().getCookie(url);
                if (cookie != null) req.addRequestHeader("Cookie", cookie);
                req.addRequestHeader("User-Agent", ua);
                req.setNotificationVisibility(android.app.DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED);
                req.setDestinationInExternalPublicDir(android.os.Environment.DIRECTORY_DOWNLOADS,
                        URLUtil.guessFileName(url, contentDisposition, mime));
                ((android.app.DownloadManager) getSystemService(DOWNLOAD_SERVICE)).enqueue(req);
            } catch (Throwable ignored) {}
        });
        web.setWebViewClient(new WebViewClient() {
            @Override public boolean shouldOverrideUrlLoading(WebView v, WebResourceRequest req) {
                return handleUrl(String.valueOf(req.getUrl()));
            }
            @Override public void onReceivedHttpError(WebView v, WebResourceRequest req, WebResourceResponse r) {
                if (r == null) return;
                int code = r.getStatusCode();
                if (req != null && req.isForMainFrame() && code >= 400) {
                    onMainFrameError(req.getUrl() == null ? null : req.getUrl().toString());
                    return;
                }
                String u = req == null || req.getUrl() == null ? "" : String.valueOf(req.getUrl());
                // 会话失效的两种表现（实测）：页面回 401/403；带失效 cookie 的 /api 请求回 404。
                // 之前只认 401/403，结果 dsh 重启换 token 后 App 卡在"蓝圈转圈"不自愈。
                if (sameOrigin(u, loadedUrl)
                        && (code == 401 || code == 403 || (code == 404 && u.contains("/api/")))) {
                    Log.i(TAG, "网页会话失效 HTTP " + code + " → 重新取地址");
                    onMainFrameError(v.getUrl());
                }
            }
            @Override public void onReceivedError(WebView v, WebResourceRequest req, WebResourceError e) {
                if (req != null && req.isForMainFrame()) {
                    onMainFrameError(req.getUrl() == null ? null : req.getUrl().toString());
                }
            }
            @Override public void onPageFinished(WebView v, String url) {
                if (destroyed || mainFrameFailed || ensuring || retryReensure != null
                        || url == null || !(url.startsWith("http://") || url.startsWith("https://"))
                        || !url.equals(v.getUrl())) return;
                resetRecovery();
                failedUrls.clear();
                mainFrameFailed = false;
                splash.setVisibility(View.GONE);
                web.setVisibility(View.VISIBLE);
            }
        });
        root.addView(web, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));

        root.addView(buildSplash(), new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
        setContentView(root);

        IntentFilter f = new IntentFilter(OverlayService.ACTION_COLLAPSE);
        f.addAction(OverlayService.ACTION_EXPAND);
        if (Build.VERSION.SDK_INT >= 33) registerReceiver(collapseReceiver, f, Context.RECEIVER_NOT_EXPORTED);
        else registerReceiver(collapseReceiver, f);

        // 用户主动打开 App = 撤销"已关闭"标记 + 记下本次开机会话，然后让小球也活着
        try { OverlayService.closedFlag(this).delete(); } catch (Throwable ignored) {}
        OverlayService.markAppRunning(this);
        try { startForegroundService(new Intent(this, OverlayService.class)); } catch (Throwable ignored) {}

        ensureAndLoad();
    }

    /** 启动画面：App 图标（圆形，和桌面一致）+ 进度文字（没有"设置向导"，只有等待） */
    private View buildSplash() {
        splash = new LinearLayout(this);
        splash.setOrientation(LinearLayout.VERTICAL);
        splash.setGravity(Gravity.CENTER);
        splash.setBackgroundColor(0xFF0B1220);

        // 2026-09-22：启动页不再画纯色圆，直接用 App 图标（自适应图标裁成圆形，和桌面上一致）。
        // 做法：把图标画进位图，按 108/72 放大取中央安全区（等于桌面的圆形裁切），再圆形裁剪。
        ImageView icon = new ImageView(this);
        try {
            int size = dp(112);
            Drawable d = getPackageManager().getApplicationIcon(getPackageName());
            Bitmap bmp = Bitmap.createBitmap(size, size, Bitmap.Config.ARGB_8888);
            Canvas cv = new Canvas(bmp);
            Path circle = new Path();
            circle.addCircle(size / 2f, size / 2f, size / 2f, Path.Direction.CW);
            cv.clipPath(circle);
            cv.scale(108f / 72f, 108f / 72f, size / 2f, size / 2f);   // 中央 72dp 铺满 = 桌面图标的样子
            d.setBounds(0, 0, size, size);
            d.draw(cv);
            icon.setImageBitmap(bmp);
        } catch (Throwable t) {
            Log.i(TAG, "图标渲染失败，退回纯色圆：" + t.getClass().getSimpleName());
            GradientDrawable bg = new GradientDrawable(GradientDrawable.Orientation.TL_BR,
                    new int[]{0xFF7FC0FF, 0xFF2F7FF0, 0xFF12408F});
            bg.setShape(GradientDrawable.OVAL);
            bg.setStroke(dp(3), 0xF2FFFFFF);
            icon.setBackground(bg);
        }
        LinearLayout.LayoutParams olp = new LinearLayout.LayoutParams(dp(112), dp(112));
        olp.bottomMargin = dp(18);
        splash.addView(icon, olp);

        TextView title = new TextView(this);
        title.setText("逗沙包");
        title.setTextColor(0xFFFFFFFF);
        title.setTextSize(26);
        title.setGravity(Gravity.CENTER);
        splash.addView(title);

        splashText = new TextView(this);
        splashText.setTextColor(0xFF9FC3F0);
        splashText.setTextSize(13);
        splashText.setGravity(Gravity.CENTER);
        splashText.setPadding(dp(24), dp(14), dp(24), dp(14));
        splashText.setText("正在启动…");
        splash.addView(splashText);

        retryBtn = new Button(this);
        retryBtn.setText("重试");
        retryBtn.setAllCaps(false);
        retryBtn.setVisibility(View.GONE);
        retryBtn.setOnClickListener(v -> { resetRecovery(); ensureAndLoad(); });
        splash.addView(retryBtn);

        TextView hint = new TextView(this);
        hint.setTextColor(0xFF5E7BA6);
        hint.setTextSize(13);
        hint.setGravity(Gravity.CENTER);
        hint.setPadding(dp(24), dp(22), dp(24), 0);
        hint.setText("你开口，手机动手");
        splash.addView(hint);
        return splash;
    }

    private void showSplash(final String text) {
        runOnUiThread(() -> {
            if (destroyed) return;
            if (splash != null) splash.setVisibility(View.VISIBLE);
            if (web != null) web.setVisibility(View.GONE);
            if (splashText != null) splashText.setText(text);
        });
    }

    /** Fetch the cached private URL; WebView handles session expiry. */
    private String fetchUrl() {
        try {
            JSONObject envelope = TermuxRunner.request(this, "/dsh-url", new JSONObject(), 6000);
            JSONObject o = envelope.getJSONObject("body");
            if (o.optBoolean("ok", false)) {
                String u = o.optString("url", "");
                return u.isEmpty() ? null : u;
            }
        } catch (Throwable ignored) {}
        return null;
    }

    private void ensureAndLoad() {
        if (destroyed || ensuring) return;
        beginRecovery();
        if (recoveryExpired()) { exhaustRecovery(); return; }
        if (!failedUrls.isEmpty()) requestRecoveryBoot();
        ensuring = true;
        final int generation = recoveryGeneration;
        new Thread(() -> {
            if (destroyed) return;
            final String u = fetchUrl();
            runOnUiThread(() -> {
                if (destroyed || generation != recoveryGeneration) return;
                ensuring = false;
                if (recoveryExpired()) { exhaustRecovery(); return; }
                if (u != null && !failedUrls.contains(u)) load(u);
                else {
                    requestRecoveryBoot();
                    maybeReensure();
                }
            });
        }, "dsb-ensure").start();
    }

    /** A failed document must never reveal Chromium's token-bearing error page. */
    private void onMainFrameError(String failedUrl) {
        if (destroyed) return;
        // The first failed document gets a full window, even after a slow cold load.
        if (failedUrls.isEmpty() && !mainFrameFailed) resetRecovery();
        if (failedUrl == null || failedUrl.isEmpty()) failedUrl = web.getUrl();
        if (failedUrl == null || failedUrl.isEmpty()) failedUrl = loadedUrl;
        if (failedUrl != null) failedUrls.add(failedUrl);
        // A redirect can fail at '/' while the cache still contains its old token URL.
        if (loadedUrl != null && sameOrigin(failedUrl, loadedUrl)) failedUrls.add(loadedUrl);
        mainFrameFailed = true;
        beginRecovery();
        showSplash("正在唤醒…已等 " + recoveryElapsedSeconds() + " 秒");
        web.stopLoading();
        requestRecoveryBoot();
        maybeReensure();
    }

    private boolean sameOrigin(String left, String right) {
        try {
            java.net.URI a = java.net.URI.create(left), b = java.net.URI.create(right);
            return a.getScheme() != null && a.getScheme().equalsIgnoreCase(b.getScheme())
                    && a.getHost() != null && a.getHost().equalsIgnoreCase(b.getHost())
                    && a.getPort() == b.getPort();
        } catch (RuntimeException ignored) { return false; }
    }

    private long recoveryElapsedSeconds() {
        return Math.max(0, SystemClock.elapsedRealtime() - recoveryStartedAt) / 1000;
    }

    private boolean recoveryExpired() {
        return recoveryStartedAt >= 0 && SystemClock.elapsedRealtime() - recoveryStartedAt >= RECOVERY_WINDOW_MS;
    }

    private void beginRecovery() {
        if (recoveryStartedAt >= 0) return;
        recoveryStartedAt = SystemClock.elapsedRealtime();
        retryBtn.setVisibility(View.GONE);
        recoveryProgress = new Runnable() {
            @Override public void run() {
                if (destroyed || recoveryProgress != this) return;
                if (recoveryExpired()) { exhaustRecovery(); return; }
                showSplash("正在唤醒…已等 " + recoveryElapsedSeconds() + " 秒");
                recoveryHandler.postDelayed(this, 1000);
            }
        };
        recoveryProgress.run();
    }

    private void requestRecoveryBoot() {
        if (destroyed || recoveryExpired()) return;
        long now = SystemClock.elapsedRealtime();
        if (lastRecoveryBoot < 0 || now - lastRecoveryBoot >= 10000) {
            lastRecoveryBoot = now;
            TermuxRunner.boot(this);
        }
    }

    /** One queued cache read; the deadline also covers a new URL that never finishes loading. */
    private void maybeReensure() {
        if (destroyed || ensuring || retryReensure != null) return;
        beginRecovery();
        if (recoveryExpired()) { exhaustRecovery(); return; }
        long delay = Math.min(STARTUP_RETRY_MS,
                RECOVERY_WINDOW_MS - (SystemClock.elapsedRealtime() - recoveryStartedAt));
        retryReensure = () -> {
            retryReensure = null;
            ensureAndLoad();
        };
        recoveryHandler.postDelayed(retryReensure, delay);
    }

    private void resetRecovery() {
        cancelRecoveryWork();
        recoveryStartedAt = -1;
        lastRecoveryBoot = -1;
        retryBtn.setVisibility(View.GONE);
    }

    private void cancelRecoveryWork() {
        recoveryHandler.removeCallbacksAndMessages(null);
        retryReensure = null;
        recoveryProgress = null;
        recoveryGeneration++;
        ensuring = false;
    }

    private void exhaustRecovery() {
        cancelRecoveryWork();
        mainFrameFailed = true;
        showSplash("暂时无法连接，请稍后重试");
        retryBtn.setVisibility(View.VISIBLE);
    }

    /** 2026-09-22：**启动画面不人为延时** —— 用户后来明确要求停止"最少展示 1.2 秒"的改动，
     *  服务就绪就立刻揭开（原行为）。 */
    private void load(String url) {
        if (destroyed) return;
        loadedUrl = url;
        lastUrlCheck = System.currentTimeMillis();
        mainFrameFailed = false;
        beginRecovery();
        retryBtn.setVisibility(View.GONE);
        Log.i(TAG, "load WebView");
        if (url.equals(web.getUrl())) web.reload(); else web.loadUrl(url);
    }

    /** 2026-09-22 用户要求：**任何网页都不许弹外部浏览器**，一律在 WebView 里打开（返回键可回退）。
     *  只有电话/邮件/深链这类非网页协议才交给系统。 */
    private boolean handleUrl(String u) {
        if (u == null) return false;
        String s = u.toLowerCase();
        if (s.startsWith("http://") || s.startsWith("https://") || s.startsWith("file://")) return false;  // 全部内嵌
        try { startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse(u))); } catch (Throwable ignored) {}
        return true;
    }

    @Override protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        if (web != null) web.onResume();
    }

    @Override protected void onResume() {
        super.onResume();
        if (mainFrameFailed && !ensuring && retryReensure == null) {
            resetRecovery();
            ensureAndLoad();
            return;
        }
        // 回到前台时确认地址还有效（dsh 重启会换 token）
        if (!ensuring && retryReensure == null && loadedUrl != null && System.currentTimeMillis() - lastUrlCheck > 60000) {
            final int generation = recoveryGeneration;
            new Thread(() -> {
                String u = fetchUrl();
                runOnUiThread(() -> {
                    if (destroyed || generation != recoveryGeneration || mainFrameFailed || ensuring || retryReensure != null) return;
                    if (u != null && !u.equals(loadedUrl) && !failedUrls.contains(u)) load(u);
                    else lastUrlCheck = System.currentTimeMillis();
                });
            }, "dsb-recheck").start();
        }
    }

    @Override protected void onActivityResult(int req, int res, Intent data) {
        if (req == FILE_CHOOSER_REQ) {
            if (filePathCallback != null) {
                filePathCallback.onReceiveValue(WebChromeClient.FileChooserParams.parseResult(res, data));
                filePathCallback = null;
            }
            return;
        }
        super.onActivityResult(req, res, data);
    }

    // Preserve the existing back-to-WebView/hide contract on pre-API-33 devices.
    @SuppressWarnings("deprecation")
    @Override public void onBackPressed() {
        if (web != null && web.canGoBack()) web.goBack();
        else moveTaskToBack(true);      // 返回 = 收起成小球，而不是退出
    }

    @Override protected void onDestroy() {
        destroyed = true;
        cancelRecoveryWork();
        try { unregisterReceiver(collapseReceiver); } catch (Throwable ignored) {}
        super.onDestroy();
    }
}
