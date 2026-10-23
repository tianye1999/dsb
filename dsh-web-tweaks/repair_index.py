#!/usr/bin/env python3
"""Update the current shell's sidebar, light theme and conclusion styles.

No historical base file is read. Shared rules live in a dedicated block so
reapplying the composer cannot remove them or mutate the gesture script.
"""
import os
import re
import sys

# 侧栏块与 patch_sidebar.py 共用一份（单一来源，避免两边规则不一致）
_SB_SRC = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'patch_sidebar.py'), encoding='utf-8').read()
_m = re.search(r"SNIPPET = r'''(.*?)'''", _SB_SRC, re.S)
_sb = _m.group(1) if _m else ''
_sc = _sb.find('<script id="dsh-rail-script">')
# ⚠️ 必须拆成 style / script 两段替换：两者之间夹着别的补丁块，整段扫会一起吃掉（踩过两次）
SB_SCRIPT = _sb[_sc:].strip() if _sc > 0 else ''
# 只取"展开态"那几条 CSS：style 块里**混着别的补丁的规则**，整段替换会把它们清掉（踩过）
_m3 = re.search(r'(html\[data-dsh-sidebar="full"\].*?)(?=\s*</style>)', _sb, re.S)
SB_CSS = _m3.group(1).strip() if _m3 else ''
from tweak_paths import targets
TARGETS = targets()

# ── 2) 浅色强制（替换深色强制）──────────────────────────────────────
LIGHT_CSS = """
      /* dsh-light-enforce（2026-09-22）：强制**浅色**（原为强制深色，用户反馈黑底白字刺眼）。
         DSH 浅色就是 body 默认档，只要不被加上深色属性即可。 */
      html{color-scheme:light !important}
      body{color-scheme:light !important}
      /* 消息气泡：用户附件里的蓝 #cce6ff + 深色字 */
      .Sixlwa_bubble{background:#cce6ff !important;color:#0f1012 !important}
"""
LIGHT_JS = """
    <script id="dsh-light-enforce">
    (function () {
      var DARK = 'data-ds-dark-theme';
      function ensure() {
        try {
          if (!document.body) return;
          if (document.body.hasAttribute(DARK)) document.body.removeAttribute(DARK);
          if (document.documentElement.hasAttribute(DARK)) document.documentElement.removeAttribute(DARK);
        } catch (e) {}
      }
      // 2026-09-22：清掉 localStorage 里存的 composer 可见性。
      // 用户要求输入框默认展开，但老的 'hidden' 会一直锁住它（补丁默认值改了也没用）。
      try {
        var m = localStorage.getItem('dsh.composer.mode');
        if (m !== 'shown') localStorage.setItem('dsh.composer.mode', 'shown');
      } catch (e) {}
      ensure();
      if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', ensure);
      try {
        new MutationObserver(ensure).observe(document.body || document.documentElement,
          { attributes: true, attributeFilter: [DARK] });
      } catch (e) {}
      setInterval(ensure, 1000);
    })();
    </script>
"""

# ── 3) 隐藏思考过程 ─────────────────────────────────────────────────
THINK_CSS = """
      /* dsh-hide-thinking（2026-09-22 用户要求）：关闭「思考过程」的显示。
         ⚠️ 只隐藏显示，不关模型推理能力（reasoningEffort 不动）——不损失回答质量。 */
      .lcKema_root{display:none !important}
"""

# ── 4) 结尾结论卡 ───────────────────────────────────────────────────
CARD_CSS = """
      /* dsh-conclusion-card（2026-09-22）：把回答末尾的引用块渲染成深色「结论卡」。
         与总结弹窗同一套配色：底 #22252b / 竖条 #4d8dff / 字白。
         人设（cordis.patch.yml 的 personaPrefix）要求每轮以引用块收尾。 */
      .EvIC1a_scroll [class*="_markdown_"] blockquote{
        background:#22252b !important; color:#ffffff !important;
        border:none !important; border-left:3px solid #4d8dff !important;
        border-radius:10px !important; padding:12px 15px !important;
        margin:14px 0 4px !important;
        box-shadow:0 2px 10px rgba(0,0,0,.10) !important;
        font-size:15px !important; line-height:23px !important;
      }
      .EvIC1a_scroll [class*="_markdown_"] blockquote,
      .EvIC1a_scroll [class*="_markdown_"] blockquote p,
      .EvIC1a_scroll [class*="_markdown_"] blockquote li{color:#c8cdd4 !important}
      .EvIC1a_scroll [class*="_markdown_"] blockquote strong,
      .EvIC1a_scroll [class*="_markdown_"] blockquote b{color:#ffffff !important;font-weight:600 !important}
      .EvIC1a_scroll [class*="_markdown_"] blockquote a{color:#8ab4ff !important}
      .EvIC1a_scroll [class*="_markdown_"] blockquote p{margin:0 !important}
      .EvIC1a_scroll [class*="_markdown_"] blockquote p + p{margin-top:6px !important}
      /* 卡内代码（2026-09-23 修正）：
         ⚠️ 原来用 filter:invert 做"深底可读"，实测**适得其反**：
            行内 code 的 computed 背景是浅色(250,250,250)、字色继承卡片(200,205,212)，
            反相后变成"深底 + 深字" → 用户看不清（截图圈出来了）。
         正解：**去掉反转**，改成"浅底 + 深字"——
            行内 code 显式给浅色底 + 固定深色字（行内代码没有语法高亮，安全）；
            代码块保留 DSH 原生浅底，用强制深色兜底（shiki 高亮多为中深色，在浅底上可读；
            若个别高亮色偏浅，比"整块看不清"仍好得多）。 */
      .EvIC1a_scroll [class*="_markdown_"] blockquote code{
        background:rgba(255,255,255,.92) !important;
        color:#0f1115 !important;
      }
      .EvIC1a_scroll [class*="_markdown_"] blockquote code *{color:#0f1115 !important}
      .EvIC1a_scroll [class*="_markdown_"] blockquote pre{
        background:#f9fafb !important;
        border:1px solid rgba(255,255,255,.14) !important;
      }
      .EvIC1a_scroll [class*="_markdown_"] blockquote pre code,
      .EvIC1a_scroll [class*="_markdown_"] blockquote pre code *{background:transparent !important}
"""


def strip_block(s, start_marker):
    """删掉 start_marker 所在的那个 CSS 块（到其后第一个 '}' 为止）。"""
    i = s.find(start_marker)
    if i < 0:
        return s, False
    j = s.find('}', i)
    if j < 0:
        return s, False
    return s[:i] + s[j + 1:], True



def _sync_assets(s, target):
    """旧 index.html 可能留有旧版打包文件名（index-XXXX.js 等）。
    DSH 升级后文件名的 hash 会变 → 照搬就会 404 → 页面空白/黑屏（2026-09-25 从 0.1.5 升到 0.1.7 时踩到）。
    这里改成按目标 dist/assets 里真实存在的文件替换。"""
    adir = os.path.join(os.path.dirname(target), 'assets')
    if not os.path.isdir(adir):
        return s, []
    real = {}
    for f in os.listdir(adir):
        m = re.fullmatch(r'([a-z]+)-[A-Za-z0-9_-]+\.(js|css)', f)
        if m:
            real.setdefault((m.group(1), m.group(2)), []).append(f)
    changed = []
    def rep(m):
        key = (m.group(1), m.group(2))
        cands = real.get(key, [])
        if len(cands) != 1 or cands[0] == m.group(0):
            return m.group(0)
        changed.append(f'{m.group(0)}→{cands[0]}')
        return cands[0]
    s = re.sub(r'(?<=assets/)([a-z]+)-[A-Za-z0-9_-]+\.(js|css)', rep, s)
    return s, changed

def main():
    for t in TARGETS:
        if not os.path.isdir(os.path.dirname(t)):
            print('skip (not found):', t)
            continue
        # Work on the current shell; never resurrect a historical private backup.
        s = open(t, encoding='utf-8').read()
        log = []

        # ⓪ 打包文件名对齐当前版本（防升级后 404 黑屏）
        s, fixed = _sync_assets(s, t)
        if fixed:
            log.append('资源名对齐:' + ';'.join(fixed))

        # ② 去掉深色强制（脚本 + CSS）
        s2 = re.sub(r'\s*<script id="dsh-dark-enforce">.*?</script>', '', s, flags=re.S)
        if s2 != s:
            s = s2
            log.append('移除深色脚本')
        s2 = s.replace('      html{color-scheme:dark}\n', '')
        s2 = s2.replace('      body{background:var(--dsw-alias-bg-base)}\n', '')
        if s2 != s:
            s = s2
            log.append('移除深色CSS')

        # Keep shared rules out of the composer-owned block, which is replaceable.
        block = '<style id="dsh-repair-style">' + LIGHT_CSS + THINK_CSS + CARD_CSS + '</style>'
        pattern = r'<style id="dsh-repair-style">.*?</style>'
        if re.search(pattern, s, re.S):
            s = re.sub(pattern, lambda _: block, s, flags=re.S)
        else:
            s = s.replace('</head>', block + '</head>', 1)
        # ⚠️ 判据必须用完整 script 标签：CSS 注释里也含 'dsh-light-enforce'，
        #    用它做判据会导致"注入了 CSS 就跳过脚本"（2026-09-22 踩到）
        if '<script id="dsh-light-enforce">' not in s:
            s = s.replace('</body>', LIGHT_JS + '</body>', 1)
            log.append('注入浅色脚本')

        # ⑤ 侧栏：三态折叠 → 两态 + 浮层（2026-09-23 用户要求：点一下全开、覆盖不挤走内容）
        #    旧块来自 BASE 底稿；用 SIDEBAR_SNIPPET 整块替换（幂等：已是 v4 就跳过）
        if SB_SCRIPT:
            sc0 = s.find('<script id="dsh-rail-script">')
            sc1 = s.find('</script>', sc0) if sc0 >= 0 else -1
            if sc0 >= 0 and sc1 > sc0:
                s = s[:sc0] + SB_SCRIPT + s[sc1 + len('</script>'):]
                log.append('侧栏script→v4')
        if SB_CSS and 'html[data-dsh-sidebar="full"] .pI_x6G_centerCol' not in s:
            st0 = s.find('<style id="dsh-rail-style">')
            st1 = s.find('</style>', st0) if st0 >= 0 else -1
            if st0 >= 0 and st1 > st0:
                s = s[:st1] + SB_CSS + '\n' + s[st1:]
                log.append('侧栏CSS追加v4')

        open(t, 'w', encoding='utf-8').write(s)
        print(f'patched: {t.split("/dist/")[-1]} → {len(s)} 字符 | ' + ', '.join(log))
    return 0


if __name__ == '__main__':
    sys.exit(main())
