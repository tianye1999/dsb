import java.util.*;

public class WebViewRecoveryTest {
    private static final String TOKEN_URL = "http://127.0.0.1:3080/?token=fixture-secret";
    private static final String FRESH_URL = "http://127.0.0.1:3080/?token=fixture-fresh";
    private static int passed, failed;

    static class Clock {
        static long now = 1000000, limit = Long.MAX_VALUE;
        static void tick(long millis) {
            check(millis >= 0 && now + millis <= limit, "recovery did not terminate within the bounded test window");
            now += millis;
        }
    }
    static class Looper { static Looper getMainLooper() { return new Looper(); } }
    static class Handler {
        static class Pending {
            final Runnable runnable; final long due;
            Pending(Runnable r, long d) { runnable = r; due = d; }
        }
        static List<Pending> pending = new ArrayList<>();
        Handler() {} Handler(Looper looper) {}
        boolean post(Runnable r) { return postDelayed(r, 0); }
        boolean postDelayed(Runnable r, long delay) { pending.add(new Pending(r, Clock.now + delay)); return true; }
        void removeCallbacks(Runnable r) { pending.removeIf(p -> p.runnable == r); }
        void removeCallbacksAndMessages(Object token) { pending.clear(); }
        static void advance(long millis) {
            long target = Clock.now + millis;
            for (int guard = 0; guard < 1000; guard++) {
                Pending next = pending.stream().filter(p -> p.due <= target)
                    .min(Comparator.comparingLong(p -> p.due)).orElse(null);
                if (next == null) { Clock.tick(target - Clock.now); return; }
                pending.remove(next); Clock.tick(next.due - Clock.now); next.runnable.run();
            }
            throw new AssertionError("unbounded recovery callback loop");
        }
    }
    static class Thread {
        static List<Runnable> workers = new ArrayList<>();
        final Runnable work;
        Thread(Runnable r, String name) { work = r; }
        void start() { workers.add(work); }
        static void sleep(long millis) throws InterruptedException {
            Clock.tick(millis);
        }
        static void drain() {
            int guard = 0;
            while (!workers.isEmpty()) {
                check(guard++ < 100, "unbounded ensure worker loop");
                workers.remove(0).run();
            }
        }
    }
    static class View {
        static final int VISIBLE = 0, GONE = 8;
        int visibility = VISIBLE;
        void setVisibility(int v) { visibility = v; }
    }
    static class LinearLayout extends View {}
    static class TextView extends View {
        String text = "";
        List<String> history = new ArrayList<>();
        void setText(String value) { text = value; history.add(value); }
    }
    static class Button extends TextView {}
    static class Bitmap {}
    static class Uri {
        final String value;
        Uri(String v) { value = v; }
        @Override public String toString() { return value; }
    }
    static class WebResourceRequest {
        final boolean main; final String url;
        WebResourceRequest(boolean m, String u) { main = m; url = u; }
        boolean isForMainFrame() { return main; }
        Uri getUrl() { return new Uri(url); }
    }
    static class WebResourceError {
        int getErrorCode() { return -6; }
        CharSequence getDescription() { return "net::ERR_CONNECTION_REFUSED " + TOKEN_URL; }
    }
    static class WebResourceResponse {
        final int code;
        WebResourceResponse(int c) { code = c; }
        int getStatusCode() { return code; }
    }
    static class WebView extends View {
        String url = TOKEN_URL;
        List<String> navigations = new ArrayList<>();
        int stops;
        String getUrl() { return url; }
        void loadUrl(String u) { url = u; navigations.add(u); }
        void reload() { navigations.add(url); }
        void stopLoading() { stops++; }
    }
    static class WebViewClient {
        public boolean shouldOverrideUrlLoading(WebView v, WebResourceRequest req) { return false; }
        public void onReceivedHttpError(WebView v, WebResourceRequest req, WebResourceResponse r) {}
        public void onReceivedError(WebView v, WebResourceRequest req, WebResourceError e) {}
        public void onPageStarted(WebView v, String url, Bitmap icon) {}
        public void onPageFinished(WebView v, String url) {}
    }
    static class Log {
        static List<String> entries = new ArrayList<>();
        static int i(String tag, String message) { entries.add(message); return 0; }
    }
    static class TermuxRunner {
        static int boots;
        static void boot(Object context) { boots++; }
    }
    static class Activity {
        boolean finishing;
        protected void onDestroy() {}
        protected void onResume() {}
        boolean isFinishing() { return finishing; }
        boolean isDestroyed() { return false; }
        void unregisterReceiver(Object receiver) {}
    }
    static class Harness extends Activity {
        static final String TAG = "DSB";
        WebView web = new WebView();
        LinearLayout splash = new LinearLayout();
        TextView splashText = new TextView();
        Button retryBtn = new Button();
        Object collapseReceiver;
        int fetches;
        String nextUrl = FRESH_URL;
        java.util.function.LongFunction<String> cacheAt;
        Runnable onFetch;
        /* PRODUCTION_FIELDS */
        String fetchUrl() {
            fetches++;
            if (onFetch != null) onFetch.run();
            return cacheAt == null ? nextUrl : cacheAt.apply(Clock.now);
        }
        void runOnUiThread(Runnable r) { r.run(); }
        boolean handleUrl(String u) { return false; }
        /* PRODUCTION_METHODS */
        final WebViewClient client = new WebViewClient() {
            /* PRODUCTION_CALLBACKS */
        };
        /* PRODUCTION_LIFECYCLE */
        Harness() {
            loadedUrl = TOKEN_URL;
            lastUrlCheck = Clock.now - 100000;
            web.visibility = View.VISIBLE;
            splash.visibility = View.GONE;
            retryBtn.visibility = View.GONE;
        }
        void error(boolean main) {
            client.onReceivedError(web, new WebResourceRequest(main, web.url), new WebResourceError());
        }
        void failAt(int code, String url) {
            WebResourceRequest request = new WebResourceRequest(true, url);
            if (code < 0) client.onReceivedError(web, request, new WebResourceError());
            else client.onReceivedHttpError(web, request, new WebResourceResponse(code));
        }
        void finished(String u) { client.onPageFinished(web, u); }
        void pageStarted(String u) { client.onPageStarted(web, u, null); }
        void clickRetry() { /* PRODUCTION_RETRY_CLICK */ }
    }

    static void check(boolean ok, String reason) { if (!ok) throw new AssertionError(reason); }
    static void ownedScreen(Harness h) {
        check(h.web.visibility == View.GONE, "network error must immediately hide native token-bearing page");
        check(h.splash.visibility == View.VISIBLE, "network error must immediately show owned wake screen");
        check(!h.splashText.text.contains("token="), "owned wake screen must not include private URL");
    }
    static void pump(java.util.function.BooleanSupplier done) {
        long previousLimit = Clock.limit;
        Clock.limit = Clock.now + 300000;
        try {
            for (int guard = 0; guard < 1000; guard++) {
                if (done.getAsBoolean()) return;
                if (!Thread.workers.isEmpty()) {
                    Thread.workers.remove(0).run();
                    continue;
                }
                Handler.Pending next = Handler.pending.stream()
                    .min(Comparator.comparingLong(p -> p.due)).orElse(null);
                if (next == null) return;
                Handler.advance(Math.max(0, next.due - Clock.now));
            }
            throw new AssertionError("unbounded delayed recovery work");
        } finally { Clock.limit = previousLimit; }
    }
    static void settle() { pump(() -> false); }
    static void navigate(Harness h) {
        int before = h.web.navigations.size();
        pump(() -> h.web.navigations.size() > before);
    }
    static void exhausted(Harness h, long started) {
        check(Clock.now - started >= 60000, "unchanged/missing cached URL must be given at least sixty seconds to recover");
        check(Clock.now - started <= 300000, "automatic wake waiting must terminate within a bounded time window");
        check(h.web.navigations.isEmpty(), "unchanged/missing suspect cached URL must never be loaded");
        check(h.retryBtn.visibility == View.VISIBLE, "bounded recovery exhaustion must offer explicit retry");
        check(Handler.pending.isEmpty() && Thread.workers.isEmpty(), "exhaustion must leave no delayed recovery work");
        ownedScreen(h);
    }
    static void elapsedProgress(Harness h) {
        int longest = 0;
        for (String text : h.splashText.history) {
            java.util.regex.Matcher elapsed = java.util.regex.Pattern.compile("已等\\s*(\\d+)\\s*秒").matcher(text);
            while (elapsed.find()) longest = Math.max(longest, Integer.parseInt(elapsed.group(1)));
            check(!text.contains("token=") && !text.contains("fixture-secret"), "wake progress must not reveal the suspect URL");
        }
        check(longest >= 60, "wake wait must display elapsed seconds through at least sixty seconds");
    }
    static void test(String name, Runnable body) {
        Clock.now = 1000000; Clock.limit = Long.MAX_VALUE;
        Handler.pending.clear(); Thread.workers.clear();
        Log.entries.clear(); TermuxRunner.boots = 0;
        try { body.run(); passed++; System.out.println("PASS " + name); }
        catch (Throwable error) { failed++; System.err.println("FAIL " + name + ": " + error); }
    }
    public static void main(String[] args) {
        test("main-frame connection error hides native error immediately even inside debounce window", () -> {
            Harness h = new Harness(); h.lastUrlCheck = Clock.now; h.error(true); ownedScreen(h);
            check(h.splashText.text.contains("唤醒"), "owned screen should report waking state");
        });
        test("subresource network failure leaves current page and recovery budget alone", () -> {
            Harness h = new Harness(); h.error(false); settle();
            check(h.web.visibility == View.VISIBLE && h.splash.visibility == View.GONE,
                "subresource failure must not replace visible page");
            check(h.fetches == 0 && Handler.pending.isEmpty(), "subresource failure must not schedule ensure");
        });
        test("recovery fetches fresh URL and stays hidden until successful page finish", () -> {
            Harness h = new Harness(); h.error(true); navigate(h);
            check(h.fetches > 0, "main-frame failure must fetch current URL through ensure path");
            check(h.web.navigations.contains(FRESH_URL), "recovery must load rotated token URL, not reload stale URL");
            ownedScreen(h);
            h.pageStarted(FRESH_URL); h.finished(FRESH_URL);
            check(h.web.visibility == View.VISIBLE && h.splash.visibility == View.GONE,
                "successful recovered page must reveal WebView");
        });
        test("error and blank page-finished callbacks cannot expose native page or reset budget", () -> {
            Harness h = new Harness(); h.error(true);
            h.finished(TOKEN_URL); h.pageStarted("about:blank"); h.finished("about:blank");
            ownedScreen(h);
            navigate(h); h.pageStarted(FRESH_URL); h.error(true); h.finished(FRESH_URL); ownedScreen(h);
        });
        test("repeated callbacks coalesce into one bounded ensure operation", () -> {
            Harness h = new Harness();
            for (int i = 0; i < 25; i++) h.error(true);
            navigate(h);
            check(Collections.frequency(h.web.navigations, FRESH_URL) == 1,
                "duplicate errors must coalesce into one recovered navigation");
            h.pageStarted(FRESH_URL); h.finished(FRESH_URL); settle();
            check(Thread.workers.isEmpty() && Handler.pending.isEmpty(), "duplicate error left extra work");
        });
        test("unchanged cached URL has a sixty-second minimum bounded recovery window", () -> {
            Harness h = new Harness(); h.nextUrl = TOKEN_URL;
            long started = Clock.now; h.error(true); settle(); exhausted(h, started);
        });
        test("internal blank/error navigation cannot unmask an exhausted failed page", () -> {
            for (String internal : new String[]{"about:blank", "chrome-error://chromewebdata/"}) {
                Harness h = new Harness();
                h.nextUrl = null; h.error(true); settle();
                check(h.retryBtn.visibility == View.VISIBLE && Handler.pending.isEmpty(),
                    "fixture must exhaust automatic recovery first");
                int fetches = h.fetches, boots = TermuxRunner.boots;
                h.pageStarted(internal); h.finished(internal); h.finished(h.web.getUrl());
                settle();
                check(h.fetches == fetches && TermuxRunner.boots == boots,
                    "internal/error page finish must not restart exhausted recovery");
                ownedScreen(h);
            }
        });
        test("successful recovery resets budget across eight intermittent failures", () -> {
            Harness h = new Harness();
            for (int i = 0; i < 8; i++) {
                String rotated = FRESH_URL + "-" + i; h.nextUrl = rotated;
                int before = h.fetches;
                h.error(true); ownedScreen(h);
                navigate(h);
                check(h.fetches > before && h.web.navigations.contains(rotated),
                    "each successful recovery must allow the next error to fetch a newly rotated address");
                h.pageStarted(rotated); h.finished(rotated);
                check(h.web.visibility == View.VISIBLE && h.splash.visibility == View.GONE,
                    "successful page finish must reveal recovered page");
            }
            check(h.web.navigations.size() == 8, "intermittent successful recoveries must not exhaust a lifetime retry allowance");
            h.nextUrl = FRESH_URL + "-ninth"; h.error(true); navigate(h);
            check(h.web.navigations.contains(h.nextUrl), "error after eight successful recoveries must still schedule automatic retry");
        });
        test("failed internal and stale page finishes preserve consumed recovery budget", () -> {
            Harness h = new Harness(); h.error(true); navigate(h);
            int fetches = h.fetches, boots = TermuxRunner.boots;
            check(fetches > 0, "fixture expected a consumed automatic retry");
            for (String rejected : new String[]{null, "about:blank", "chrome-error://chromewebdata/", TOKEN_URL}) {
                h.finished(rejected);
                check(h.fetches == fetches && TermuxRunner.boots == boots,
                    "non-successful page finish must not restart recovery");
                ownedScreen(h);
            }
            h.pageStarted(FRESH_URL); h.error(true);
            fetches = h.fetches; boots = TermuxRunner.boots;
            h.finished(FRESH_URL);
            check(h.fetches == fetches && TermuxRunner.boots == boots,
                "failed document finish must not synchronously restart recovery");
            ownedScreen(h);
        });
        test("explicit retry button gives exhausted recovery a fresh bounded attempt", () -> {
            Harness h = new Harness();
            h.nextUrl = null; h.error(true); settle();
            int before = h.fetches;
            check(h.retryBtn.visibility == View.VISIBLE, "fixture expected exhausted recovery");
            h.nextUrl = FRESH_URL; h.clickRetry(); navigate(h);
            check(h.fetches > before && h.web.navigations.contains(FRESH_URL) && h.retryBtn.visibility == View.GONE,
                "manual retry should fetch fresh URL and clear exhausted button");
            h.pageStarted(FRESH_URL); h.finished(FRESH_URL);
            h.nextUrl = FRESH_URL + "-after-manual"; h.error(true); navigate(h);
            check(h.web.navigations.contains(h.nextUrl), "manual retry should reset bounded automatic recovery budget");
        });
        test("main-frame HTTP error also protects token URL and recovers", () -> {
            Harness h = new Harness();
            h.client.onReceivedHttpError(h.web, new WebResourceRequest(true, TOKEN_URL), new WebResourceResponse(503));
            ownedScreen(h); navigate(h); check(h.fetches > 0, "HTTP main-frame failure did not ensure");
        });
        for (int failure : new int[]{-6, 401, 403}) {
            test("main-frame failure " + failure + " requests maintenance wake before recovery", () -> {
                Harness h = new Harness(); h.failAt(failure, TOKEN_URL); ownedScreen(h); navigate(h);
                check(TermuxRunner.boots > 0, "main-frame refusal/auth failure must request Termux maintenance wake even when cache is populated");
                check(h.web.navigations.contains(FRESH_URL), "available new address should recover after maintenance wake");
            });
        }
        test("unchanged address remains quarantined when maintenance wake has no effect", () -> {
            Harness h = new Harness(); h.nextUrl = TOKEN_URL;
            long started = Clock.now; h.error(true); settle();
            check(TermuxRunner.boots > 0, "unchanged cached address must trigger maintenance wake");
            exhausted(h, started); elapsedProgress(h);
            check(h.fetches > 1 && h.fetches < 1000, "waiting must poll cache without an unbounded busy loop");
            int fetches = h.fetches, boots = TermuxRunner.boots;
            Handler.advance(60000); Thread.drain();
            check(h.fetches == fetches && TermuxRunner.boots == boots,
                "maintenance wake without effect must stop after its bounded window");
        });
        test("missing cached address waits with elapsed progress then terminates without navigation", () -> {
            Harness h = new Harness(); h.nextUrl = null;
            long started = Clock.now; h.error(true); settle();
            check(TermuxRunner.boots > 0, "missing cached address must request maintenance wake");
            exhausted(h, started); elapsedProgress(h);
        });
        test("stale cache may rotate after sixty-five seconds and only the new address loads", () -> {
            Harness h = new Harness(); long started = Clock.now;
            h.cacheAt = now -> now - started < 65000 ? TOKEN_URL : FRESH_URL;
            h.error(true); navigate(h);
            check(h.web.navigations.equals(Collections.singletonList(FRESH_URL)),
                "wait must skip all stale cache reads and load only the rotated address");
            check(Clock.now - started >= 65000, "recovery must remain active beyond sixty seconds for a late new address");
            check(TermuxRunner.boots > 0, "cache rotation recovery must have requested maintenance wake");
            elapsedProgress(h); ownedScreen(h);
            h.pageStarted(FRESH_URL); h.finished(FRESH_URL);
            check(h.web.visibility == View.VISIBLE, "late rotated address should reveal after successful document finish");
        });
        test("alternating missing and stale cache is not mistaken for a usable address", () -> {
            Harness h = new Harness(); long started = Clock.now;
            h.cacheAt = now -> now - started >= 65000 ? FRESH_URL
                : ((now - started) / 2000 % 2 == 0 ? null : TOKEN_URL);
            h.error(true); navigate(h);
            check(h.web.navigations.equals(Collections.singletonList(FRESH_URL)),
                "null-to-old cache transition must not end quarantine early");
            check(Clock.now - started >= 65000, "mixed stale/missing cache must keep the recovery window open");
        });
        test("actual failed request URL is quarantined even when loaded URL differs", () -> {
            Harness h = new Harness(); h.nextUrl = FRESH_URL;
            long started = Clock.now;
            h.failAt(401, FRESH_URL); settle();
            exhausted(h, started);
            check(TermuxRunner.boots > 0, "actual failed request URL must cause maintenance wake");
        });
        test("same-origin redirect failure does not reload the previous token address", () -> {
            Harness h = new Harness(); h.nextUrl = TOKEN_URL;
            long started = Clock.now;
            h.failAt(403, "http://127.0.0.1:3080/login?token=fixture-redirect"); settle();
            exhausted(h, started);
        });
        test("missing request and WebView URL still quarantine the last attempted token address", () -> {
            Harness h = new Harness(); h.web.url = null; h.nextUrl = TOKEN_URL;
            long started = Clock.now;
            h.failAt(401, null); settle();
            exhausted(h, started);
            check(TermuxRunner.boots > 0, "missing callback URL must still request maintenance wake");
        });
        test("new address that never finishes loading times out within the original ninety-second window", () -> {
            Harness h = new Harness(); long started = Clock.now;
            h.error(true); navigate(h);
            check(h.web.navigations.equals(Collections.singletonList(FRESH_URL)), "fixture expected a new address to start loading");
            h.pageStarted(FRESH_URL); settle();
            check(Clock.now - started >= 60000 && Clock.now - started <= 90000,
                "loading a new address must not cancel or extend the ninety-second watchdog");
            check(h.retryBtn.visibility == View.VISIBLE, "hung new document must offer manual retry after timeout");
            check(Handler.pending.isEmpty() && Thread.workers.isEmpty(), "hung document timeout must cancel all recovery work");
            ownedScreen(h);
        });
        test("successively different failed addresses cannot restart the original ninety-second window", () -> {
            Harness h = new Harness(); long started = Clock.now;
            h.cacheAt = now -> FRESH_URL + "-attempt-" + (now - started);
            h.error(true);
            for (int i = 0; i < 32 && h.retryBtn.visibility != View.VISIBLE; i++) {
                int before = h.web.navigations.size(); navigate(h);
                if (h.web.navigations.size() == before) break;
                String failed = h.web.navigations.get(h.web.navigations.size() - 1);
                h.pageStarted(failed); h.failAt(401, failed); h.finished(failed); ownedScreen(h);
            }
            settle();
            check(h.web.navigations.size() > 2, "fixture expected several different recovery addresses to fail");
            check(Clock.now - started >= 60000 && Clock.now - started <= 90000,
                "a newly failed address must not grant another ninety-second window");
            check(h.retryBtn.visibility == View.VISIBLE && Handler.pending.isEmpty() && Thread.workers.isEmpty(),
                "successive failures must exhaust one shared window and stop delayed work");
            ownedScreen(h);
        });
        test("manual retry cannot load a previously failed cached address", () -> {
            Harness h = new Harness(); h.nextUrl = TOKEN_URL;
            h.error(true); settle();
            check(h.retryBtn.visibility == View.VISIBLE, "fixture expected first recovery window to expire");
            long manualStarted = Clock.now; h.clickRetry(); settle();
            check(h.web.navigations.isEmpty(), "manual retry must preserve quarantine of the known failed cached address");
            check(Clock.now - manualStarted >= 60000 && Clock.now - manualStarted <= 90000,
                "manual retry must get its own bounded wait without reloading the failed address");
            check(h.retryBtn.visibility == View.VISIBLE && Handler.pending.isEmpty(), "manual retry with unchanged cache must terminate");
            h.nextUrl = FRESH_URL; h.clickRetry(); navigate(h);
            check(h.web.navigations.equals(Collections.singletonList(FRESH_URL)), "manual retry may load a genuinely new address");
        });
        test("successful document finish cancels progress retries and watchdog permanently", () -> {
            Harness h = new Harness(); h.error(true); navigate(h);
            check(!Handler.pending.isEmpty(), "fixture expected a loading watchdog before document success");
            h.pageStarted(FRESH_URL); h.finished(FRESH_URL);
            check(Handler.pending.isEmpty() && Thread.workers.isEmpty(), "successful finish must cancel every delayed recovery callback");
            int fetches = h.fetches, boots = TermuxRunner.boots, updates = h.splashText.history.size();
            Handler.advance(180000); Thread.drain();
            check(h.fetches == fetches && TermuxRunner.boots == boots && h.splashText.history.size() == updates,
                "success must prevent later cache polling maintenance wake and progress updates");
            check(h.web.visibility == View.VISIBLE && h.splash.visibility == View.GONE && h.retryBtn.visibility == View.GONE,
                "old watchdog must not hide a successful document");
        });
        test("stale HTTP start and finish callbacks cannot reveal a failed document", () -> {
            Harness h = new Harness(); h.nextUrl = TOKEN_URL; h.error(true); settle();
            for (String stale : new String[]{TOKEN_URL, "http://127.0.0.1:3080/", "https://example.invalid/old"}) {
                h.pageStarted(stale); h.finished(stale); h.finished(h.web.getUrl()); ownedScreen(h);
            }
            check(Handler.pending.isEmpty() && Thread.workers.isEmpty(), "stale HTTP callbacks must not reopen expired recovery");
            h.nextUrl = FRESH_URL; h.clickRetry(); navigate(h);
            h.pageStarted(TOKEN_URL); h.finished(TOKEN_URL); ownedScreen(h);
            h.pageStarted(FRESH_URL); h.finished(FRESH_URL);
            check(h.web.visibility == View.VISIBLE, "only the newly loaded document may reveal after genuine success");
        });
        test("external main-frame failure preserves the valid DSH cached address", () -> {
            Harness h = new Harness(); h.nextUrl = TOKEN_URL;
            String external = "https://github.com/tianye1999/dsb/issues"; h.web.url = external;
            h.failAt(503, external); navigate(h);
            check(h.web.navigations.equals(Collections.singletonList(TOKEN_URL)),
                "different-origin failure must not quarantine the valid DSH address");
            h.pageStarted(TOKEN_URL); h.finished(TOKEN_URL);
            check(h.web.visibility == View.VISIBLE && Handler.pending.isEmpty(), "valid DSH cached document should recover normally");
        });
        for (int status : new int[]{401, 403, 404}) {
            test("external subresource HTTP " + status + " cannot invalidate the DSH session", () -> {
                Harness h = new Harness(); h.nextUrl = TOKEN_URL;
                h.client.onReceivedHttpError(h.web,
                    new WebResourceRequest(false, "https://example.invalid/api/asset"), new WebResourceResponse(status));
                settle();
                check(h.web.visibility == View.VISIBLE && h.splash.visibility == View.GONE,
                    "off-origin subresource auth failure must not replace the valid DSH document");
                check(h.fetches == 0 && TermuxRunner.boots == 0 && h.web.navigations.isEmpty(),
                    "off-origin subresource auth failure must not wake or quarantine the DSH session");
            });
        }
        for (int status : new int[]{401, 403, 404}) {
            test("same-origin API HTTP " + status + " still recovers an expired DSH session", () -> {
                Harness h = new Harness();
                h.client.onReceivedHttpError(h.web,
                    new WebResourceRequest(false, "http://127.0.0.1:3080/api/session"), new WebResourceResponse(status));
                ownedScreen(h); navigate(h);
                check(TermuxRunner.boots > 0 && h.web.navigations.contains(FRESH_URL),
                    "same-origin auth/API failure must still wake and recover using a rotated address");
            });
        }
        test("destroy cancels scheduled retry before any URL fetch", () -> {
            Harness h = new Harness(); h.error(true); h.onDestroy(); settle();
            check(h.fetches == 0, "destroyed activity must not fetch URL from delayed recovery");
            check(h.web.navigations.isEmpty(), "destroyed activity must not navigate");
            check(Handler.pending.isEmpty(), "destroy must remove pending retry");
        });
        test("destroyed activity ignores pending ensure worker completion", () -> {
            Harness h = new Harness(); h.error(true); Handler.advance(60000);
            check(!Thread.workers.isEmpty(), "fixture expected an asynchronous ensure worker");
            h.onDestroy(); Thread.drain();
            check(h.web.navigations.isEmpty(), "late worker must not navigate a destroyed activity");
            check(h.web.visibility == View.GONE, "late worker must not reveal native page");
        });
        test("destroy during cache wait cancels delayed work and ignores worker completion", () -> {
            Harness h = new Harness(); h.nextUrl = null; long started = Clock.now;
            boolean[] destroyedDuringWait = {false};
            int[] snapshot = new int[3];
            h.onFetch = () -> {
                if (!destroyedDuringWait[0] && Clock.now - started >= 10000) {
                    destroyedDuringWait[0] = true; h.onDestroy();
                    snapshot[0] = h.fetches; snapshot[1] = TermuxRunner.boots;
                    snapshot[2] = h.splashText.history.size();
                }
            };
            h.error(true); settle();
            check(destroyedDuringWait[0], "fixture expected destruction while waiting for a cached URL");
            Handler.advance(120000); Thread.drain();
            check(h.fetches == snapshot[0] && TermuxRunner.boots == snapshot[1],
                "destroy must stop subsequent cache polls and maintenance wake requests");
            check(h.splashText.history.size() == snapshot[2] && h.web.navigations.isEmpty(),
                "late worker/progress callbacks must not update destroyed activity UI");
            check(Handler.pending.isEmpty() && Thread.workers.isEmpty(), "destroy must remove every delayed recovery callback");
        });
        test("network error logging excludes URL and browser descriptions", () -> {
            Harness h = new Harness(); h.error(true);
            for (String entry : Log.entries)
                check(!entry.contains("fixture-secret") && !entry.contains("token="), "network error log leaked private URL");
        });
        test("cached URL retries every second and respect the recovery deadline", () -> {
            Harness h = new Harness(); h.nextUrl = null; h.error(true);
            for (int attempt = 0; attempt < 5; attempt++) {
                Handler.Pending retry = Handler.pending.stream()
                    .filter(p -> p.runnable == h.retryReensure).findFirst().orElseThrow();
                check(retry.due - Clock.now == 1000,
                    "cached URL retry " + attempt + " must wait exactly one second");
                Handler.advance(1000);
                Thread.drain();
            }
            Harness late = new Harness();
            late.recoveryStartedAt = Clock.now - (Harness.RECOVERY_WINDOW_MS - 375);
            late.maybeReensure();
            Handler.Pending finalRetry = Handler.pending.stream()
                .filter(p -> p.runnable == late.retryReensure).findFirst().orElseThrow();
            check(finalRetry.due - Clock.now == 375,
                "final cached URL retry must be clipped to the remaining recovery window");
        });
        System.out.println("WEBVIEW_RECOVERY_TESTS passed=" + passed + " failed=" + failed);
        if (failed != 0) throw new AssertionError("WebView recovery regressions: " + failed);
    }
}
