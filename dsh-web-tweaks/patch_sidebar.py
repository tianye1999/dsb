#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""侧栏补丁：两态（隐藏 ⇄ 全开浮层）—— 2026-09-23 按用户要求重做。

用户要求：① 点一下箭头就"全部展开"（不要中间的窄条态，别点两下）
          ② 展开后是"浮在内容之上"，不能把内容挤走。
实现要点（踩过的坑都在注释里）：
  · App 是按**自己的 collapsed 状态类**（.hHd-Xa_root.hHd-Xa_collapsed）决定渲染
    "窄条图标"还是"完整侧栏"的 —— 光改网格列宽，列宽 0 时 App 干脆不渲染内容。
    → 所以直接点 App 自己的开关 .hHd-Xa_toggle（aria-label=打开/收起侧边栏）。
  · "不挤走内容"：列宽仍给足（App 要据此渲染），但给内容列 .pI_x6G_centerCol
    做反向位移（margin-left 负值 + width:100vw），视觉位置完全不变。
  · ⚠️ 两个踩过的坑：CSS 变量必须带单位（无单位 width 无效）；
    侧栏若用 position:fixed 脱离网格流，后面的列会被**自动重排**（内容列掉进 0px 列）。
"""
import os
import re
import shutil

from tweak_paths import targets as patch_targets
targets = patch_targets()

SNIPPET = r'''
    <!-- DSH 侧栏两态折叠 + 浮层（本地改动，2026-09-23 重做）：
         点一下 = 全开（浮在内容之上、内容零位移），再点一下 = 收起。
         做法：直接调 App 自己的 .hHd-Xa_toggle（App 才负责渲染完整侧栏）；列宽照给，
         不挤走内容靠给 centerCol 反向位移。 -->
    <style id="dsh-rail-style">
      #dsh-rail-toggle{
        position:fixed;left:6px;top:6px;z-index:120;
        width:26px;height:26px;padding:0;border-radius:8px;cursor:pointer;
        border:1px solid color-mix(in srgb, currentColor 22%, transparent);
        background:color-mix(in srgb, currentColor 8%, transparent);
        color:inherit;font:13px/1 system-ui,sans-serif;
        display:flex;align-items:center;justify-content:center;
        opacity:.4;transition:opacity .15s ease;
      }
      #dsh-rail-toggle:hover{opacity:1}
      html[data-dsh-sidebar="hidden"] .pI_x6G_sidebarCol{visibility:hidden}
      html[data-dsh-sidebar="full"] .pI_x6G_sidebarCol{
        z-index:100;visibility:visible !important;
        background:var(--dsw-alias-bg-base,#fff);
        box-shadow:6px 0 24px rgba(0,0,0,.28);
        animation:dsh-sidebar-in .16s ease-out;
      }
      @keyframes dsh-sidebar-in{from{transform:translateX(-10px);opacity:.6}to{transform:none;opacity:1}}
      /* 内容列反向左移同样宽度 + 铺满视口 → 视觉位置完全不变（内容不会被挤走） */
      html[data-dsh-sidebar="full"] .pI_x6G_centerCol{
        width:100vw !important;
        margin-left:calc(-1 * var(--dsh-sidebar-w,280px)) !important;
      }
      /* Brand content is supplied by the dsb-brand sidebar slots. */
      button.hHd-Xa_brand, .hHd-Xa_brand *{pointer-events:none !important}
      button.hHd-Xa_brand{cursor:default !important}
    </style>
    <script id="dsh-rail-script">
    (function () {
      var FALLBACK_FULL = 280;
      var lastFull = FALLBACK_FULL + 'px';   // 注意带单位：无单位 width 无效（踩过）
      function frame() { return document.querySelector('div[style*="grid-template-columns"]'); }
      function appRoot() { return document.querySelector('.hHd-Xa_root'); }
      function appExpanded() { var r = appRoot(); return !!r && !r.classList.contains('hHd-Xa_collapsed'); }
      function apply() {
        var f = frame(); if (!f) return;
        var parts = (f.style.gridTemplateColumns || '').split(/\s+/);
        if (parts.length < 3) return;
        var cur = parseFloat(parts[0]) || 0;
        if (cur >= 120) lastFull = cur + 'px';
        // 展开/隐藏以 App 自己的 collapsed 类为准（它决定渲染窄条还是完整侧栏）
        var mode = appExpanded() ? 'full' : 'hidden';
        var want = mode === 'hidden' ? '0px' : lastFull;
        if (parts[0] !== want) { parts[0] = want; f.style.gridTemplateColumns = parts.join(' '); }
        document.documentElement.style.setProperty('--dsh-sidebar-w', lastFull);
        document.documentElement.setAttribute('data-dsh-sidebar', mode);
        var b = document.getElementById('dsh-rail-toggle');
        if (b) {
          b.textContent = mode === 'hidden' ? '\u203a' : '\u2039';
          b.title = '\u4fa7\u680f\uff1a' + (mode === 'hidden' ? '\u5df2\u9690\u85cf' : '\u5df2\u5c55\u5f00') + '\uff08\u70b9\u51fb\u5207\u6362\uff09';
        }
      }
      function cycle() {
        // 点 App 自己的开关，但要**按方向选对按钮**：同类名下同时有「打开侧边栏」「收起侧边栏」
        // 「收起右侧边栏」等，取第一个会点错（实测：收起时点了"打开"）。排除含「右」的。
        var want = appExpanded() ? '收起' : '打开';
        var bs = document.querySelectorAll('.hHd-Xa_toggle, [aria-label*="侧边栏"]'), hit = false;
        for (var i = 0; i < bs.length; i++) {
          var l = bs[i].getAttribute('aria-label') || '';
          if (l.indexOf('侧边栏') >= 0 && l.indexOf('右') < 0 && l.indexOf(want) >= 0) { bs[i].click(); hit = true; break; }
        }
        if (!hit) { var t = document.querySelector('.hHd-Xa_toggle'); if (t) t.click(); }
        setTimeout(apply, 100); setTimeout(apply, 350); setTimeout(apply, 800);
      }
      function boot() {
        if (document.getElementById('dsh-rail-toggle')) { apply(); return; }
        var b = document.createElement('button');
        b.id = 'dsh-rail-toggle'; b.type = 'button'; b.title = '\u4fa7\u680f';
        b.addEventListener('click', cycle);
        document.body.appendChild(b);
        apply();
        new MutationObserver(apply).observe(document.body, { subtree: true, attributes: true, attributeFilter: ['style', 'class'] });
        setInterval(apply, 1000);
      }
      if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot); else boot();
    })();
    </script>
'''

BEGIN = '<!-- DSH 侧栏'
for t in targets:
    if not os.path.exists(t):
        print('skip (not found):', t)
        continue
    s = open(t, encoding='utf-8').read()
    if 'dsh-rail-toggle' in s:
        # Legacy rail styles also contain other patches: update only our layers.
        def layers(match):
            style = match.group(0)
            for selector, layer in ((r'#dsh-rail-toggle', 120),
                                    (r'html\[data-dsh-sidebar="full"\] \.pI_x6G_sidebarCol', 100)):
                pattern = '(' + selector + r'\s*\{[^}]*?\bz-index\s*:\s*)[^;}]+'
                style = re.sub(pattern, lambda m: m.group(1) + str(layer), style)
            return style
        updated = re.sub(r'<style id="dsh-rail-style">.*?</style>', layers, s, flags=re.S)
        if updated != s:
            open(t, 'w', encoding='utf-8').write(updated)
            print('patched sidebar layers:', t)
        else:
            print('already patched:', t)
        continue
    if '</body>' not in s:
        print('skip (no </body>):', t)
        continue
    shutil.copyfile(t, t + '.orig')
    s = s.replace('  </body>', SNIPPET + '  </body>') if '  </body>' in s else s.replace('</body>', SNIPPET + '</body>')
    open(t, 'w', encoding='utf-8').write(s)
    print('patched:', t, len(s), 'bytes')
