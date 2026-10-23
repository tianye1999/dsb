#!/usr/bin/env python3
# 2026-09-22：字号/行距规则收编成补丁（原来只手工改在 index.html 里，DSH 升级会丢）。
#
# 三块：
#   LEGACY : 之前只手工注入的全局字号/留白（text-size-adjust、EvIC1a 留白、
#            bOPqQW 统计行、xzv4MW 按钮、lcKema 思考字号）
#   SHRINK : 内容卡片（代码/读取/写入/编辑/搜索 + 上下文注入 + bash + others）与「思考」同字号 6.5px
#   TIGHT  : 正文行距 1.4；内容块行高/留白压到最小；表格行距压到最小
#
# 选择器依据（CDP 实测，2026-09-22）：
#   内容卡片    : ._root_luwio_9（通用卡片外壳，所有 variant 共用）
#                 + .lcKema_root(思考) / .o3BgMG_root(代码/读取/写入/编辑/搜索)
#                 + .CY-8Ka_root[data-variant="bash"] / .XrJvXW_root(上下文注入)
#                 + ._5OnbHa_root(others，任务列表等通用卡片)
#   卡片固定行高: ._row_luwio_16{height:calc(24px + var(--dsh-content-font-delta,0px))}
#                 body 上 --dsh-content-font-delta = calc(--dsh-content-font-size - 14px)
#                 → 在卡片上钉 --dsh-content-font-delta:-9px（行高 24→15px、图标 16→7px）
#   子调用      : .ztWv_q_subCalls{margin:4px 0 2px 22px; row-gap:4px}
#   消息行      : .EvIC1a_flowItem{margin-top:16px}
#   表格        : th/td padding:10px + line-height:1.4 → 行高 42~64px

from tweak_paths import targets
TARGETS = targets()

# ── 1) 旧的全局字号/留白（2026-09-21 定，原来只手工注入，这里收编）──
LEGACY_MARK = 'text-size-adjust:160%'
BODYBUMP_MARK = 'dsh-body-font-bump'   # 2026-09-22 正文 +1 号
CODEBLOCK_MARK = 'dsh-codeblock-tight'  # 2026-09-22 代码块缩小
LEGACY = """
      /* —— 全局字号/留白（2026-09-21 定；2026-09-22 收编进补丁）—— */
      #root{zoom:1 !important}
      html{-webkit-text-size-adjust:160% !important;text-size-adjust:160% !important}
      .EvIC1a_scroll{padding-left:4px !important;padding-right:4px !important}
      .uV2eYG_root{padding-left:4px !important;padding-right:4px !important}
      .bOPqQW_root{padding-left:4px !important;padding-right:4px !important}
      .bOPqQW_root, .bOPqQW_root *{font-size:5px !important;line-height:1.5 !important}
      /* 2026-09-22：消息操作条缩到一半（原作者意图），但**必须同时隐藏它的
         悬浮提示气泡** —— 否则那个 tooltip（"复制"）会飘在页面左边中间。
         原因：tooltip 的 left/top 是按**未缩放**坐标写进 inline style 的，
         而父容器被 zoom:0.5 缩放 → 位置算错；且触屏没有 hover 去隐藏它
         → 永久可见。实测：气泡固定在 (0,333)、31x20、黑底白字。
         触屏上 tooltip 本来就无用，直接 display:none。 */
      .xzv4MW_actions{zoom:0.5}
      ._bubble_1nw3t_1{display:none !important}
      .lcKema_root, .lcKema_root *{font-size:6.5px !important}
      /* dsh-hide-thinking（2026-09-22 用户要求）：关闭"思考过程"的显示。
         ⚠️ 只隐藏显示，**不关模型的推理能力**（reasoningEffort 保持不动）——
            推理继续做，只是不给用户看，这样不损失回答质量。
         前端「思考」块的根类就是 .lcKema_root（其下有 lcKema_thinkBody），
         整个块连外壳一起隐藏。想恢复：删掉这条或改成 display:block。 */
      .lcKema_root{display:none !important}
      /* dsh-body-font-bump（2026-09-22）：正文/气泡 +1 号。
         为什么必须显式写：正文原本靠继承浏览器默认字号 + text-size-adjust:160%
         自动放大（实测 19.2px），CSS 里没有可调的基准 → 只能直接给字号。
         ⚠️ 必须 :not() 排除已被 SHRINK/TIGHT 钉成 6.5px 的卡片，
            否则 !important 冲突会让卡片字号随继承变化。 */
      .EvIC1a_scroll,
      .EvIC1a_scroll :not(.lcKema_root):not(.lcKema_root *):not(.o3BgMG_root):not(.o3BgMG_root *):not(.CY-8Ka_root):not(.CY-8Ka_root *):not(.XrJvXW_root):not(.XrJvXW_root *):not(._5OnbHa_root):not(._5OnbHa_root *):not(._root_luwio_9):not(._root_luwio_9 *){
        font-size:20px !important;
      }
"""

BODYBUMP = """
      /* dsh-body-font-bump（2026-09-22）：正文/气泡 +1 号（见上）*/
      .EvIC1a_scroll,
      .EvIC1a_scroll :where(:not(.lcKema_root):not(.lcKema_root *):not(.o3BgMG_root):not(.o3BgMG_root *):not(.CY-8Ka_root):not(.CY-8Ka_root *):not(.XrJvXW_root):not(.XrJvXW_root *):not(._5OnbHa_root):not(._5OnbHa_root *):not(._root_luwio_9):not(._root_luwio_9 *):not(.md-code-block):not(.md-code-block *)){
        font-size:20px !important;
      }
"""


CODEBLOCK = """
      /* dsh-codeblock-tight（2026-09-22）：代码块字号/行距压小。
         背景：实测代码块原本与正文同字号（25.2px、行高 35.28px），一屏放不下几行。
         用户要求"字号缩小、行间距缩小"。
         DOM（实测）：[class*="_markdown_"] > .md-code-block(._block_rsn9u_4)
                        > ._bannerWrap_rsn9u_24 + ._content_rsn9u_74
                        > pre._plain_rsn9u_103 > code
         取值：17px / 行高 22px（正文 24px / 33.6px 的 ~0.7 倍，代码块该比正文小）
         ⚠️ pre 的 16px padding 也一起收，否则四周留白仍占屏。 */
      /* ⚠️ 特异性坑（2026-09-22 实测踩到）：
         正文那条规则带了 12 个 :not()，特异性高达 (0,2,0)，
         会压过 .md-code-block pre 的 (0,1,1) —— 于是"代码块 17px"根本不生效。
         这里用 .EvIC1a_scroll 前缀把特异性提到 (0,3,1)+，确保赢。
         （同时本块在文件里排在正文规则之后，双保险。） */
      /* 2026-09-22 再压（用户："代码区域尽量最小，我甚至不需要看内容"）
         渲染值 = authored × 1.2（WebView 文本自动放大）：
           代码块  11px → 13.2px   （原 20.4px）
           行内代码 11px → 13.2px   （原 21px）
         行高全压到最小；内层 .line 是 shiki 切的 span，必须一并压
         （否则外层压了、内层仍撑 28.56px，实测踩到）。 */
      .EvIC1a_scroll .md-code-block pre,
      .EvIC1a_scroll .md-code-block pre code,
      .EvIC1a_scroll .md-code-block code,
      .EvIC1a_scroll .md-code-block pre code .line,
      .EvIC1a_scroll .md-code-block pre code span,
      .EvIC1a_scroll pre._plain_rsn9u_103,
      .EvIC1a_scroll pre._plain_rsn9u_103 code,
      .EvIC1a_scroll pre._plain_rsn9u_103 code .line,
      .EvIC1a_scroll pre._plain_rsn9u_103 code span,
      .EvIC1a_scroll ._content_rsn9u_74 pre,
      .EvIC1a_scroll ._content_rsn9u_74 code,
      pre._plain_rsn9u_103, pre._plain_rsn9u_103 code,
      pre._plain_rsn9u_103 code .line{
        font-size:11px !important; line-height:13px !important;
      }
      /* 行内代码（正文里 `xxx` 那种，实测 127 个）：同样压到最小
         DSH 原生是 .875em，在 24px 正文下渲染 21px——偏大。 */
      .EvIC1a_scroll [class*="_markdown_"] :not(pre) > code,
      .EvIC1a_scroll [class*="_markdown_"] :not(pre) > code *,
      [class*="_markdown_"] :not(pre) > code{
        font-size:11px !important; line-height:16px !important;
        padding:0 3px !important;
      }
      .EvIC1a_scroll .md-code-block pre{padding:2px 5px !important}
      .EvIC1a_scroll .md-code-block ._content_rsn9u_74{padding:0 !important}
      .EvIC1a_scroll .md-code-block ._bannerWrap_rsn9u_24{padding:0 !important;min-height:0 !important}
      .EvIC1a_scroll .md-code-block{margin-top:3px !important;margin-bottom:3px !important;border-radius:6px !important}

      /* dsh-table-tight（2026-09-22）：表格也压小。
         背景：表格原为正文 24px（用户圈出"类似这种区域也缩小"）。
         实测：table/th/td 全 24px、行高 24px、padding 上下1px 右16px
               —— 行距本就最小，偏大的是字号与"右侧 16px 留白导致换行"。
         取值：15px（渲染 18px）/ 行高 20px；右 padding 16 → 8px 让列更靠拢。
         ⚠️ 必须带 .EvIC1a_scroll 前缀：正文那条规则（已用 :where 降特异性到
            (0,1,0)）仍会设 20px，表格要更具体才能赢。 */
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
"""

# ── 2) 内容卡片与「思考」同字号（6.5px，text-size-adjust 160% 后渲染 9.6px）──
SHRINK_MARK = 'dsh-shrink-content'
SHRINK = """
      /* dsh-shrink-content（2026-09-22）：工具卡片（代码/读取/写入/编辑/搜索）+
         上下文注入 + bash + others（任务列表等），与「思考」同字号 6.5px（渲染 9.6px） */
      .o3BgMG_root, .o3BgMG_root *,
      .CY-8Ka_root[data-variant="bash"], .CY-8Ka_root[data-variant="bash"] *,
      .XrJvXW_root, .XrJvXW_root *,
      ._5OnbHa_root, ._5OnbHa_root *{font-size:6.5px !important;line-height:1 !important}
"""

# ── 3) 正文 1.4；内容块 + 思考 + 表格压到最小 ──
TIGHT_MARK = 'dsh-tight-blocks'
TIGHT = """
      /* dsh-tight-blocks:begin（2026-09-22 v4）
         ① 正文/气泡行距 1.4（保持正常）；
         ② 内容块（卡片/任务列表/工具调用条目）压到最小：
            - 卡片文字 + 「思考」都 line-height:1；
            - 卡片行高写死 24px（._row_luwio_16，文字只有 ~10px）→ 用 App 自带变量
              --dsh-content-font-delta:-9px：行高 24→15px、图标 16→7px、svg 14→5px，
              行内留白 14→5px（≈1/3）；再往下文字/图标会被 overflow:hidden 裁掉；
            - 变量只在卡片作用域里设，不碰正文（正文用 body 上的默认值，设 :root 会被 body 覆盖）；
            - 子调用 margin 4/2→1/1、row-gap 4→1px；消息行 margin 16→1px；
            - 块内段落/列表 margin 2px、pre 上下 padding 2px；
         ③ 表格行距也压到最小：th/td line-height 1（15.6px）、上下 padding 10px→1px。 */
      .EvIC1a_scroll *{line-height:1.4 !important}
      .lcKema_root, .lcKema_root *,
      .o3BgMG_root, .o3BgMG_root *,
      .CY-8Ka_root[data-variant="bash"], .CY-8Ka_root[data-variant="bash"] *,
      .XrJvXW_root, .XrJvXW_root *,
      ._5OnbHa_root, ._5OnbHa_root *{line-height:1 !important}
      .EvIC1a_scroll table, .EvIC1a_scroll table *{line-height:1 !important}
      .EvIC1a_scroll th, .EvIC1a_scroll td{padding-top:1px !important;padding-bottom:1px !important}
      .EvIC1a_scroll ._root_luwio_9,
      .lcKema_root, .o3BgMG_root, .CY-8Ka_root, .XrJvXW_root, ._5OnbHa_root{
        --dsh-content-font-size:6.5px !important;
        --dsh-content-font-size-secondary:6.5px !important;
        --dsh-content-font-delta:-9px !important;
      }
      .ztWv_q_subCalls{margin-top:1px !important;margin-bottom:1px !important;row-gap:1px !important}
      .EvIC1a_flowItem{margin-top:1px !important}
      .o3BgMG_root p, .o3BgMG_root ol, .o3BgMG_root ul, .o3BgMG_root li,
      .CY-8Ka_root p, .CY-8Ka_root ol, .CY-8Ka_root ul, .CY-8Ka_root li,
      .XrJvXW_root p, .XrJvXW_root ol, .XrJvXW_root ul, .XrJvXW_root li,
      ._5OnbHa_root p, ._5OnbHa_root ol, ._5OnbHa_root ul, ._5OnbHa_root li{margin-top:2px !important;margin-bottom:2px !important}
      .o3BgMG_root pre, .CY-8Ka_root pre, .XrJvXW_root pre, ._5OnbHa_root pre{padding-top:2px !important;padding-bottom:2px !important}
      /* dsh-tight-blocks:end */
"""

# ── 4) 新版「终端卡」（2026-09-23 用户截图反馈）────────────────────────────
TERMINAL_MARK = 'dsh-terminal-tight'
TERMINAL = """
      /* dsh-terminal-tight（2026-09-23）：新版「终端卡」（bash/命令输出）压小。
         背景：用户截图反馈"返回代码信息这块字号太大、行距太大"。
         实测（CDP 读 App WebView 真实 DOM 的 computed style）：
           头部 CY-8Ka_root 仍被旧规则覆盖，但**内容体被拆成了独立节点**：
             CY-8Ka_card > CY-8Ka_bodyWrap > _block_1gdtu_1.CY-8Ka_terminal
               > _header_1gdtu_32（_prompt_1gdtu_70 / _promptLine_1gdtu_78 /
                                    _cwd_1gdtu_108 / _command_1gdtu_116 /
                                    _runStateLabel_1gdtu_99 / _copyButton_1gdtu_143）
               > _output_1gdtu_156 > _line_1gdtu_180
           这些节点**不在 .CY-8Ka_root 之下** → 旧规则够不着
           → 继承正文 20px（渲染 24px / 行高 33.6px），一屏放不下几行。
         取值：10px / 行高 12px（渲染 ≈12px / 14.4px，约正文的一半）。
         特异性：html body 前缀 → (0,3,0)，稳赢正文那条 :where(...) 的 (0,1,0)。 */
      html body .EvIC1a_scroll .CY-8Ka_card,
      html body .EvIC1a_scroll .CY-8Ka_card *,
      html body .EvIC1a_scroll .CY-8Ka_bodyWrap,
      html body .EvIC1a_scroll .CY-8Ka_bodyWrap *,
      html body .EvIC1a_scroll .CY-8Ka_terminal,
      html body .EvIC1a_scroll .CY-8Ka_terminal *,
      html body .EvIC1a_scroll [class*="_1gdtu_"],
      html body .EvIC1a_scroll [class*="_1gdtu_"] *{
        font-size:10px !important; line-height:12px !important;
      }
      /* 卡内留白一起收（头部那条最占地方） */
      html body .EvIC1a_scroll ._header_1gdtu_32{padding:2px 6px !important;min-height:0 !important}
      html body .EvIC1a_scroll ._output_1gdtu_156,
      html body .EvIC1a_scroll ._line_1gdtu_180{padding-top:0 !important;padding-bottom:0 !important;margin:0 !important}
      html body .EvIC1a_scroll .CY-8Ka_card{margin-top:2px !important;margin-bottom:2px !important;border-radius:6px !important}
      /* 工具调用折叠组头（"32 次工具调用 · 5 条消息"）同属代码信息块：一起压小 */
      html body .EvIC1a_scroll [class*="l_V-RG_"]{font-size:10px !important;line-height:12px !important}
"""

# 插到 composer 补丁注入的 CSS 后面（patch_composer.py 先跑，保证这个锚点存在）
ANCHOR = '#dsh-composer-dot:hover{opacity:1}'

import re

# Body/code/table typography is owned by add_missing_rules.py, applied next.
rules = LEGACY[:LEGACY.index('      /* dsh-body-font-bump')] + SHRINK + TIGHT + TERMINAL
block = '<style id="dsh-font-rules">' + rules + '</style>'
for t in TARGETS:
    s = open(t, encoding='utf-8').read()
    pattern = r'<style id="dsh-font-rules">.*?</style>'
    if re.search(pattern, s, re.S):
        s = re.sub(pattern, lambda _: block, s, flags=re.S)
    else:
        s = s.replace('</head>', block + '</head>', 1)
    open(t, 'w', encoding='utf-8').write(s)
    print('patched:', t)
