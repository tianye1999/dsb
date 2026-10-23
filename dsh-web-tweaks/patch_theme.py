#!/usr/bin/env python3
"""
patch_theme.py —— DSH 主题补丁（2026-09-22 重写）

═══ 历史纠错 ═══
本补丁原先是**强制深色**（给 body 加 data-ds-dark-theme）。
2026-09-22 用户反馈「黑底白字太刺眼」，并给了参考截图（浅色风）。

排查后发现两个事实：
  1. **DSH 原生就有浅色主题**，而且就是 body 的默认档位：
       body                     { --dsw-alias-bg-base: var(--dsw-static-neutral-bluish-00) }   ← #fff
       body[data-ds-dark-theme] { --dsw-alias-bg-base: var(--dsw-static-neutral-bluish-950) } ← #151517
     是我多此一举把它强制成了深色。
  2. DSH 浅色令牌跟用户附件里的颜色**几乎是同一套**：
       附件 #0f1012  ≈  --dsw-static-neutral-bluish-1000 = #0f1115
       附件 #e9eaec  ≈  --dsw-static-neutral-bluish-150  = #e9ecf2

所以现在的做法：**不再强制深色 → 改为强制浅色**（并主动清掉可能残留的深色属性），
再只做一处品牌化微调：消息气泡换成附件里的那个蓝 #cce6ff。

实测 DSH 原生浅色（临时移除深色属性后）：
  body #fff / 字 #0f1115 / 消息气泡 #edf3fe / 输入框 #fff
"""
import os

TARGETS = [
    os.path.expanduser('~/.dsh/profiles/node_modules/@deepseek-ai/dsh/node_modules/@deepseek-ai/dsh-web-frontend/dist/index.html'),
    '/usr/local/lib/node_modules/@deepseek-ai/dsh/node_modules/@deepseek-ai/dsh-web-frontend/dist/index.html',
]

# ── 1) 字号再大一档（历史遗留，保留）──
ZOOM_FROM = '#root{zoom:1.07}'
ZOOM_TO = '#root{zoom:1.13}'

# ── 2) 浅色主题强制 + 气泡品牌化 ──
LIGHT_MARK = 'dsh-light-enforce'
LIGHT_CSS = """
      /* dsh-light-enforce（2026-09-22）：改成强制**浅色**。
         原来这里强制深色（data-ds-dark-theme），用户反馈黑底白字刺眼。
         DSH 浅色就是 body 默认档，所以只要不被加上深色属性即可。
         ⚠️ 用 !important 是因为 body 上的浅色令牌本身就是样式表规则。 */
      html{color-scheme:light !important}
      body{color-scheme:light !important}
      /* 消息气泡：用户附件里的蓝（DSH 原生浅色是 #edf3fe，这里换成附件的 #cce6ff）
         + 深色字，保证对比度（附件实测字色 #0f1012） */
      .Sixlwa_bubble{background:#cce6ff !important;color:#0f1012 !important}
"""
LIGHT_JS = """
    <script id="dsh-light-enforce">
    (function () {
      var DARK = 'data-ds-dark-theme';
      function ensure() {
        try {
          if (!document.body) return;
          // 清掉任何被加上来的深色属性（含其它脚本/组件加的）
          if (document.body.hasAttribute(DARK)) document.body.removeAttribute(DARK);
          if (document.documentElement.hasAttribute(DARK)) document.documentElement.removeAttribute(DARK);
        } catch (e) {}
      }
      ensure();
      if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', ensure);
      // 只监听该属性的变化；回调里 removeAttribute 会再次触发，
      // 但那时已无该属性 → ensure() 内部直接返回，不会死循环。
      try {
        new MutationObserver(ensure).observe(document.body || document.documentElement,
          { attributes: true, attributeFilter: [DARK] });
      } catch (e) {}
      setInterval(ensure, 1000);
    })();
    </script>
"""

for t in TARGETS:
    if not os.path.exists(t):
        print('skip (not found):', t)
        continue
    s = open(t, encoding='utf-8').read()
    changed = []

    # ── 0) 迁移：删掉旧的"强制深色"注入（否则和新脚本打架）──
    #     旧脚本会 setAttribute 深色，新脚本会 removeAttribute → 无限互搏。
    import re as _re
    s2 = _re.sub(r'\s*<script id="dsh-dark-enforce">.*?</script>', '', s, flags=_re.S)
    if s2 != s:
        s = s2
        changed.append('移除旧 dsh-dark-enforce 脚本')
    # 旧深色 CSS 块是"注释 + html/body 两行"，整段删掉
    old_css = _re.compile(
        r"\n\s*/\* —— 深色主题：强制官方深色档位 \+ 暗色表单控件 —— \*/\s*\n"
        r"\s*html\{color-scheme:dark\}\s*\n"
        r"\s*body\{background:var\(--dsw-alias-bg-base\)\}\s*\n")
    s3 = old_css.sub('\n', s)
    if s3 != s:
        s = s3
        changed.append('移除旧深色 CSS')

    if ZOOM_FROM in s:
        s = s.replace(ZOOM_FROM, ZOOM_TO, 1)
        changed.append('zoom 1.07→1.13')

    if LIGHT_MARK not in s:
        marker = '.Sixlwa_bubble{max-width:100% !important}'
        if marker in s:
            s = s.replace(marker, marker + LIGHT_CSS, 1)
            s = s.replace('  </body>', LIGHT_JS + '  </body>', 1)
            changed.append('注入浅色强制 + 气泡蓝 #cce6ff')
        else:
            print('  ⚠ 找不到 CSS 锚点，跳过:', t)
            continue

    if changed:
        open(t, 'w', encoding='utf-8').write(s)
        print('patched:', t, '|', ', '.join(changed))
    else:
        print('already patched:', t)
