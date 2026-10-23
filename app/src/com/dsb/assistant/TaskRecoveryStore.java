package com.dsb.assistant;

import android.util.AtomicFile;
import java.io.File;
import java.io.FileOutputStream;
import java.nio.charset.StandardCharsets;
import java.util.UUID;
import org.json.JSONObject;

/** Process-local serialization plus durable, identity-scoped recovery markers. */
final class TaskRecoveryStore {
    private static final Object LOCK = new Object();
    private final AtomicFile file;

    static final class State {
        final boolean pending;
        final String id, bridge;
        State(boolean pending, String id, String bridge) {
            this.pending = pending; this.id = id; this.bridge = bridge;
        }
    }

    TaskRecoveryStore(File path) { file = new AtomicFile(path); }

    State read() { synchronized (LOCK) { return readLocked(); } }

    private State readLocked() {
        File base = file.getBaseFile();
        if (!base.exists() && !new File(base + ".bak").exists() && !new File(base + ".new").exists())
            return new State(false, "", "");
        try {
            byte[] bytes = file.readFully();
            if (bytes.length > 16384) throw new IllegalStateException("oversized recovery record");
            JSONObject json = new JSONObject(new String(bytes, StandardCharsets.UTF_8));
            if (!(json.opt("version") instanceof Integer) || json.getInt("version") != 1
                    || !(json.opt("pending") instanceof Boolean)
                    || !(json.opt("id") instanceof String) || !(json.opt("bridge") instanceof String))
                throw new IllegalStateException("invalid recovery record");
            boolean pending = json.getBoolean("pending");
            String id = json.getString("id"), bridge = json.getString("bridge");
            if (pending ? (!id.matches("[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}") || bridge.isEmpty())
                        : (!id.isEmpty() || !bridge.isEmpty()))
                throw new IllegalStateException("invalid recovery identity");
            return new State(pending, id, bridge);
        } catch (Exception error) {
            return new State(true, "", "");
        }
    }

    State begin(String bridge) {
        synchronized (LOCK) {
            if (readLocked().pending || bridge == null || bridge.isEmpty()) return null;
            State next = new State(true, UUID.randomUUID().toString(), bridge);
            return writeLocked(next) ? next : null;
        }
    }

    boolean clear(String id) {
        synchronized (LOCK) {
            State current = readLocked();
            if (id == null || id.isEmpty() || !current.pending || !id.equals(current.id)) return false;
            return writeLocked(new State(false, "", ""));
        }
    }

    private boolean writeLocked(State next) {
        FileOutputStream stream = null;
        try {
            byte[] bytes = new JSONObject().put("version", 1).put("pending", next.pending)
                .put("id", next.id).put("bridge", next.bridge).toString().getBytes(StandardCharsets.UTF_8);
            stream = file.startWrite();
            stream.write(bytes);
            stream.flush();
            stream.getFD().sync();
            file.finishWrite(stream);
            stream = null;
            State saved = readLocked();
            return saved.pending == next.pending && saved.id.equals(next.id) && saved.bridge.equals(next.bridge);
        } catch (Exception error) {
            if (stream != null) file.failWrite(stream);
            return false;
        }
    }
}
