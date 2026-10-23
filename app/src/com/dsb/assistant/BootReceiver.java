package com.dsb.assistant;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.os.SystemClock;
import android.util.Log;

/**
 * 开机 / App 更新后自动就位：
 *   1) 起悬浮球服务（前台服务，specialUse）
 *   2) 让 Termux 侧把 dsh web + URL 服务 + 手机代理桥拉起来
 * 不需要用户点任何东西。Android 12+ 起后台启动前台服务需要豁免，
 * 本 App 持有 SYSTEM_ALERT_WINDOW（悬浮球本来就要），属于系统明列的豁免项。
 *
 * 2026-09-22 加的一道闸：用户从最近任务里把 App 划掉后，**有些 ROM（开发验证用的那台就是）
 * 会补发一次 BOOT_COMPLETED**，想借"开机自启"的名义把 App 再拉起来 —— 实测
 * 划掉后 50ms 就收到了一次（设备 uptime 5 小时，根本没重启）。
 *
 * ⚠️ 判据 v2（2026-09-22 16:10 实测修正）：
 * 旧版靠"用户关闭标记"（user_closed.flag）分辨真假开机，但**这条路会丢**：
 * 划掉后系统 3ms 内 SIGKILL，onTaskRemoved 经常来不及把 flag 写完 →
 * 49ms 后 BootReceiver 读到 closedAtBoot=-1 → 把假开机当真开机 → 球和 dsh 全被拉回来
 * （实测日志：16:10:56.800 划掉 → .849 genuineBoot=true → .851 起了 boot.sh）。
 * 现在改用**开机会话对比**：
 *   * 广播的 bootCount == App 上次运行时的 bootCount → 同一次开机会话，根本没重启
 *     → 这是 ROM 借 BOOT_COMPLETED 复活 App → 不但不复活，还主动收球 + 停 dsh
 *   * bootCount 变了（真重启）/ App 从没运行过 → 正常就位
 *   * MY_PACKAGE_REPLACED（用户安装更新）→ 正常就位
 * 运行时会话记录由 OverlayService.markAppRunning()/MainActivity 在 App 起来时落盘。
 */
public class BootReceiver extends BroadcastReceiver {
    @Override public void onReceive(Context context, Intent intent) {
        String a = intent == null ? null : intent.getAction();
        // 先校验系统 action，再读标记和做启停决策；显式调用也不能绕过。
        if (!Intent.ACTION_BOOT_COMPLETED.equals(a) && !Intent.ACTION_MY_PACKAGE_REPLACED.equals(a)) return;
        boolean packageReplaced = Intent.ACTION_MY_PACKAGE_REPLACED.equals(a);
        long upMs = SystemClock.elapsedRealtime();
        int nowBoot = OverlayService.bootCount(context);
        int closedBoot = OverlayService.closedFlagBootCount(context);
        int runBoot = OverlayService.appRunBootCount(context);
        long runAt = OverlayService.appRunElapsedMs(context);

        // 同一次开机会话 = 广播的 bootCount 与"App 运行过的那次开机"一致
        // （关闭标记也算一路证据，但它经常抢写失败，只能当辅助）
        boolean sameSession = nowBoot != -1 && (runBoot == nowBoot || closedBoot == nowBoot);
        // ⚠️ 启动竞态：ROM 在 App 刚被拉起来时也会补发一次 BOOT_COMPLETED
        //（实测 force-stop → start 后 1.6 秒收到）。那是"正在启动"，绝不能被当成"用户划掉"，
        //  否则刚打开 App 就把球和 dsh 一起干掉。
        //  两个判据都要看（只用运行标记不够：进程刚 fork 出来时标记还没被重写）：
        //    * 运行标记年龄 < 15s  → App 刚起来
        //    * **本进程启动 < 15s** → Receiver 自己就在一个刚起来的进程里（划掉后 ROM 也是
        //      新起一个进程来收这条广播，所以"新进程 + 假开机"= 启动竞态，一律不处理；
        //      该场景下球本来就已经随旧进程消失，dsh 由 3098 存活连接兜底停掉）。
        long runAge = (runAt == -1) ? -1 : (upMs - runAt);
        long procAge = upMs - android.os.Process.getStartElapsedRealtime();
        boolean justStarted = (runAge >= 0 && runAge < 15000) || (procAge >= 0 && procAge < 15000);
        boolean fakeBoot = !packageReplaced && sameSession && !justStarted;
        Log.i("DSB", "BootReceiver: " + a + " bootCount=" + nowBoot + " runBoot=" + runBoot
                + " closedAtBoot=" + closedBoot + " runAge=" + (runAge / 1000) + "s"
                + " procAge=" + (procAge / 1000) + "s uptime=" + (upMs / 1000) + "s"
                + " fakeBoot=" + fakeBoot + " justStarted=" + justStarted);

        if (justStarted && sameSession && !packageReplaced) {
            Log.i("DSB", "ROM 补发的假开机，但 App/进程刚启动（runAge=" + (runAge / 1000)
                    + "s procAge=" + (procAge / 1000) + "s，启动竞态）→ 不处理，交给正常启动流程");
            return;
        }
        if (fakeBoot) {
            Log.i("DSB", "ROM 补发的假开机（同一 bootCount，且 App 已运行 " + (runAge / 1000) + "s）→ 视为用户划掉：收球 + 停 dsh，不复活");
            OverlayService.markUserClosed(context);                                   // 落盘，START_STICKY 再拉也不冒头
            try { context.stopService(new Intent(context, OverlayService.class)); } catch (Throwable ignored) {}
            TermuxRunner.shutdown(context);                                           // 手机侧 dsh 也一起停
            return;
        }

        // 真开机 / App 更新 / 首次运行：清标记，就位
        try { OverlayService.closedFlag(context).delete(); } catch (Throwable ignored) {}
        try {
            context.startForegroundService(new Intent(context, OverlayService.class));
        } catch (Throwable t) {
            Log.e("DSB", "startFGS failed: " + t.getClass().getSimpleName());
        }
        TermuxRunner.boot(context);
    }
}
