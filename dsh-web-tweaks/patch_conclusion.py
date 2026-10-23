#!/usr/bin/env python3
"""
patch_conclusion.py —— 「结尾结论卡」样式（2026-09-22）

═══ 需求 ═══
用户两次澄清后定稿：
  · 回复太长、推理过程不想看那么细
  · **不是**结论优先 —— 结论要放**最后**
  · 结论用「像弹窗那种样式」突出显示
  · 这样就不需要额外的总结弹窗了

═══ 实现 ═══
1) 人设（~/.dsh/profiles/web/cordis.patch.yml 的 personaPrefix）要求回答
   **永远以一条 markdown 引用块收尾**，格式：
       > **结论**：一句话
       >
       > 下一步：做什么
2) 本补丁把 `._markdown_kcgor_5 blockquote`（原生元素，无混淆类名，
   实测规则 `border-left:2px solid var(--dsw-alias-label-caption);padding-left:14px`）
   渲染成深色卡片 —— 与总结弹窗同一套配色：
       #22252b 深蓝灰底 / #4d8dff 蓝竖条 / #ffffff 主字 / #c8cdd4 次字

═══ 已排除的坑 ═══
· 代码块内的文字原本是深色，落在深色卡片上会看不见
  → 用颜色反转（filter:invert(1) hue-rotate(180deg)）保住 shiki 的词法配色。
  （推测：卡里应很少出现代码；若观感不对可改用浅色--bg+白字方案。）
· 卡片内的 hr/marker 等继承色一并调亮。
"""
import os

TARGETS = [
    os.path.expanduser('~/.dsh/profiles/node_modules/@deepseek-ai/dsh/node_modules/@deepseek-ai/dsh-web-frontend/dist/index.html'),
    '/usr/local/lib/node_modules/@deepseek-ai/dsh/node_modules/@deepseek-ai/dsh-web-frontend/dist/index.html',
]

MARK = 'dsh-conclusion-card'

CSS = """
      /* dsh-conclusion-card（2026-09-22）：把回答末尾的引用块渲染成深色「结论卡」。
         与总结弹窗（dsh-recap）同一套配色：底 #22252b / 竖条 #4d8dff / 字白。
         人设里要求每轮回答都以 > **结论**：… 的引用块收尾。 */
      .EvIC1a_scroll ._markdown_kcgor_5 blockquote{
        background:#22252b !important;
        color:#ffffff !important;
        border:none !important;
        border-left:3px solid #4d8dff !important;
        border-radius:10px !important;
        padding:12px 15px !important;
        margin:14px 0 4px !important;
        box-shadow:0 2px 10px rgba(0,0,0,.10) !important;
        font-size:15px !important;
        line-height:23px !important;
      }
      /* 卡内文字：主字纯白（结论行靠 <strong> 加粗更醒目），次字米白 */
      .EvIC1a_scroll ._markdown_kcgor_5 blockquote,
      .EvIC1a_scroll ._markdown_kcgor_5 blockquote p,
      .EvIC1a_scroll ._markdown_kcgor_5 blockquote li{
        color:#c8cdd4 !important;
      }
      .EvIC1a_scroll ._markdown_kcgor_5 blockquote strong,
      .EvIC1a_scroll ._markdown_kcgor_5 blockquote b{
        color:#ffffff !important;
        font-weight:600 !important;
      }
      .EvIC1a_scroll ._markdown_kcgor_5 blockquote a{color:#8ab4ff !important}
      .EvIC1a_scroll ._markdown_kcgor_5 blockquote p{margin:0 !important}
      .EvIC1a_scroll ._markdown_kcgor_5 blockquote p + p{margin-top:6px !important}
      .EvIC1a_scroll ._markdown_kcgor_5 blockquote hr{
        border:none !important; border-top:1px solid rgba(255,255,255,.14) !important;
        margin:8px 0 !important;
      }
      /* 卡内代码：深底上原本的深色代码看不见 → 颜色反转，保住 shiki 词法配色 */
      .EvIC1a_scroll ._markdown_kcgor_5 blockquote pre,
      .EvIC1a_scroll ._markdown_kcgor_5 blockquote code{
        filter:invert(1) hue-rotate(180deg) !important;
      }
"""


def main():
    for t in TARGETS:
        if not os.path.exists(t):
            print('skip (not found):', t)
            continue
        s = open(t, encoding='utf-8').read()
        if MARK in s:
            print('already patched:', t)
            continue
        # 插到我们 style 块的末尾（用 rail 样式里最后一个标记定位）
        marker = '#dsh-composer-dot:hover{opacity:1}'
        if marker not in s:
            print('anchor missing:', t)
            continue
        s = s.replace(marker, marker + CSS, 1)
        open(t, 'w', encoding='utf-8').write(s)
        print('patched:', t, len(s), 'chars')


if __name__ == '__main__':
    main()
