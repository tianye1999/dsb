import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import {spawnSync} from 'node:child_process';
const app=path.resolve(path.dirname(new URL(import.meta.url).pathname),'..');
const dir=fs.mkdtempSync('/tmp/dsb-recovery-');
const files={
  'android/Manifest.java':'package android; public class Manifest { public static class permission { public static final String WRITE_SECURE_SETTINGS="write"; }}',
  'android/content/pm/PackageManager.java':'package android.content.pm; public class PackageManager { public static final int PERMISSION_GRANTED=0; }',
  'android/content/Context.java':`package android.content; public class Context {
    public boolean granted; public int reads,writes; public String list; public int enabled;
    public int checkSelfPermission(String p){return granted?0:-1;} public Context getContentResolver(){return this;}
    public String getPackageName(){return "com.dsb.assistant";}
  }`,
  'android/provider/Settings.java':`package android.provider; import android.content.Context; public class Settings { public static class Secure {
    public static final String ENABLED_ACCESSIBILITY_SERVICES="list",ACCESSIBILITY_ENABLED="on";
    public static String getString(Context c,String k){c.reads++;return c.list;}
    public static int getInt(Context c,String k,int d){c.reads++;return c.enabled;}
    public static boolean putString(Context c,String k,String v){c.writes++;c.list=v;return true;}
    public static boolean putInt(Context c,String k,int v){c.writes++;c.enabled=v;return true;}
  }}`,
  'android/os/SystemClock.java':'package android.os; public class SystemClock { public static long elapsedRealtime(){return System.nanoTime()/1000000;} }',
  'android/util/Log.java':'package android.util; public class Log { public static int i(String t,String s){return 0;} }',
  'com/dsb/assistant/AgentAccessibilityService.java':'package com.dsb.assistant; public class AgentAccessibilityService { public static Object INSTANCE; }',
  'com/dsb/assistant/AccessibilityRecovery.java':fs.existsSync(app+'/src/com/dsb/assistant/AccessibilityRecovery.java')?fs.readFileSync(app+'/src/com/dsb/assistant/AccessibilityRecovery.java','utf8'):'',
  'com/dsb/assistant/RecoveryTest.java':`package com.dsb.assistant; import android.content.Context;
    public class RecoveryTest {
      static int n; static void check(boolean b){if(!b)throw new AssertionError();n++;}
      public static void main(String[] a)throws Exception{
        Context c=new Context();c.list="third/.One";
        check(!AccessibilityRecovery.enable(c)); check(c.writes==0&&c.reads==0);
        check(!AccessibilityRecovery.await(c,0,true)); check(c.writes==0);
        c.granted=true; check(AccessibilityRecovery.enable(c));
        String own="com.dsb.assistant/com.dsb.assistant.AgentAccessibilityService";
        check(c.list.equals("third/.One:"+own)&&c.enabled==1);
        int before=c.writes;check(AccessibilityRecovery.enable(c));check(c.writes==before);
        c.list="third/.One:com.dsb.assistant/.AgentAccessibilityService:other/.Two";c.enabled=0;
        check(AccessibilityRecovery.enable(c));check(c.list.equals("third/.One:com.dsb.assistant/.AgentAccessibilityService:other/.Two"));
        check(!AccessibilityRecovery.await(c,0,true));check(c.list.equals("third/.One:other/.Two:"+own)&&c.enabled==1);
        c.granted=false;AgentAccessibilityService.INSTANCE=new Object();
        check(AccessibilityRecovery.await(c,0,true));
        System.out.println("OPTIONAL_ACCESSIBILITY_RECOVERY_PASS checks="+n);
      }
    }`
};
try{
  for(const [name,code] of Object.entries(files)){const f=dir+'/'+name;fs.mkdirSync(path.dirname(f),{recursive:true});fs.writeFileSync(f,code);}
  for(const [cmd,args] of [['javac',['-encoding','UTF-8','-d',dir,...Object.keys(files).map(f=>dir+'/'+f)]],['java',['-cp',dir,'com.dsb.assistant.RecoveryTest']]]){
    const r=spawnSync(cmd,args,{encoding:'utf8',timeout:30000});assert.equal(r.status,0,r.stdout+r.stderr);process.stdout.write(r.stdout);
  }
}finally{fs.rmSync(dir,{recursive:true,force:true});}
