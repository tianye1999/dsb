#!/usr/bin/env python3
"""
add_missing_rules.py —— 补上字号/代码/表格规则（2026-09-22）

背景：patch_fonts.py 的幂等判断只看 LEGACY_MARK，导致它"后加"的规则
（body-font-bump / codeblock-tight / table-tight）在文件里一直缺席；
而 patch_composer 的清理 bug 又把已有规则连带删掉。
这里按**已实现并实测过的值**直接写入，锚点用 composer 块末尾，
幂等（存在同名标记就跳过）。

这些值是 2026-09-22 一轮轮实测确定的（渲染值 = authored × 1.2 文本自动放大）：
  正文      18px → 渲染 21.6px（2026-09-24 用户要求调小；原 20px → 24px）
  行内代码  11px → 渲染 13.2px
  代码块    11px → 渲染 13.2px（行高 13px → 15.6px）
  表格      15px → 渲染 18px（行高 20px）
"""
import os
import sys
import re

from tweak_paths import targets
TARGETS = targets()
ANCHOR = '#dsh-composer-dot:hover{opacity:1}'

RULES = {
    'dsh-body-font-bump': """
      /* dsh-body-font-bump（2026-09-22；2026-09-24 用户要求"再小一点点" → 20px 改 18px）：正文 18px（渲染 21.6px）。
         正文原本靠"继承浏览器默认字号 + text-size-adjust:160%"自动放大，
         CSS 里没有可调基准 → 只能显式给字号。
         ⚠️ 用 :where() 包裹那串 :not()：否则特异性飙到 (0,13,0)，
            会压死后面的代码块/表格规则（2026-09-22 踩过）。
         ⚠️ 排除项里要含 .md-code-block 和表格，否则它们会被正文规则覆盖。 */
      .EvIC1a_scroll,
      .EvIC1a_scroll :where(:not(.lcKema_root):not(.lcKema_root *):not(.o3BgMG_root):not(.o3BgMG_root *):not(.CY-8Ka_root):not(.CY-8Ka_root *):not(.XrJvXW_root):not(.XrJvXW_root *):not(._5OnbHa_root):not(._5OnbHa_root *):not(._root_luwio_9):not(._root_luwio_9 *):not(.md-code-block):not(.md-code-block *):not(table):not(table *)){
        font-size:18px !important;
      }
""",
    'dsh-codeblock-tight': """
      /* dsh-codeblock-tight（2026-09-22）：代码块与行内代码压到最小。
         shiki 把代码切成 span.line，只压外层压不住内层 → 一并覆盖。 */
      .EvIC1a_scroll .md-code-block pre,
      .EvIC1a_scroll .md-code-block pre code,
      .EvIC1a_scroll .md-code-block code,
      .EvIC1a_scroll .md-code-block pre code .line,
      .EvIC1a_scroll .md-code-block pre code span,
      .EvIC1a_scroll pre._plain_rsn9u_103,
      .EvIC1a_scroll pre._plain_rsn9u_103 code,
      .EvIC1a_scroll pre._plain_rsn9u_103 code .line,
      .EvIC1a_scroll ._content_rsn9u_74 pre,
      .EvIC1a_scroll ._content_rsn9u_74 code{
        font-size:11px !important; line-height:13px !important;
      }
      .EvIC1a_scroll [class*="_markdown_"] :not(pre) > code,
      .EvIC1a_scroll [class*="_markdown_"] :not(pre) > code *{
        font-size:11px !important; line-height:16px !important; padding:0 3px !important;
      }
      .EvIC1a_scroll .md-code-block pre{padding:2px 5px !important}
      .EvIC1a_scroll .md-code-block ._content_rsn9u_74{padding:0 !important}
      .EvIC1a_scroll .md-code-block ._bannerWrap_rsn9u_24{padding:0 !important;min-height:0 !important}
      .EvIC1a_scroll .md-code-block{margin-top:3px !important;margin-bottom:3px !important;border-radius:6px !important}
""",
    'dsh-tooltip-off': """
      /* dsh-tooltip-off（2026-09-22）：隐藏 tooltip 气泡。
         现象：页面左边中间永久飘着一块黑底白字「复制」小卡片。
         根因：① patch_fonts 给 .xzv4MW_actions 加了 zoom:0.5；
               ② tooltip 的 left/top 按**未缩放**坐标写进 inline style，
                  父容器缩放后位置算错，飘到页面左边；
               ③ 触屏无 hover → tooltip 永不被隐藏，永久可见。
         触屏上 tooltip 本就无用，直接隐藏。 */
      ._bubble_1nw3t_1{display:none !important}
""",
    'dsh-table-tight': """
      /* dsh-table-tight（2026-09-22）：表格 15px（渲染 18px）/ 行高 20px，
         右 padding 16 → 8px（列更靠拢、少换行）。 */
      .EvIC1a_scroll [class*="_markdown_"] table,
      .EvIC1a_scroll [class*="_markdown_"] table th,
      .EvIC1a_scroll [class*="_markdown_"] table td,
      .EvIC1a_scroll [class*="_tableScroll_"] table,
      .EvIC1a_scroll [class*="_tableScroll_"] th,
      .EvIC1a_scroll [class*="_tableScroll_"] td{
        font-size:15px !important; line-height:20px !important;
      }
      .EvIC1a_scroll [class*="_markdown_"] th,
      .EvIC1a_scroll [class*="_markdown_"] td,
      .EvIC1a_scroll [class*="_tableScroll_"] th,
      .EvIC1a_scroll [class*="_tableScroll_"] td{
        padding-top:0 !important; padding-bottom:0 !important; padding-right:8px !important;
      }
      .EvIC1a_scroll [class*="_tableScroll_"] table code{font-size:13px !important;line-height:16px !important}
""",
}


def main():
    for t in TARGETS:
        if not os.path.exists(t):
            print('skip (not found):', t)
            continue
        s = open(t, encoding='utf-8').read()
        if ANCHOR not in s:
            print('✗ anchor missing:', t)
            continue
        block = '<style id="dsh-content-rules">' + ''.join(RULES.values()) + '</style>'
        pattern = r'<style id="dsh-content-rules">.*?</style>'
        if re.search(pattern, s, re.S):
            s = re.sub(pattern, lambda _: block, s, flags=re.S)
        else:
            s = s.replace('</head>', block + '</head>', 1)
        open(t, 'w', encoding='utf-8').write(s)
        print(f'patched: {t.split("/dist/")[-1]} → {len(s)} 字符')
    return 0


if __name__ == '__main__':
    sys.exit(main())
