
import os
from tweak_paths import targets as patch_targets
targets = patch_targets()
EXTRA = '''
      /* —— 追加：字号整体稍大一档 + 用户消息铺满宽度 —— */
      #root{zoom:1.07}
      .Sixlwa_userStack{max-width:100% !important}
      .Sixlwa_userRow{align-items:stretch !important}
      .Sixlwa_bubble{max-width:100% !important}
'''
for t in targets:
    s = open(t, encoding='utf-8').read()
    if 'Sixlwa_userStack' in s:
        print('already patched:', t); continue
    # 插到我们自己的 <style id="dsh-rail-style"> 里
    marker = 'html[data-dsh-sidebar="hidden"] .pI_x6G_sidebarCol{visibility:hidden}'
    if marker not in s:
        print('marker missing in', t); continue
    s = s.replace(marker, marker + EXTRA, 1)
    if '#root{zoom' not in s:
        print('zoom 注入失败', t); continue
    open(t, 'w', encoding='utf-8').write(s)
    print('patched:', t, len(s), 'bytes')
