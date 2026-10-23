
import re
from tweak_paths import targets
t = targets()[0]
s = open(t, encoding='utf-8').read()

CSS = '''
      /* 输入框出现时轻微上滑淡入（display 切换会重启动画） */
      .wSkVaW_composerSeat{animation:dsh-composer-in .18s ease}
      @keyframes dsh-composer-in{from{opacity:0;transform:translateY(14px)}to{opacity:1;transform:none}}
'''

# v2（2026-09-22 用户要求）：往上翻看历史时输入框自动收起。
#   · 上滑离开底部超过 COLLAPSE_AFTER px → 收起（把屏幕让给内容）
#   · 到底后继续上滑 → 唤起（原有行为，保留）
#   · 桌面滚轮同理：往上滚收起、到底后往下滚唤起
JS = '''
    <script id="dsh-composer-gesture">
    (function () {
      // 阈值：上滑离开底部多少像素就收起输入框
      var COLLAPSE_AFTER = 60;
      // 到底后继续上滑多少像素才唤起（原行为阈值）
      var SHOW_AFTER = 26;

      var lastY = 0, startedAtBottom = false, armed = true;
      var startTop = 0, scrollingUpMode = false, upAccum = 0;

      function scroller() {
        return document.querySelector('[data-conversation-scroll]')
            || document.querySelector('[class*="_scroll"]')
            || document.scrollingElement;
      }
      function atBottom(el) {
        try {
          if (el === document.scrollingElement) {
            return window.innerHeight + window.scrollY >= document.body.scrollHeight - 4;
          }
          return el.scrollTop + el.clientHeight >= el.scrollHeight - 4;
        } catch (e) { return false; }
      }
      function awayFromBottom(el) {
        try {
          if (el === document.scrollingElement) {
            return document.body.scrollHeight - (window.innerHeight + window.scrollY) > COLLAPSE_AFTER;
          }
          return el.scrollHeight - (el.scrollTop + el.clientHeight) > COLLAPSE_AFTER;
        } catch (e) { return false; }
      }
      function mode() {
        var m = document.documentElement.getAttribute('data-dsh-composer');
        if (m) return m;
        try { return localStorage.getItem('dsh.composer.mode') || 'hidden'; } catch (e) { return 'hidden'; }
      }
      function shown() { return mode() === 'shown'; }
      function hidden() { return !shown(); }
      function show() {
        if (!hidden()) return;
        var dot = document.getElementById('dsh-composer-dot');
        if (dot) dot.click();              // 复用右下角小圆点的切换逻辑
      }
      function hide() {
        if (!shown()) return;
        var dot = document.getElementById('dsh-composer-dot');
        if (dot) dot.click();
      }

      // —— 手指 ——
      document.addEventListener('touchstart', function (e) {
        var el = scroller(); if (!el) return;
        lastY = startTop = e.touches[0].clientY;
        startedAtBottom = atBottom(el);
        armed = true; scrollingUpMode = false; upAccum = 0;
        startTop = el.scrollTop;
      }, { passive: true });

      document.addEventListener('touchmove', function (e) {
        var el = scroller(); if (!el) return;
        var y = e.touches[0].clientY;
        var dy = y - lastY;                 // >0 = 手指往上移 = 内容往下翻（看更早）
        lastY = y;

        // ① 往上翻看历史 → 收起输入框
        if (dy > 0) {
          if (!scrollingUpMode) { scrollingUpMode = true; upAccum = 0; }
          upAccum += dy;
          if (upAccum > SHOW_AFTER && awayFromBottom(el) && shown()) hide();
        } else if (dy < 0) {
          scrollingUpMode = false; upAccum = 0;
        }

        // ② 到底后继续上滑 → 唤起（原行为）
        if (armed && hidden() && startedAtBottom && dy < -SHOW_AFTER && atBottom(el)) {
          armed = false; show();
        }
      }, { passive: true });

      // —— 桌面滚轮 ——
      document.addEventListener('wheel', function (e) {
        var el = scroller();
        if (!el) return;
        if (e.deltaY < 0) {                 // 往上滚 = 看更早
          if (awayFromBottom(el) && shown()) hide();
        } else if (hidden() && atBottom(el)) {
          show();
        }
      }, { passive: true });
    })();
    </script>
'''

# 幂等：v1 已存在则原位替换；v2 已存在则跳过
m = re.search(r'<script id="dsh-composer-gesture">.*?</script>', s, re.S)
if m:
    body = m.group(0)
    if 'COLLAPSE_AFTER' in body:
        print('already patched v2'); raise SystemExit(0)
    s = s[:m.start()] + JS.strip() + s[m.end():]
    action = 'v1 → v2 替换'
else:
    if 'dsh-composer-gesture' in s:
        print('already patched'); raise SystemExit(0)
    marker = '.Sixlwa_bubble{max-width:100% !important}'
    assert marker in s, 'style marker missing'
    s = s.replace(marker, marker + CSS, 1).replace('</body>', JS + '</body>', 1)
    action = '首次注入'

open(t, 'w', encoding='utf-8').write(s)
print(action, '完成，文件大小', len(s), '字节')
