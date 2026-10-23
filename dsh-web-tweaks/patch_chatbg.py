#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对话页背景改成暖白 —— 2026-09-24 用户要求。

用户："另一款工具在对话界面的背景颜色看上去比较舒服，能不能把这个颜色放到我们
DSB App 的对话页面，让它的背景色来使用呢？"

查证：
  · 参考底色 = body background rgb(247,246,243) = #F7F6F3
    （它的 CSS 变量 --background: 44 22% 96%，暖白）
  · DSB 侧：.pI_x6G_frame（App 外框）/ .wSkVaW_root（对话根）/ .P3OORG_panel（面板）
    三层都是 background: var(--dsw-alias-bg-base)；该 token 定义在
    body{--dsw-alias-bg-base:var(--dsw-static-neutral-bluish-00)}（浅色=纯白）
    body[data-ds-dark-theme]{...bluish-950}（深色）
  → 所以只改这一个 token 就够，不必碰任何具体类名（类名是 CSS Module 哈希，升级就变）。

幂等：以 dsh-chat-bg 为存在标记；必须排在 repair_index.py **之后**（那个脚本会从基础文件重建 index.html）。
"""
import os
import sys

from tweak_paths import targets
TARGET = targets()[0]
MARK = 'dsh-chat-bg'

STYLE = '''
    <!-- %s（2026-09-24 用户要求：对话页底色用暖白 #F7F6F3） -->
    <style id="%s">
      /* DSB 的 frame/root/panel 都吃 --dsw-alias-bg-base；改 token 比改哈希类名稳。
         深色档给参考配色深色的同款值（0 0%% 8%% = #141414），避免深色下被强制成暖白。 */
      body { --dsw-alias-bg-base: #F7F6F3 !important; }
      body[data-ds-dark-theme] { --dsw-alias-bg-base: #141414 !important; }
    </style>
''' % (MARK, MARK)


def main():
    if not os.path.isfile(TARGET):
        print('✗ 找不到 %s' % TARGET)
        return 1
    s = open(TARGET, encoding='utf-8').read()
    if MARK in s:
        print('✓ 已有 %s，跳过（幂等）' % MARK)
        return 0
    if '</head>' not in s:
        print('✗ index.html 里没有 </head>，锚点变了，需人工看')
        return 1
    s = s.replace('</head>', STYLE + '</head>', 1)
    open(TARGET, 'w', encoding='utf-8').write(s)
    print('✓ 已注入 %s（%d 字节）' % (MARK, len(STYLE)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
