package com.dsb.assistant;

import android.Manifest;
import android.content.Context;
import android.content.pm.PackageManager;
import android.os.SystemClock;
import android.provider.Settings;
import android.util.Log;

/** Optional recovery, enabled only by a pre-existing development permission grant. */
final class AccessibilityRecovery {
    private static boolean allowed(Context c) {
        return c.checkSelfPermission(Manifest.permission.WRITE_SECURE_SETTINGS) == PackageManager.PERMISSION_GRANTED;
    }
    private static String own(Context c) { return c.getPackageName() + "/" + AgentAccessibilityService.class.getName(); }
    private static boolean isOwn(Context c, String entry) {
        return entry.equals(own(c)) || entry.equals(c.getPackageName() + "/.AgentAccessibilityService");
    }
    private static String list(Context c) {
        return Settings.Secure.getString(c.getContentResolver(), Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES);
    }
    private static boolean listed(Context c, String current) {
        if (current != null) for (String entry : current.split(":")) if (isOwn(c, entry)) return true;
        return false;
    }
    static synchronized boolean enable(Context c) {
        if (!allowed(c)) return false;
        try {
            String current = list(c);
            if (!listed(c, current)) {
                String next = current == null || current.isEmpty() ? own(c) : current + ":" + own(c);
                if (!Settings.Secure.putString(c.getContentResolver(), Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES, next)) return false;
            }
            return Settings.Secure.getInt(c.getContentResolver(), Settings.Secure.ACCESSIBILITY_ENABLED, 0) == 1
                || Settings.Secure.putInt(c.getContentResolver(), Settings.Secure.ACCESSIBILITY_ENABLED, 1);
        } catch (Exception e) {
            Log.i("DSH", "accessibility recovery failed: " + e.getClass().getSimpleName());
            return false;
        }
    }
    private static synchronized boolean removeOwn(Context c) {
        if (!allowed(c)) return false;
        String current = list(c);
        if (!listed(c, current)) return true;
        java.util.List<String> others = new java.util.ArrayList<>();
        for (String entry : current.split(":", -1)) if (!isOwn(c, entry)) others.add(entry);
        return Settings.Secure.putString(c.getContentResolver(), Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES, String.join(":", others));
    }
    // Background threads only. A setting write is not evidence of a bound service.
    static boolean await(Context c, long waitMs, boolean rebind) {
        if (AgentAccessibilityService.INSTANCE != null) return true;
        if (!allowed(c)) return false;
        try {
            if (rebind) {
                if (!removeOwn(c)) return false;
                try { Thread.sleep(700); }
                finally { enable(c); } // Re-read the list, preserving other services changed during the wait.
            }
            if (!enable(c)) return false;
            long until = SystemClock.elapsedRealtime() + Math.min(30000, Math.max(0, waitMs));
            while (AgentAccessibilityService.INSTANCE == null && SystemClock.elapsedRealtime() < until) Thread.sleep(300);
            return AgentAccessibilityService.INSTANCE != null;
        } catch (InterruptedException e) { Thread.currentThread().interrupt(); return false; }
        catch (Exception e) { Log.i("DSH", "accessibility binding failed: " + e.getClass().getSimpleName()); return false; }
    }
}
