package com.dsb.assistant;

import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;

/** A system action receipt, not proof that the user's goal was achieved. */
public final class ActionReceipt {
    private final CountDownLatch ready = new CountDownLatch(1);
    private String state = "pending";

    public synchronized void finish(String result) {
        if (!"pending".equals(state)) return;
        state = result;
        ready.countDown();
    }

    public static ActionReceipt of(String state) {
        ActionReceipt receipt = new ActionReceipt();
        receipt.finish(state);
        return receipt;
    }

    public String await(long timeoutMs) throws InterruptedException {
        try {
            if (!ready.await(timeoutMs, TimeUnit.MILLISECONDS)) finish("timeout");
        } catch (InterruptedException e) {
            finish("cancelled");
            throw e;
        }
        return state();
    }

    public synchronized String state() { return state; }

    public static boolean succeeded(String state) {
        return "completed".equals(state) || "verified".equals(state) || "accepted".equals(state);
    }
}
