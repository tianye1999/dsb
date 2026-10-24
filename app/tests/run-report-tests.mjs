import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const app = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const root = path.dirname(app);
const source = name => path.join(app, 'src/com/dsb/assistant', name + '.java');
assert.deepEqual(fs.readFileSync(path.join(root, 'termux/trusted-entry.py')),
  fs.readFileSync(path.join(app, 'termux/trusted-entry.py')), 'trusted entry asset mirror');
const overlay = fs.readFileSync(source('OverlayService'), 'utf8');
assert.ok(overlay.indexOf('panel.addView(reportBox)') > overlay.indexOf('panel.addView(statusRow)'),
  'report follows the native task result');
assert.match(overlay, /panel\.addView\(reportBox\);\s*renderReportPanel\(\)/,
  'late panel creation restores the in-memory summary');
assert.match(overlay, /DshClient\.queryReport\(OverlayService\.this, reference\)/);
assert.match(overlay, /else if \(reportPending\)\s*\{\s*reportPending = false;\s*showPanel\(true\)/,
  'normal completed /do leaves a visible bubble route to the report');
assert.match(overlay, /reportDetailLoaded = detail != null;/,
  'failed detail query remains retryable');

const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'dsb-report-app-'));
try {
  const files = {
    'android/content/Context.java': 'package android.content; public class Context {}',
    'com/dsb/assistant/TermuxRunner.java': `package com.dsb.assistant;
      import android.content.Context; import org.json.JSONObject;
      public final class TermuxRunner {
        static JSONObject envelope, lastBody; static String lastPath; static int calls; static boolean fail;
        static JSONObject request(Context c, String path, JSONObject body, int timeout) {
          calls++; lastPath=path; lastBody=body; if (fail) throw new IllegalStateException("private_failure");
          return envelope;
        }
      }`,
    'org/json/JSONArray.java': `package org.json;
      import java.util.ArrayList; import java.util.List;
      public class JSONArray {
        private final List<Object> values = new ArrayList<>();
        public JSONArray put(Object value) { values.add(value); return this; }
        public int length() { return values.size(); }
        public JSONObject optJSONObject(int index) {
          Object value = index >= 0 && index < values.size() ? values.get(index) : null;
          return value instanceof JSONObject ? (JSONObject)value : null;
        }
      }`,
    'org/json/JSONObject.java': `package org.json;
      import java.util.HashMap; import java.util.Map;
      public class JSONObject {
        private final Map<String,Object> values = new HashMap<>();
        public JSONObject put(String key,Object value) { values.put(key,value); return this; }
        public Object opt(String key) { return values.get(key); }
        public String optString(String key) { return optString(key, ""); }
        public String optString(String key,String fallback) {
          Object value=opt(key); return value instanceof String ? (String)value : fallback;
        }
        public int optInt(String key,int fallback) {
          Object value=opt(key); return value instanceof Number ? ((Number)value).intValue() : fallback;
        }
        public int getInt(String key) { return ((Number)opt(key)).intValue(); }
        public JSONObject getJSONObject(String key) { return (JSONObject)opt(key); }
        public JSONObject optJSONObject(String key) {
          Object value=opt(key); return value instanceof JSONObject ? (JSONObject)value : null;
        }
        public JSONArray optJSONArray(String key) {
          Object value=opt(key); return value instanceof JSONArray ? (JSONArray)value : null;
        }
        public boolean has(String key) { return values.containsKey(key); }
        public boolean isNull(String key) { return opt(key)==null; }
        public int length() { return values.size(); }
      }`,
  };
  const paths = [];
  for (const [name, content] of Object.entries(files)) {
    const target = path.join(dir, name);
    fs.mkdirSync(path.dirname(target), { recursive: true });
    fs.writeFileSync(target, content);
    paths.push(target);
  }
  paths.push(source('ExecutionReport'), source('DshClient'), path.join(app, 'tests/ReportPresentationTest.java'));
  for (const [command, args] of [
    ['javac', ['--release', '11', '-Xlint:all', '-Werror', '-encoding', 'UTF-8', '-d', dir, ...paths]],
    ['java', ['-cp', dir, 'com.dsb.assistant.ReportPresentationTest']],
  ]) {
    const result = spawnSync(command, args, { encoding: 'utf8', timeout: 30000 });
    assert.equal(result.status, 0, result.stdout + result.stderr);
    process.stdout.write(result.stdout);
  }
} finally {
  fs.rmSync(dir, { recursive: true, force: true });
}
