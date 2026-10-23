package com.dsb.assistant;

import android.accessibilityservice.AccessibilityService;
import android.accessibilityservice.GestureDescription;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.Context;
import android.graphics.Bitmap;
import android.graphics.Path;
import android.os.Bundle;
import android.util.Log;
import android.view.Display;
import android.view.accessibility.AccessibilityEvent;
import android.view.accessibility.AccessibilityNodeInfo;

/** 无障碍服务：截屏 + 手势 + 输入（视觉 agent 的执行手） */
public class AgentAccessibilityService extends AccessibilityService {
    public static volatile AgentAccessibilityService INSTANCE;

    /**
     * 最近一次窗口变化时的前台包名（2026-09-23 加）。
     *
     * ═══ 为什么需要 ═══
     * 用户要求"TTS 朗读结束后，如果我在其他 App，就把界面带回逗沙包"。
     * 但 ActivityManager.getRunningTasks() 自 Android 5 起**对第三方应用只返回自己的任务**，
     * 拿不到别的 App（实测 topPackage() 一直返回 null，导致该功能不生效）。
     * 无障碍服务本来就能收到全系统的窗口事件 → 在这里顺手记一份即可，无需任何新权限。
     */
    public static volatile String lastTopPackage = null;
    /** 2026-10-01 加：最近一次无障碍事件（给 safe_tap 当"页面变了没"的零往返信号） */
    public static volatile long lastEventTs = 0L;
    public static volatile int lastEventType = 0;
    public static volatile int eventCount = 0;


    public interface ShotCallback { void done(String path, Bitmap bmp, String error); }

    @Override protected void onServiceConnected() {
        super.onServiceConnected();
        INSTANCE = this;
        Log.i("DSH", "accessibility connected");
    }

    @Override public void onDestroy() { INSTANCE = null; super.onDestroy(); }
    @Override public void onAccessibilityEvent(AccessibilityEvent event) {
        try {
            int t = event.getEventType();
            // 2026-10-01：记一份事件流指纹，搭快照的便车写进 look.json（零额外往返）
            lastEventTs = System.currentTimeMillis();
            lastEventType = t;
            eventCount++;

            // 记录前台包名（任何窗口事件都记，供 OverlayService 判断"用户在看哪个 App"）
            if (t == AccessibilityEvent.TYPE_WINDOW_STATE_CHANGED
                    || t == AccessibilityEvent.TYPE_WINDOW_CONTENT_CHANGED) {
                CharSequence tp = event.getPackageName();
                if (tp != null && tp.length() > 0) lastTopPackage = tp.toString();
            }
            // Tier-2 事件驱动盯梢（2026-10-01）：通知事件 → 落盘 notifications.jsonl。
            // 通道 = look.json 同款（getExternalFilesDir，手机侧 adb shell cat 读）。
            // 轻量：后台线程写；异常全吞——绝不因通知事件影响无障碍服务稳定性。
            if (t == AccessibilityEvent.TYPE_NOTIFICATION_STATE_CHANGED) {
                try {
                    final CharSequence npkg = event.getPackageName();
                    final StringBuilder sb = new StringBuilder();
                    try {   // 通知对象里的 tickerText 往往是有效文本（getText() 有时为空）
                        android.app.Notification nn = (android.app.Notification) event.getParcelableData();
                        if (nn != null && nn.tickerText != null) sb.append(nn.tickerText);
                    } catch (Throwable ignoreT) {}
                    for (int i = 0; i < event.getText().size(); i++) {
                        CharSequence tx = event.getText().get(i);
                        if (tx != null && tx.length() > 0) sb.append(' ').append(tx);
                    }
                    for (int i = 0; i < event.getText().size(); i++) {
                        CharSequence tx = event.getText().get(i);
                        if (tx != null && tx.length() > 0) sb.append(' ').append(tx);
                    }
                    final String body = sb.toString().trim().replace("\\", "\\\\").replace("\"", "\\\"");
                    if (body.length() > 0 && npkg != null) {
                        final String pkg = npkg.toString().replace("\"", "");
                        final long ts = System.currentTimeMillis();
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
                                lines.add("{\"ts\":" + ts + ",\"pkg\":\"" + pkg + "\",\"text\":\"" + body + "\"}");
                                while (lines.size() > 240) lines.remove(0);   // 滚动裁剪：留最近 ~200 条 + 余量
                                java.io.PrintWriter pw = new java.io.PrintWriter(
                                        new java.io.OutputStreamWriter(new java.io.FileOutputStream(nf), "UTF-8"));
                                for (String l : lines) pw.println(l);
                                pw.close();
                            } catch (Throwable ignored) {}
                        }}, "notif-capture").start();
                    }
                } catch (Throwable ignored) {}
            }
        } catch (Throwable ignored) {}
    }
    @Override public void onInterrupt() {}

    /** 主屏截图（旧调用点保持不变）。 */
    public void shot(final ShotCallback cb) {
        shot(Display.DEFAULT_DISPLAY, cb);
    }

    /**
     * 指定逻辑显示器截图（2026-10-20 虚拟屏）。
     *
     * 为什么必须按 displayId 截：本机 `screencap -d <36>` 直接报
     * "Display Id '36' is not valid"（叠加屏 screencap 拿不到），
     * 而 AccessibilityService.takeScreenshot(displayId) 是系统给无障碍的
     * 唯一按显示器取帧的公开 API。失败必须**明确报错**，
     * 绝不能让调用方拿主屏的帧当成虚拟屏（那会让安全闸门核对错页面）。
     */
    public void shot(int displayId, final ShotCallback cb) {
        try {
            takeScreenshot(displayId, getMainExecutor(), new TakeScreenshotCallback() {
                @Override public void onSuccess(ScreenshotResult result) {
                    Bitmap bmp = null;
                    try {
                        bmp = Bitmap.wrapHardwareBuffer(result.getHardwareBuffer(), result.getColorSpace());
                        bmp = (bmp == null) ? null : bmp.copy(Bitmap.Config.ARGB_8888, false);
                    } catch (Throwable t) {
                        cb.done(null, null, "转换位图失败：" + t);
                        return;
                    } finally {
                        try { result.getHardwareBuffer().close(); } catch (Throwable ignored) {}
                    }
                    if (bmp == null) cb.done(null, null, "位图为空");
                    else cb.done(null, bmp, null);
                }
                @Override public void onFailure(int errorCode) {
                    cb.done(null, null, "截图失败 displayId=" + displayId + " code=" + errorCode);
                }
            });
        } catch (Throwable t) {
            cb.done(null, null, "截图异常 displayId=" + displayId + "：" + t);
        }
    }

    public void tap(int x, int y) {
        Path p = new Path();
        p.moveTo(x, y);
        dispatch(p, 0, 80);
    }

    public void swipe(int x1, int y1, int x2, int y2, int ms) {
        Path p = new Path();
        p.moveTo(x1, y1);
        p.lineTo(x2, y2);
        dispatch(p, 0, Math.max(60, ms));
    }

    public void longPress(int x, int y) {
        Path p = new Path();
        p.moveTo(x, y);
        dispatch(p, 0, 800);
    }

    /** 2026-09-20 加：公开的手势派发入口。
     *  为什么需要：dispatchGesture 在 AccessibilityService 里是 protected，
     *  OverlayService（同类但不同实例）调不到 → 编译报 cannot find symbol。
     *  暴露成 public 后，可通过 CmdReceiver 命令「点击 X Y」触发真正的手势注入，
     *  用于穿过不接受 `adb shell input tap` 合成事件的自绘界面（如微信搜索页）。 */
    public void tapAt(int x, int y, long ms) {
        Path p = new Path();
        p.moveTo(x, y);
        dispatch(p, 0, Math.max(60, ms));
    }

    public void swipeAt(int x1, int y1, int x2, int y2, long ms) {
        Path p = new Path();
        p.moveTo(x1, y1);
        p.lineTo(x2, y2);
        dispatch(p, 0, Math.max(60, ms));
    }

    /** 手势结果回调（异步，dispatchGesture 的 onCompleted/onCancelled）。 */
    public interface GestureDone { void done(boolean accepted, String status); }

    /**
     * 把一次手势**绑定到指定逻辑显示器**（2026-10-20 虚拟屏）。
     *
     * 为什么用 accessibility 而不是 `input swipe -d`：
     *   - GestureDescription.Builder.setDisplayId(displayId) 是 Android 官方
     *     给"在同一显示器坐标系里注入手势"的接口（API 30+），坐标就是
     *     takeScreenshot(displayId) 那张图的像素坐标，同源；
     *   - 不会有 shell 侧 display 参数被忽略/串屏的隐患。
     * 结果通过 cb 异步回报：调用方（OverlayService）写成回执文件，
     * safe_tap 必须等到"completed"才算下发成功，**超时/取消一律当失败**。
     */
    public boolean gestureOnDisplay(int displayId, int x1, int y1, int x2, int y2,
                                    long durationMs, final GestureDone cb) {
        if (displayId < 0 || x1 < 0 || y1 < 0 || x2 < 0 || y2 < 0) {
            if (cb != null) cb.done(false, "bad_args");
            return false;
        }
        try {
            Path p = new Path();
            p.moveTo(x1, y1);
            p.lineTo(x2, y2);
            GestureDescription.Builder b = new GestureDescription.Builder()
                    .addStroke(new GestureDescription.StrokeDescription(p, 0, Math.max(40, durationMs)));
            if (displayId != Display.DEFAULT_DISPLAY) b.setDisplayId(displayId);
            final GestureDescription g = b.build();
            boolean accepted = dispatchGesture(g, new GestureResultCallback() {
                @Override public void onCompleted(GestureDescription d) {
                    if (cb != null) cb.done(true, "completed");
                }
                @Override public void onCancelled(GestureDescription d) {
                    if (cb != null) cb.done(true, "cancelled");
                }
            }, new android.os.Handler(android.os.Looper.getMainLooper()));
            if (!accepted && cb != null) cb.done(false, "rejected");
            return accepted;
        } catch (Throwable t) {
            Log.i("DSH", "gestureOnDisplay failed display=" + displayId + ": " + t.getClass().getSimpleName());
            if (cb != null) cb.done(false, "exception:" + t);
            return false;
        }
    }

    /**
     * 列出某逻辑显示器上的无障碍窗口（诊断 + 树来源证据）。
     * 返回 {"displayId","windowCount","appWindows","focusedWindowId","rootPkg","bounds"}。
     * 拿不到窗口时 appWindows=0 —— 调用方必须把它当"覆盖未知"，不能当"页面为空"。
     */
    public org.json.JSONObject windowsOnDisplay(int displayId) {
        org.json.JSONObject out = new org.json.JSONObject();
        try {
            out.put("displayId", displayId);
            android.util.SparseArray<java.util.List<android.view.accessibility.AccessibilityWindowInfo>> all =
                    getWindowsOnAllDisplays();
            java.util.List<android.view.accessibility.AccessibilityWindowInfo> ws = all == null ? null : all.get(displayId);
            int total = 0;
            if (all != null) for (int i = 0; i < all.size(); i++) total += all.valueAt(i) == null ? 0 : all.valueAt(i).size();
            int app = 0, focusedId = -1;
            String rootPkg = "";
            String bounds = "";
            if (ws != null) {
                for (android.view.accessibility.AccessibilityWindowInfo w : ws) {
                    try {
                        if (w.getType() == android.view.accessibility.AccessibilityWindowInfo.TYPE_APPLICATION) {
                            app++;
                            if (w.isFocused() || w.isActive()) {
                                focusedId = w.getId();
                                AccessibilityNodeInfo r = w.getRoot();
                                if (r != null) {
                                    rootPkg = String.valueOf(r.getPackageName());
                                    android.graphics.Rect rr = new android.graphics.Rect();
                                    r.getBoundsInScreen(rr);
                                    bounds = rr.left + "," + rr.top + "," + rr.right + "," + rr.bottom;
                                }
                            }
                        }
                    } catch (Throwable ignored) {}
                }
            }
            out.put("windowCount", total);
            out.put("windowsOnDisplay", ws == null ? 0 : ws.size());
            out.put("appWindows", app);
            out.put("focusedWindowId", focusedId);
            out.put("rootPkg", rootPkg);
            out.put("bounds", bounds);
        } catch (Throwable t) {
            try { out.put("error", String.valueOf(t)); } catch (Throwable ignored) {}
        }
        return out;
    }

    private void dispatch(Path path, long start, long duration) {
        try {
            GestureDescription.StrokeDescription s = new GestureDescription.StrokeDescription(path, start, duration);
            dispatchGesture(new GestureDescription.Builder().addStroke(s).build(), null, null);
        } catch (Throwable t) {
            Log.i("DSH", "dispatchGesture failed: " + t.getClass().getSimpleName());
        }
    }

    public ActionReceipt gestureWithReceipt(int x, int y, int x2, int y2, long duration) {
        ActionReceipt receipt = new ActionReceipt();
        android.util.DisplayMetrics metrics = getResources().getDisplayMetrics();
        if (x < 0 || y < 0 || x2 < 0 || y2 < 0 || x >= metrics.widthPixels || x2 >= metrics.widthPixels
                || y >= metrics.heightPixels || y2 >= metrics.heightPixels || duration < 1 || duration > 2000) {
            receipt.finish("invalid_gesture");
            return receipt;
        }
        try {
            Path path = new Path(); path.moveTo(x, y); path.lineTo(x2, y2);
            GestureDescription gesture = new GestureDescription.Builder()
                .addStroke(new GestureDescription.StrokeDescription(path, 0, duration)).build();
            boolean accepted = dispatchGesture(gesture, new GestureResultCallback() {
                @Override public void onCompleted(GestureDescription g) { receipt.finish("completed"); }
                @Override public void onCancelled(GestureDescription g) { receipt.finish("cancelled"); }
            }, new android.os.Handler(android.os.Looper.getMainLooper()));
            if (!accepted) receipt.finish("rejected");
        } catch (Throwable t) { receipt.finish("dispatch_failed"); }
        return receipt;
    }

    public ActionReceipt typeWithReceipt(String text) {
        ActionReceipt receipt = new ActionReceipt();
        AccessibilityNodeInfo root = null;
        AccessibilityNodeInfo focus = null;
        try {
            root = getRootInActiveWindow();
            focus = root == null ? null : root.findFocus(AccessibilityNodeInfo.FOCUS_INPUT);
            if (focus == null || !focus.isEditable() || !focus.isEnabled() || focus.isPassword()) {
                receipt.finish("input_target_unavailable");
                return receipt;
            }
            Bundle args = new Bundle();
            args.putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, text);
            if (!focus.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, args)) {
                receipt.finish("input_rejected");
                return receipt;
            }
            final AccessibilityNodeInfo target = AccessibilityNodeInfo.obtain(focus);
            new android.os.Handler(android.os.Looper.getMainLooper()).postDelayed(() -> {
                AccessibilityNodeInfo currentRoot = null, currentFocus = null;
                try {
                    currentRoot = getRootInActiveWindow();
                    currentFocus = currentRoot == null ? null : currentRoot.findFocus(AccessibilityNodeInfo.FOCUS_INPUT);
                    boolean verified = currentFocus != null && currentFocus.equals(target)
                        && target.refresh() && target.isFocused()
                        && text.contentEquals(target.getText() == null ? "" : target.getText());
                    receipt.finish(verified ? "verified" : "input_unverified");
                } catch (Throwable t) { receipt.finish("input_unverified"); }
                finally {
                    target.recycle();
                    if (currentFocus != null) currentFocus.recycle();
                    if (currentRoot != null) currentRoot.recycle();
                }
            }, 150);
        } catch (Throwable t) { receipt.finish("input_failed"); }
        finally {
            if (focus != null) focus.recycle();
            if (root != null) root.recycle();
        }
        return receipt;
    }

    /** 往当前聚焦的输入框写文字 */
    public boolean typeText(String text) {
        try {
            AccessibilityNodeInfo root = getRootInActiveWindow();
            if (root == null) return false;
            AccessibilityNodeInfo focus = root.findFocus(AccessibilityNodeInfo.FOCUS_INPUT);
            if (focus == null) return false;
            Bundle args = new Bundle();
            args.putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, text);
            return focus.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, args);
        } catch (Throwable t) {
            Log.i("DSH", "typeText failed: " + t.getClass().getSimpleName());
            return false;
        }
    }

    /**
     * 把当前界面里「有文字/描述 + 可点/可输入」的元素导成紧凑文本：
     *   - "登录" @(630,2600) [可点]
     * 给做决定的模型当坐标锚点，比纯猜像素准得多（有些 App 会屏蔽控件树，那就只能靠截图）。
     */
    public String dumpNodes(int maxCount) {
        try {
            AccessibilityNodeInfo root = getRootInActiveWindow();
            if (root == null) return "";
            StringBuilder sb = new StringBuilder();
            walk(root, sb, maxCount, 0);
            return sb.toString();
        } catch (Throwable t) {
            return "";
        }
    }

    private void walk(AccessibilityNodeInfo n, StringBuilder sb, int maxCount, int depth) {
        if (n == null || depth > 25 || sb.length() > 3500) return;
        int lines = 0;
        for (int i = 0; i < sb.length(); i++) if (sb.charAt(i) == '\n') lines++;
        if (lines >= maxCount) return;
        try {
            CharSequence text = n.getText();
            CharSequence desc = n.getContentDescription();
            String label = (text != null && text.length() > 0) ? text.toString().trim()
                    : (desc != null && desc.length() > 0 ? desc.toString().trim() : null);
            if (label != null && !label.isEmpty() && (n.isClickable() || n.isEditable() || n.isFocusable())) {
                android.graphics.Rect r = new android.graphics.Rect();
                n.getBoundsInScreen(r);
                if (r.width() > 0 && r.height() > 0) {
                    sb.append("- \"").append(label.length() > 40 ? label.substring(0, 40) : label).append("\" @(")
                      .append(r.centerX()).append(',').append(r.centerY()).append(')');
                    if (n.isEditable()) sb.append(" [输入框]");
                    sb.append('\n');
                }
            }
            for (int i = 0; i < n.getChildCount(); i++) walk(n.getChild(i), sb, maxCount, depth + 1);
        } catch (Throwable ignored) {}
    }

    /**
     * 诊断用：dump **所有**节点（不过滤可点/可聚焦），带 viewIdResourceName。
     *
     * 2026-09-22 加，为了验证「微信气泡到底可不可读」：
     *   参考 jev-chat-jarvis —— 它按 viewIdResourceName == "com.tencent.mm:id/bkl"
     *   找微信气泡，并且直接读 node.getText()。所以关键是有没有这个 id、有没有文字。
     *   原来的 dumpNodes() 有三个过滤器（label 非空 / 可点可编辑可聚焦 / 截断 3500），
     *   会把气泡节点整个滤掉，无法用来判断。
     *
     * @param filter 只看 viewId 含该子串的节点；空串=全部
     * @param maxChars 输出上限（防止 logcat 截断）
     */
    /** 诊断：报告根节点详情 + 遍历统计（判断是"树被屏蔽"还是"我的遍历坏了"）*/
    public String probeRoot() {
        StringBuilder sb = new StringBuilder();
        try {
            AccessibilityNodeInfo root = getRootInActiveWindow();
            if (root == null) return "root=null（getRootInActiveWindow 返回空）";
            sb.append("root: cls=").append(root.getClassName())
              .append(" pkg=").append(root.getPackageName())
              .append(" children=").append(root.getChildCount())
              .append(" text=").append(root.getText())
              .append(" clickable=").append(root.isClickable())
              .append("\n");
            // 逐层统计（广度），看每层有多少节点
            java.util.ArrayDeque<AccessibilityNodeInfo> q = new java.util.ArrayDeque<>();
            q.addLast(root);
            int total = 0;
            int[] perDepth = new int[12];
            java.util.ArrayDeque<int[]> qd = new java.util.ArrayDeque<>();
            qd.addLast(new int[]{0});
            // 简化的层序遍历（只统计数量，不打印）
            java.util.ArrayDeque<AccessibilityNodeInfo> q2 = new java.util.ArrayDeque<>();
            java.util.ArrayDeque<Integer> d2 = new java.util.ArrayDeque<>();
            q2.addLast(root); d2.addLast(0);
            while (!q2.isEmpty() && total < 8000) {
                AccessibilityNodeInfo n = q2.removeLast();
                int d = d2.removeLast();
                total++;
                if (d < perDepth.length) perDepth[d]++;
                try {
                    for (int i = n.getChildCount() - 1; i >= 0; i--) {
                        AccessibilityNodeInfo c = n.getChild(i);
                        if (c != null) { q2.addLast(c); d2.addLast(d + 1); }
                    }
                } catch (Throwable ignored) {}
            }
            sb.append("总节点数=").append(total).append("  每层节点数=");
            for (int i = 0; i < perDepth.length && perDepth[i] > 0; i++) {
                sb.append("L").append(i).append(":").append(perDepth[i]).append(" ");
            }
            // 尝试用 windows 拿（有些 App 只在 window 里暴露）
            try {
                java.util.List<android.view.accessibility.AccessibilityWindowInfo> ws = getWindows();
                sb.append("\n窗口数=").append(ws == null ? -1 : ws.size());
                if (ws != null) for (int i = 0; i < ws.size() && i < 5; i++) {
                    AccessibilityNodeInfo wr = ws.get(i).getRoot();
                    sb.append("  [w").append(i).append(" type=").append(ws.get(i).getType())
                      .append(" children=").append(wr == null ? -1 : wr.getChildCount()).append("]");
                }
            } catch (Throwable t) { sb.append("\n窗口枚举失败：").append(t); }
        } catch (Throwable t) { sb.append("probeRoot 异常：").append(t); }
        return sb.toString();
    }

    /**
     * 机器可读的控件树快照（2026-10-01 加，给手机侧 safe_tap.py 用）。
     *
     * 与 dumpAllNodes 的区别：返回 JSON 数组文本、由调用方写文件（不受 logcat 单行 4KB 截断）。
     * **不过滤可点/可聚焦** —— 危险词禁区必须能看见「不可点的 TextView」，
     * 否则 safe_tap.py 的 G2 闸门会被悄悄削弱。
     * text 为空时退回 contentDescription（覆盖面比 uiautomator 的 text 属性更广）。
     */
    /** 数字串归一化成 #（"剩余 3 秒" → "剩余 # 秒"），让 treeHash / digest 容忍动态文本 */
    private static String normText(String s) {
        if (s == null) return "";
        StringBuilder b = new StringBuilder(s.length());
        boolean inNum = false, inSp = false;
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            if (c >= '0' && c <= '9') {
                if (!inNum) { b.append('#'); inNum = true; }
                inSp = false;
            } else if (c == ' ' || c == '\t' || c == '\n' || c == '\r') {
                if (!inSp) { b.append(' '); inSp = true; }
                inNum = false;
            } else {
                b.append(c); inNum = false; inSp = false;
            }
        }
        return b.toString().trim();
    }

    private static String sha1_16(String s) {
        try {
            java.security.MessageDigest md = java.security.MessageDigest.getInstance("SHA-1");
            byte[] d = md.digest(s.getBytes("UTF-8"));
            StringBuilder b = new StringBuilder();
            for (int i = 0; i < 8; i++) b.append(String.format("%02x", d[i]));
            return b.toString();
        } catch (Throwable t) { return "0000000000000000"; }
    }

    /**
     * 机器可读的快照（2026-10-01；把"按节点点击"要用的字段**一次冻结**，免得第二批再改格式）。
     * 返回 {"treeHash":..,"count":N,"nodes":[{...}]}，每个节点：
     *   viewId / text / desc / norm（数字→#）/ rect（原始，点击落点用）/ qrect（量化 8px，指纹用）
     *   / ord（同 viewId+norm+desc 内的 0 基序号 —— 列表里区分同名行唯一可靠的手段）
     *   / clickable / enabled / focusable（硬前置）/ depth / sib（层级兜底）
     *   / digest = sha1(viewId|norm|desc|cls|qrect|ord) 前 8 hex —— **G4 按身份计数可直接复用**
     *   / handle = "h1.<treeHash8>.<index>.<digest8>"（. 分隔、**无空格**，避开 adb 广播那个坑）
     *
     * treeHash = 按序遍历全树把 (depth|viewId|norm|desc|qrect|clickable|enabled) 拼串取 sha1 前 16 位。
     * ⚠️ 只做"数字归一 + rect 量化"：**动画/大幅位移仍会让它变**，它只承担"粗判变了没"，
     *    精确匹配交给节点级 digest。
     * **不过滤可点/可聚焦** —— 危险词禁区必须能看见"不可点的 TextView"。
     */
    public org.json.JSONObject snapshot(int maxNodes) {
        return snapshotFromRoot(getRootInActiveWindow(), Display.DEFAULT_DISPLAY, maxNodes);
    }

    /**
     * 指定逻辑显示器的控件树快照（2026-10-20 虚拟屏）。
     *
     * 选树规则：getWindowsOnAllDisplays() 里**只取该 displayId 的 TYPE_APPLICATION 窗口**，
     * 优先 isFocused/isActive；一个都没有就返回空树 + coverage=none。
     * ⚠️ 绝不在虚拟屏请求时回退 getRootInActiveWindow()（那是主屏的树）——
     *    宁可报"覆盖未知"让闸门 fail closed，也不能把主屏页面当成虚拟屏页面。
     */
    public org.json.JSONObject snapshotOnDisplay(int displayId, int maxNodes) {
        if (displayId == Display.DEFAULT_DISPLAY) return snapshot(maxNodes);
        AccessibilityNodeInfo chosen = null;
        int winId = -1, appWindows = 0, allWindows = 0;
        this.lastDisplayWindowError = "";
        try {
            android.util.SparseArray<java.util.List<android.view.accessibility.AccessibilityWindowInfo>> all =
                    getWindowsOnAllDisplays();
            if (all == null) this.lastDisplayWindowError = "getWindowsOnAllDisplays=null";
            else {
                for (int i = 0; i < all.size(); i++) {
                    java.util.List<android.view.accessibility.AccessibilityWindowInfo> l = all.valueAt(i);
                    allWindows += l == null ? 0 : l.size();
                }
                java.util.List<android.view.accessibility.AccessibilityWindowInfo> ws = all.get(displayId);
                if (ws == null) this.lastDisplayWindowError = "no-window-list-for-display";
                else {
                    for (android.view.accessibility.AccessibilityWindowInfo w : ws) {
                        try {
                            if (w.getType() != android.view.accessibility.AccessibilityWindowInfo.TYPE_APPLICATION) continue;
                            appWindows++;
                            if (chosen == null && (w.isFocused() || w.isActive())) {
                                chosen = w.getRoot();
                                winId = w.getId();
                            }
                        } catch (Throwable ignored) {}
                    }
                    if (chosen == null) {   // 没有 focused 标记：取本显示器最后一个应用窗口
                        for (android.view.accessibility.AccessibilityWindowInfo w : ws) {
                            try {
                                if (w.getType() != android.view.accessibility.AccessibilityWindowInfo.TYPE_APPLICATION) continue;
                                AccessibilityNodeInfo r = w.getRoot();
                                if (r != null) { chosen = r; winId = w.getId(); }
                            } catch (Throwable ignored) {}
                        }
                    }
                }
            }
        } catch (Throwable t) {
            this.lastDisplayWindowError = String.valueOf(t);
        }
        org.json.JSONObject o = snapshotFromRoot(chosen, displayId, maxNodes);
        try {
            o.put("displayId", displayId);
            o.put("windowsOnDisplay", allWindows);
            o.put("appWindows", appWindows);
            o.put("windowId", winId);
            o.put("coverage", chosen == null ? "none" : "tree");
            // 该屏窗口的包名 —— safe_tap 用它核对"前台 App 是否仍是目标"，为空即拒绝。
            if (chosen != null) {
                try { o.put("rootPkg", String.valueOf(chosen.getPackageName())); }
                catch (Throwable ignored) {}
            }
            if (!this.lastDisplayWindowError.isEmpty()) o.put("windowError", this.lastDisplayWindowError);
        } catch (Throwable ignored) {}
        return o;
    }

    /** 最近一次 snapshotOnDisplay 的窗口枚举错误（诊断用，空=无错误）。 */
    public volatile String lastDisplayWindowError = "";

    /** 从给定 root 生成快照（主屏/虚拟屏共用同一套指纹与节点字段）。 */
    public org.json.JSONObject snapshotFromRoot(AccessibilityNodeInfo root, int displayId, int maxNodes) {
        org.json.JSONArray arr = new org.json.JSONArray();
        StringBuilder fp = new StringBuilder();
        try {
            if (root == null) {
                org.json.JSONObject o = new org.json.JSONObject();
                o.put("treeHash", "0000000000000000");
                o.put("windowId", -1);
                o.put("displayId", displayId);
                o.put("count", 0);
                o.put("nodes", arr);
                return o;
            }
            java.util.ArrayDeque<AccessibilityNodeInfo> stack = new java.util.ArrayDeque<>();
            java.util.ArrayDeque<Integer> depths = new java.util.ArrayDeque<>();
            java.util.ArrayDeque<Integer> sibs = new java.util.ArrayDeque<>();
            stack.addLast(root); depths.addLast(0); sibs.addLast(0);
            android.graphics.Rect r = new android.graphics.Rect();
            java.util.HashMap<String, Integer> ordMap = new java.util.HashMap<>();
            int visited = 0;
            while (!stack.isEmpty() && visited < 4000 && arr.length() < maxNodes) {
                AccessibilityNodeInfo n = stack.removeLast();
                int depth = depths.removeLast();
                int sib = sibs.removeLast();
                if (n == null) continue;
                visited++;
                try {
                    String vid = "";
                    try { String v = n.getViewIdResourceName(); if (v != null) vid = v; } catch (Throwable ignored) {}
                    CharSequence txt = n.getText();
                    CharSequence desc = n.getContentDescription();
                    String sv = txt == null ? "" : txt.toString().trim();
                    if (sv.isEmpty() && desc != null) sv = desc.toString().trim();
                    String dv = desc == null ? "" : desc.toString().trim();
                    String cls = String.valueOf(n.getClassName());
                    if (!sv.isEmpty()) {
                        n.getBoundsInScreen(r);
                        if (r.width() > 0 && r.height() > 0) {
                            String norm = normText(sv);
                            // 2026-10-01 修（复测者）：**先截断再哈希** ——
                            // 否则长文本会出现"digest 不同、但发出来的 norm 相同"，字段与指纹脱钩。
                            String t120 = sv.length() > 120 ? sv.substring(0, 120) : sv;
                            String d120 = dv.length() > 120 ? dv.substring(0, 120) : dv;
                            String n120 = norm.length() > 120 ? norm.substring(0, 120) : norm;
                            String key = vid + "|" + norm + "|" + dv;
                            Integer prev = ordMap.get(key);
                            int ord = prev == null ? 0 : prev.intValue();
                            ordMap.put(key, ord + 1);
                            String q = (r.left / 8 * 8) + "," + (r.top / 8 * 8) + ","
                                     + (r.right / 8 * 8) + "," + (r.bottom / 8 * 8);
                            boolean clk = n.isClickable();
                            boolean en = n.isEnabled();
                            boolean foc = n.isFocusable();
                            String digest = sha1_16(vid + "|" + n120 + "|" + d120 + "|" + cls + "|" + q + "|" + ord).substring(0, 8);
                            org.json.JSONArray rect = new org.json.JSONArray();
                            rect.put(r.left); rect.put(r.top); rect.put(r.right); rect.put(r.bottom);
                            org.json.JSONObject o = new org.json.JSONObject();
                            o.put("viewId", vid);
                            o.put("cls", cls);
                            o.put("text", t120);
                            o.put("desc", d120);
                            o.put("norm", n120);
                            // 2026-10-01（复测者 §25⑤）：选中态进快照 —— 让 agent 在**决策阶段**就
                            // 避开"已选中/禁用"这类必然无效的点击（那批 5/100 的无效率全是这一类）。
                            // ⚠️ 故意**不进 treeHash/digest**：选中态是易变状态，进指纹会让
                            //    "点一下选中态变化"被误判成"世界变了"。
                            boolean selN = false, chkN = false, chkableN = false, visN = true;
                            try { selN = n.isSelected(); } catch (Throwable ignored) {}
                            try { chkN = n.isChecked(); } catch (Throwable ignored) {}
                            try { chkableN = n.isCheckable(); } catch (Throwable ignored) {}
                            try { visN = n.isVisibleToUser(); } catch (Throwable ignored) {}
                            o.put("selected", selN);
                            o.put("checked", chkN);
                            o.put("checkable", chkableN);
                            o.put("visibleToUser", visN);
                            o.put("rect", rect);
                            o.put("qrect", q);
                            o.put("ord", ord);
                            o.put("clickable", clk);
                            o.put("enabled", en);
                            o.put("focusable", foc);
                            o.put("depth", depth);
                            o.put("sib", sib);
                            o.put("digest", digest);
                            arr.put(o);
                            fp.append(depth).append('|').append(vid).append('|').append(norm).append('|')
                              .append(d120).append('|').append(q).append('|').append(clk ? 1 : 0)
                              .append(en ? 1 : 0).append('\n');
                        }
                    }
                    for (int i = n.getChildCount() - 1; i >= 0; i--) {
                        AccessibilityNodeInfo c = n.getChild(i);
                        if (c != null) { stack.addLast(c); depths.addLast(depth + 1); sibs.addLast(i); }
                    }
                } catch (Throwable ignored) {}
            }
            String treeHash = sha1_16(fp.toString());
            for (int i = 0; i < arr.length(); i++) {
                org.json.JSONObject o = arr.optJSONObject(i);
                if (o == null) continue;
                o.put("handle", "h1." + treeHash.substring(0, 8) + "."
                        + String.format("%04d", i) + "." + o.optString("digest", ""));
            }
            org.json.JSONObject out = new org.json.JSONObject();
            out.put("treeHash", treeHash);
            out.put("displayId", displayId);
            try { out.put("windowId", root.getWindowId()); } catch (Throwable ignored) { out.put("windowId", -1); }
            out.put("count", arr.length());
            out.put("nodes", arr);
            return out;
        } catch (Throwable t) {
            org.json.JSONObject o = new org.json.JSONObject();
            try {
                o.put("treeHash", "0000000000000000");
                o.put("displayId", displayId);
                o.put("coverage", "none");
                o.put("count", 0);
                o.put("nodes", arr);
            } catch (Throwable ignored) {}
            return o;
        }
    }

    /** 兼容旧调用：只要节点数组 */
    public String snapshotJson(int maxNodes) {
        org.json.JSONArray a = snapshot(maxNodes).optJSONArray("nodes");
        return a == null ? "[]" : a.toString();
    }

    public String dumpAllNodes(String filter, int maxChars) {
        try {
            AccessibilityNodeInfo root = getRootInActiveWindow();
            if (root == null) return "（getRootInActiveWindow 返回 null）";
            StringBuilder sb = new StringBuilder();
            java.util.ArrayDeque<AccessibilityNodeInfo> stack = new java.util.ArrayDeque<>();
            stack.addLast(root);
            int visited = 0, matched = 0;
            while (!stack.isEmpty() && visited < 6000) {
                AccessibilityNodeInfo n = stack.removeLast();
                if (n == null) continue;
                visited++;
                try {
                    String vid = null;
                    try { vid = n.getViewIdResourceName(); } catch (Throwable ignored) {}
                    CharSequence txt = n.getText();
                    CharSequence desc = n.getContentDescription();
                    String t = txt == null ? "" : txt.toString().trim();
                    String d = desc == null ? "" : desc.toString().trim();
                    boolean hit = (filter == null || filter.isEmpty())
                            || (vid != null && vid.contains(filter));
                    if (hit && (!t.isEmpty() || !d.isEmpty() || (filter != null && !filter.isEmpty() && vid != null))) {
                        matched++;
                        if (sb.length() < maxChars) {
                            android.graphics.Rect r = new android.graphics.Rect();
                            n.getBoundsInScreen(r);
                            sb.append("id=").append(vid == null ? "-" : vid.replace("com.tencent.mm:id/", "mm/"))
                              .append(" cls=").append(n.getClassName() == null ? "-" :
                                      n.getClassName().toString().replace("android.widget.", "").replace("android.view.", ""))
                              .append(" t=\"").append(t.length() > 34 ? t.substring(0, 34) : t).append("\"");
                            if (!d.isEmpty()) sb.append(" d=\"").append(d.length() > 24 ? d.substring(0, 24) : d).append("\"");
                            sb.append(" @").append(r.centerX()).append(",").append(r.centerY());
                            if (n.isClickable()) sb.append(" C");
                            if (n.isEditable()) sb.append(" E");
                            sb.append("\n");
                        }
                    }
                } catch (Throwable ignored) {}
                try {
                    for (int i = n.getChildCount() - 1; i >= 0; i--) stack.addLast(n.getChild(i));
                } catch (Throwable ignored) {}
            }
            String head = "visited=" + visited + " matched=" + matched + "\n";
            String body = sb.toString();
            if (body.length() >= maxChars) body += "…（截断）";
            return head + body;
        } catch (Throwable t) {
            return "dumpAllNodes 异常：" + t;
        }
    }

    public void globalBack() { performGlobalAction(GLOBAL_ACTION_BACK); }
    public void globalHome() { performGlobalAction(GLOBAL_ACTION_HOME); }
    public void globalRecents() { performGlobalAction(GLOBAL_ACTION_RECENTS); }

    // ================= 剪贴板（给"发中文"用：input text 不支持非 ASCII）=================

    /**
     * 往系统剪贴板写文本。
     * 为什么需要：`adb shell input text` 只支持 ASCII，中文发不出去；
     * 剪贴板 + 粘贴是绕开这个限制的标准办法。
     * 无障碍服务是前台服务，写剪贴板不受 Android 10+ 的后台限制。
     */
    public boolean setClipboard(String text) {
        try {
            ClipboardManager cm = (ClipboardManager) getSystemService(Context.CLIPBOARD_SERVICE);
            if (cm == null) return false;
            cm.setPrimaryClip(ClipData.newPlainText("dsh", text));
            return true;
        } catch (Throwable t) {
            Log.i("DSH", "setClipboard 失败: " + t.getClass().getSimpleName());
            return false;
        }
    }

    /**
     * WRITE_CLIPBOARD 被 AppOps 拒绝时，setPrimaryClip/clearPrimaryClip 静默返回、不抛异常——
     * 所以先查一次，不允许（或查不到）就如实报失败，不能把"没抛异常"当成功。
     */
    private boolean clipboardWriteAllowed() {
        try {
            android.app.AppOpsManager ao = (android.app.AppOpsManager) getSystemService(Context.APP_OPS_SERVICE);
            int mode = ao.unsafeCheckOpNoThrow("android:write_clipboard", android.os.Process.myUid(), getPackageName());
            return mode == android.app.AppOpsManager.MODE_ALLOWED;   // 剪贴板服务只认 MODE_ALLOWED（DEFAULT ≠ 允许）
        } catch (Throwable t) {
            Log.i("DSH", "clipboardWriteAllowed 查不到: " + t.getClass().getSimpleName());
            return false;
        }
    }

    /** 写剪贴板并标记敏感：Android 13+ 不在剪贴板预览/输入法建议里显示原文（2026-10-21，safe_tap type 用）。 */
    public boolean setClipboardSensitive(String text) {
        try {
            if (!clipboardWriteAllowed()) return false;
            ClipboardManager cm = (ClipboardManager) getSystemService(Context.CLIPBOARD_SERVICE);
            if (cm == null) return false;
            ClipData cd = ClipData.newPlainText("dsb", text);
            if (android.os.Build.VERSION.SDK_INT >= 33) {
                android.os.PersistableBundle ex = new android.os.PersistableBundle();
                ex.putBoolean(android.content.ClipDescription.EXTRA_IS_SENSITIVE, true);
                cd.getDescription().setExtras(ex);
            }
            cm.setPrimaryClip(cd);
            return true;
        } catch (Throwable t) {
            Log.i("DSH", "setClipboardSensitive 失败: " + t.getClass().getSimpleName());
            return false;
        }
    }

    /** 清空剪贴板（clearPrimaryClip，API 28+）。 */
    public boolean clearClipboard() {
        try {
            if (!clipboardWriteAllowed()) return false;
            ClipboardManager cm = (ClipboardManager) getSystemService(Context.CLIPBOARD_SERVICE);
            if (cm == null) return false;
            cm.clearPrimaryClip();
            return true;
        } catch (Throwable t) {
            Log.i("DSH", "clearClipboard 失败: " + t.getClass().getSimpleName());
            return false;
        }
    }

    public String getClipboard() {
        try {
            ClipboardManager cm = (ClipboardManager) getSystemService(Context.CLIPBOARD_SERVICE);
            if (cm == null || !cm.hasPrimaryClip() || cm.getPrimaryClip() == null) return "";
            CharSequence cs = cm.getPrimaryClip().getItemAt(0).coerceToText(this);
            return cs == null ? "" : cs.toString();
        } catch (Throwable t) { return ""; }
    }

    /**
     * 把剪贴板内容粘贴进当前聚焦的可编辑框。
     * 优先用无障碍 ACTION_PASTE 贴在真正的 Editable 节点上（不依赖输入法）；
     * 失败则返回 false，由调用方退回 KEYCODE_PASTE(279) 或提示。
     */
    public boolean pasteIntoFocused() {
        try {
            AccessibilityNodeInfo root = getRootInActiveWindow();
            if (root == null) return false;
            AccessibilityNodeInfo focus = root.findFocus(AccessibilityNodeInfo.FOCUS_INPUT);
            if (focus == null) return false;
            return focus.performAction(AccessibilityNodeInfo.ACTION_PASTE);
        } catch (Throwable t) {
            Log.i("DSH", "pasteIntoFocused 失败: " + t.getClass().getSimpleName());
            return false;
        }
    }

    /**
     * 往当前聚焦输入框写文本：ACTION_SET_TEXT（不经输入法）。
     * 比 ACTION_PASTE 更直接，但有些 App 的输入框不支持。
     */
    public boolean setTextIntoFocused(String text) {
        try {
            AccessibilityNodeInfo root = getRootInActiveWindow();
            if (root == null) return false;
            AccessibilityNodeInfo focus = root.findFocus(AccessibilityNodeInfo.FOCUS_INPUT);
            if (focus == null) return false;
            Bundle args = new Bundle();
            args.putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, text);
            return focus.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, args);
        } catch (Throwable t) {
            Log.i("DSH", "setTextIntoFocused 失败: " + t.getClass().getSimpleName());
            return false;
        }
    }
}
