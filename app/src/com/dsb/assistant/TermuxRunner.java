package com.dsb.assistant;

import android.content.Context;
import android.content.Intent;
import android.util.Log;

/**
 * 与 Termux 的官方接口（RUN_COMMAND intent）打交道的唯一入口。
 *
 * 为什么用它：dsh 跑在 Termux 的 proot Ubuntu 里，它的 token 文件在 Termux 私有目录，
 * 别的 App 读不到；而 RUN_COMMAND 允许我们把命令交给 Termux 执行（Termux 侧已开
 * allow-external-apps=true）。实测：新签名的第三方 App 只要在 manifest 里声明
 * com.termux.permission.RUN_COMMAND 并获得授权。这是 dangerous 权限，不是 DSB 专属白名单。
 */
public final class TermuxRunner {
    private static final String TAG = "DSB";

    private static final String ACTION = "com.termux.RUN_COMMAND";
    private static final String SERVICE = "com.termux.app.RunCommandService";
    private static final String BASH = "/data/data/com.termux/files/usr/bin/bash";
    private static final String TERMUX_HOME = "/data/data/com.termux/files/home";

    /** 手机侧引导脚本（幂等）：确保 URL 服务 / dsh web / 手机代理桥都在跑 */
    public static final String BOOT_SCRIPT = TERMUX_HOME + "/dsb/boot.sh";

    public static boolean run(Context ctx, String shellCommand) {
        try {
            Intent i = new Intent();
            i.setClassName("com.termux", SERVICE);
            i.setAction(ACTION);
            i.putExtra("com.termux.RUN_COMMAND_PATH", BASH);
            i.putExtra("com.termux.RUN_COMMAND_ARGUMENTS", new String[]{"-lc", shellCommand});
            i.putExtra("com.termux.RUN_COMMAND_WORKDIR", TERMUX_HOME);
            i.putExtra("com.termux.RUN_COMMAND_BACKGROUND", true);
            i.putExtra("com.termux.RUN_COMMAND_RUNNER", "app-shell");
            i.putExtra("com.termux.RUN_COMMAND_COMMAND_LABEL", "dsb-boot");
            i.putExtra("com.termux.RUN_COMMAND_COMMAND_DESCRIPTION", "DSH App: 确保手机侧服务在跑");
            ctx.startForegroundService(i);
            Log.i(TAG, "RUN_COMMAND boot/lifecycle request sent");
            return true;
        } catch (Throwable t) {
            Log.e(TAG, "RUN_COMMAND failed: " + t.getClass().getSimpleName());
            return false;
        }
    }

    /** 拉起手机侧引导（幂等、不阻塞、结果不回收） */
    public static void boot(Context ctx) {
        run(ctx, "(" + BOOT_SCRIPT + " >/dev/null 2>&1 &)");
    }

    /**
     * 用户划掉 App：把手机侧也停掉（省电）。
     * 停的是 dsh web / URL 小服务 / 代理桥 / 看门狗；下次打开 App 或重启手机时 boot.sh 会全拉回来。
     */
    public static void shutdown(Context ctx) {
        run(ctx, "(" + TERMUX_HOME + "/dsb/shutdown.sh >/dev/null 2>&1 &)");
    }

    /** No shell command or payload is constructed from a task. Transport timeout is not a stop receipt. */
    public static org.json.JSONObject request(Context ctx, String route, org.json.JSONObject body, int timeoutMs) throws Exception {
        String id = java.util.UUID.randomUUID().toString();
        byte[] random = new byte[32]; new java.security.SecureRandom().nextBytes(random);
        StringBuilder n = new StringBuilder(); for (byte b : random) n.append(String.format("%02x", b & 255));
        String nonce = n.toString();
        java.io.File root = new java.io.File(ctx.getFilesDir(), "trusted-channel");
        byte[] payload = new org.json.JSONObject().put("path", route).put("body", body).toString().getBytes("UTF-8");
        TrustedFiles.create(root, id, nonce, payload, Math.min(960000, timeoutMs + 10000));
        long until = android.os.SystemClock.elapsedRealtime() + timeoutMs;
        try {
            org.json.JSONObject meta = new org.json.JSONObject().put("request_id", id).put("nonce", nonce)
                .put("size", payload.length).put("sha256", TrustedFiles.sha256(payload)).put("timeout_ms", timeoutMs)
                .put("route", route);
            Intent i = new Intent(ACTION).setClassName("com.termux", SERVICE);
            i.putExtra("com.termux.RUN_COMMAND_PATH", "/data/data/com.termux/files/usr/bin/python3");
            i.putExtra("com.termux.RUN_COMMAND_ARGUMENTS", new String[]{TERMUX_HOME + "/dsb/trusted-entry.py"});
            i.putExtra("com.termux.RUN_COMMAND_STDIN", meta.toString());
            i.putExtra("com.termux.RUN_COMMAND_BACKGROUND", true);
            i.putExtra("com.termux.RUN_COMMAND_WORKDIR", TERMUX_HOME);
            i.putExtra("com.termux.RUN_COMMAND_COMMAND_LABEL", "dsb-trusted-channel");
            ctx.startForegroundService(i);
            while (android.os.SystemClock.elapsedRealtime() < until) {
                byte[] reply = TrustedFiles.result(root, id);
                if (reply != null) return new org.json.JSONObject(new String(reply, "UTF-8"));
                Thread.sleep(40);
            }
            throw new java.net.SocketTimeoutException("trusted_channel_timeout_state_unknown");
        } finally { TrustedFiles.cleanup(root, id); }
    }

}
