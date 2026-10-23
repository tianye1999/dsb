import java.util.*;
import java.util.concurrent.*;
import java.util.concurrent.atomic.*;
import java.util.function.BooleanSupplier;
import com.dsb.assistant.ActionReceipt;

public class StopLifecycleTest {
    static void check(boolean b, String text) { if (!b) throw new AssertionError(text); }
    static void until(BooleanSupplier p) throws Exception {
        long end = System.nanoTime() + TimeUnit.SECONDS.toNanos(5);
        while (!p.getAsBoolean() && System.nanoTime() < end) Thread.sleep(5);
        check(p.getAsBoolean(), "condition timed out");
    }
    static void await(CountDownLatch latch) {
        try { if (!latch.await(5, TimeUnit.SECONDS)) throw new AssertionError("latch timeout"); }
        catch (InterruptedException e) { Thread.currentThread().interrupt(); }
    }
    interface Case { void run(OverlayService s) throws Exception; }
    static int failed;
    static void test(String name, Case body) throws Exception {
        DshClient.reset();
        OverlayService s = new OverlayService();
        try { body.run(s); System.out.println("PASS " + name); }
        catch (Throwable t) { failed++; System.out.println("FAIL " + name + ": " + t); }
        finally { DshClient.release.countDown(); DshClient.cancelRelease.countDown(); s.decision.countDown();
            s.exec.shutdownNow(); s.exec.awaitTermination(5, TimeUnit.SECONDS);
            for (Thread thread : Thread.getAllStackTraces().keySet()) if (thread.getName().equals("task-cancel")) {
                thread.join(2000); check(!thread.isAlive(), "cancellation thread leaked between tests");
            }
        }
    }
    public static void main(String[] args) throws Exception {
        test("queued progress cannot overwrite a settled reply", s -> {
            DshClient.holdDo = true;
            s.runGoal("progress-race");
            until(() -> DshClient.doCalls.get() == 1 && !s.main.delayed.isEmpty());
            s.main.deferThread = Thread.currentThread();
            Runnable tick = s.main.delayed.remove();
            tick.run();
            DshClient.release.countDown();
            s.exec.submit(() -> {}).get(5, TimeUnit.SECONDS);
            check(s.status.startsWith("已回复，结果未核验"), "reply not established: " + s.status);
            s.main.deferThread = null;
            Runnable queued;
            while ((queued = s.main.deferred.poll()) != null) queued.run();
            check(s.status.startsWith("已回复，结果未核验"), "stale progress replaced reply: " + s.status);
        });
        test("ask persists identity and stop discards its late reply", s -> {
            DshClient.holdDo = true;
            s.askDsh("synthetic question"); until(() -> DshClient.askCalls.get() == 1);
            String id = TaskRecoveryStore.snapshot(s.getFilesDir()).id;
            check(!id.isEmpty() && id.equals(DshClient.lastAskId), "ask omitted persisted ID");
            check(s.taskWorkerRunning && s.agentRunning, "ask escaped lifecycle");
            s.stopTask(); DshClient.release.countDown(); until(() -> !s.cancelInFlight && !s.taskWorkerRunning);
            check(DshClient.cancelIds.size() == 2 && DshClient.cancelIds.stream().allMatch(id::equals), "ask stop lost identity");
            check(s.logs.stream().noneMatch(x -> x.startsWith("💡 DSH：")), "late ask reply surfaced");
        });
        test("unknown task blocks ask without network admission", s -> {
            DshClient.failDo = true;
            s.runGoal("unknown"); until(() -> !s.taskWorkerRunning);
            s.askDsh("must not send");
            s.exec.submit(() -> {}).get(5, TimeUnit.SECONDS);
            check(DshClient.askCalls.get() == 0, "ask bypassed unknown barrier");
        });
        test("ask reply settles transport but never verifies goal", s -> {
            s.askDsh("synthetic"); until(() -> !s.taskWorkerRunning);
            check(DshClient.askCalls.get() == 1, "ask not executed");
            check(!TaskRecoveryStore.snapshot(s.getFilesDir()).pending, "ask settlement not persisted");
            check(s.status.equals("已回复，结果未核验"), "ask claimed success or stayed running");
        });
        test("persisted task ID accompanies work and both stop requests", s -> {
            DshClient.holdDo = true;
            s.runGoal("identity"); until(() -> DshClient.doCalls.get() == 1);
            String id = TaskRecoveryStore.snapshot(s.getFilesDir()).id;
            check(id.equals(DshClient.lastDoId), "do omitted persisted task ID");
            s.stopTask(); DshClient.release.countDown(); until(() -> !s.cancelInFlight);
            check(DshClient.cancelIds.size() == 2, "expected two cancellation checks");
            check(DshClient.cancelIds.stream().allMatch(id::equals), "cancel omitted/changed original task ID");
        });
        test("pending admission is persisted before remote request", s -> {
            DshClient.holdDo = true;
            s.runGoal("persist-before-send"); until(() -> DshClient.doCalls.get() == 1);
            check(TaskRecoveryStore.snapshot(s.getFilesDir()).pending, "request sent without pending record");
            DshClient.release.countDown(); until(() -> !s.taskWorkerRunning);
            check(!TaskRecoveryStore.snapshot(s.getFilesDir()).pending, "known reply retained pending");
        });
        test("persistence failure prevents remote admission", s -> {
            TaskRecoveryStore.failWrite = true;
            try {
                s.runGoal("must-not-send"); Thread.sleep(100);
                check(DshClient.doCalls.get() == 0, "request sent after failed persistence");
            } finally { TaskRecoveryStore.failWrite = false; }
        });
        test("a second service cannot overlap an old worker", s -> {
            OverlayService next = new OverlayService();
            try {
                DshClient.holdDo = true;
                s.runGoal("old-worker"); until(() -> DshClient.doCalls.get() == 1);
                next.runGoal("must-not-overlap");
                check(DshClient.doCalls.get() == 1 && !next.taskWorkerRunning,"second service overlapped old worker");
                DshClient.release.countDown(); until(() -> !s.taskWorkerRunning);
                next.runGoal("after-worker-exit"); until(() -> !next.taskWorkerRunning);
                check(DshClient.doCalls.get() == 2,"process worker gate leaked");
            } finally { next.exec.shutdownNow(); next.exec.awaitTermination(5,TimeUnit.SECONDS); }
        });
        test("a second service cannot overlap cancellation", s -> {
            OverlayService next = new OverlayService();
            try {
                DshClient.holdDo = true; DshClient.holdCancel = true;
                s.runGoal("old-cancel"); until(() -> DshClient.doCalls.get() == 1);
                s.stopTask(); until(() -> DshClient.cancelCalls.get() == 1);
                DshClient.release.countDown(); until(() -> !s.taskWorkerRunning);
                next.runGoal("must-not-overlap-cancel");
                check(DshClient.doCalls.get() == 1 && !next.taskWorkerRunning,"second service overlapped cancellation");
                DshClient.cancelRelease.countDown(); until(() -> !s.cancelInFlight);
                next.runGoal("after-cancel-exit"); until(() -> !next.taskWorkerRunning);
                check(DshClient.doCalls.get() == 2,"process cancellation gate leaked");
            } finally { next.exec.shutdownNow(); next.exec.awaitTermination(5,TimeUnit.SECONDS); }
        });
        test("transport failure remains blocked until explicit confirmed stop", s -> {
            DshClient.failDo = true;
            s.runGoal("unknown"); until(() -> !s.taskWorkerRunning);
            check(s.cancelUnconfirmed, "transport error silently unlocked");
            s.runGoal("no-replay"); check(DshClient.doCalls.get() == 1, "admitted new task after unknown");
            s.stopTask(); until(() -> !s.cancelUnconfirmed && !s.cancelInFlight);
            check(!TaskRecoveryStore.snapshot(s.getFilesDir()).pending, "cancel did not persist settlement");
        });
        test("fresh service restores pending and original bridge without replay", s -> {
            TaskRecoveryStore store = new TaskRecoveryStore(new java.io.File(s.getFilesDir(), "task-recovery.json"));
            store.begin("original-bridge");
            s.configuredBridge = "changed-bridge";
            restore(s);
            check(s.cancelUnconfirmed, "restart forgot pending");
            s.runGoal("must-not-send"); check(DshClient.doCalls.get() == 0, "restart replayed/admitted task");
            s.stopTask(); until(() -> !s.cancelUnconfirmed && !s.cancelInFlight);
            check("original-bridge".equals(DshClient.lastCancelBase), "cancel redirected after restart");
        });
        test("corrupt recovery identity cannot cancel current configured bridge", s -> {
            TaskRecoveryStore.records.put(s.getFilesDir().getPath(), new TaskRecoveryStore.State(true, "", ""));
            restore(s); s.stopTask();
            check(s.cancelUnconfirmed, "corruption unlocked");
            check(DshClient.cancelCalls.get() == 0, "corruption cancelled an unrelated bridge");
        });
        test("failed clear stays blocked despite remote cancellation acknowledgement", s -> {
            DshClient.holdDo = true;
            s.runGoal("clear-failure"); until(() -> DshClient.doCalls.get() == 1);
            TaskRecoveryStore.failWrite = true;
            try {
                s.stopTask(); DshClient.release.countDown(); until(() -> !s.cancelInFlight);
                check(s.cancelUnconfirmed, "failed durable clear unlocked task");
            } finally { TaskRecoveryStore.failWrite = false; }
            s.stopTask(); until(() -> !s.cancelInFlight && !s.cancelUnconfirmed);
        });
        for (String reply : new String[]{"任务已完成", "执行失败，需要人工处理"}) {
            test("reply wording never verifies goal: " + reply, s -> {
                DshClient.replyText = reply;
                s.runGoal("synthetic-reply");
                until(() -> !s.taskWorkerRunning);
                check(s.status.startsWith("已回复，结果未核验"), "false completion status: " + s.status);
                check(s.logs.contains("💡 " + reply), "assistant reply was lost");
                check(s.actions.get() == 0, "reply started accessibility fallback");
            });
        }
        for (String reply : new String[]{"", " \n\t "}) {
            test("empty reply stays failure: " + reply.length(), s -> {
                DshClient.replyText = reply;
                s.runGoal("synthetic-empty");
                until(() -> !s.taskWorkerRunning);
                check(s.cancelUnconfirmed && s.status.contains("停止未确认"), "empty reply unlocked: " + s.status);
                check(s.actions.get() == 0, "empty reply started fallback");
            });
        }
        test("late successful reply after stop cannot replace stopped status", s -> {
            DshClient.replyText = "任务已完成"; DshClient.holdDo = true;
            s.runGoal("synthetic-late"); until(() -> DshClient.doCalls.get() == 1);
            s.stopTask(); DshClient.release.countDown();
            until(() -> !s.taskWorkerRunning);
            until(() -> s.logs.stream().anyMatch(x -> x.contains("已停止当前会话")));
            check(s.logs.stream().noneMatch(x -> x.equals("💡 任务已完成")), "late reply surfaced");
            check(!s.status.startsWith("已回复") && !s.status.startsWith("✅ 完成"), "late reply overwrote stop");
        });
        test("actual integer validator rejects coercion fractions and overflow", s -> {
            for (Object value : new Object[]{"100", 1.5, 3e9, Double.NaN, Double.POSITIVE_INFINITY, true})
                check(!OverlayService.integerField(new JSONObject().put("x", value), "x"), "accepted invalid number: " + value);
            check(!OverlayService.integerField(new JSONObject(), "x"), "accepted missing coordinate");
            check(OverlayService.integerField(new JSONObject().put("x", 100), "x"), "rejected integer");
            check(OverlayService.integerField(new JSONObject().put("x", 100.0), "x"), "rejected exact integer number");
        });
        test("failed receipt prevents later batch actions", s -> {
            s.receipt = ActionReceipt.of("rejected");
            DshClient.action = new JSONObject().put("actions", new JSONArray(
                new JSONObject().put("action", "wait"), new JSONObject().put("action", "wait")));
            s.runGoalByAccessibility("rejected batch");
            until(() -> !s.taskWorkerRunning);
            check(s.actions.get() == 1, "second action executed after rejected receipt");
        });
        test("stop interrupts receipt wait without holding task lock", s -> {
            s.receipt = new ActionReceipt();
            DshClient.action = new JSONObject().put("actions", new JSONArray(
                new JSONObject().put("action", "wait"), new JSONObject().put("action", "wait")));
            s.runGoalByAccessibility("pending receipt"); until(() -> s.actions.get() == 1);
            Thread.sleep(50);
            long start = System.nanoTime(); s.stopTask();
            check(System.nanoTime()-start < TimeUnit.MILLISECONDS.toNanos(300), "stop blocked on task lock");
            until(() -> !s.taskWorkerRunning);
            check("cancelled".equals(s.receipt.state()), "receipt did not terminate on stop");
            s.receipt.finish("completed");
            check("cancelled".equals(s.receipt.state()), "late callback changed cancelled receipt");
            check(s.actions.get() == 1, "late callback advanced old batch");
        });
        test("new task blocked until old worker and cancellation settle", s -> {
            DshClient.holdDo = true; DshClient.holdCancel = true;
            s.runGoal("old"); until(() -> DshClient.doCalls.get() == 1);
            s.stopTask(); until(() -> DshClient.cancelCalls.get() > 0);
            s.runGoal("new");
            check(!s.logs.contains("🎯 目标：new"), "new task accepted while cancel outstanding");
            DshClient.release.countDown(); DshClient.cancelRelease.countDown();
            until(() -> s.logs.stream().anyMatch(x -> x.contains("已停止当前会话")));
            s.runGoal("fresh"); until(() -> DshClient.doCalls.get() == 2);
        });
        test("stop during batch delay prevents second dispatch", s -> {
            DshClient.action = new JSONObject().put("actions", new JSONArray(
                new JSONObject().put("action", "wait"), new JSONObject().put("action", "wait")));
            s.runGoalByAccessibility("batch"); until(() -> s.actions.get() == 1);
            Thread.sleep(80); s.stopTask();
            Thread.sleep(600); check(s.actions.get() == 1, "second action executed after stop");
        });
        test("late allow after stop cannot execute single action", s -> {
            DshClient.action = new JSONObject().put("action", "wait").put("sensitive", true);
            s.runGoalByAccessibility("confirm"); until(() -> s.waiting);
            s.stopTask(); s.decision.countDown(); Thread.sleep(150);
            check(s.actions.get() == 0, "late allow dispatched a stopped action");
        });
        test("failed do cannot leak fallback into later task", s -> {
            DshClient.failDo = true; s.runGoal("failed-old");
            until(() -> !s.agentRunning);
            s.runGoalByAccessibility("blocked-explicit");
            check(s.actions.get() == 0, "unknown task admitted native action");
            s.stopTask(); until(() -> !s.cancelUnconfirmed && !s.cancelInFlight);
            s.runGoalByAccessibility("new-explicit");
            until(() -> s.logs.stream().anyMatch(x -> x.startsWith("—— 任务结束")));
            check(s.logs.stream().noneMatch(x -> x.contains("目标：failed-old（")), "old fallback revived");
        });
        test("cancel failure is not reported as success and blocks restart", s -> {
            DshClient.holdDo = true; DshClient.failCancel = true;
            s.runGoal("old"); until(() -> DshClient.doCalls.get() == 1); s.stopTask();
            DshClient.release.countDown(); Thread.sleep(200);
            check(s.logs.stream().noneMatch(x -> x.contains("已停止当前会话")), "false cancellation success");
            s.runGoal("unsafe-new"); check(!s.logs.contains("🎯 目标：unsafe-new"), "restart after unconfirmed cancel");
            DshClient.failCancel = false; s.stopTask();
            until(() -> s.logs.stream().anyMatch(x -> x.contains("已停止当前会话")));
        });
        test("queued worker stopped before entry sends no do request", s -> {
            CountDownLatch blocker = new CountDownLatch(1);
            s.exec.execute(() -> await(blocker)); s.runGoal("queued"); s.stopTask();
            blocker.countDown(); Thread.sleep(150);
            check(DshClient.doCalls.get() == 0, "cancelled queued worker sent do");
        });
        test("old progress timer cannot overwrite later task", s -> {
            s.runGoal("old"); until(() -> !s.agentRunning);
            Runnable old = s.main.delayed.peek();
            DshClient.holdDo = true; s.runGoal("new"); until(() -> DshClient.doCalls.get() == 2);
            s.status = "new-status"; if (old != null) old.run();
            check(s.status.equals("new-status"), "old timer overwrote new task status");
            DshClient.release.countDown();
        });
        if (failed > 0) throw new AssertionError(failed + " regression cases failed");
    }
    static void restore(OverlayService s) throws Exception {
        java.lang.reflect.Method m = s.getClass().getDeclaredMethod("restoreTaskRecovery");
        m.setAccessible(true); m.invoke(s);
    }
    static class OverlayService {
        /* PRODUCTION_FIELDS */
        final List<String> logs = new CopyOnWriteArrayList<>();
        final AtomicInteger actions = new AtomicInteger();
        ActionReceipt receipt = ActionReceipt.of("completed");
        final CountDownLatch decision = new CountDownLatch(1);
        final Object decisionLock = new Object();
        String pendingDecision;
        volatile boolean waiting, panelShown;
        volatile String status = "";
        String configuredBridge = "test";
        final java.io.File files = new java.io.File("synthetic-" + UUID.randomUUID());
        java.io.File getFilesDir() { return files; }
        final TextView statusView = new TextView();
        final Button stopButton = new Button();
        final int MAX_STEPS = 1;
        void log(String s) { logs.add(s); }
        void toast(String s) {}
        void setStatus(String s) { status = s; }
        void setBubbleRunning(boolean b) {}
        void collapseWebUi() {}
        void showPanel(boolean b) {}
        void showConfirmBar(boolean b) {}
        void setDecision(String d) {}
        String waitForDecision(long ms) { waiting = true; await(decision); return "allow"; }
        String waitForDecision(long gen, long ms) { return waitForDecision(ms); }
        boolean ensureAccessibility(int ms) { return true; }
        String bridgeUrl() { return configuredBridge; }
        String foregroundPackage() { return "test"; }
        Bitmap capture(int ms) { return null; }
        String jpegBase64(Bitmap b) { return ""; }
        String recentNotifications() { return ""; }
        boolean looksSensitive(String text) { return false; }
        ActionReceipt executeAction(JSONObject a) { actions.incrementAndGet(); return receipt; }
        /* PRODUCTION_METHODS */
    }
    static class Handler {
        final Queue<Runnable> delayed = new ConcurrentLinkedQueue<>();
        final Queue<Runnable> deferred = new ConcurrentLinkedQueue<>();
        volatile Thread deferThread;
        Handler(Object o) {}
        void post(Runnable r) { if (Thread.currentThread() == deferThread) deferred.add(r); else r.run(); }
        void postDelayed(Runnable r, long ms) { delayed.add(r); }
    }
    static class Looper { static Object getMainLooper() { return null; } static Object myLooper() { return null; } }
    static class TextView { void setText(String s) {} }
    static class Button { void setText(String s) {} void setEnabled(boolean b) {} }
    static class Bitmap {}
    static class TermuxRunner { static void boot(Object o) {} }
    static class AgentAccessibilityService {
        static AgentAccessibilityService INSTANCE = new AgentAccessibilityService();
        String dumpNodes(int n) { return "nodes"; }
    }
    static class JSONObject {
        final Map<String,Object> data = new HashMap<>();
        JSONObject put(String k, Object v) { data.put(k,v); return this; }
        Object opt(String k) { return data.get(k); }
        String optString(String k, String d) { Object v=data.get(k); return v==null?d:v.toString(); }
        String optString(String k) { return optString(k, ""); }
        int optInt(String k) { return 0; }
        boolean optBoolean(String k, boolean d) { return (Boolean)data.getOrDefault(k,d); }
        JSONArray optJSONArray(String k) { return (JSONArray)data.get(k); }
        boolean has(String k) { return data.containsKey(k); }
    }
    static class JSONArray {
        JSONObject[] values;
        JSONArray(JSONObject... v) { values=v; }
        int length() { return values.length; }
        JSONObject optJSONObject(int i) { return values[i]; }
    }
    static class DshClient {
        static String lastCancelBase;
        static volatile String lastDoId;
        static volatile String lastAskId;
        static List<String> cancelIds = new CopyOnWriteArrayList<>();
        static volatile boolean holdDo, holdCancel, failDo, failCancel;
        static volatile String replyText;
        static volatile JSONObject action;
        static CountDownLatch release, cancelRelease;
        static AtomicInteger doCalls = new AtomicInteger(), cancelCalls = new AtomicInteger();
        static AtomicInteger askCalls = new AtomicInteger();
        static void reset() { holdDo=holdCancel=failDo=failCancel=false; doCalls.set(0); cancelCalls.set(0);
            lastDoId=""; cancelIds.clear();
            lastAskId=""; askCalls.set(0);
            release=new CountDownLatch(1); cancelRelease=new CountDownLatch(1);
            replyText="done"; action=new JSONObject().put("action", "done"); }
        static class Result { boolean ok=true; String text="done", error="test-failure", failureKind="rejected"; int statusCode=200; JSONObject action; }
        static Result post(OverlayService context, String base, String path, JSONObject body, int ms) {
            Result r = new Result();
            if (path.equals("/do")) { lastDoId=body.optString("task_id", ""); doCalls.incrementAndGet(); if(holdDo) await(release); r.ok=!failDo; r.text=replyText; }
            if (path.equals("/ask")) { lastAskId=body.optString("task_id", ""); askCalls.incrementAndGet(); if(holdDo) await(release); r.ok=!failDo; r.text=replyText; }
            if (path.equals("/cancel")) { lastCancelBase=base; cancelIds.add(body.optString("task_id", "")); cancelCalls.incrementAndGet(); if(holdCancel) await(cancelRelease); r.ok=!failCancel; }
            if (path.equals("/step")) r.action=action;
            return r;
        }
    }
    static class TaskRecoveryStore {
        static final Map<String,State> records = new ConcurrentHashMap<>();
        static boolean failWrite;
        final String key;
        TaskRecoveryStore(java.io.File file) { key=file.getParent(); }
        static class State {
            final boolean pending; final String id, bridge;
            State(boolean pending, String id, String bridge) { this.pending=pending; this.id=id; this.bridge=bridge; }
        }
        static State snapshot(java.io.File dir) { return records.getOrDefault(dir.getPath(), new State(false,"","")); }
        State read() { return records.getOrDefault(key, new State(false,"","")); }
        State begin(String bridge) {
            if (failWrite || read().pending) return null;
            State s = new State(true, UUID.randomUUID().toString(), bridge); records.put(key,s); return s;
        }
        boolean clear(String id) {
            if (failWrite || id.isEmpty() || !id.equals(read().id)) return false;
            records.put(key,new State(false,"","")); return true;
        }
    }
}
