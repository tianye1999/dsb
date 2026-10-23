
import os
from tweak_paths import targets
t = targets()[0]
s = open(t, encoding='utf-8').read()
if 'dsh-composer-autohide' in s:
    print('already patched'); raise SystemExit(0)

JS = '''
    <script id="dsh-composer-autohide">
    (function () {
      // 发送后自动收起输入框：靠"输入内容被清空"来判定（发送成功的可靠信号）
      var last = null;
      function mode() { return document.documentElement.getAttribute('data-dsh-composer'); }
      function seat() { return document.querySelector('.wSkVaW_composerSeat'); }
      function input() {
        var s = seat(); if (!s) return null;
        return s.querySelector('textarea,[contenteditable="true"]');
      }
      function value(i) {
        if (!i) return null;
        return (i.value !== undefined ? i.value : (i.textContent || ''));
      }
      function hide() {
        if (mode() !== 'shown') return;
        var dot = document.getElementById('dsh-composer-dot');
        if (dot) dot.click();          // 复用右下角小圆点的切换
      }
      setInterval(function () {
        if (mode() !== 'shown') { last = null; return; }
        var i = input();
        if (!i) return;
        var v = value(i);
        if (last !== null && last.trim() !== '' && (v || '').trim() === '') {
          setTimeout(hide, 350);      // 刚被清空 → 判定为已发送，稍等一拍再收起
        }
        last = v;
      }, 250);
    })();
    </script>
'''
s = s.replace('</body>', JS + '</body>', 1)
if 'dsh-composer-autohide' not in s: raise SystemExit('注入失败')
open(t, 'w', encoding='utf-8').write(s)
print('patched, size', len(s))
