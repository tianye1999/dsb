#!/usr/bin/env python3
"""
patch_composer.py —— 底部输入框：默认展开 + 右下角圆点切换（2026-09-22 重写）

═══ 这个补丁治什么 ═══
1) 输入框默认**展开**（原来默认 hidden，打开页面看不到输入框）
2) 右下角小圆点：点一下收起/展开（收起后靠它唤出）
3) 发送后自动收起（由 patch_autohide.py 配合）
4) DSH 原生「回到底部」按钮顺手展开输入框并真正滚到底

═══ 为什么要重写 ═══
旧版幂等判断是 `if 'dsh-composer-dot' in s: already patched` ——
而这个字符串在 **CSS 和 JS 里都出现**。于是只要 CSS 在、JS 被删（或反之），
补丁就误判"已打过"而拒绝重打 → 出现"改了半天不生效"的假象（2026-09-22 踩到）。
现在改成：**先无条件清掉本补丁注入的 CSS/JS，再重新注入**，
无论文件处于什么状态，跑一次就得到确定结果。
"""
import os
import re

from tweak_paths import targets
TARGETS = targets()

# 用拼接而不用 % 格式化：CSS/JS 里有 100% 这类百分号，会撞上格式化（2026-09-22 踩到）
CSS = """
      /* —— 底部输入框（默认展开，可点圆点切换） + 右下角小圆点唤出 —— */
      html[data-dsh-composer="hidden"] .wSkVaW_composerSeat,
      html[data-dsh-composer="hidden"] .wSkVaW_composerStack,
      html[data-dsh-composer="hidden"] [data-composer-seat]{display:none !important}
      #dsh-composer-dot{
        position:fixed;right:10px;bottom:10px;z-index:2147483000;
        width:26px;height:26px;padding:0;border-radius:50%;cursor:pointer;
        border:1px solid color-mix(in srgb, currentColor 22%, transparent);
        background:color-mix(in srgb, currentColor 8%, transparent);
        color:inherit;font:12px/1 system-ui,sans-serif;
        display:flex;align-items:center;justify-content:center;
        opacity:.35;transition:opacity .15s ease, bottom .15s ease;
      }
      #dsh-composer-dot:hover{opacity:1}
"""

JS = """
    <script id="dsh-composer-script">
    (function () {
      // 2026-09-22 用户要求：输入框**默认展开**（原来默认 hidden）。
      // 原因：打开页面时输入框是收起的，用户看不到它，以为坏了。
      // 仍保留「发送后自动收起」+「点右下角圆点切换」。
      var mode = 'shown';
      try { mode = localStorage.getItem('dsh.composer.mode') || 'shown'; } catch (e) {}
      function seat() { return document.querySelector('.wSkVaW_composerSeat'); }
      function apply() {
        document.documentElement.setAttribute('data-dsh-composer', mode);
        var d = document.getElementById('dsh-composer-dot');
        if (!d) return;
        var h = 0, s = seat();
        if (mode === 'shown' && s) { try { h = s.getBoundingClientRect().height || 0; } catch (e) {} }
        d.style.bottom = (h ? h + 10 : 10) + 'px';
        d.textContent = mode === 'hidden' ? '\u2303' : '\u2304';
        d.title = mode === 'hidden' ? '\u663e\u793a\u8f93\u5165\u6846' : '\u9690\u85cf\u8f93\u5165\u6846';
      }
      function toggle() {
        mode = (mode === 'hidden') ? 'shown' : 'hidden';
        try { localStorage.setItem('dsh.composer.mode', mode); } catch (e) {}
        apply();
        if (mode === 'shown') {
          setTimeout(function () {
            var s = seat(); if (!s) return;
            var i = s.querySelector('textarea,[contenteditable="true"]');
            if (i) { try { i.focus(); } catch (e) {} }
          }, 80);
        }
      }
      function boot() {
        if (document.getElementById('dsh-composer-dot')) { apply(); return; }
        var b = document.createElement('button');
        b.id = 'dsh-composer-dot'; b.type = 'button';
        b.addEventListener('click', toggle);
        document.body.appendChild(b);
        apply();
        new MutationObserver(apply).observe(document.body, { subtree: true, attributes: true, attributeFilter: ['style', 'class'] });
        setInterval(apply, 1000);
      }
      if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot); else boot();

      // —— 让 DSH 原生的「回到底部」按钮顺手展开输入框，并真正滚到底 ——
      // 类名(EvIC1a_toBottom)是运行时生成的、HTML 里没有，所以按 aria-label 匹配；
      // 用事件委托挂在 document 上，按钮反复出现/消失都不用重绑。
      // 注意：这段必须在 <script> 标签【内部】——插到标签外浏览器不会执行（踩过这个坑）。
      function scrollConvToBottom() {
        var sc = document.querySelector('.wSkVaW_scrollBody');
        if (sc) { try { sc.scrollTop = sc.scrollHeight; } catch (e) {} }
      }
      document.addEventListener('click', function (ev) {
        var t = ev.target;
        while (t && t !== document) {
          var al = t.getAttribute && t.getAttribute('aria-label');
          if (al === '\u56de\u5230\u5e95\u90e8') {          // 回到底部
            if (mode !== 'shown') toggle();                  // 只在隐藏时展开
            // 输入框展开会占掉 212px，按钮自己滚到的「底部」就不够了 → 延迟补滚两次
            setTimeout(scrollConvToBottom, 120);
            setTimeout(scrollConvToBottom, 400);
            return;
          }
          t = t.parentElement;
        }
      }, true);   // 捕获阶段：按钮自己的滚动逻辑照常执行
    })();
    </script>
"""

# 清理边界：CSS 用注释头定位（比找 #dsh-composer-dot 可靠，后者在 CSS+JS 里都有）
# ── 清理边界（2026-09-22 用字符串定位，不用正则）──────────────────────────
# 为什么不用正则：`#dsh-composer-dot{display:none}` 到 `#dsh-composer-dot:hover`
# 之间的非贪婪匹配会跨过**夹在中间的其它规则**（如 .Sixlwa_bubble 锚点），
# 把不该删的一起吃掉 → 锚点没了、补丁链全断（2026-09-22 连踩两次）。
# 字符串定位明确、每步可验证。
JS_RE = re.compile(r'<script id="dsh-composer-script">.*?</script>', re.S)
JS_END = '</script>'
# ⚠️ 不能用 '#dsh-composer-dot{display:none !important}' 做起点标记 ——
#    新版 CSS（输入框默认展开）已经**不再包含**这条规则，但旧文件里有；
#    拿它当起点会把"从旧标记到 :hover 之间的一切"都删掉，
#    而 .Sixlwa_bubble 锚点恰好夹在那段中间 → 锚点被误删、补丁链断（踩到）。
#    改用两个都存在的标记：起点 = 圆点定位规则，终点 = :hover（块的最后一条）。
CSS_FIRST = '#dsh-composer-dot{\n        position:fixed'
CSS_LAST = '#dsh-composer-dot:hover{opacity:1}'


def strip_composer_css(s):
    """删掉本补丁注入的 CSS 块（含其上方紧邻的注释），返回 (新串, 删了几份)。"""
    n = 0
    while True:
        i = s.find(CSS_FIRST)
        if i < 0:
            i = s.find('#dsh-composer-dot{display:none !important}')
        if i < 0:
            break
        j = s.find(CSS_LAST, i)
        if j < 0:
            break
        j += len(CSS_LAST)
        # 往上吃掉紧邻的注释块（本补丁的块注释：/* ... */）
        k = s.rfind('/*', 0, i)
        if k >= 0:
            close = s.find('*/', k)
            if close >= 0 and s[close + 2:i].strip() == '':
                i = k
        # 连前面的换行一起删，避免留空行
        while i > 0 and s[i - 1] in ' \t':
            i -= 1
        if i > 0 and s[i - 1] == '\n':
            i -= 1
        s = s[:i] + s[j:]
        n += 1
    return s, n


ANCHOR = '.Sixlwa_bubble{max-width:100% !important}'


def main():
    for t in TARGETS:
        if not os.path.exists(t):
            print('skip (not found):', t)
            continue
        s = open(t, encoding='utf-8').read()
        if ANCHOR not in s:
            print('anchor missing:', t)
            continue
        before = s
        # Replace only owned blocks. Legacy shared styles contain other patches.
        style = '<style id="dsh-composer-style">' + CSS + '</style>'
        pattern = r'<style id="dsh-composer-style">.*?</style>'
        if re.search(pattern, s, re.S):
            s = re.sub(pattern, lambda _: style, s, flags=re.S)
        else:
            s = s.replace('</head>', style + '</head>', 1)
        if JS_RE.search(s):
            s = JS_RE.sub(lambda _: JS.strip(), s)
        else:
            s = s.replace('</body>', JS.strip() + '</body>', 1)
        if s == before:
            print('no change:', t)
        else:
            open(t, 'w', encoding='utf-8').write(s)
            print('patched:', t, len(s), 'chars', '| 默认值 shown' if "var mode = 'shown'" in s else '| ⚠ 默认值异常')


if __name__ == '__main__':
    main()
