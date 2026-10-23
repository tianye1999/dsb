
import re
from tweak_paths import targets
t = targets()[0]
s = open(t, encoding='utf-8').read()

# ⚠️ 幂等检查（2026-09-20 补）：必须先判断"已存在就跳过"。
# 旧版靠 "re.sub 后内容是否变化" 来判断，那不可靠——如果文件里的块和 NEW 不完全相同
# （比如被手工调整过），re.sub 仍会匹配并替换，s2 != s，于是每次都"替换"一遍，
# 实测导致重复注入、文件从 13991 涨到 16034 字节。
if 'id="dsh-hide-tabs"' in s:
    print('already patched, skip')
    raise SystemExit(0)

NEW = '''<script id="dsh-hide-tabs">
    (function () {
      // 1) 找到「对话 / 轨迹」那一行；2) 藏掉它；3) 把它上层残留的空白（高度/内边距）也收掉
      var hidden = [];
      function collapseChain(el) {
        el.style.setProperty('display', 'none', 'important');
        var cur = el, p = el.parentElement, depth = 0;
        while (p && p !== document.body && depth < 3) {
          p.style.setProperty('padding-top', '0px', 'important');
          p.style.setProperty('padding-bottom', '0px', 'important');
          p.style.setProperty('min-height', '0px', 'important');
          p.style.setProperty('margin-top', '0px', 'important');
          p.style.setProperty('margin-bottom', '0px', 'important');
          var others = Array.prototype.filter.call(p.children, function (c) {
            return c !== cur && c.offsetHeight > 1 && (c.textContent || '').trim() !== '';
          });
          if (others.length === 0) {
            p.style.setProperty('height', '0px', 'important');
            p.style.setProperty('display', 'none', 'important');
            cur = p; p = p.parentElement; depth++;
          } else break;
        }
      }
      function hide() {
        var best = null, bestH = 1e9;
        var all = document.querySelectorAll('div,nav,header,ul,section');
        for (var i = 0; i < all.length; i++) {
          var el = all[i];
          if (el === document.body || el === document.documentElement) continue;
          if (el.children.length > 10) continue;
          var txt = el.textContent || '';
          if (txt.indexOf('\u5bf9\u8bdd') < 0 || txt.indexOf('\u8f68\u8ff9') < 0) continue;
          var h = el.clientHeight || 0;
          if (h <= 0 || h > 160) continue;          // 只看高度很小的那条 tab 行
          if (h < bestH) { best = el; bestH = h; }
        }
        if (best) collapseChain(best);
      }
      hide();
      new MutationObserver(hide).observe(document.body, { childList: true, subtree: true });
      setInterval(hide, 1000);
    })();
    </script>'''

s2 = re.sub(r'<script id="dsh-hide-tabs">.*?</script>', NEW, s, count=1, flags=re.S)
if s2 == s:
    # 没有旧块 → 说明是全新（或升级后的）index.html：直接注入，而不是报错退出
    if '  </body>' in s:
        s2 = s.replace('  </body>', NEW + '\n  </body>', 1)
    elif '</body>' in s:
        s2 = s.replace('</body>', NEW + '\n</body>', 1)
    else:
        print('注入失败：找不到 </body>'); raise SystemExit(1)
    open(t, 'w', encoding='utf-8').write(s2)
    print('已注入 dsh-hide-tabs（原先没有），文件大小', len(s2))
    raise SystemExit(0)
open(t, 'w', encoding='utf-8').write(s2)
print('已替换 dsh-hide-tabs，文件大小', len(s2))
