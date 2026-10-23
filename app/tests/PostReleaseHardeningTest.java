import java.io.*;
import java.nio.file.*;

public class PostReleaseHardeningTest {
    private File root;
    private File getExternalFilesDir(String ignored) { return root; }
    /* PRODUCTION_IO */
    /* PRODUCTION_CLEAN */

    interface Operation { void run() throws Exception; }
    static void check(boolean ok, String message) {
        if (!ok) throw new AssertionError(message);
    }
    static void rejected(Operation op) throws Exception {
        try { op.run(); } catch (IOException | IllegalArgumentException expected) { return; }
        throw new AssertionError("unsafe path accepted");
    }
    void files() throws Exception {
        Path base = Files.createTempDirectory("dsb-output-");
        try {
            root = Files.createDirectory(base.resolve("files")).toFile();
            File good = new File(root, "look-good_123.json");
            writeAtomically(good, "first");
            writeAtomically(good, "second");
            check(Files.readString(good.toPath()).equals("second"), "atomic replacement");
            File copy = new File(root, "look-good_123.jpg");
            copyFile(good, copy);
            check(Files.readString(copy.toPath()).equals("second"), "copy");
            Path outside = base.resolve("outside");
            Files.writeString(outside, "untouched");
            rejected(() -> writeAtomically(new File(root, "../outside"), "bad"));
            rejected(() -> writeAtomically(outside.toFile(), "bad"));
            rejected(() -> writeAtomically(new File("/"), "bad"));
            rejected(() -> writeAtomically(new File(root, "../files/look-back.json"), "bad"));
            Path sibling = Files.createDirectory(base.resolve("files-other"));
            rejected(() -> writeAtomically(sibling.resolve("bad.json").toFile(), "bad"));
            Path link = root.toPath().resolve("look-link.json");
            Files.createSymbolicLink(link, outside);
            rejected(() -> writeAtomically(link.toFile(), "bad"));
            rejected(() -> copyFile(link.toFile(), copy));
            rejected(() -> copyFile(good, link.toFile()));
            Path insideLink = root.toPath().resolve("look-inside.json");
            Files.createSymbolicLink(insideLink, good.toPath());
            rejected(() -> writeAtomically(insideLink.toFile(), "bad"));
            Path tmpLink = root.toPath().resolve("look-tmp.json.tmp");
            Files.createSymbolicLink(tmpLink, outside);
            rejected(() -> writeAtomically(new File(root, "look-tmp.json"), "bad"));
            Files.createSymbolicLink(root.toPath().resolve("escape"), base);
            rejected(() -> writeAtomically(new File(root, "escape/outside"), "bad"));
            check(Files.readString(outside).equals("untouched"), "outside file changed");
            check(!Files.exists(root.toPath().resolve("look-back.json")), "traversal created a file");
            for (String n : new String[]{null, "", "../outside", "/tmp/outside", "a/b", "a\\b", "x".repeat(65)})
                check(!validNonce(n), "unsafe nonce accepted");
            for (String n : new String[]{"123456789", "abc_DEF-123", "a".repeat(64)})
                check(validNonce(n), "valid nonce rejected");
            root = null;
            rejected(() -> writeAtomically(good, "bad"));
            System.out.println("OUTPUT_PATH_BOUNDARY_PASS (write/copy/traversal/absolute/sibling/symlink/temp/null)");
        } finally {
            try (var paths = Files.walk(base)) {
                for (Path p : paths.sorted(java.util.Comparator.reverseOrder()).toArray(Path[]::new)) Files.delete(p);
            }
        }
    }
    static void speech() {
        check(clean(null).equals(""), "null");
        check(clean("[label](https://example.test) and **bold**").equals("label and bold"), "markdown");
        check(clean("before ```code``` after").equals("before after"), "code fence");
        for (String s : new String[]{"[unfinished", "[](missing", "[outer [inner](url)",
                "[a]x[b](url)", "[a](u)[b](v)", "[a](u(v))"}) {
            String expected = s.replaceAll("\\[([^\\]]*)\\]\\([^)]*\\)", "$1");
            check(clean(s).equals(expected), "link compatibility: " + s);
        }
        java.util.Random random = new java.util.Random(7);
        String chars = "abc[]()$\\ ";
        for (int trial = 0; trial < 5000; trial++) {
            StringBuilder input = new StringBuilder();
            for (int i = 0; i < 100; i++) input.append(chars.charAt(random.nextInt(chars.length())));
            String s = input.toString();
            check(cleanLinks(s).equals(s.replaceAll("\\[([^\\]]*)\\]\\([^)]*\\)", "$1")), "link parity");
        }
        long start = System.nanoTime();
        for (String pattern : new String[]{"[\\", "[]("}) {
            String s = pattern.repeat(100000);
            check(clean(s).equals(s), "malformed link changed");
        }
        check(System.nanoTime() - start < 5_000_000_000L, "adversarial links too slow");
        System.out.println("TTS_LINEAR_LINK_CLEAN_PASS (compatibility + 200k/300k adversarial inputs)");
    }
    public static void main(String[] args) throws Exception {
        if (args.length == 0 || args[0].equals("files")) new PostReleaseHardeningTest().files();
        if (args.length == 0 || args[0].equals("tts")) speech();
    }
}
