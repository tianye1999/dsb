package com.dsb.assistant;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.util.Log;

/**
 * adb 调试入口：
 *   adb shell am broadcast -n com.dsb.assistant/.CmdReceiver -a com.dsb.assistant.CMD \
 *       --es token dsh-8f3a2c --es cmd "把当前屏幕上的内容念出来"
 * 真正的门槛是清单里的接收权限 INJECT_EVENTS（shell、root、系统及平台签名 App 持有，普通 App 拿不到）：
 * 手机上任意 App 都不能给助手下指令。口令 token 是公开的兼容参数，不是秘密（R6-0，2026-10-21）。
 */
public class CmdReceiver extends BroadcastReceiver {
    @Override public void onReceive(Context context, Intent intent) {
        if ("com.dsb.assistant.TRANSPORT_READ".equals(intent.getAction())
                || "com.dsb.assistant.TRANSPORT_WRITE".equals(intent.getAction())) {
            setResultCode(0);
            try {
                java.io.File root = new java.io.File(context.getFilesDir(), "trusted-channel");
                String id = intent.getStringExtra("request_id"), nonce = intent.getStringExtra("nonce");
                int offset = intent.getIntExtra("offset", -1);
                if ("com.dsb.assistant.TRANSPORT_READ".equals(intent.getAction())) {
                    setResultData(TrustedFiles.read(root, id, nonce, offset,
                        intent.getIntExtra("length", TrustedFiles.CHUNK)));
                } else {
                    String encoded = intent.getStringExtra("data");
                    if (encoded == null || encoded.length() > 4 * ((TrustedFiles.WRITE_CHUNK + 2) / 3)) return;
                    TrustedFiles.write(root, id, nonce, offset, intent.getIntExtra("total", -1),
                        intent.getStringExtra("sha256"), java.util.Base64.getDecoder().decode(encoded));
                    setResultData("");
                }
                setResultCode(1);
            } catch (Exception e) { /* Never log task content, URL tokens, or nonces. */ }
            return;
        }
        String token = intent.getStringExtra("token");
        if (!OverlayService.TOKEN.equals(token)) {
            Log.i("DSH", "CmdReceiver 拒绝：token 不对");
            return;
        }
        String cmd = intent.getStringExtra("cmd");
        if (cmd == null || cmd.isEmpty()) return;
        OverlayService.dispatch(context, cmd);
    }
}
