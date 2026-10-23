import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { test } from 'node:test';

const app = path.resolve(path.dirname(new URL(import.meta.url).pathname), '..');
const source = name => fs.readFileSync(path.join(app, 'src/com/dsb/assistant', name + '.java'), 'utf8');

test('public manifest has one real accessibility service and no installer authority', () => {
  const result = spawnSync('python3', ['-c', `
import xml.etree.ElementTree as E
r = E.parse(${JSON.stringify(path.join(app, 'AndroidManifest.xml'))}).getroot()
a = '{http://schemas.android.com/apk/res/android}'
services = [s for s in r.findall('application/service') if s.get(a+'permission') == 'android.permission.BIND_ACCESSIBILITY_SERVICE']
assert [s.get(a+'name') for s in services] == ['.AgentAccessibilityService']
assert [x.get(a+'name') for x in r.findall('application/receiver')] == ['.BootReceiver', '.CmdReceiver']
assert len(r.findall('application/activity')) == 1 and len(r.findall('application/service')) == 3
assert not r.findall('application/provider')
assert r.find("application/receiver[@"+a+"name='.BootReceiver']").get(a+'exported') == 'false'
permissions = [p.get(a+'name') for p in r.findall('uses-permission')]
assert not any('INSTALL_PACKAGES' in p or 'UPDATE_PACKAGES' in p for p in permissions)
assert permissions.count('android.permission.WRITE_SECURE_SETTINGS') == 1
actions = [x.get(a+'name') for x in r.findall("application/receiver[@"+a+"name='.BootReceiver']/intent-filter/action")]
assert actions == ['android.intent.action.BOOT_COMPLETED', 'android.intent.action.MY_PACKAGE_REPLACED']
`], { encoding: 'utf8' });
  assert.equal(result.status, 0, result.stderr);
});

test('recovery is permission gated, setup grants but never writes settings, and logs stay redacted', () => {
  const overlay = source('OverlayService');
  const accessibility = source('AgentAccessibilityService');
  assert.doesNotMatch(overlay, /Settings\.Secure\.put(?:String|Int)/);
  assert.match(overlay, /ensureAccessibility\(this\)/);
  assert.match(overlay, /ensureAccessibility\(25000\)/);
  const recovery = source('AccessibilityRecovery');
  assert.match(recovery, /checkSelfPermission\(Manifest\.permission\.WRITE_SECURE_SETTINGS\)/);
  const setup = fs.readFileSync(path.join(app, 'setup-adb.sh'), 'utf8');
  assert.match(setup, /pm grant.*WRITE_SECURE_SETTINGS/);
  assert.doesNotMatch(setup, /settings put/);
  for(const file of ['build-phone.sh','tests/build-instrumentation.sh','tests/build-stop-candidate.sh']){
    const build=fs.readFileSync(path.join(app,file),'utf8');
    assert.match(build,/KEYSTORE=\$\{DSB_KEYSTORE:\?/);
    assert.match(build,/ANDROID_HOME=\$\{ANDROID_HOME:-\$\{ANDROID_SDK_ROOT:-/);
    assert.match(build,/--ks "\$KEYSTORE"/);
    assert.doesNotMatch(build,/\/root\/dsb-app\/dsb\.keystore|keytool|bash -x/);
  }
  assert.doesNotMatch(accessibility, /findAccessibilityNodeInfosByText|scheduleConfirmClick/);
  const sink = overlay.match(/private void log\(final String s\) \{\s*(?:\/\/[^\n]*\n\s*)*(Log\.i\([^;]+;)/);
  assert.ok(sink, 'shared logcat sink exists');
  assert.match(sink[1], /s\.length\(\)/);
  assert.doesNotMatch(sink[1], /,\s*s\s*\)/);
  assert.doesNotMatch(source('MainActivity'), /Log\.[a-z]\([^;]*(?:\+\s*(?:url|u)\b|getDescription\()/);
  assert.doesNotMatch(source('TtsSpeaker'), /Log\.[a-z]\([^;]*s\.substring/);
  assert.doesNotMatch(source('DshClient'), /Log\.[a-z]\(/);
  assert.doesNotMatch(source('CmdReceiver'), /Log\.[a-z]\([^;]*(?:getStringExtra|\+\s*(?:cmd|token|nonce|encoded)\b)/);
  assert.doesNotMatch(source('TermuxRunner'), /Log\.[a-z]\([^;]*(?:\+\s*(?:shellCommand|body|payload|nonce)\b|,\s*t\s*\))/);
  assert.doesNotMatch(source('NotificationWatcher'), /Log\.[a-z]\([^;]*(?:getPackageName|\+\s*(?:title|text|sub)\b)/);
  for (const file of fs.readdirSync(path.join(app, 'src/com/dsb/assistant')).filter(f => f.endsWith('.java'))) {
    assert.ok(!/Log\.[a-z]\([^;]*(?:\+\s*t\s*[,);]|,\s*t\s*\))/.test(source(file.slice(0, -5))),
      file + ' must log exception type rather than raw messages or stacks');
  }
});

test('WebView debug switch follows the debuggable bit, not the SDK version', () => {
  assert.doesNotMatch(source('MainActivity'), /RecapBridge|WebMessagePort|addJavascriptInterface/);
  const call = source('MainActivity').match(/WebView\.setWebContentsDebuggingEnabled\(([^;]+)\);/);
  assert.ok(call);
  const expression = call[1].replace('getApplicationInfo().flags', 'flags')
    .replace('ApplicationInfo.FLAG_DEBUGGABLE', '2');
  assert.match(expression, /flags\s*&\s*2/);
  const enabled = new Function('flags', `return ${expression};`);
  for (const flags of [0, 1, 4, 128]) assert.equal(enabled(flags), false);
  for (const flags of [2, 3, 6, 130]) assert.equal(enabled(flags), true);
});

test('actual BootReceiver ignores unknown actions before state access and preserves boot decisions', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'dsb-public-release-'));
  const panelSink = source('OverlayService').match(/private void log\(final String s\) \{\s*(?:\/\/[^\n]*\n\s*)*(Log\.i\([^;]+;)/)[1];
  const files = {
    'android/content/BroadcastReceiver.java': `package android.content;
      public abstract class BroadcastReceiver { public abstract void onReceive(Context c, Intent i); }`,
    'android/content/Intent.java': `package android.content;
      public class Intent {
        public static final String ACTION_BOOT_COMPLETED="android.intent.action.BOOT_COMPLETED";
        public static final String ACTION_MY_PACKAGE_REPLACED="android.intent.action.MY_PACKAGE_REPLACED";
        private String action; public Intent(String a) { action=a; }
        public Intent(Context c, Class<?> k) {} public String getAction() { return action; }
      }`,
    'android/content/Context.java': `package android.content;
      public class Context { public int starts, stops;
        public void startForegroundService(Intent i) { starts++; }
        public void stopService(Intent i) { stops++; }
      }`,
    'android/os/SystemClock.java': `package android.os;
      public class SystemClock { public static long elapsedRealtime() { return 1000000; } }`,
    'android/os/Process.java': `package android.os;
      public class Process { public static long start=0; public static long getStartElapsedRealtime() { return start; } }`,
    'android/util/Log.java': `package android.util;
      public class Log { public static String last; public static int i(String t,String s) { last=s; return 0; }
        public static int e(String t,String s) { return 0; } }`,
    'com/dsb/assistant/OverlayService.java': `package com.dsb.assistant;
      import android.content.Context;
      public class OverlayService {
        static int reads, boot=7, closed=-1, run=6, marked; static long runAt=-1;
        static boolean deleted;
        static int bootCount(Context c) { reads++; return boot; }
        static int closedFlagBootCount(Context c) { reads++; return closed; }
        static int appRunBootCount(Context c) { reads++; return run; }
        static long appRunElapsedMs(Context c) { reads++; return runAt; }
        static void markUserClosed(Context c) { marked++; }
        static java.io.File closedFlag(Context c) { return new java.io.File("unused") {
          public boolean delete() { deleted=true; return true; }
        }; }
      }`,
    'com/dsb/assistant/TermuxRunner.java': `package com.dsb.assistant;
      public class TermuxRunner { static int boots, stops;
        public static void boot(android.content.Context c) { boots++; }
        public static void shutdown(android.content.Context c) { stops++; }
      }`,
    'com/dsb/assistant/BootReceiver.java': source('BootReceiver'),
    'com/dsb/assistant/PublicBootTest.java': `package com.dsb.assistant;
      import android.content.*;
      import android.util.Log;
      public class PublicBootTest {
        static void check(boolean ok, String msg) { if (!ok) throw new AssertionError(msg); }
        static void panelLog(String s) { ${panelSink} }
        static Context reset() {
          OverlayService.reads=0; OverlayService.boot=7; OverlayService.closed=-1;
          OverlayService.run=6; OverlayService.runAt=-1; OverlayService.marked=0;
          OverlayService.deleted=false; TermuxRunner.boots=0; TermuxRunner.stops=0;
          android.os.Process.start=0; return new Context();
        }
        static void send(Context c, String action) { new BootReceiver().onReceive(c, new Intent(action)); }
        static void started(Context c) {
          check(c.starts==1 && c.stops==0 && TermuxRunner.boots==1 && TermuxRunner.stops==0
            && OverlayService.deleted && OverlayService.marked==0, "genuine boot/update must start");
        }
        static void stopped(Context c) {
          check(c.starts==0 && c.stops==1 && TermuxRunner.boots==0 && TermuxRunner.stops==1
            && OverlayService.marked==1 && !OverlayService.deleted, "fake boot must stop");
        }
        public static void main(String[] args) {
          for (String a : new String[]{null,"","other.action","android.intent.action.PACKAGE_REPLACED"}) {
            Context c=reset(); OverlayService.run=7; send(c,a);
            check(OverlayService.reads==0 && c.starts==0 && c.stops==0 && TermuxRunner.boots==0
              && TermuxRunner.stops==0 && !OverlayService.deleted, "unknown action touched state");
          }
          Context c=reset(); new BootReceiver().onReceive(c,null);
          check(OverlayService.reads==0, "null intent touched state");
          c=reset(); send(c,Intent.ACTION_BOOT_COMPLETED); started(c);
          c=reset(); OverlayService.run=-1; send(c,Intent.ACTION_BOOT_COMPLETED); started(c);
          c=reset(); OverlayService.run=7; send(c,Intent.ACTION_BOOT_COMPLETED); stopped(c);
          c=reset(); OverlayService.closed=7; send(c,Intent.ACTION_BOOT_COMPLETED); stopped(c);
          c=reset(); OverlayService.run=7; OverlayService.runAt=999000;
          send(c,Intent.ACTION_BOOT_COMPLETED); check(c.starts==0 && c.stops==0 && TermuxRunner.boots==0 && TermuxRunner.stops==0, "run race");
          c=reset(); OverlayService.run=7; android.os.Process.start=999000;
          send(c,Intent.ACTION_BOOT_COMPLETED); check(c.starts==0 && c.stops==0 && TermuxRunner.boots==0 && TermuxRunner.stops==0, "process race");
          c=reset(); OverlayService.run=7; OverlayService.runAt=985000; android.os.Process.start=985000;
          send(c,Intent.ACTION_BOOT_COMPLETED); stopped(c);
          c=reset(); OverlayService.boot=-1; send(c,Intent.ACTION_BOOT_COMPLETED); started(c);
          c=reset(); OverlayService.run=7; OverlayService.closed=7;
          send(c,Intent.ACTION_MY_PACKAGE_REPLACED); started(c);
          System.out.println("BOOT_ACTION_AND_CLOSED_AT_BOOT_PASS (14 cases)");
          for (String s : new String[]{null,"","https://host/path?token=private#fragment",
              "request body: private", "task: private", "notification: private", "screen: private"}) {
            panelLog(s);
            check(android.util.Log.last.equals("panel event chars=" + (s==null ? 0 : s.length())), "panel log leaked content");
          }
          System.out.println("ACTUAL_PANEL_LOG_SINK_PASS (7 content/null cases)");
        }
      }`,
  };
  try {
    for (const [name, content] of Object.entries(files)) {
      const file = path.join(dir, name);
      fs.mkdirSync(path.dirname(file), { recursive: true });
      fs.writeFileSync(file, content);
    }
    for (const [cmd, args] of [['javac', ['-encoding', 'UTF-8', '-d', dir, ...Object.keys(files).map(f => path.join(dir, f))]],
      ['java', ['-cp', dir, 'com.dsb.assistant.PublicBootTest']]]) {
      const result = spawnSync(cmd, args, { encoding: 'utf8', timeout: 30000 });
      assert.equal(result.status, 0, result.stdout + result.stderr);
      if (result.stdout) process.stdout.write(result.stdout);
    }
  } finally { fs.rmSync(dir, { recursive: true, force: true }); }
});
