#!/usr/bin/env python
"""打分台总入口：一个端口，两个页签。

    evals/.venv/Scripts/python.exe evals/scoreboard.py
    # http://127.0.0.1:7860

1. **A/B 双模型盲测**（arena.py）——换模型有没有变好，看胜率。
2. **单模型多答 + DPO**（sampling.py）——同一个模型采样多次，打分挑出
   chosen/rejected 做成偏好对，同时产出模型级评测报告。

**两个页签都不预设场景。** 评什么由「评测档案」决定（`profiles.py`）：
提示词、评分维度、对话角色名、题库、规则体检词。内置「预问诊（医疗）」与
「通用问答」两份，`evals/profiles/*.json` 可以加任意任务（写作、代码、客服、
合同审阅），不用改代码。

两个页面共用一个进程，但会话文件分开存：状态结构不一样，混在一起会互相覆盖。
"""

from __future__ import annotations

import argparse

import gradio as gr

import skin
from arena import app as ab_app
from sampling import build as build_sampling


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=7860)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    demo = gr.TabbedInterface(
        [ab_app, build_sampling()],
        ["A/B 双模型盲测", "单模型多答 + DPO"],
        title="模型打分台",
    )
    demo.launch(server_name=args.host, server_port=args.port,
                inbrowser=not args.no_browser, theme=skin.THEME, css=skin.CSS)


if __name__ == "__main__":
    main()
