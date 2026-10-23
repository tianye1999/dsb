package com.dsb.assistant.stoptests;

import android.app.Instrumentation;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.os.Bundle;
import android.widget.TextView;
import android.widget.Button;
import org.json.JSONObject;
import org.json.JSONArray;
import java.io.*;
import java.lang.reflect.*;
import java.net.*;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.*;
import java.util.concurrent.atomic.AtomicInteger;

/** Real Android lifecycle/HTTP tests. Fake brain never emits gestures or text input. */
public final class StopInstrumentation extends Instrumentation {
    private Object service;
    private Class<?> serviceClass;
    private Context context;
    private final JSONArray results = new JSONArray();
    private final JSONObject diagnostics = new JSONObject();
    private FakeBridge bridge;
    private boolean bubbleTest;
    private boolean recoveryTest;
    private String originalBridge;
    private boolean hadBridge;
    private boolean savedPrefs;
    private String runId = "";
    private boolean repairTestBridge;
    private android.widget.LinearLayout testSurface;
    private android.widget.EditText normalInput, passwordInput;
    private android.widget.TextView readonlyInput;
    private android.widget.Button testButton, endButton;
    private final java.util.concurrent.atomic.AtomicInteger buttonClicks = new java.util.concurrent.atomic.AtomicInteger();
    private final java.util.concurrent.atomic.AtomicInteger endClicks = new java.util.concurrent.atomic.AtomicInteger();
    private final AtomicInteger touchEvents = new AtomicInteger();

    @Override public void onCreate(Bundle args) {
        super.onCreate(args);
        bubbleTest = args != null && "true".equals(args.getString("bubble"));
        recoveryTest = args != null && "true".equals(args.getString("recovery"));
        runId = args == null ? "" : args.getString("runId", "");
        repairTestBridge = args != null && "true".equals(args.getString("repairTestBridge"));
        start();
    }
    interface Check { boolean get() throws Exception; }
    private void until(Check check, long ms) throws Exception {
        long end = System.nanoTime() + TimeUnit.MILLISECONDS.toNanos(ms);
        while (!check.get() && System.nanoTime() < end) Thread.sleep(20);
        if (!check.get()) throw new AssertionError("condition timeout after " + ms + "ms");
    }
    private Object field(String name) throws Exception {
        Field f = serviceClass.getDeclaredField(name); f.setAccessible(true); return f.get(service);
    }
    private boolean flag(String name) throws Exception { return (Boolean) field(name); }
    private void call(String name, String arg) throws Exception {
        Method m = arg == null ? serviceClass.getDeclaredMethod(name) : serviceClass.getDeclaredMethod(name, String.class);
        m.setAccessible(true);
        final Throwable[] failure = new Throwable[1];
        runOnMainSync(() -> { try { if(arg == null) m.invoke(service); else m.invoke(service,arg); }
            catch(Throwable t) { failure[0]=t; } });
        if (failure[0] != null) throw new AssertionError(failure[0]);
    }
    private void report(String phase) throws Exception {
        JSONObject out = new JSONObject().put("phase",phase).put("timestamp",System.currentTimeMillis()).put("runId",runId)
            .put("results",results).put("diagnostics",diagnostics).put("doRequests",bridge == null ? 0 : bridge.doCount.get());
        if (service != null) {
            out.put("running",flag("agentRunning")).put("worker",flag("taskWorkerRunning"))
                .put("cancelInFlight",flag("cancelInFlight")).put("cancelUnconfirmed",flag("cancelUnconfirmed"));
            TextView bubble = (TextView) field("bubble");
            int[] xy = new int[2];
            runOnMainSync(() -> bubble.getLocationOnScreen(xy));
            out.put("bubbleX",xy[0]+bubble.getWidth()/2).put("bubbleY",xy[1]+bubble.getHeight()/2);
        }
        try(FileOutputStream f = new FileOutputStream(new File(context.getExternalFilesDir(null), "stop-device-test.json"))) {
            f.write(out.toString(2).getBytes(StandardCharsets.UTF_8));
        }
    }
    private void passed(String name) throws Exception { results.put(new JSONObject().put("test",name).put("ok",true)); report("running"); }
    private void idle() throws Exception {
        until(() -> !flag("agentRunning") && !flag("taskWorkerRunning") && !flag("cancelInFlight") && !flag("cancelUnconfirmed"), 20000);
    }
    private void expectStatus(String prefix) {
        final Throwable[] failure = new Throwable[1];
        runOnMainSync(() -> {
            try {
                Method show = serviceClass.getDeclaredMethod("showPanel", boolean.class);
                show.setAccessible(true); show.invoke(service, true);
                String text = ((TextView) field("statusView")).getText().toString();
                if (!text.startsWith(prefix)) throw new AssertionError("expected " + prefix + ", got " + text);
            } catch (Throwable e) { failure[0] = e; }
        });
        if (failure[0] != null) throw new AssertionError(failure[0]);
    }
    private void recoveryUi(boolean enabled, String text) throws Exception {
        Method m=serviceClass.getDeclaredMethod("showPanel",boolean.class); m.setAccessible(true);
        runOnMainSync(() -> { try {
            m.invoke(service,true);
            Button stop=(Button)field("stopButton");
            if(stop.isEnabled()!=enabled || !stop.getText().toString().contains(enabled?"重试":"确认"))
                throw new AssertionError("incorrect recovery button state");
            if(!((TextView)field("statusView")).getText().toString().contains(text))
                throw new AssertionError("missing preserved recovery reason: "+text);
        } catch(Exception e) { throw new RuntimeException(e); } });
    }
    private void responseContract(String body, int code, boolean ok, String kind) throws Exception {
        bridge.responseBody=body; bridge.probeStatus=code;
        Class<?> client=Class.forName("com.dsb.assistant.DshClient",true,context.getClassLoader());
        Object r=client.getMethod("post",String.class,String.class,JSONObject.class,int.class)
            .invoke(null,"http://127.0.0.1:"+bridge.server.getLocalPort(),"/probe",new JSONObject(),1000);
        if(r.getClass().getField("ok").getBoolean(r)!=ok || !kind.equals(r.getClass().getField("failureKind").get(r)))
            throw new AssertionError("HTTP acknowledgement contract: "+code+" "+body);
    }
    private void taskResponseContract(String route, JSONObject response, boolean ok) throws Exception {
        String id="11111111-1111-4111-8111-111111111111";
        bridge.responseRoute=route; bridge.responseBody=response.toString();
        Class<?> client=Class.forName("com.dsb.assistant.DshClient",true,context.getClassLoader());
        Object r=client.getMethod("post",String.class,String.class,JSONObject.class,int.class)
            .invoke(null,"http://127.0.0.1:"+bridge.server.getLocalPort(),route,new JSONObject().put("task_id",id),1000);
        if(r.getClass().getField("ok").getBoolean(r)!=ok)
            throw new AssertionError("task receipt contract: "+route+" "+response);
    }
    private void actionReceipt(JSONObject action, String expected) throws Exception {
        Method execute = serviceClass.getDeclaredMethod("executeAction", JSONObject.class);
        execute.setAccessible(true);
        Object receipt = execute.invoke(service, action);
        String state = (String) receipt.getClass().getMethod("await", long.class).invoke(receipt, 1000L);
        if (!expected.equals(state)) throw new AssertionError("receipt expected=" + expected + " actual=" + state);
    }
    private void buildTestSurface() throws Exception {
        runOnMainSync(() -> {
            android.widget.LinearLayout box = new android.widget.LinearLayout(context);
            box.setOrientation(android.widget.LinearLayout.VERTICAL);
            box.setBackgroundColor(0xEE1B1B1B);
            android.widget.EditText normal = new android.widget.EditText(context);
            normal.setShowSoftInputOnFocus(false);
            android.widget.TextView readonly = new android.widget.TextView(context);
            readonly.setFocusable(true); readonly.setFocusableInTouchMode(true);
            readonly.setText("readonly-field", android.widget.TextView.BufferType.NORMAL);
            android.widget.EditText password = new android.widget.EditText(context);
            password.setShowSoftInputOnFocus(false);
            password.setInputType(android.text.InputType.TYPE_CLASS_TEXT | android.text.InputType.TYPE_TEXT_VARIATION_PASSWORD);
            android.widget.Button button = new android.widget.Button(context);
            button.setText("TS-TAP");
            button.setOnClickListener(v -> buttonClicks.incrementAndGet());
            button.setOnTouchListener((v, event) -> {
                touchEvents.incrementAndGet();
                try { diagnostics.put("lastButtonTouch", event.getActionMasked()).put("touchX", event.getRawX()).put("touchY", event.getRawY()); }
                catch(Exception ignored) {}
                return false;
            });
            android.widget.Button endBtn = new android.widget.Button(context);
            endBtn.setText("TS-END");
            endBtn.setOnClickListener(v -> endClicks.incrementAndGet());
            android.widget.LinearLayout.LayoutParams row = new android.widget.LinearLayout.LayoutParams(
                android.view.ViewGroup.LayoutParams.MATCH_PARENT, android.view.ViewGroup.LayoutParams.WRAP_CONTENT);
            box.addView(normal, row); box.addView(readonly, row); box.addView(password, row);
            box.addView(button, row); box.addView(endBtn, row);
            android.view.WindowManager wm = (android.view.WindowManager) context.getSystemService(Context.WINDOW_SERVICE);
            android.view.WindowManager.LayoutParams lp = new android.view.WindowManager.LayoutParams(
                android.view.ViewGroup.LayoutParams.MATCH_PARENT, android.view.ViewGroup.LayoutParams.WRAP_CONTENT,
                android.view.WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY,
                0, android.graphics.PixelFormat.TRANSLUCENT);
            lp.gravity = android.view.Gravity.CENTER;
            lp.softInputMode = android.view.WindowManager.LayoutParams.SOFT_INPUT_STATE_ALWAYS_HIDDEN;
            wm.addView(box, lp);
            testSurface = box; normalInput = normal; readonlyInput = readonly;
            passwordInput = password; testButton = button; endButton = endBtn;
        });
        until(() -> { final boolean[] ok = new boolean[1];
            runOnMainSync(() -> { try { int[] xy = new int[2]; testButton.getLocationOnScreen(xy);
                ok[0] = xy[1] > 0 && testButton.getWidth() > 0 && testSurface.hasWindowFocus(); } catch (Throwable t) { } });
            return ok[0]; }, 5000);
    }
    private int[] centerOf(final android.view.View v) throws Exception {
        final int[] xy = new int[2];
        runOnMainSync(() -> { v.getLocationOnScreen(xy); xy[0] += v.getWidth()/2; xy[1] += v.getHeight()/2; });
        return xy;
    }
    private void removeTestSurface() {
        if (testSurface == null) return;
        final Throwable[] error = new Throwable[1];
        runOnMainSync(() -> { try {
            ((android.view.WindowManager) context.getSystemService(Context.WINDOW_SERVICE))
                .removeViewImmediate(testSurface);
        } catch (Throwable t) { error[0] = t; } });
        if (error[0] != null) throw new AssertionError("test surface cleanup failed", error[0]);
        testSurface = null;
    }
    private String callGesture(int x, int y) throws Exception {
        return callGestureXY(x, y, x, y);
    }
    private String callGestureXY(int x, int y, int x2, int y2) throws Exception {
        Class<?> acc = Class.forName("com.dsb.assistant.AgentAccessibilityService", true, context.getClassLoader());
        Object inst = acc.getField("INSTANCE").get(null);
        Object r = acc.getMethod("gestureWithReceipt", int.class, int.class, int.class, int.class, long.class)
            .invoke(inst, x, y, x2, y2, 60);
        return (String) r.getClass().getMethod("await", long.class).invoke(r, 5000L);
    }
    private String callType(String text) throws Exception {
        Class<?> acc = Class.forName("com.dsb.assistant.AgentAccessibilityService", true, context.getClassLoader());
        Object inst = acc.getField("INSTANCE").get(null);
        Object r = acc.getMethod("typeWithReceipt", String.class).invoke(inst, text);
        return (String) r.getClass().getMethod("await", long.class).invoke(r, 5000L);
    }
    private void callHidePanel() throws Exception {
        Method m = serviceClass.getDeclaredMethod("showPanel", boolean.class);
        m.setAccessible(true);
        runOnMainSync(() -> { try { m.invoke(service, false); } catch (Throwable t) { throw new RuntimeException(t); } });
    }
    private void assertSurfaceReady(int[] center) throws Exception {
        final boolean[] visible = new boolean[1];
        final int[] origin = new int[2];
        final int[] size = new int[2];
        runOnMainSync(() -> {
            visible[0] = testSurface != null && testSurface.isShown();
            if (testSurface != null) { testSurface.getLocationOnScreen(origin);
                size[0] = testSurface.getWidth(); size[1] = testSurface.getHeight(); }
        });
        if (!visible[0]) throw new AssertionError("test surface not visible");
        if (center[0] < origin[0] || center[0] > origin[0] + size[0]
                || center[1] < origin[1] || center[1] > origin[1] + size[1])
            throw new AssertionError("control center outside test surface");
        try {
            TextView bubble = (TextView) field("bubble");
            final int[] bxy = new int[2];
            runOnMainSync(() -> bubble.getLocationOnScreen(bxy));
            int bcx = bxy[0] + bubble.getWidth() / 2, bcy = bxy[1] + bubble.getHeight() / 2;
            if (Math.abs(bcx - center[0]) < bubble.getWidth() && Math.abs(bcy - center[1]) < bubble.getHeight())
                throw new AssertionError("orange bubble overlaps tap target; move surface or bubble");
        } catch (Throwable t) { throw new AssertionError("cannot establish unobstructed test target", t); }
    }
    private void focusField(final android.view.View f, final String name) throws Exception {
        diagnostics.put("focusAttempt", name);
        JSONArray attempts = diagnostics.optJSONArray("focusAttempts");
        if (attempts == null) { attempts = new JSONArray(); diagnostics.put("focusAttempts", attempts); }
        final JSONObject attempt = new JSONObject().put("name", name);
        attempts.put(attempt);
        final long started = android.os.SystemClock.elapsedRealtime();
        final boolean[] requested = new boolean[1];
        try {
            if (!ownFocusState(f, attempt, false)) throw new AssertionError("fixture_window_not_focused: " + name);
            runOnMainSync(() -> requested[0] = f.requestFocus());
            attempt.put("requestFocusAccepted", requested[0]);
            if (!requested[0]) throw new AssertionError("fixture_focus_request_rejected: " + name);
            until(() -> {
            if (!ownFocusState(f, attempt, true)) throw new AssertionError("fixture_window_not_focused: " + name);
            try {
            Class<?> acc = Class.forName("com.dsb.assistant.AgentAccessibilityService", true, context.getClassLoader());
            Object inst = acc.getField("INSTANCE").get(null);
            android.view.accessibility.AccessibilityNodeInfo root = (android.view.accessibility.AccessibilityNodeInfo) acc.getMethod("getRootInActiveWindow").invoke(inst);
            attempt.put("rootMissing", root == null).put("polls", attempt.optInt("polls") + 1);
            if (root == null) return false;
            android.view.accessibility.AccessibilityNodeInfo focus = null;
            try {
                boolean rootOwn = context.getPackageName().contentEquals(root.getPackageName() == null ? "" : root.getPackageName());
                attempt.put("rootOwnPackage", rootOwn).put("rootWindowId", rootOwn ? root.getWindowId() : -1);
                if (!rootOwn) return false;
                focus = root.findFocus(android.view.accessibility.AccessibilityNodeInfo.FOCUS_INPUT);
                attempt.put("nodeMissing", focus == null);
                boolean focusOwn = focus != null && context.getPackageName().contentEquals(focus.getPackageName() == null ? "" : focus.getPackageName());
                attempt.put("inputOwnPackage", focusOwn);
                if (!focusOwn) return false;
                android.graphics.Rect r = new android.graphics.Rect();
                focus.getBoundsInScreen(r);
                int[] center = centerOf(f);
                attempt.put("inputWindowId", focus.getWindowId()).put("bounds", r.toShortString())
                    .put("targetX", center[0]).put("targetY", center[1]);
                return root.getWindowId() == focus.getWindowId()
                    && focus.getWindowId() == attempt.getInt("viewWindowId") && r.contains(center[0], center[1]);
            } finally { if (focus != null) focus.recycle(); root.recycle(); }
            } catch (Exception t) { attempt.put("readErrorType", t.getClass().getSimpleName()); return false; }
            }, 5000);
            attempt.put("ready", true);
        } catch (Throwable t) {
            attempt.put("ready", false).put("failureType", t.getClass().getSimpleName());
            throw t;
        } finally { attempt.put("elapsedMs", android.os.SystemClock.elapsedRealtime() - started); }
    }
    private boolean ownFocusState(android.view.View f, JSONObject attempt, boolean requireViewFocus) throws Exception {
        final boolean[] ready = new boolean[1];
        final Throwable[] error = new Throwable[1];
        runOnMainSync(() -> { try {
            boolean attached = f.isAttachedToWindow(), shown = f.isShown(), windowFocus = f.hasWindowFocus();
            attempt.put("attached", attached).put("shown", shown).put("windowFocused", windowFocus)
                .put("viewFocused", f.isFocused());
            android.view.accessibility.AccessibilityNodeInfo ownNode = f.createAccessibilityNodeInfo();
            try { attempt.put("viewWindowId", ownNode.getWindowId()); } finally { ownNode.recycle(); }
            ready[0] = attached && shown && windowFocus && (!requireViewFocus || f.isFocused());
        } catch (Throwable t) { error[0] = t; } });
        if (error[0] != null) throw new AssertionError("own-view diagnostic failure", error[0]);
        return ready[0];
    }
    private void verifyLostWindowFocusRefused() throws Exception {
        final android.view.WindowManager wm = (android.view.WindowManager) context.getSystemService(Context.WINDOW_SERVICE);
        final int[] originalFlags = new int[1];
        final String[] before = new String[1];
        runOnMainSync(() -> {
            before[0] = normalInput.getText().toString();
            android.view.WindowManager.LayoutParams lp = (android.view.WindowManager.LayoutParams) testSurface.getLayoutParams();
            originalFlags[0] = lp.flags;
            lp.flags |= android.view.WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE;
            wm.updateViewLayout(testSurface, lp);
        });
        try {
            until(() -> { final boolean[] lost = new boolean[1];
                runOnMainSync(() -> lost[0] = !testSurface.hasWindowFocus());
                return lost[0]; }, 5000);
            final long started = android.os.SystemClock.elapsedRealtime();
            boolean refused = false;
            try { focusField(normalInput, "intentional own-window focus loss"); }
            catch (AssertionError expected) {
                if (!expected.getMessage().contains("fixture_window_not_focused")) throw expected;
                refused = true;
            }
            final String[] after = new String[1];
            runOnMainSync(() -> after[0] = normalInput.getText().toString());
            long elapsed = android.os.SystemClock.elapsedRealtime() - started;
            diagnostics.put("lostWindowRefusalMs", elapsed).put("lostWindowTextUnchanged", before[0].equals(after[0]));
            if (!refused || elapsed >= 5000 || !before[0].equals(after[0]))
                throw new AssertionError("lost-window fixture must refuse before input dispatch and timeout");
            passed("own-window focus loss refuses setup before input dispatch");
        } finally {
            runOnMainSync(() -> {
                android.view.WindowManager.LayoutParams lp = (android.view.WindowManager.LayoutParams) testSurface.getLayoutParams();
                lp.flags = originalFlags[0];
                wm.updateViewLayout(testSurface, lp);
            });
            until(() -> { final boolean[] ready = new boolean[1];
                runOnMainSync(() -> ready[0] = testSurface.hasWindowFocus());
                return ready[0]; }, 5000);
        }
    }
    private void actionReceiptTap(int[] from, int[] to) throws Exception {
        actionReceipt(new JSONObject().put("action", "tap")
            .put("x", from[0]).put("y", from[1])
            .put("x2", to[0]).put("y2", to[1]), "completed");
    }
    @Override public void onStart() {
        Bundle out = new Bundle();
        boolean success = false;
        try {
            context = getTargetContext();
            serviceClass = Class.forName("com.dsb.assistant.OverlayService", true, context.getClassLoader());
            runOnMainSync(() -> context.startForegroundService(new Intent().setClassName(context,"com.dsb.assistant.OverlayService")));
            Field instance = serviceClass.getDeclaredField("INSTANCE");
            instance.setAccessible(true);
            until(() -> instance.get(null) != null, 15000);
            service = instance.get(null);
            SharedPreferences prefs=context.getSharedPreferences("dsh",Context.MODE_PRIVATE);
            if (prefs.contains("stoptests_had_bridge")) {
                SharedPreferences.Editor restore = prefs.edit();
                if (prefs.getBoolean("stoptests_had_bridge", false)) restore.putString("bridge", prefs.getString("stoptests_bridge", ""));
                else restore.remove("bridge");
                if (!restore.remove("stoptests_had_bridge").remove("stoptests_bridge").commit())
                    throw new AssertionError("previous test bridge restoration failed");
            }
            // Explicit one-time recovery for the pre-backup fixture crash, never a normal test default.
            if (repairTestBridge) {
                String old = prefs.getString("bridge", "");
                diagnostics.put("bridgeBeforeExplicitRepair", old);
                if (!old.matches("http://127\\.0\\.0\\.1:[0-9]+") || old.equals("http://127.0.0.1:3096"))
                    throw new AssertionError("not a leaked local test bridge: " + old);
                if (!prefs.edit().remove("bridge").commit()) throw new AssertionError("explicit test bridge repair failed");
            }
            hadBridge=prefs.contains("bridge"); originalBridge=prefs.getString("bridge",null);
            savedPrefs=true;
            if (!prefs.edit().putBoolean("stoptests_had_bridge", hadBridge)
                    .putString("stoptests_bridge", originalBridge == null ? "" : originalBridge).commit())
                throw new AssertionError("test bridge backup failed");
            bridge=new FakeBridge();
            if (!prefs.edit().putString("bridge","http://127.0.0.1:"+bridge.server.getLocalPort()).commit())
                throw new AssertionError("test bridge preference write failed");

            for (String reply : new String[]{"任务已完成", "执行失败，需要人工处理", "", " \n\t "}) {
                bridge.reset(); bridge.replyText=reply;
                call("runGoal","TEST reply semantics only, no phone actions");
                if (reply.trim().isEmpty()) {
                    until(() -> !flag("taskWorkerRunning"), 20000);
                    if (!flag("cancelUnconfirmed")) throw new AssertionError("empty reply unlocked admission");
                    expectStatus("停止未确认");
                    call("stopTask",null);
                }
                idle();
                if (bridge.doCount.get()!=1) throw new AssertionError("reply test did not reach fake bridge exactly once");
                expectStatus(reply.trim().isEmpty()?"已停止":"已回复，结果未核验");
                passed("reply semantics: " + JSONObject.quote(reply));
            }

            // Hold the cancellation response after releasing the old HTTP worker.
            bridge.reset(); bridge.holdDo=true; bridge.holdCancel=true;
            call("runGoal","TEST stop admission, no phone actions");
            until(() -> bridge.doCount.get()==1,10000);
            call("stopTask",null); until(() -> bridge.cancelCount.get()==1,5000);
            long generation=(Long)field("taskGeneration");
            call("runGoal","TEST must not start");
            if ((Long)field("taskGeneration")!=generation) throw new AssertionError("new admission during cancel");
            bridge.cancelRelease.countDown(); idle();
            if(bridge.doCount.get()!=1) throw new AssertionError("blocked task reached bridge");
            passed("delayed cancel blocks new admission until worker and HTTP settle");

            bridge.reset(); bridge.holdDo=true; bridge.failCancel=true;
            call("runGoal","TEST cancel failure"); until(() -> bridge.doCount.get()==1,5000);
            call("stopTask",null); until(() -> !flag("cancelInFlight"),20000);
            if(!flag("cancelUnconfirmed")) throw new AssertionError("failed cancellation reported success");
            call("runGoal","TEST must remain blocked");
            if(bridge.doCount.get()!=1) throw new AssertionError("new request after failed cancel");
            bridge.failCancel=false; call("stopTask",null); idle();
            passed("cancel HTTP failure stays blocked and explicit retry recovers");

            // A failed HTTP status must not be overridden by JSON ok:true.
            bridge.reset(); bridge.holdDo=true; bridge.cancelStatus=503;
            call("runGoal","TEST HTTP rejection recovery"); until(() -> bridge.doCount.get()==1,5000);
            call("stopTask",null); until(() -> !flag("cancelInFlight"),20000);
            if(!flag("cancelUnconfirmed")) throw new AssertionError("HTTP 503 ok:true unlocked task");
            if(recoveryTest) {
                report("WAITING_RECOVERY_OPEN"); until(() -> flag("panelShown"),90000);
            }
            recoveryUi(true,"503");
            int before=bridge.doCount.get(); call("runGoal","TEST blocked recovery");
            if(bridge.doCount.get()!=before) throw new AssertionError("replayed goal during recovery");
            // Changing preferences must not redirect cancellation to a different bridge.
            prefs.edit().putString("bridge","http://127.0.0.1:1").commit();
            bridge.cancelStatus=200; bridge.holdCancel=true;
            if(recoveryTest) { report("WAITING_RECOVERY_RETRY"); until(() -> flag("cancelInFlight"),90000); }
            else runOnMainSync(() -> { try { ((Button)field("stopButton")).performClick(); }
                catch(Exception e) { throw new RuntimeException(e); } });
            until(() -> flag("cancelInFlight"),5000); recoveryUi(false,"正在停止");
            int cancelBefore=bridge.cancelCount.get(); call("stopTask",null);
            Thread.sleep(100); if(bridge.cancelCount.get()>cancelBefore+1) throw new AssertionError("duplicate cancel worker");
            bridge.cancelRelease.countDown(); idle();
            if(bridge.doCount.get()!=before) throw new AssertionError("recovery replayed task");
            prefs.edit().putString("bridge","http://127.0.0.1:"+bridge.server.getLocalPort()).commit();
            passed("HTTP rejection preserved in UI; retry stays on original bridge without replay");

            bridge.reset(); bridge.holdDo=true; bridge.dropCancel=true;
            call("runGoal","TEST dropped connection recovery"); until(() -> bridge.doCount.get()==1,5000);
            call("stopTask",null); until(() -> !flag("cancelInFlight"),20000);
            if(!flag("cancelUnconfirmed")) throw new AssertionError("connection loss unlocked task");
            recoveryUi(true,"无法连接");
            call("runGoal","TEST still blocked offline");
            if(bridge.doCount.get()!=1) throw new AssertionError("offline task replayed");
            bridge.dropCancel=false; call("stopTask",null); idle();
            call("runGoal","TEST explicit fresh task after connection recovery"); idle();
            if(bridge.doCount.get()!=2) throw new AssertionError("fresh task did not complete after reconnect");
            passed("connection failure stays blocked; explicit retry and fresh task recover");

            responseContract("{\"ok\":true}",200,true,"");
            responseContract("{\"ok\":false}",200,false,"rejected");
            responseContract("{\"ok\":\"true\"}",200,false,"response");
            responseContract("{}",200,false,"response");
            responseContract("not json",200,false,"response");
            responseContract("{\"ok\":true}",503,false,"http");
            responseContract("{\"ok\":true}",302,false,"http");
            for(String route:new String[]{"/do","/ask","/step","/cancel"}) {
                JSONObject valid=new JSONObject().put("ok",true).put("protocol",2)
                    .put("task_id","11111111-1111-4111-8111-111111111111")
                    .put("transport_settled",true).put("stop_confirmed",true);
                taskResponseContract(route,new JSONObject().put("ok",true),false);
                taskResponseContract(route,valid,true);
                taskResponseContract(route,new JSONObject(valid.toString()).put("task_id","wrong"),false);
                taskResponseContract(route,new JSONObject(valid.toString()).put("protocol","2"),false);
                taskResponseContract(route,new JSONObject(valid.toString())
                    .put(route.equals("/cancel")?"stop_confirmed":"transport_settled","true"),false);
            }
            bridge.responseBody=null;
            bridge.responseRoute="/probe";
            passed("HTTP acknowledgement requires 2xx, literal booleans and matching task protocol; redirects rejected");

            bridge.reset(); bridge.holdDo=true; call("runGoal","TEST fresh task after stop");
            until(() -> bridge.doCount.get()==1,5000);
            Thread.sleep(2200);
            if(!flag("agentRunning") || !flag("bubbleBusy")) throw new AssertionError("old task cleanup affected fresh task");
            bridge.release.countDown(); idle();
            passed("fresh task survives old timer interval and completes after successful stop");

            if(bubbleTest) {
                bridge.reset(); bridge.holdDo=true;
                call("runGoal","TEST orange bubble stop; no device actions");
                until(() -> bridge.doCount.get()==1,5000);
                report("WAITING_GATED_BUBBLE_TAP");
                until(() -> bridge.cancelCount.get()>0,90000);
                idle(); passed("actual orange bubble touch reaches stop and settles HTTP");
            }
            // Only rejected/no-op actions: never inject into the user's foreground app.
            idle();
            Class<?> accessibility = Class.forName("com.dsb.assistant.AgentAccessibilityService", true, context.getClassLoader());
            Field accInstance = accessibility.getField("INSTANCE");
            if (accInstance.get(null) == null) {
                String enabled = android.provider.Settings.Secure.getString(context.getContentResolver(),
                    android.provider.Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES);
                android.content.ComponentName expected = new android.content.ComponentName(context.getPackageName(), accessibility.getName());
                boolean alreadyEnabled = false;
                for (String component : (enabled == null ? "" : enabled).split(":"))
                    if (expected.equals(android.content.ComponentName.unflattenFromString(component))) alreadyEnabled = true;
                if (!alreadyEnabled) throw new AssertionError("DSB accessibility was not enabled; refusing new permission");
                Method restore = serviceClass.getDeclaredMethod("ensureAccessibility", int.class);
                restore.setAccessible(true);
                if (!Boolean.TRUE.equals(restore.invoke(service, 25000)))
                    throw new AssertionError("existing accessibility binding restoration failed");
                diagnostics.put("existingAccessibilityBindingRestored", true);
            }
            Object originalAcc = accInstance.get(null);
            if (originalAcc == null) throw new AssertionError("accessibility unavailable for receipt tests");
            actionReceipt(new JSONObject().put("action", "unsupported_test_action"), "unsupported_action");
            actionReceipt(new JSONObject().put("action", "tap"), "invalid_coordinates");
            actionReceipt(new JSONObject().put("action", "swipe").put("x", -1).put("y", -1).put("x2", -2).put("y2", -2), "invalid_gesture");
            actionReceipt(new JSONObject().put("action", "type"), "missing_text");
            JSONObject nullText = new JSONObject().put("action", "type");
            nullText.put("text", JSONObject.NULL);
            actionReceipt(nullText, "invalid_text");
            actionReceipt(new JSONObject().put("action", "type").put("text", 123), "invalid_text");
            actionReceipt(new JSONObject().put("action", "tap").put("x", "100").put("y", "100"), "invalid_coordinates");
            // 1.5 is not an integer: must be rejected, not truncated by optInt.
            actionReceipt(new JSONObject().put("action", "tap").put("x", 1.5).put("y", 1.5), "invalid_coordinates");
            actionReceipt(new JSONObject().put("action", "tap").put("x", 3000000000L).put("y", 3000000000L), "invalid_coordinates");
            try {
                runOnMainSync(() -> { try { accInstance.set(null, null); } catch(Exception e) { throw new RuntimeException(e); } });
                actionReceipt(new JSONObject().put("action", "wait"), "accessibility_unavailable");
            } finally {
                runOnMainSync(() -> { try { accInstance.set(null, originalAcc); } catch(Exception e) { throw new RuntimeException(e); } });
            }
            passed("native invalid/missing/unsupported/unavailable actions return explicit failures without side effects");

            // Own no-send test surface: real gesture/input receipts against our own views only.
            buildTestSurface();
            callHidePanel();
            focusField(normalInput, "initial own-window readiness");
            int[] bc = centerOf(testButton);
            diagnostics.put("tapX", bc[0]).put("tapY", bc[1]);
            assertSurfaceReady(bc);
            String gs = callGesture(bc[0], bc[1]);
            if (!"completed".equals(gs)) throw new AssertionError("gesture receipt=" + gs);
            until(() -> buttonClicks.get() == 1, 1500);
            passed("gesture receipt completed with real own-button postcondition");

            focusField(normalInput, "normal input");
            String ts = callType("ocr-ts-123");
            if (!"verified".equals(ts)) throw new AssertionError("type receipt=" + ts);
            final String[] actualText = new String[1];
            runOnMainSync(() -> actualText[0] = normalInput.getText().toString());
            if (!"ocr-ts-123".equals(actualText[0])) throw new AssertionError("field text mismatch: " + actualText[0]);
            passed("type verified against own field actual text");

            focusField(readonlyInput, "readonly input");
            String readonlyReceipt = callType("x");
            final String[] readonlyText = new String[1];
            runOnMainSync(() -> readonlyText[0] = readonlyInput.getText().toString());
            if (!("input_target_unavailable".equals(readonlyReceipt) || "input_rejected".equals(readonlyReceipt))
                    || !"readonly-field".equals(readonlyText[0]))
                throw new AssertionError("readonly receipt=" + readonlyReceipt + " actual=" + readonlyText[0]);
            focusField(passwordInput, "password input");
            String passwordReceipt = callType("x");
            if (!"input_target_unavailable".equals(passwordReceipt)) throw new AssertionError("password receipt=" + passwordReceipt);
            passed("readonly and password fields refuse text input");

            verifyLostWindowFocusRefused();

            // Mismatch without timing guesses: take the pending receipt first, then
            // overwrite the field on the main thread, then await the verification.
            focusField(normalInput, "normal input");
            Class<?> accClass2 = Class.forName("com.dsb.assistant.AgentAccessibilityService", true, context.getClassLoader());
            Object accInst2 = accClass2.getField("INSTANCE").get(null);
            final Object pending = accClass2.getMethod("typeWithReceipt", String.class).invoke(accInst2, "ocr-ts-mismatch");
            runOnMainSync(() -> { try { normalInput.setText("overwritten"); } catch (Throwable ignored) { } });
            String mm = (String) pending.getClass().getMethod("await", long.class).invoke(pending, 5000L);
            if (!"input_unverified".equals(mm)) throw new AssertionError("mismatch receipt=" + mm);
            passed("overwritten field content reported as input_unverified");

            // executeAction tap branch keeps endpoint == start: the endpoint must not be pressed.
            int startBefore = buttonClicks.get();
            int endBefore = endClicks.get();
            int[] b1 = centerOf(testButton);
            int[] b2 = centerOf(endButton);
            assertSurfaceReady(b1);
            actionReceiptTap(b1, b2);
            until(() -> buttonClicks.get() == startBefore + 1, 1500);
            if (buttonClicks.get() != startBefore + 1) throw new AssertionError("start clicks " + startBefore + " -> " + buttonClicks.get());
            if (endClicks.get() != endBefore) throw new AssertionError("endpoint button was pressed");
            passed("executeAction tap keeps endpoint inert and presses start exactly once");

            for (String command : new String[]{
                    "点击 " + b1[0] + " " + b1[1], "长按 " + b1[0] + " " + b1[1],
                    "滑动 " + b1[0] + " " + b1[1] + " " + b2[0] + " " + b2[1],
                    "无障碍做 TEST quarantined entry", "盲操 TEST quarantined entry"}) {
                callHidePanel();
                assertSurfaceReady(b1);
                long beforeGeneration = (Long) field("taskGeneration");
                int beforeDo = bridge.doCount.get(), beforeStep = bridge.stepCount.get();
                int beforeStart = buttonClicks.get(), beforeEnd = endClicks.get();
                int beforeTouch = touchEvents.get();
                call("handleCommand", command);
                if ((Long) field("taskGeneration") != beforeGeneration || flag("agentRunning") || flag("taskWorkerRunning"))
                    throw new AssertionError("legacy entry started a task: " + command);
                expectStatus("旧执行入口已停用");
                Thread.sleep(350);
                if (bridge.doCount.get() != beforeDo || bridge.stepCount.get() != beforeStep
                        || buttonClicks.get() != beforeStart || endClicks.get() != beforeEnd || touchEvents.get() != beforeTouch)
                    throw new AssertionError("legacy entry dispatched work: " + command);
                passed("legacy entry refused without dispatch: " + command.split(" ")[0]);
            }
            success=true;
        } catch(Throwable t) {
            try { results.put(new JSONObject().put("ok",false).put("error",t.toString())); } catch(Exception ignored) {}
        } finally {
            try { removeTestSurface(); }
            catch(Throwable t) { success=false; out.putString("surfaceCleanupError", t.toString()); }
            try {
                if(bridge!=null) { bridge.holdDo=false; bridge.failCancel=false; bridge.holdCancel=false; bridge.cancelStatus=200; bridge.dropCancel=false;
                    bridge.release.countDown(); bridge.cancelRelease.countDown(); }
                if(service!=null && (flag("agentRunning") || flag("cancelUnconfirmed"))) { call("stopTask",null); idle(); }
                if(savedPrefs) {
                    SharedPreferences.Editor restore = context.getSharedPreferences("dsh",Context.MODE_PRIVATE).edit();
                    if(hadBridge) restore.putString("bridge",originalBridge); else restore.remove("bridge");
                    if(!restore.remove("stoptests_had_bridge").remove("stoptests_bridge").commit())
                        throw new AssertionError("test bridge restoration failed");
                }
                if(service!=null) { Method m=serviceClass.getDeclaredMethod("showPanel",boolean.class); m.setAccessible(true);
                    runOnMainSync(() -> { try { m.invoke(service,false); } catch(Exception ignored) {} }); }
                report(success?"PASS":"FAIL");
            } catch(Throwable t) { success=false; out.putString("cleanupError",t.toString()); }
            if(bridge!=null) bridge.close();
            out.putString("stream", "DSB_TEST_FINISH:" + runId + ":" + (success ? "PASS" : "FAIL") + "\n" + results.toString());
            finish(success?-1:0,out);
        }
    }
    private static final class FakeBridge implements AutoCloseable {
        final ServerSocket server=new ServerSocket(0,16,InetAddress.getByName("127.0.0.1"));
        final ExecutorService io=Executors.newCachedThreadPool();
        final AtomicInteger doCount=new AtomicInteger(), cancelCount=new AtomicInteger(), stepCount=new AtomicInteger();
        volatile boolean holdDo,holdCancel,failCancel,sensitiveWait,dropCancel;
        volatile int cancelStatus=200;
        volatile int probeStatus=200;
        volatile String responseBody;
        volatile String responseRoute="/probe";
        volatile String replyText="test completed";
        volatile CountDownLatch release=new CountDownLatch(1), cancelRelease=new CountDownLatch(1);
        FakeBridge() throws IOException { io.execute(() -> { while(!server.isClosed()) try {
            Socket s=server.accept(); io.execute(() -> serve(s));
        } catch(IOException ignored) {} }); }
        void reset() { holdDo=holdCancel=failCancel=sensitiveWait=dropCancel=false; cancelStatus=200; doCount.set(0);cancelCount.set(0);
            stepCount.set(0);
            replyText="test completed";
            release=new CountDownLatch(1);cancelRelease=new CountDownLatch(1); }
        void serve(Socket socket) {
            try(Socket s=socket) {
                s.setSoTimeout(10000);
                InputStream in=new BufferedInputStream(s.getInputStream());
                ByteArrayOutputStream header=new ByteArrayOutputStream();
                int suffix=0;
                while(header.size()<16384) { int b=in.read(); if(b<0) throw new EOFException(); header.write(b);
                    suffix=(suffix<<8)|b; if(suffix==0x0d0a0d0a) break; }
                String[] lines=header.toString("US-ASCII").split("\r\n");
                String route=lines[0].split(" ")[1]; int length=0;
                for(String line:lines) if(line.toLowerCase(java.util.Locale.ROOT).startsWith("content-length:")) length=Integer.parseInt(line.substring(15).trim());
                if(length>10000000) throw new IOException("oversize test request");
                byte[] buffer=new byte[8192];
                ByteArrayOutputStream payload=new ByteArrayOutputStream();
                while(length>0) { int n=in.read(buffer,0,Math.min(length,buffer.length)); if(n<0) throw new EOFException(); payload.write(buffer,0,n); length-=n; }
                JSONObject request=new JSONObject(payload.toString("UTF-8"));
                JSONObject result=new JSONObject().put("ok",true).put("text","test completed");
                result.put("protocol",2).put("task_id",request.optString("task_id",""))
                    .put("transport_settled",true).put("stop_confirmed",true);
                if(route.equals(responseRoute)&&responseBody!=null) { /* Explicit parser contract probe. */ }
                else if(route.equals("/do")) { doCount.incrementAndGet(); if(holdDo) release.await(100,TimeUnit.SECONDS);
                    // A syntactically valid receipt with blank text must still fail in the App.
                    result.put("text",replyText); }
                else if(route.equals("/cancel")) { cancelCount.incrementAndGet(); release.countDown();
                    if(dropCancel) return;
                    if(holdCancel) cancelRelease.await(15,TimeUnit.SECONDS);
                    result.put("ok",!failCancel).put("error",failCancel?"TEST cancellation refused":""); }
                else if(route.equals("/step")) { stepCount.incrementAndGet();
                    result.put("action",new JSONObject().put("action",sensitiveWait?"wait":"done")
                    .put("sensitive",sensitiveWait).put("reason","test-only no-op")); }
                else result.put("ok",false).put("error","unexpected route");
                byte[] body=(route.equals(responseRoute)&&responseBody!=null?responseBody:result.toString()).getBytes(StandardCharsets.UTF_8);
                OutputStream out=s.getOutputStream();
                int code=route.equals("/cancel")?cancelStatus:route.equals("/probe")?probeStatus:200;
                out.write(("HTTP/1.1 "+code+" Test\r\nLocation: /redirected\r\nContent-Type: application/json\r\nContent-Length: "+body.length+"\r\nConnection: close\r\n\r\n").getBytes(StandardCharsets.US_ASCII));
                out.write(body);out.flush();
            } catch(Exception ignored) {}
        }
        public void close() { try { server.close(); } catch(Exception ignored) {} io.shutdownNow(); }
    }
}
