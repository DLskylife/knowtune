"""打分台的深色皮肤：近黑底 + 明确的框。

上一版是 Gradio 默认的 Soft 主题——白底、无边框的输入框浮在灰白面板上，看起来
像一张没画完的表。这里做两件事，和「模型评测」那张站点卡用同一套语言：

1. **近黑**（`#05070a`）打底，面板抬到 `#0b1017`，靠 1px `#1c2532` 的框划边界，
   不再靠"留白"分层——深色下留白只会变成一团黑。
2. **每个区块都有容器**：折叠面板是一张卡、标题条带分隔线、输入框有描边和聚焦
   光晕、按钮分主次（渐变 / 描边）。框不是为了好看，是为了让人一眼看出
   "这一格是一个字段"。

颜色只覆盖 Gradio 自己的 CSS 变量 + 少量结构规则，不碰组件内部结构——
升级 Gradio 时最多是某个框少了描边，不会整页塌掉。
"""

from __future__ import annotations

import sys

import gradio as gr

# Gradio 5 和 6 的主题注入点是两套：5.x 只认 `Blocks(theme=, css=)` 构造参数，
# 6.x 改成了 `launch(theme=, css=)`。这个项目按 6.x 写，装 5.x 会在别处抛一堆
# `unexpected keyword argument`，报错位置离真因很远。这里提前拦住，说清用哪个解释器。
_MAJOR = int(gr.__version__.split(".")[0])
if _MAJOR < 6:
    raise SystemExit(
        f"[skin] 需要 Gradio 6，当前 {gr.__version__}（解释器 {sys.executable}）。\n"
        "       打分台要用 evals 自己的 venv：\n"
        "       evals\\.venv\\Scripts\\python.exe evals\\scoreboard.py --port 7860"
    )

BG = "#05070a"
PANEL = "#0b1017"
SUNK = "#070b11"
LINE = "#1c2532"
LINE_SOFT = "#22303f"
TEXT = "#e6edf5"
MUTED = "#8296ac"
CYAN = "#22d3ee"
BLUE = "#3b82f6"

# font 只能传 gr.themes.Font 对象，传字符串列表会在 launch() 里比较主题时炸
# （`'str' object has no attribute 'name'`），而且是打完 "Running on local URL"
# 之后才炸——页面能开、点一下就 "Connection to the server was lost"。
# 字体改由下面的 CSS 指定，没必要为这点事去构造 Font。
THEME = gr.themes.Base(
    primary_hue=gr.themes.colors.cyan,
    neutral_hue=gr.themes.colors.slate,
    radius_size=gr.themes.sizes.radius_md,
)

CSS = f"""
/* ── 底色：变量层 ───────────────────────────────────────────────
   Gradio 6 把主题变量挂在 .gradio-container 上，改这里就够，
   不必去猜每个组件内部的 svelte 类名。 */
.gradio-container {{
  --body-background-fill: {BG};
  --background-fill-primary: {BG};
  --background-fill-secondary: {PANEL};
  --block-background-fill: {PANEL};
  --block-border-color: {LINE};
  --block-border-width: 1px;
  --block-radius: 12px;
  --block-shadow: none;
  --panel-background-fill: {PANEL};
  --panel-border-color: {LINE};
  --input-background-fill: {SUNK};
  --input-background-fill-focus: #0a1119;
  --input-border-color: {LINE_SOFT};
  --input-border-color-focus: {CYAN};
  --input-border-width: 1px;
  --input-radius: 8px;
  --body-text-color: {TEXT};
  --body-text-color-subdued: {MUTED};
  --border-color-primary: {LINE};
  --border-color-accent: {CYAN};
  --color-accent: {CYAN};
  --color-accent-soft: rgba(34, 211, 238, .12);
  --link-text-color: #67e8f9;
  --slider-color: {CYAN};
  --checkbox-background-color-selected: {CYAN};
  --table-border-color: {LINE};
  --table-even-background-fill: #0a0f16;
  --shadow-drop: none;
  --shadow-drop-lg: none;
}}

html, body, gradio-app, .gradio-container {{
  background-color: {BG};
  color: {TEXT};
  font-family: system-ui, "Segoe UI", "Microsoft YaHei", sans-serif;
  /* 关键：range 的未填充轨道、number 的上下箭头、select 弹层都由它决定，
     不设的话滑块右半边会是浏览器默认的白灰条，在近黑底上像一道裂缝。 */
  color-scheme: dark;
}}
/* 顶部两团极淡的角光，避免整页像一张纯黑图 */
.gradio-container {{
  background-image:
    radial-gradient(1100px 380px at 12% -8%, rgba(34, 211, 238, .075), transparent 62%),
    radial-gradient(900px 340px at 90% -12%, rgba(59, 130, 246, .075), transparent 60%);
  background-repeat: no-repeat;
  max-width: 1440px;
}}

.gradio-container h1, gradio-app > h1 {{
  font-size: 19px;
  font-weight: 640;
  letter-spacing: .4px;
  color: #f2f8fd;
  margin: 6px 0 14px;
  padding-bottom: 12px;
  border-bottom: 1px solid {LINE};
}}
.gradio-container h2, .gradio-container h3, .gradio-container h4 {{ color: #eaf2fa; }}
.gradio-container p, .gradio-container li, .gradio-container span {{ color: inherit; }}

/* ── 页签 ── */
.tab-container {{
  background: #080d14;
  border: 1px solid {LINE};
  border-radius: 11px;
  padding: 4px;
  gap: 4px;
  width: 100%;
}}
.tab-container button {{
  border: 1px solid transparent;
  border-radius: 8px;
  color: {MUTED};
  font-weight: 560;
  padding: 7px 14px;
  transition: background .15s, color .15s, box-shadow .15s;
}}
.tab-container button:hover {{ color: #b9cade; background: rgba(255, 255, 255, .03); }}
.tab-container button.selected {{
  color: #eafcff;
  background: linear-gradient(180deg, rgba(34, 211, 238, .18), rgba(59, 130, 246, .14));
  box-shadow: inset 0 0 0 1px rgba(34, 211, 238, .38);
}}

/* ── 折叠面板 = 一张卡 ── */
.gradio-container .block.gr-accordion {{
  background: {PANEL};
  border: 1px solid {LINE};
  border-radius: 12px;
  overflow: hidden;
  padding: 0;
}}
.gradio-container .block.gr-accordion > .label-wrap {{
  padding: 11px 16px;
  margin: 0;
  font-weight: 600;
  font-size: 13.5px;
  letter-spacing: .25px;
  color: #dbe7f3;
  background: linear-gradient(180deg, rgba(255, 255, 255, .03), rgba(255, 255, 255, 0));
}}
.gradio-container .block.gr-accordion > .label-wrap:hover {{ background: rgba(255, 255, 255, .045); }}
.gradio-container .block.gr-accordion > .label-wrap.open {{
  border-bottom: 1px solid {LINE};
  color: #f0f7fd;
}}
/* ① 里面还嵌了三块题库来源，同色同框会看成"卡片套卡片"。
   内层压暗一档，层次就出来了。 */
.gradio-container .block.gr-accordion .block.gr-accordion {{
  background: #080d14;
  border-color: #182231;
}}

/* 展开后的内容区：左右留白，别让字段贴着卡片边 */
.gradio-container .block.gr-accordion > .label-wrap ~ div {{ padding: 14px 16px 16px; }}

/* ── 普通块：也给一圈很淡的描边，让「一格就是一块」 ── */
.gradio-container .block:not(.gr-accordion):not(.hide-container) {{
  border-radius: 10px;
}}

/* ── 输入控件 ── */
.gradio-container .input-container {{
  background: {SUNK};
  border: 1px solid {LINE_SOFT};
  border-radius: 8px;
  transition: border-color .15s, box-shadow .15s;
}}
.gradio-container .input-container:focus-within {{
  border-color: {CYAN};
  box-shadow: 0 0 0 3px rgba(34, 211, 238, .13);
}}
.gradio-container .input-container textarea,
.gradio-container .input-container input {{
  background: transparent;
  color: {TEXT};
  font-size: 13px;
  line-height: 1.7;
}}
.gradio-container input[type="text"], .gradio-container input[type="number"] {{
  font-size: 13px;
}}
.gradio-container textarea::placeholder, .gradio-container input::placeholder {{ color: #55677c; }}

/* 下拉：收起态与展开层 */
.gradio-container .wrap-inner {{
  background: {SUNK};
  border: 1px solid {LINE_SOFT};
  border-radius: 8px;
}}
.gradio-container .wrap-inner:focus-within,
.gradio-container .wrap-inner:focus {{ border-color: {CYAN}; }}
.gradio-container ul.options {{
  background: #0b1119;
  border: 1px solid {LINE_SOFT};
  border-radius: 9px;
  box-shadow: 0 14px 34px rgba(0, 0, 0, .55);
}}
.gradio-container ul.options li {{ color: {TEXT}; }}
.gradio-container ul.options li:hover,
.gradio-container ul.options li.selected {{ background: rgba(34, 211, 238, .13); }}

/* ── 按钮：主按钮渐变实底，次按钮描边 ── */
.gradio-container button.primary {{
  background: linear-gradient(135deg, {CYAN} 0%, {BLUE} 100%);
  border: 1px solid rgba(34, 211, 238, .45);
  color: #04121a;
  font-weight: 660;
  letter-spacing: .2px;
}}
.gradio-container button.primary:hover {{ filter: brightness(1.07); }}
.gradio-container button.secondary {{
  background: #0f1620;
  border: 1px solid {LINE_SOFT};
  color: #cfdceb;
  font-weight: 550;
}}
.gradio-container button.secondary:hover {{
  background: #16202c;
  border-color: #2c3d4f;
  color: #eaf4fd;
}}
.gradio-container button.sm, .gradio-container button.lg {{ border-radius: 8px; }}

/* 滑块：轨道交给 color-scheme，这里只钉颜色和拇指大小 */
.gradio-container input[type=range] {{
  accent-color: {CYAN};
  color-scheme: dark !important;  /* 未走的那半截轨道由元素自身的 color-scheme 决定 */
  height: 18px;
  cursor: pointer;
}}
/* 不自定义 track：Gradio 自己按 value 画"已走/未走"两段，
   一旦手动铺背景，青色进度就没了，只剩一个孤零零的拇指。
   未走的那半截用的是 --neutral-200（#e2e8f0），深色下像一道白裂缝，
   所以在滑块容器里局部把它压暗——不要全局改，那是边框和分隔线的色。 */
.gradio-container .slider_input_container {{
  --neutral-200: {LINE};
  --neutral-100: #131b25;
}}
.gradio-container input[type=range]::-webkit-slider-thumb {{
  -webkit-appearance: none;
  width: 14px;
  height: 14px;
  margin-top: -4.5px;
  border-radius: 50%;
  background: #eafcff;
  border: 2px solid {CYAN};
  box-shadow: 0 0 0 3px rgba(34, 211, 238, .16);
}}

/* 单选/复选：Gradio 自己的未选中态是白底 + 浅灰边，深色页面上两颗白点
   比选中的那颗还亮，看上去像三个都选中了。这里把空态压回深底。 */
.gradio-container input[type=radio],
.gradio-container input[type=checkbox] {{
  background: #0a1119 !important;
  border: 1.5px solid #2c3d4f !important;
}}
.gradio-container input[type=radio]:checked,
.gradio-container input[type=checkbox]:checked {{
  background: {CYAN} !important;
  border-color: #06b6d4 !important;
}}
/* 选中后的内点原本是白的，青底白心扎眼；换成近黑，跟主按钮同一个调子 */
.gradio-container input[type=radio]:checked::after,
.gradio-container input[type=checkbox]:checked::after {{
  background: #04121a !important;
}}
.gradio-container input[type=radio]:hover,
.gradio-container input[type=checkbox]:hover {{ border-color: #3d5470 !important; }}

.gradio-container label.selected {{
  background: rgba(34, 211, 238, .14) !important;
  border: 1px solid rgba(34, 211, 238, .45);
  color: #b6f4ff !important;
}}
.gradio-container label:not(.selected) {{
  background: transparent;
  border: 1px solid {LINE};
  color: {MUTED};
}}
.gradio-container label:not(.selected):hover {{
  border-color: #2c3d4f;
  color: #c8d8e8;
}}

/* 行内代码、引用、表格
   Gradio 的 --code-background-fill 默认是白的，深色下 `safe` 会变成一块
   白砖头、字还看不见——变量和规则都得压一遍。 */
.gradio-container {{ --code-background-fill: #0f1722; }}
.gradio-container code, .gradio-container .prose code {{
  background: #0f1722 !important;
  color: #7dd3fc !important;
  border: 1px solid {LINE};
  border-radius: 5px;
  padding: 1px 5px;
  font-size: 12px;
}}
.gradio-container blockquote {{
  border-left: 2px solid {CYAN};
  background: rgba(34, 211, 238, .06);
  margin: 8px 0;
  padding: 6px 12px;
  color: #b9cade;
}}
.gradio-container table {{ border-color: {LINE}; }}
.gradio-container th {{ background: #0f1620; color: #dbe7f3; }}

/* 文件上传框 */
.gradio-container .file-preview, .gradio-container .upload-container {{
  border: 1px dashed #2a3b4d;
  border-radius: 10px;
  background: #080d14;
}}

/* ── 状态徽标 ──────────────────────────────────────────────────
   Gradio 给 error/warning/info 各自配了 *-50 的浅底（#fef2f2 / #fef9c3 /
   #eff6ff），在近黑页面上是一贴一贴的亮片——一屏报十二个错时整页都花了。
   统一换成同一深色系：底色压到 10% 亮度，字提亮到 300 档。 */
.gradio-container .error,
.gradio-container span.error,
.gradio-container label.error {{
  background: #2a1015 !important;
  border: 1px solid #5e2230 !important;
  color: #fda4af !important;
}}
.gradio-container .warning,
.gradio-container span.warning,
.gradio-container label.warning {{
  background: #241a08 !important;
  border: 1px solid #574018 !important;
  color: #fcd34d !important;
}}
.gradio-container .success,
.gradio-container span.success,
.gradio-container label.success {{
  background: #0b1f16 !important;
  border: 1px solid #1c5138 !important;
  color: #6ee7b7 !important;
}}
.gradio-container .info,
.gradio-container span.info,
.gradio-container label.info {{
  background: #0c1a24 !important;
  border: 1px solid #1d4157 !important;
  color: #7dd3fc !important;
}}

/* toast：三种变体各压一档，标题与正文跟着走 */
.gradio-container .toast-body {{
  background: {PANEL} !important;
  border: 1px solid {LINE} !important;
  color: {TEXT} !important;
  box-shadow: 0 14px 34px rgba(0, 0, 0, .6) !important;
}}
.gradio-container .toast-body.error {{ background: #1c0d12 !important; border-color: #5e2230 !important; }}
.gradio-container .toast-body.warning {{ background: #1e1608 !important; border-color: #574018 !important; }}
.gradio-container .toast-body.success {{ background: #0a1a13 !important; border-color: #1c5138 !important; }}
.gradio-container .toast-title,
.gradio-container .toast-message-text,
.gradio-container .toast-title-row span {{ color: {TEXT} !important; }}
.gradio-container .toast-icon svg {{ color: #8296ac; }}
.gradio-container .toast-icon.error svg {{ color: #fda4af; }}
.gradio-container .toast-icon.warning svg {{ color: #fcd34d; }}
.gradio-container .toast-icon.success svg {{ color: #6ee7b7; }}
.gradio-container .toast-close {{ color: {MUTED} !important; background: transparent !important; }}
.gradio-container .timer {{ background: #22303f !important; }}

/* 进度条与转圈：默认那条浅灰在深底上像没渲染完 */
.gradio-container .progress-bar,
.gradio-container .progress-level-inner {{ background: {CYAN} !important; }}

/* 页脚别亮过正文 */
footer {{ opacity: .38; }}
footer:hover {{ opacity: .8; }}

/* 滚动条 */
::-webkit-scrollbar {{ width: 10px; height: 10px; }}
::-webkit-scrollbar-track {{ background: {BG}; }}
::-webkit-scrollbar-thumb {{ background: #1b2634; border-radius: 6px; border: 2px solid {BG}; }}
::-webkit-scrollbar-thumb:hover {{ background: #2a3a4c; }}

/* 打分台的候选回答：等宽、可读 */
.cand textarea {{ font-family: ui-monospace, Consolas, monospace; font-size: 13px; line-height: 1.65; }}
#casehead h3, #samphead h3 {{ margin-bottom: 2px; color: #f0f7fd; }}
/* 题库手输框：默认 12px 行高偏挤，中文题面读起来像糊在一起 */
.casebox textarea {{ font-size: 13px; line-height: 1.8; }}
/* 三个小工具按钮不要各占三分之一宽，那是把空白当成了功能 */
.case-tools {{ max-width: 520px; gap: 8px; }}
"""
