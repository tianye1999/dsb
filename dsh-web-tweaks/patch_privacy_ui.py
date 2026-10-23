#!/usr/bin/env python3
"""DSH 0.2.1-rc.2 scoped feedback navigation and narrow-screen layout."""
from pathlib import Path
import re
from tweak_paths import targets

STYLE = '''<style id="dsh-privacy-ui-style">
/* Reserve the fixed rail control's own lane; never overlay the brand. */
.hHd-Xa_logoRow { padding-left:32px !important; }
.hHd-Xa_brandName { letter-spacing:0 !important; }
.hHd-Xa_brandName::after { content:none !important; }
@media (max-width:600px) {
  [role="menu"] ._shortcut_4ub78_198 { display:none !important; }
  /* Statistics are optional detail, not clipped fragments on a phone. */
  [data-composer-stat] { display:none !important; }
  .uV2eYG_row { flex-wrap:wrap !important; gap:8px !important; }
  .uV2eYG_trailing { flex:1 1 100% !important; min-width:0 !important; }
  .uV2eYG_standardControls { flex:1 !important; min-width:0 !important; }
  ._7KE1Ra_root { flex:1 !important; min-width:0 !important; }
  ._7KE1Ra_trigger {
    max-width:100% !important; height:auto !important; min-height:28px;
    flex-wrap:wrap !important; row-gap:2px;
  }
  ._7KE1Ra_triggerLabel {
    display:block !important;
    white-space:normal !important; overflow:visible !important;
    overflow-wrap:anywhere; text-overflow:clip !important;
  }
  ._7KE1Ra_triggerEffort {
    display:block !important; flex:0 0 auto !important;
    white-space:nowrap !important; overflow:visible !important;
    overflow-wrap:normal; text-overflow:clip !important;
  }
  ._7KE1Ra_triggerIcon { display:none !important; }
  ._7KE1Ra_triggerLabel ._1C9Wka_label { white-space:normal !important; flex-wrap:wrap; }
}
</style>'''

SCRIPT = r'''<script id="dsh-privacy-ui-script">
(function () {
  var issues = 'https://github.com/tianye1999/dsb/issues';
  // This account menu has no public action slot. Capture before React's contact
  // action so no account/device query parameters or upstream form are sent.
  function openFeedback(event) {
    if (event.type === 'keydown' && event.key !== 'Enter' && event.key !== ' ') return;
    var item = event.target instanceof Element ? event.target.closest('[role="menuitem"]') : null;
    if (!item || !/^(意见反馈|Feedback)$/.test(item.textContent.trim())) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    window.location.assign(issues);
  }
  document.addEventListener('click', openFeedback, true);
  document.addEventListener('keydown', openFeedback, true);
  function labelProduct() {
    if (document.title === 'DeepSeek Harness') document.title = '逗沙包 · 基于 DeepSeek Harness';
    document.querySelectorAll('[role="dialog"][aria-label="预览版说明"] p').forEach(function (el, index) {
      var text = index === 0 ? '逗沙包 · 基于 DeepSeek Harness。底座当前为 0.2 预览版，产品功能和插件 API 仍可能变化。' : '逗沙包是独立项目，不代表 DeepSeek 官方，也不暗示官方背书。';
      if (el.textContent !== text) el.textContent = text;
    });
    // Exact product headings only. Never rewrite conversation text/model/account labels.
    document.querySelectorAll('[role="dialog"] h1, [role="dialog"] h2, .hHd-Xa_localBuildTitle').forEach(function (el) {
      if (el.textContent.trim() === 'DeepSeek Harness') el.textContent = '逗沙包 · 基于 DeepSeek Harness';
    });
  }
  var labelScheduled = false;
  function scheduleProductLabel() {
    if (labelScheduled) return;
    labelScheduled = true;
    requestAnimationFrame(function () {
      labelScheduled = false;
      labelProduct();
    });
  }
  new MutationObserver(scheduleProductLabel).observe(document.documentElement, {childList:true, subtree:true});
  labelProduct();
})();
</script>'''


def patch(source):
    for kind, marker, block in (('style', 'dsh-privacy-ui-style', STYLE),
                                ('script', 'dsh-privacy-ui-script', SCRIPT)):
        pattern = r'<' + kind + r' id="' + marker + r'">.*?</' + kind + '>'
        if re.search(pattern, source, re.S):
            source = re.sub(pattern, lambda _, block=block: block, source, flags=re.S)
        else:
            anchor = '</head>' if kind == 'style' else '</body>'
            if anchor not in source:
                raise ValueError('missing ' + anchor)
            source = source.replace(anchor, block + '\n' + anchor, 1)
    return source


if __name__ == '__main__':
    for target in targets():
        path = Path(target)
        source = path.read_text(encoding='utf-8')
        path.write_text(patch(source), encoding='utf-8')
        print('patched:', target)
