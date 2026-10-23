package com.dsb.assistant;

import java.io.*;
import java.nio.file.Files;
import java.nio.file.StandardCopyOption;
import java.security.MessageDigest;
import java.util.Base64;
import java.util.Properties;

/** App-private spool, shared by the main and :web processes. No user-selected paths. */
public final class TrustedFiles {
    public static final int CHUNK = 12288, MAX_REQUEST = 40 * 1024 * 1024, MAX_REPLY = 4 * 1024 * 1024;
    public static final int READ_CHUNK = 64 * 1024, WRITE_CHUNK = 32 * 1024;
    private static File dir(File root, String id) throws IOException {
        if (id == null || !id.matches("[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}")) throw new IOException("request_id");
        return new File(root, id);
    }
    private static void save(File f, byte[] bytes) throws IOException {
        try (FileOutputStream s = new FileOutputStream(f)) { s.write(bytes); s.getFD().sync(); }
    }
    private static Properties meta(File root, String id, String nonce) throws IOException {
        File d = dir(root,id);
        Properties m = new Properties();
        try (FileInputStream in = new FileInputStream(new File(d,"meta"))) { m.load(in); }
        if (nonce == null || !nonce.matches("[0-9a-f]{64}") || !nonce.equals(m.getProperty("nonce"))
                || System.currentTimeMillis() >= Long.parseLong(m.getProperty("expires"))) throw new IOException("expired_or_unknown_request");
        return m;
    }
    public static void create(File root, String id, String nonce, byte[] request, int ttlMs) throws IOException {
        if (request.length > MAX_REQUEST || ttlMs < 1 || ttlMs > 960000 || !nonce.matches("[0-9a-f]{64}")) throw new IOException("request_limit");
        if (!root.isDirectory() && !root.mkdirs()) throw new IOException("spool_unavailable");
        File[] old = root.listFiles();
        if (old == null) throw new IOException("spool_unavailable");
        int live = 0;
        for (File f : old) {
            if (!f.isDirectory()) continue;
            try {
                Properties p = new Properties();
                try (FileInputStream s = new FileInputStream(new File(f,"meta"))) { p.load(s); }
                if (System.currentTimeMillis() >= Long.parseLong(p.getProperty("expires"))) cleanup(root,f.getName());
                else live++;
            } catch (Exception e) { live++; }
        }
        if (live >= 32) throw new IOException("spool_capacity");
        File d = dir(root,id);
        if (!d.mkdir()) throw new IOException("request_exists");
        try {
            save(new File(d,"request"),request);
            Properties m = new Properties();
            m.setProperty("nonce",nonce); m.setProperty("expires",Long.toString(System.currentTimeMillis()+ttlMs));
            ByteArrayOutputStream bytes = new ByteArrayOutputStream(); m.store(bytes,null);
            save(new File(d,"meta"),bytes.toByteArray());
        } catch (IOException e) { cleanup(root,id); throw e; }
    }
    public static String read(File root, String id, String nonce, int offset) throws IOException {
        return read(root, id, nonce, offset, CHUNK);
    }
    public static String read(File root, String id, String nonce, int offset, int length) throws IOException {
        if (length < 1 || length > READ_CHUNK) throw new IOException("read_limit");
        meta(root,id,nonce);
        try (RandomAccessFile f = new RandomAccessFile(new File(dir(root,id),"request"),"r")) {
            if (offset < 0 || offset > f.length()) throw new IOException("offset");
            byte[] b = new byte[(int)Math.min(length,f.length()-offset)]; f.seek(offset); f.readFully(b);
            return Base64.getEncoder().encodeToString(b);
        }
    }
    // Only CmdReceiver's main-process serialized onReceive calls this writer.
    public static synchronized void write(File root, String id, String nonce, int offset, int total, String digest, byte[] chunk) throws Exception {
        meta(root,id,nonce);
        if (offset < 0 || total < 1 || total > MAX_REPLY || chunk.length < 1 || chunk.length > WRITE_CHUNK
            || offset > total-chunk.length || digest == null || !digest.matches("[0-9a-f]{64}")) throw new IOException("reply_limit");
        File d=dir(root,id), contract=new File(d,"reply-meta"), ready=new File(d,"ready"), part=new File(d,"reply-part");
        String spec=total+":"+digest;
        if (contract.exists()) {
            if (!spec.equals(new String(Files.readAllBytes(contract.toPath()),"UTF-8"))) throw new IOException("reply_changed");
        } else save(contract,spec.getBytes("UTF-8"));
        File target = ready.exists() ? ready : part;
        try (RandomAccessFile f=new RandomAccessFile(target,"rw")) {
            if (offset > f.length()) throw new IOException("reply_gap");
            f.seek(offset);
            if (offset < f.length()) {
                if (offset+chunk.length>f.length()) throw new IOException("reply_overlap");
                byte[] prev=new byte[chunk.length]; f.readFully(prev);
                if (!MessageDigest.isEqual(prev,chunk)) throw new IOException("reply_changed");
            } else { if (ready.exists()) throw new IOException("reply_complete"); f.write(chunk); f.getFD().sync(); }
        }
        if (!ready.exists() && part.length()==total) {
            if (!sha256(Files.readAllBytes(part.toPath())).equals(digest)) throw new IOException("reply_digest");
            Files.move(part.toPath(),ready.toPath(),StandardCopyOption.ATOMIC_MOVE);
        }
    }
    public static byte[] result(File root,String id) throws IOException {
        File f=new File(dir(root,id),"ready"); return f.exists()?Files.readAllBytes(f.toPath()):null;
    }
    public static String sha256(byte[] data) {
        try { StringBuilder s=new StringBuilder(); for(byte b:MessageDigest.getInstance("SHA-256").digest(data)) s.append(String.format("%02x",b&255)); return s.toString(); }
        catch(Exception e) { throw new IllegalStateException(e); }
    }
    public static void cleanup(File root,String id) throws IOException {
        File d=dir(root,id);
        for(String name:new String[]{"meta","request","reply-meta","reply-part","ready"}) {
            File f=new File(d,name); if(f.exists()&&!f.delete()) throw new IOException("spool_cleanup");
        }
        if(d.exists()&&!d.delete()) throw new IOException("spool_cleanup");
    }
}
