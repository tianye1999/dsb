package com.dsb.assistant;

import java.io.*;
import java.lang.reflect.*;
import java.nio.file.*;
import java.util.*;

public final class TrustedFilesTest {
    private static final String NONCE = String.join("", Collections.nCopies(64, "a"));
    private static int checks;

    interface Test { void run() throws Exception; }
    interface SpoolTest { void run(File root) throws Exception; }

    private static void check(boolean condition, String label) {
        if (!condition) throw new AssertionError(label);
        checks++;
    }
    private static void rejected(Test operation, String label) throws Exception {
        try { operation.run(); } catch (IOException expected) { checks++; return; }
        throw new AssertionError(label + " accepted");
    }
    private static void spool(SpoolTest test) throws Exception {
        Path root = Files.createTempDirectory("dsb-trusted-files-case-");
        try { test.run(root.toFile()); }
        finally {
            try (java.util.stream.Stream<Path> files = Files.walk(root)) {
                for (Path file : (Iterable<Path>) files.sorted(Comparator.reverseOrder())::iterator) Files.delete(file);
            }
        }
    }
    private static String create(File root, byte[] request) throws Exception {
        String id = UUID.randomUUID().toString();
        TrustedFiles.create(root, id, NONCE, request, 30000);
        return id;
    }
    private static byte[] pattern(int size) {
        byte[] data = new byte[size];
        for (int i = 0; i < size; i++) data[i] = (byte) (i * 37 + 13);
        return data;
    }
    private static byte[] read(File root, String id, String nonce, int offset, int length) throws Exception {
        Method method;
        try { method = TrustedFiles.class.getMethod("read", File.class, String.class, String.class, int.class, int.class); }
        catch (NoSuchMethodException missing) { throw new AssertionError("bounded directional READ API is absent", missing); }
        try { return Base64.getDecoder().decode((String) method.invoke(null, root, id, nonce, offset, length)); }
        catch (InvocationTargetException error) {
            Throwable cause = error.getCause();
            if (cause instanceof Exception) throw (Exception) cause;
            throw error;
        }
    }
    private static void expire(File root, String id) throws Exception {
        File file = new File(new File(root, id), "meta");
        Properties meta = new Properties();
        try (InputStream stream = new FileInputStream(file)) { meta.load(stream); }
        meta.setProperty("expires", "1");
        try (OutputStream stream = new FileOutputStream(file)) { meta.store(stream, null); }
    }
    private static void write(File root, String id, byte[] reply) throws Exception {
        String digest = TrustedFiles.sha256(reply);
        for (int offset = 0; offset < reply.length; offset += 32768) {
            TrustedFiles.write(root, id, NONCE, offset, reply.length, digest,
                    Arrays.copyOfRange(reply, offset, Math.min(offset + 32768, reply.length)));
        }
    }

    public static void main(String[] args) throws Exception {
        check(TrustedFiles.CHUNK == 12288, "legacy default remains 12 KiB");
        check(TrustedFiles.MAX_REQUEST == 40 * 1024 * 1024 && TrustedFiles.MAX_REPLY == 4 * 1024 * 1024, "size caps unchanged");
        spool(root -> {
            String expired = create(root, new byte[]{1});
            String live = create(root, new byte[]{2});
            expire(root, expired);
            TrustedFiles.pruneExpired(root);
            check(!new File(root, expired).exists(), "startup prune removes expired spool");
            check(new File(root, live).exists(), "startup prune preserves live spool");
        });
        spool(root -> {
            byte[] data = pattern(65536 + 97);
            String id = create(root, data);
            check(Arrays.equals(Base64.getDecoder().decode(TrustedFiles.read(root, id, NONCE, 0)), Arrays.copyOf(data, 12288)), "old READ default");
            check(Arrays.equals(read(root, id, NONCE, 0, 65536), Arrays.copyOf(data, 65536)), "64 KiB READ exact bytes");
            check(Arrays.equals(read(root, id, NONCE, 65536, 65536), Arrays.copyOfRange(data, 65536, data.length)), "READ final partial");
            check(read(root, id, NONCE, data.length, 1).length == 0, "live EOF probe empty");
            check(read(root, id, NONCE, 13, 1)[0] == data[13], "one-byte READ");
            rejected(() -> read(root, id, NONCE, 0, 0), "zero length");
            rejected(() -> read(root, id, NONCE, 0, -1), "negative length");
            rejected(() -> read(root, id, NONCE, 0, 65537), "overlarge length");
            rejected(() -> read(root, id, NONCE, -1, 1), "negative offset");
            rejected(() -> read(root, id, NONCE, data.length + 1, 1), "past EOF");
            rejected(() -> read(root, id, "b" + NONCE.substring(1), 0, 1), "wrong nonce");
            rejected(() -> read(root, id, "bad", 0, 1), "malformed nonce");
            expire(root, id);
            rejected(() -> read(root, id, NONCE, data.length, 1), "expired EOF probe");
            rejected(() -> TrustedFiles.write(root, id, NONCE, 0, 1, TrustedFiles.sha256(new byte[]{1}), new byte[]{1}), "expired WRITE");
        });
        spool(root -> {
            String id = create(root, new byte[]{1});
            byte[] reply = pattern(32768 * 2 + 19);
            check(TrustedFiles.result(root, id) == null, "unfinished reply absent");
            String digest = TrustedFiles.sha256(reply);
            byte[] first = Arrays.copyOf(reply, 32768);
            TrustedFiles.write(root, id, NONCE, 0, reply.length, digest, first);
            check(TrustedFiles.result(root, id) == null, "partial reply not published");
            TrustedFiles.write(root, id, NONCE, 0, reply.length, digest, first);
            rejected(() -> TrustedFiles.write(root, id, NONCE, 32768 * 2, reply.length, digest, new byte[]{1}), "gap");
            byte[] changed = first.clone(); changed[0] ^= 1;
            rejected(() -> TrustedFiles.write(root, id, NONCE, 0, reply.length, digest, changed), "changed replay");
            rejected(() -> TrustedFiles.write(root, id, NONCE, 32768, reply.length, String.join("", Collections.nCopies(64, "0")), new byte[]{1}), "changed digest contract");
            rejected(() -> TrustedFiles.write(root, id, NONCE, 32768, reply.length + 1, digest, new byte[]{1}), "changed total contract");
            for (int offset = 32768; offset < reply.length; offset += 32768) {
                TrustedFiles.write(root, id, NONCE, offset, reply.length, digest,
                        Arrays.copyOfRange(reply, offset, Math.min(offset + 32768, reply.length)));
            }
            check(Arrays.equals(TrustedFiles.result(root, id), reply), "32 KiB chunks complete exact reply");
            TrustedFiles.write(root, id, NONCE, 0, reply.length, digest, first);
            check(Arrays.equals(TrustedFiles.result(root, id), reply), "complete replay idempotent");
            rejected(() -> TrustedFiles.write(root, id, NONCE, 0, reply.length, digest, new byte[32769]), "overlarge WRITE chunk");
            rejected(() -> TrustedFiles.write(root, id, NONCE, 0, reply.length, digest, new byte[0]), "empty WRITE chunk");
            rejected(() -> TrustedFiles.write(root, id, "b" + NONCE.substring(1), 0, reply.length, digest, first), "WRITE wrong nonce");
        });
        spool(root -> {
            String id = create(root, new byte[]{1});
            byte[] reply = pattern(TrustedFiles.MAX_REPLY);
            write(root, id, reply);
            check(Arrays.equals(TrustedFiles.result(root, id), reply), "4 MiB reply cap inclusive");
            String other = create(root, new byte[]{2});
            rejected(() -> TrustedFiles.write(root, other, NONCE, 0, TrustedFiles.MAX_REPLY + 1, TrustedFiles.sha256(new byte[]{1}), new byte[]{1}), "reply above 4 MiB");
            rejected(() -> TrustedFiles.write(root, other, NONCE, 0, 1, String.join("", Collections.nCopies(64, "0")), new byte[]{1}), "reply digest mismatch");
            check(TrustedFiles.result(root, other) == null, "bad digest never published");
        });
        spool(root -> {
            rejected(() -> TrustedFiles.create(root, UUID.randomUUID().toString(), NONCE, new byte[]{1}, 0), "zero TTL");
            rejected(() -> TrustedFiles.create(root, UUID.randomUUID().toString(), NONCE, new byte[]{1}, 960001), "TTL above bound");
            rejected(() -> TrustedFiles.create(root, UUID.randomUUID().toString(), "bad", new byte[]{1}, 1), "invalid creation nonce");
            rejected(() -> TrustedFiles.create(root, "../escape", NONCE, new byte[]{1}, 1000), "invalid request id");
            rejected(() -> TrustedFiles.create(root, UUID.randomUUID().toString(), NONCE, new byte[TrustedFiles.MAX_REQUEST + 1], 1000), "request above 40 MiB");
            for (int index = 0; index < 32; index++) create(root, new byte[]{1});
            rejected(() -> create(root, new byte[]{1}), "32 live request limit");
        });
        System.out.println("TRUSTED_FILES_PASS checks=" + checks);
    }
}
