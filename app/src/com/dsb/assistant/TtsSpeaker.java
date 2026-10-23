package com.dsb.assistant;

import android.content.Context;
import android.os.Build;
import android.os.Bundle;
import android.speech.tts.TextToSpeech;
import android.speech.tts.UtteranceProgressListener;
import android.speech.tts.Voice;
import android.util.Log;

import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.Set;

/**
 * 本地 TTS 朗读（2026-09-23 新功能）
 *
 * ═══ 需求 ═══
 * 用户："当卡片信息显示完之后，使用本地的 TTS 把卡片的内容给我讲一下。"
 * 即：每轮回答末尾的「结论卡」出现后，用手机本地语音念出来。
 *
 * ═══ 为什么做在 App 里，而不是 Termux 侧 ═══
 * · 容器（proot Ubuntu）里没有 /dev/snd，**放不出声**，只能生成文件；
 *   而 Android 的音频输出必须由 App 进程调用 AudioTrack/MediaPlayer/TextToSpeech。
 * · Termux 侧要放音得装 Termux:API（多一个 App），且 espeak-ng 是机械音。
 * · 本 App 已有 CmdReceiver 广播入口 → 加一个命令即可，零额外依赖，且用系统语音引擎。
 *
 * ═══ 用法 ═══
 *   adb shell am broadcast -n com.dsb.assistant/.CmdReceiver \
 *       -a com.dsb.assistant.CMD --es token dsh-8f3a2c --es cmd "朗读 要念的文字"
 *   adb shell am broadcast ... --es cmd "停止朗读"
 *
 * ═══ 设计要点 ═══
 * · **单例**：TTS 引擎初始化慢（几百毫秒），每次新建会很卡；常驻一个实例复用。
 * · **懒初始化 + 初始化后自动念**：第一次收到命令时引擎可能还没就绪，
 *   把待念文本存起来，等 onInit 回调再念（否则第一次调用必然静默失败）。
 * · **打断上一次**：新内容来时先 stop()，避免排队堆积（用户在连续对话时会烦）。
 * · **中文优先**：优先选中文语音；引擎不支持中文时回退到默认 Locale 并记日志
 *   （而不是静默不响 —— 那样无法排查）。
 */
public final class TtsSpeaker {

    private static final String TAG = "DSH-TTS";

    private static TtsSpeaker INSTANCE;

    private TextToSpeech tts;
    private boolean ready = false;
    private boolean failed = false;

    /** 引擎还没就绪时收到的待念文本（onInit 后补念）。 */
    private String pending;

    /** 最近一次实际使用的语音描述（供日志/排查）。 */
    private String voiceInfo = "(未初始化)";

    // ── 朗读音量 / 语速（2026-09-23 用户要求"声音调大一点、速度调快一点"）──
    // 音量：用 TTS 自己的 KEY_PARAM_VOLUME（只影响朗读，**不动系统媒体音量** ——
    //       否则会连带改掉听音乐/看视频的音量）。
    // 语速：TextToSpeech.setSpeechRate，1.0 = 正常。
    // 两者都持久化到 SharedPreferences，重启后仍生效。
    private float volume = 1.0f;
    private float rate = 1.3f;
    private static final String SP = "dsh";
    private static final String K_VOL = "tts_volume";
    private static final String K_RATE = "tts_rate";
    private Context appCtx;

    /**
     * 朗读结果回报文件（2026-09-23 加）。
     *
     * ═══ 为什么需要 ═══
     * 手机侧守护（tts_reader.py）只"发广播"就结束了，**无法知道到底念成功没有**。
     * 用户反馈"上面那张卡我没听到 TTS"时，我只能看到守护说"已念"，
     * App 侧却没有出声 —— 静默失败，查不出来。
     * 现在 App 把每次朗读的结果写到这里，守护读它来判断成功/失败并决定要不要重试。
     *
     * 路径选 App 私有目录：proot 容器里以同一 uid 运行，可直接读；
     * 不必申请存储权限（/sdcard 需要权限，本 App 没有）。
     */
    /**
     * 朗读结果回报端点（2026-09-23 定）。
     *
     * ═══ 为什么用 HTTP 而不是写文件 ═══
     * 最初想写 /data/data/com.dsb.assistant/files/tts_status.json，实测**行不通**：
     *   · 容器（proot）的 /data/data 只映射了自己（com.termux）的目录，
     *     看不到 com.dsb.assistant（Android uid 隔离）→ 守护读不到；
     *   · /sdcard/Android/data/<包>/ 普通应用在 Android 11+ 也访问不到。
     * 而 **HTTP 是通的**：App 一直在用 127.0.0.1:3097（url-server）。
     * 所以改成 App POST 结果到守护监听的端口。
     */
    private static final String STATUS_URL = "http://127.0.0.1:3099/tts-result";
    /** 朗读自然结束时的回调（只对最近一次 speak 生效，用完即清）。 */
    private volatile Runnable doneCallback;

    private TtsSpeaker() {}

    public static synchronized TtsSpeaker get(Context ctx) {
        if (INSTANCE == null) INSTANCE = new TtsSpeaker();
        INSTANCE.ensureInit(ctx.getApplicationContext());
        return INSTANCE;
    }

    private void ensureInit(Context app) {
        if (tts != null || failed) return;
        appCtx = app;
        try {
            android.content.SharedPreferences sp = app.getSharedPreferences(SP, Context.MODE_PRIVATE);
            volume = sp.getFloat(K_VOL, 1.0f);
            rate = sp.getFloat(K_RATE, 1.3f);
        } catch (Throwable ignored) {}
        try {
            tts = new TextToSpeech(app, status -> {
                if (status != TextToSpeech.SUCCESS) {
                    failed = true;
                    ready = false;
                    Log.w(TAG, "❌ TTS 引擎初始化失败 status=" + status);
                    return;
                }
                ready = true;
                configureLanguage();
                Log.i(TAG, "✅ TTS 就绪 " + voiceInfo);
                // 补念：初始化期间收到的文本
                if (pending != null) {
                    String p = pending;
                    pending = null;
                    speak(p);
                }
            });
            tts.setOnUtteranceProgressListener(new UtteranceProgressListener() {
                @Override public void onStart(String id) {
                    Log.i(TAG, "▶ 开始朗读 " + id);
                    writeStatus("speaking", id, 0, null, null);
                }
                @Override public void onDone(String id) {
                    Log.i(TAG, "⏹ 朗读结束 " + id);
                    writeStatus("done", id, 0, null, null);
                    Runnable cb = doneCallback;
                    doneCallback = null;
                    if (cb != null) {
                        try { cb.run(); } catch (Throwable t) { Log.w(TAG, "onDone 回调异常：" + t.getClass().getSimpleName()); }
                    }
                }
                @Override public void onError(String id) {
                    Log.w(TAG, "❌ 朗读出错 " + id);
                    writeStatus("error", id, 0, null, "引擎回调 onError");
                }
            });
        } catch (Throwable t) {
            failed = true;
            Log.w(TAG, "❌ TTS 创建异常：" + t.getClass().getSimpleName());
        }
    }

    /** 选中文语音；挑不到就退回默认，并如实记录（不静默失败）。 */
    private void configureLanguage() {
        if (tts == null) return;
        List<Locale> candidates = new ArrayList<>();
        Locale zhCN = Locale.SIMPLIFIED_CHINESE;          // zh_CN
        candidates.add(zhCN);
        candidates.add(Locale.CHINESE);                    // zh
        if (Build.VERSION.SDK_INT >= 21) candidates.add(Locale.forLanguageTag("zh-Hans-CN"));

        for (Locale loc : candidates) {
            try {
                int r = tts.setLanguage(loc);
                if (r != TextToSpeech.LANG_MISSING_DATA && r != TextToSpeech.LANG_NOT_SUPPORTED) {
                    voiceInfo = "中文 " + loc + "（系统默认语音）";
                    applyBestVoice(loc);
                    return;
                }
            } catch (Throwable ignored) {}
        }
        // 回退：默认 Locale
        try {
            tts.setLanguage(Locale.getDefault());
            voiceInfo = "⚠ 未找到中文语音，回退到 " + Locale.getDefault();
            Log.w(TAG, voiceInfo);
        } catch (Throwable t) {
            voiceInfo = "⚠ 语言设置失败：" + t.getClass().getSimpleName();
        }
    }

    /** 在系统提供的 voices 里挑一个该语言的语音（质量通常比默认好）。 */
    private void applyBestVoice(Locale want) {
        if (Build.VERSION.SDK_INT < 21 || tts == null) return;
        try {
            Set<Voice> voices = tts.getVoices();
            if (voices == null || voices.isEmpty()) return;
            for (Voice v : voices) {
                if (v == null || v.getLocale() == null) continue;
                if (!v.getLocale().getLanguage().equals(want.getLanguage())) continue;
                if (v.isNetworkConnectionRequired()) continue;   // 只要本地语音（用户要求"本地 TTS"）
                tts.setVoice(v);
                voiceInfo = "中文语音：" + v.getName() + "（本地，无需联网）";
                return;
            }
        } catch (Throwable ignored) {}
    }

    /** 朗读。可在任意线程调用。 */
    public void speak(String text) { speak(text, null); }

    /**
     * 朗读，并在**自然念完**时回调 onDone。
     * 2026-09-23 加：用户要求"朗读结束后把界面带回逗沙包，如果我在其他 App"。
     * 只在 onDone 触发；onError 不触发（失败时不该抢屏）。
     */
    public void speak(String text, Runnable onDone) {
        doneCallback = onDone;
        if (text == null) return;
        String s = clean(text);
        if (s.isEmpty()) return;
        if (failed || tts == null) { Log.w(TAG, "TTS 不可用，忽略文字长度=" + s.length()); return; }
        if (!ready) { pending = s; Log.i(TAG, "引擎未就绪，暂存待念"); return; }
        try {
            tts.stop();                                   // 打断上一次，避免排队
            // 语速：每次念之前都设一遍（引擎可能被系统重置）
            try { tts.setSpeechRate(rate); } catch (Throwable ignored) {}
            String uttId = "dsh-" + System.currentTimeMillis();
            writeStatus("started", uttId, s.length(), s.substring(0, Math.min(30, s.length())), null);
            if (Build.VERSION.SDK_INT >= 21) {
                Bundle params = new Bundle();
                // 音量只作用于本次朗读，不影响系统媒体音量
                params.putFloat(TextToSpeech.Engine.KEY_PARAM_VOLUME, volume);
                tts.speak(s, TextToSpeech.QUEUE_FLUSH, params, uttId);
            } else {
                tts.speak(s, TextToSpeech.QUEUE_FLUSH, null);
            }
            Log.i(TAG, "🔊 朗读 " + s.length() + " 字（音量 " + volume + "，语速 " + rate + "，" + voiceInfo + "）");
        } catch (Throwable t) {
            Log.w(TAG, "❌ speak 异常：" + t.getClass().getSimpleName());
        }
    }

    /** 设置朗读音量（0.0~1.0，只影响朗读，不动系统媒体音量）。 */
    public String setVolume(float v) {
        if (v < 0f) v = 0f;
        if (v > 1f) v = 1f;
        volume = v;
        save();
        return "🔊 朗读音量 = " + volume + "（1.0 为最大；系统媒体音量未改动）";
    }

    /** 设置语速（1.0 正常，建议 0.5~3.0）。 */
    public String setRate(float r) {
        if (r < 0.1f) r = 0.1f;
        if (r > 4f) r = 4f;
        rate = r;
        try { if (tts != null) tts.setSpeechRate(rate); } catch (Throwable ignored) {}
        save();
        return "⏩ 语速 = " + rate + "（1.0 正常，当前约 " + Math.round(rate * 100) + "%）";
    }

    public float volume() { return volume; }
    public float rate() { return rate; }

    /** 把这次朗读的结果回报给手机侧守护（HTTP POST，后台线程，不阻塞 TTS 回调）。 */
    private void writeStatus(final String state, final String uttId, final int chars,
                             final String preview, final String err) {
        new Thread(() -> {
            java.net.HttpURLConnection c = null;
            try {
                org.json.JSONObject o = new org.json.JSONObject();
                o.put("state", state);
                o.put("uttId", uttId == null ? "" : uttId);
                o.put("chars", chars);
                o.put("preview", preview == null ? "" : preview);
                o.put("error", err == null ? "" : err);
                o.put("at", System.currentTimeMillis());
                o.put("engine", voiceInfo == null ? "" : voiceInfo);
                byte[] body = o.toString().getBytes("UTF-8");
                c = (java.net.HttpURLConnection) new java.net.URL(STATUS_URL).openConnection();
                c.setRequestMethod("POST");
                c.setDoOutput(true);
                c.setConnectTimeout(1500);
                c.setReadTimeout(1500);
                c.setRequestProperty("Content-Type", "application/json; charset=utf-8");
                c.setFixedLengthStreamingMode(body.length);
                try (java.io.OutputStream os = c.getOutputStream()) { os.write(body); }
                int code = c.getResponseCode();
                if (code / 100 != 2) Log.w(TAG, "⚠ 回报结果 HTTP " + code);
            } catch (Throwable t) {
                // 守护没在监听时失败是正常的，不必吵
                Log.i(TAG, "（结果回报失败，守护可能未监听：" + t.getClass().getSimpleName() + "）");
            } finally {
                if (c != null) try { c.disconnect(); } catch (Throwable ignored) {}
            }
        }, "dsh-tts-report").start();
    }

    private void save() {
        if (appCtx == null) return;
        try {
            appCtx.getSharedPreferences(SP, Context.MODE_PRIVATE).edit()
                    .putFloat(K_VOL, volume).putFloat(K_RATE, rate).apply();
        } catch (Throwable ignored) {}
    }

    public void stop() {
        try { if (tts != null) tts.stop(); } catch (Throwable ignored) {}
        pending = null;
    }

    /** 供日志/排查：当前语音信息。 */
    public String info() {
        if (failed) return "TTS 初始化失败";
        if (!ready) return "TTS 初始化中…";
        return voiceInfo;
    }

    /** 当前正在用的语音名（用于「换男声」等切换）。 */
    public String currentVoiceName() {
        if (Build.VERSION.SDK_INT < 21 || tts == null) return null;
        try { Voice v = tts.getVoice(); return v == null ? null : v.getName(); } catch (Throwable t) { return null; }
    }

    /**
     * 列出引擎里的候选语音（2026-09-23 用户要求"选一个男的声音"）。
     * 只列中文语音；带性别、质量、是否需联网 —— 用户据此挑，再用「用语音 <名字>」切换。
     */
    public String listVoices() {
        if (failed) return "TTS 初始化失败";
        if (!ready) return "TTS 初始化中，请稍后再试";
        if (Build.VERSION.SDK_INT < 21) return "系统版本过低，无法枚举语音";
        try {
            Set<Voice> voices = tts.getVoices();
            if (voices == null || voices.isEmpty()) return "引擎没有报告任何语音（可能需要在系统设置里下载语音包）";
            StringBuilder zh = new StringBuilder();
            StringBuilder other = new StringBuilder();
            int nZh = 0, nM = 0;
            for (Voice v : voices) {
                if (v == null || v.getLocale() == null) continue;
                String lang = v.getLocale().getLanguage();
                String line = v.getName()
                        + " | 性别=" + genderOf(v)
                        + " | 质量=" + v.getQuality()
                        + (v.isNetworkConnectionRequired() ? " | 需联网" : " | 本地");
                if ("zh".equals(lang)) {
                    nZh++;
                    if (v.getFeatures() != null && v.getFeatures().contains("male")) nM++;
                    zh.append("\n  ").append(line);
                } else if (other.length() < 300) {
                    other.append("\n  ").append(line);
                }
            }
            String cur = currentVoiceName();
            return "当前语音：" + (cur == null ? "(默认)" : cur)
                    + "\n中文语音 " + nZh + " 个（含 male 标记 " + nM + " 个）：" + zh
                    + (other.length() > 0 ? "\n其它语言（部分）：" + other : "");
        } catch (Throwable t) {
            return "枚举语音失败：" + t;
        }
    }

    private static String genderOf(Voice v) {
        try {
            Set<String> f = v.getFeatures();
            if (f == null) return "未标注";
            if (f.contains("male")) return "男";
            if (f.contains("female")) return "女";
            return "未标注";
        } catch (Throwable t) { return "未标注"; }
    }

    /**
     * 按名字切换语音（用户挑好男声后用）。
     * 名字要完全匹配引擎里的 name；找不到就保持原样并如实报告。
     */
    public String useVoice(String name) {
        if (!ready || tts == null) return "TTS 未就绪";
        if (Build.VERSION.SDK_INT < 21) return "系统版本过低";
        try {
            Set<Voice> voices = tts.getVoices();
            if (voices == null) return "引擎没有报告语音";
            for (Voice v : voices) {
                if (v != null && name.equals(v.getName())) {
                    int r = tts.setVoice(v);
                    if (r == TextToSpeech.SUCCESS) {
                        voiceInfo = "指定语音：" + v.getName() + "（性别=" + genderOf(v) + "）";
                        return "✅ 已切换到 " + voiceInfo;
                    }
                    return "❌ 切换失败，返回码 " + r;
                }
            }
            return "❌ 没找到名为「" + name + "」的语音（先用「列出语音」看可选名字）";
        } catch (Throwable t) {
            return "切换语音异常：" + t;
        }
    }

    /**
     * 把不适合朗读的内容清掉：
     * · markdown 记号（**、#、` 等）念出来很吵
     * · 代码块整体跳过（用户选择"跳过代码，只念正文"）
     * · 多余空白压平
     */
    static String clean(String raw) {
        if (raw == null) return "";
        String s = raw;
        // 去掉围栏代码块（```…```）
        s = s.replaceAll("(?s)```.*?```", " ");
        // 去掉行内代码的反引号，但保留里面的文字（如命令名，念出来有意义）
        s = s.replace("`", "");
        // markdown 强调/标题/引用记号
        s = s.replaceAll("(?m)^\\s{0,3}#{1,6}\\s*", "");
        s = s.replaceAll("(?m)^\\s{0,3}>\\s?", "");
        s = s.replaceAll("(?m)^\\s{0,3}[-*+]\\s+", "");
        s = s.replace("**", "").replace("__", "");
        s = cleanLinks(s);
        // URL 不念（又长又吵）
        s = s.replaceAll("https?://\\S+", " ");
        // 压缩空白
        s = s.replaceAll("[\\t\\x0B\\f\\r]+", " ");
        s = s.replaceAll("\\n{2,}", "\n");
        s = s.replaceAll(" {2,}", " ");
        return s.trim();
    }

    // Advance past each label/URL once, including malformed links; no regex backtracking.
    private static String cleanLinks(String s) {
        StringBuilder out = new StringBuilder(s.length());
        int cursor = 0, copied = 0;
        while (cursor < s.length()) {
            int start = s.indexOf('[', cursor);
            if (start < 0) break;
            int labelEnd = s.indexOf(']', start + 1);
            if (labelEnd < 0) break;
            cursor = labelEnd + 1;
            if (cursor >= s.length() || s.charAt(cursor) != '(') continue;
            int end = s.indexOf(')', cursor + 1);
            if (end < 0) break;
            out.append(s, copied, start).append(s, start + 1, labelEnd);
            cursor = copied = end + 1;
        }
        return out.append(s, copied, s.length()).toString();
    }
}
