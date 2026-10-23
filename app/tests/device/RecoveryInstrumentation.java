package com.dsb.assistant.stoptests;

import android.app.Instrumentation;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.os.Bundle;
import java.io.*;
import java.lang.reflect.*;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.TimeUnit;
import org.json.JSONArray;
import org.json.JSONObject;

/** Owned bridge only. The harness keeps the device alive across instrumentation exits. */
public final class RecoveryInstrumentation extends Instrumentation {
    private String runId, phase, fixture;
    private Context context;
    private Class<?> serviceClass, storeClass;
    private Object service;
    private final JSONArray checks = new JSONArray();
    private final JSONObject report = new JSONObject();

    @Override public void onCreate(Bundle args) {
        super.onCreate(args);
        runId=args.getString("runId", ""); phase=args.getString("phase", "");
        fixture=args.getString("fixture", ""); start();
    }
    private void check(boolean value, String message) {
        if (!value) throw new AssertionError(message);
        checks.put(message);
    }
    interface Condition { boolean get() throws Exception; }
    private void until(Condition condition) throws Exception {
        long end=System.nanoTime()+TimeUnit.SECONDS.toNanos(25);
        while(!condition.get() && System.nanoTime()<end) Thread.sleep(20);
        if(!condition.get()) throw new AssertionError("condition timeout");
    }
    private Object invoke(Object target, String name, Class<?>[] types, Object... args) throws Exception {
        Method m=target.getClass().getDeclaredMethod(name,types); m.setAccessible(true);
        return m.invoke(target,args);
    }
    private Object value(Object target, String field) throws Exception {
        Field f=target.getClass().getDeclaredField(field); f.setAccessible(true); return f.get(target);
    }
    private boolean flag(String name) throws Exception { return (Boolean)value(service,name); }
    private Object store(File file) throws Exception {
        Constructor<?> ctor=storeClass.getDeclaredConstructor(File.class); ctor.setAccessible(true);
        return ctor.newInstance(file);
    }
    private Object read(Object s) throws Exception { return invoke(s,"read",new Class<?>[0]); }
    private boolean pending(Object s) throws Exception { return (Boolean)value(read(s),"pending"); }
    private void call(String name, String arg) throws Exception {
        final Throwable[] error=new Throwable[1];
        runOnMainSync(() -> { try { invoke(service,name,arg==null?new Class<?>[0]:new Class<?>[]{String.class},
            arg==null?new Object[0]:new Object[]{arg}); } catch(Throwable t) { error[0]=t; } });
        if(error[0]!=null) throw new AssertionError(error[0]);
    }
    private void idle() throws Exception {
        until(() -> !flag("taskWorkerRunning") && !flag("agentRunning")
            && !flag("cancelInFlight") && !flag("cancelUnconfirmed"));
    }
    private JSONObject counts() throws Exception {
        HttpURLConnection c=(HttpURLConnection)new URL(fixture+"/state").openConnection();
        c.setConnectTimeout(2000); c.setReadTimeout(2000); c.setInstanceFollowRedirects(false);
        try(InputStream in=c.getInputStream(); ByteArrayOutputStream out=new ByteArrayOutputStream()) {
            byte[] buffer=new byte[1024]; int n; while((n=in.read(buffer))!=-1) out.write(buffer,0,n);
            return new JSONObject(out.toString("UTF-8"));
        } finally { c.disconnect(); }
    }
    private void contractTests() throws Exception {
        serviceClass=Class.forName("com.dsb.assistant.OverlayService",true,context.getClassLoader());
        runOnMainSync(() -> context.startForegroundService(new Intent().setClassName(context,serviceClass.getName())));
        Field instance=serviceClass.getDeclaredField("INSTANCE"); instance.setAccessible(true);
        until(() -> instance.get(null)!=null); service=instance.get(null);
        Object s=store(new File(context.getFilesDir(),"task-recovery.json"));
        SharedPreferences prefs=context.getSharedPreferences("dsh",Context.MODE_PRIVATE);
        check(!pending(s) && !flag("taskWorkerRunning") && !flag("cancelInFlight") && !flag("cancelUnconfirmed"),
            "contract fixture does not take over existing task");
        boolean had=prefs.contains("bridge"); String original=prefs.getString("bridge","");
        if(!prefs.edit().putString("bridge",fixture).commit()) throw new IOException("fixture setting failed");
        JSONArray cases=new JSONArray(); report.put("cases",cases);
        try {
            for(String scenario:new String[]{"completed","failed","refused","stopped","unknown"}) {
                int before=counts().getInt("doCount");
                call("handleCommand","CONTRACT_"+scenario);
                until(() -> counts().getInt("doCount")==before+1);
                String id=(String)value(service,"taskRecoveryId");
                if(scenario.equals("stopped")) {
                    until(() -> counts().optBoolean("runnerStarted",false));
                    call("handleCommand","停止"); idle();
                } else {
                    until(() -> !flag("taskWorkerRunning"));
                    if(!scenario.equals("unknown")) idle();
                }
                final String[] status=new String[1];
                runOnMainSync(() -> { try { status[0]=(String)value(service,"lastStatusText"); }
                    catch(Exception e) { throw new RuntimeException(e); } });
                if(scenario.equals("unknown")) {
                    check(pending(s) && flag("cancelUnconfirmed"),"empty transport reply preserves unknown barrier");
                    call("handleCommand","CONTRACT_must_not_replay");
                    check(counts().getInt("doCount")==before+1 && !flag("taskWorkerRunning"),"unknown refuses new main-entry work");
                    call("handleCommand","停止"); idle();
                } else if(scenario.equals("stopped")) {
                    check(status[0].startsWith("已停止") && !pending(s),"main-entry stop confirms actual bridge cancellation");
                } else {
                    check(status[0].startsWith("已回复，结果未核验") && !pending(s),scenario+" runner result never becomes verified App goal");
                }
                cases.put(new JSONObject().put("scenario",scenario).put("taskId",id).put("status",status[0]));
            }
        } finally {
            if(pending(s)) {
                if(!fixture.equals(value(read(s),"bridge"))) throw new AssertionError("refuse unrelated pending cleanup");
                call("handleCommand","停止"); idle();
            }
            SharedPreferences.Editor restore=prefs.edit();
            if(had) restore.putString("bridge",original); else restore.remove("bridge");
            if(!restore.commit()) throw new IOException("contract fixture restore failed");
        }
        check(!pending(s),"contract fixture leaves no pending task");
    }
    private void storeTests() throws Exception {
        File root=new File(context.getCacheDir(),"recovery-test-"+runId);
        if(!root.mkdir()) throw new IOException("test directory exists/unavailable");
        File f=new File(root,"state.json"); Object s=store(f);
        check(!pending(s),"missing owned state is idle");
        Object first=invoke(s,"begin",new Class<?>[]{String.class},fixture);
        check(first!=null && pending(store(f)),"pending survives new store instance");
        String id=(String)value(first,"id");
        check(fixture.equals(value(read(store(f)),"bridge")),"original bridge survives readback");
        check(invoke(s,"begin",new Class<?>[]{String.class},"changed")==null,"pending blocks a second admission");
        check(!(Boolean)invoke(s,"clear",new Class<?>[]{String.class},"wrong"),"wrong identity cannot clear");
        check((Boolean)invoke(s,"clear",new Class<?>[]{String.class},id),"matching identity clears");
        Object second=invoke(s,"begin",new Class<?>[]{String.class},fixture);
        check(second!=null && !(Boolean)invoke(s,"clear",new Class<?>[]{String.class},id),"stale identity cannot clear new task");
        try(FileOutputStream out=new FileOutputStream(f)) { out.write("broken".getBytes(StandardCharsets.UTF_8)); }
        check(pending(store(f)) && invoke(s,"begin",new Class<?>[]{String.class},fixture)==null,"corrupt owned record fails closed");
        File blocker=new File(root,"blocked");
        try(FileOutputStream out=new FileOutputStream(blocker)) { out.write(1); }
        check(invoke(store(new File(blocker,"state.json")),"begin",new Class<?>[]{String.class},fixture)==null,
            "unwritable owned path cannot admit");
        for(File child:root.listFiles()) if(!child.delete()) throw new IOException("owned test cleanup failed");
        if(!root.delete()) throw new IOException("owned test directory cleanup failed");
    }
    private void lifecycleTests() throws Exception {
        serviceClass=Class.forName("com.dsb.assistant.OverlayService",true,context.getClassLoader());
        runOnMainSync(() -> context.startForegroundService(new Intent().setClassName(context,serviceClass.getName())));
        Field instance=serviceClass.getDeclaredField("INSTANCE"); instance.setAccessible(true);
        until(() -> instance.get(null)!=null); service=instance.get(null);
        Object s=store(new File(context.getFilesDir(),"task-recovery.json"));
        SharedPreferences p=context.getSharedPreferences("dsh",Context.MODE_PRIVATE);
        if(phase.equals("prepare") || phase.equals("prepare_bridge")) {
            check(!pending(s) && !flag("taskWorkerRunning") && !flag("agentRunning")
                && !flag("cancelInFlight") && !flag("cancelUnconfirmed"),"no preexisting task taken over");
            check(!p.contains("recoverytests_run"),"no preexisting recovery fixture overwritten");
            if(!p.edit().putString("recoverytests_run",runId).putString("recoverytests_fixture",fixture)
                .putBoolean("recoverytests_had_bridge",p.contains("bridge"))
                .putString("recoverytests_bridge",p.getString("bridge",""))
                .putString("bridge",fixture).commit()) throw new IOException("fixture preferences not saved");
            call("runGoal",phase.equals("prepare_bridge")?"hang":"TEST owned restart marker; no device actions");
            until(() -> counts().getInt("doCount")==1 && pending(s));
            check(flag("taskWorkerRunning"),"owned request active before process exit");
            report.put("taskId",value(read(s),"id"));
            return; // Instrumentation exit ends this target process; external fixture survives.
        }
        if(!p.contains("recoverytests_run") && (phase.equals("cleanup") || phase.equals("cleanup_unknown"))) return;
        check(runId.equals(p.getString("recoverytests_run","")) && fixture.equals(p.getString("recoverytests_fixture","")),
            "fixture ownership matches before recovery");
        if(phase.equals("verify_unknown")) {
            String id=(String)value(read(s),"id"); report.put("taskId",id);
            check(pending(s) && flag("cancelUnconfirmed"),"fresh App preserves unknown task");
            check(fixture.equals(value(service,"taskBridgeBase")),"unknown retains original bridge");
            call("stopTask",null); until(() -> !flag("cancelInFlight"));
            check(pending(s) && flag("cancelUnconfirmed") && !flag("taskWorkerRunning"),"real restarted bridge cannot unlock App");
            JSONObject state=counts();
            check(state.getInt("doCount")==1 && state.getInt("cancelCount")>=2 && state.getBoolean("workersAlive"),"old fake worker survives both actual cancellation requests");
            check(id.equals(state.getString("taskId")) && id.equals(value(read(s),"id")),"end-to-end original task identity preserved");
            call("runGoal","must-not-run");
            check(counts().getInt("doCount")==1 && !flag("taskWorkerRunning"),"unknown blocks new task without replay");
            return;
        }
        if(phase.equals("cleanup_unknown")) {
            JSONObject state=counts();
            check(runId.equals(state.getString("runId")) && !state.getBoolean("workersAlive")
                && state.getBoolean("cleanupVerified"),"harness proved only owned test execution stopped");
            if(pending(s)) {
                String id=(String)value(read(s),"id");
                check(fixture.equals(value(read(s),"bridge")) && id.equals(state.getString("taskId"))
                    && (Boolean)invoke(s,"clear",new Class<?>[]{String.class},id),"clear only verified test-owned pending identity");
            }
            call("restoreTaskRecovery",null);
        }
        if(phase.equals("verify")) {
            check(pending(s) && flag("cancelUnconfirmed"),"fresh process restores pending admission barrier");
            report.put("taskId",value(read(s),"id"));
            check(fixture.equals(value(service,"taskBridgeBase")),"fresh process restores original bridge");
            if(!p.edit().putString("bridge","http://127.0.0.1:1").commit()) throw new IOException("decoy setting failed");
            call("runGoal","TEST blocked task must not replay");
            check(counts().getInt("doCount")==1 && !flag("taskWorkerRunning"),"restart and changed settings do not replay");
        }
        if(pending(s)) {
            check(fixture.equals(value(read(s),"bridge")),"only the owned fixture may be cancelled");
            call("stopTask",null); idle();
        }
        check(!pending(s),"confirmed stop clears durable pending marker");
        if(phase.equals("verify")) {
            check(counts().getInt("cancelCount")>=2 && counts().getInt("doCount")==1,"original bridge cancelled without replay");
            if(!p.edit().putString("bridge",fixture).commit()) throw new IOException("fixture setting failed");
            call("runGoal","TEST explicit fresh task after confirmed recovery"); idle();
            check(counts().getInt("doCount")==2 && !pending(s),"explicit fresh task admitted only after confirmed recovery");
        }
        SharedPreferences.Editor restore=p.edit();
        if(p.getBoolean("recoverytests_had_bridge",false)) restore.putString("bridge",p.getString("recoverytests_bridge",""));
        else restore.remove("bridge");
        if(!restore.remove("recoverytests_run").remove("recoverytests_fixture")
            .remove("recoverytests_had_bridge").remove("recoverytests_bridge").commit())
            throw new IOException("original settings restoration failed");
    }
    @Override public void onStart() {
        Bundle out=new Bundle(); boolean success=false;
        try {
            if(!runId.matches("[0-9a-f-]{36}") || !fixture.matches("http://127\\.0\\.0\\.1:[0-9]+")
                || !(phase.equals("store") || phase.equals("prepare") || phase.equals("verify") || phase.equals("cleanup")
                    || phase.equals("preflight") || phase.equals("prepare_bridge") || phase.equals("verify_unknown") || phase.equals("cleanup_unknown") || phase.equals("contract")))
                throw new IllegalArgumentException("invalid fixture arguments");
            context=getTargetContext();
            storeClass=Class.forName("com.dsb.assistant.TaskRecoveryStore",true,context.getClassLoader());
            if(phase.equals("preflight")) check(!pending(store(new File(context.getFilesDir(),"task-recovery.json"))),"no preexisting pending App task");
            else if(phase.equals("contract")) contractTests();
            else if(phase.equals("store")) storeTests(); else lifecycleTests();
            success=true;
        } catch(Throwable error) { out.putString("error",error.toString()); }
        try {
            report.put("runId",runId).put("phase",phase).put("ok",success).put("checks",checks)
                .put("pid",android.os.Process.myPid());
            try(FileOutputStream f=new FileOutputStream(new File(context.getExternalFilesDir(null),"recovery-device-test.json"))) {
                f.write(report.toString().getBytes(StandardCharsets.UTF_8));
            }
        } catch(Throwable error) { success=false; out.putString("reportError",error.toString()); }
        out.putString("stream","RECOVERY_TEST_FINISH:"+runId+":"+phase+":"+(success?"PASS":"FAIL")+"\n");
        finish(success?-1:0,out);
    }
}
