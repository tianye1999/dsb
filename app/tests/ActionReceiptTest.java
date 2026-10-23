import com.dsb.assistant.ActionReceipt;

public class ActionReceiptTest {
    private static void check(boolean ok, String message) {
        if (!ok) throw new AssertionError(message);
    }
    public static void main(String[] args) throws Exception {
        for (String failure : new String[]{"rejected", "cancelled", "timeout", "input_unverified", "input_rejected", "unsupported_action"}) {
            check(!ActionReceipt.succeeded(ActionReceipt.of(failure).await(1)), failure);
        }
        for (String success : new String[]{"accepted", "completed", "verified"}) {
            check(ActionReceipt.succeeded(ActionReceipt.of(success).await(1)), success);
        }
        ActionReceipt timed = new ActionReceipt();
        check("timeout".equals(timed.await(2)), "missing callback did not time out");
        timed.finish("completed");
        check("timeout".equals(timed.state()), "late completion overwrote timeout");
        ActionReceipt cancelled = new ActionReceipt();
        Thread.currentThread().interrupt();
        try { cancelled.await(10); throw new AssertionError("interrupt ignored"); }
        catch (InterruptedException expected) {}
        check("cancelled".equals(cancelled.state()), "interrupt not recorded");
        cancelled.finish("completed");
        check("cancelled".equals(cancelled.state()), "late callback overwrote cancellation");
        ActionReceipt completed = new ActionReceipt();
        Thread callback = new Thread(() -> completed.finish("completed"));
        callback.start();
        check("completed".equals(completed.await(1000)), "callback not received");
        callback.join();
        System.out.println("PASS receipt success/failure, timeout, interruption, late callback, completion");
    }
}
