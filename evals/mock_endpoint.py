"""假的 OpenAI 兼容端点：没有真模型时用来试手 / 演示 / 回归。

两件事要同时成立，否则打分台的问题会被 mock 掩盖：

1. **能拉开差距**——同一个问题要给出"好 / 中 / 差"三档回答，不然人打分时
   五个滑块全按 3，报告里每条极差都是 0，看不出统计到底算没算对。
2. **样本要不一样**——K 次采样如果逐字相同，相似度就是 1.00，DPO 对子会被
   全部剔除。这是真实存在的退化场景，但演示时不该只有这一种。

所以按**全局请求计数**轮转三档（不是按对话轮次：同一条问题重采样时轮次相同，
轮转不了）。模型名含 `base` 的一律走差档，方便 A/B 页一眼看出盲测有没有生效。

    evals/.venv/Scripts/python.exe evals/mock_endpoint.py --port 18080
"""

from __future__ import annotations

import argparse
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

RED_FLAGS = ("出血", "吐血", "呕血", "咯血", "胸痛", "胸口", "出汗", "意识", "抽搐",
             "炸开", "头痛", "晕", "抬不起来", "含糊", "紫", "喘不上")
QUESTIONS = {
    "胸口": "这种胸闷是压榨样的吗？持续多久了？",
    "咳嗽": "咳嗽多久了？痰是什么颜色的？",
    "肚子": "疼在哪个位置？是持续疼还是一阵阵的？",
    "发烧": "烧到多少度？烧了几天？",
    "头晕": "是天旋地转还是昏沉沉的？站起来会更晕吗？",
    "皮疹": "疹子是从哪里开始起的？有没有呼吸困难？",
    "尿": "小便时有灼痛吗？有没有发烧或腰痛？",
    "腰": "腿有没有发麻或者没力气？",
    "心慌": "心慌是突然开始还是慢慢加重的？持续多久？",
    "孩子": "孩子多大了？现在精神怎么样？",
}

# 通用任务的兜底：三档长短与结构都不同，方便演示"极差 / 相似度 / DPO 对子"。
GENERIC = [
    "简短版：这件事的结论取决于前提条件，条件变了结论也会变。",
    "先说结论，再说理由：这类问题没有单一答案，要看具体场景和约束。"
    "如果你能补充上下文（目标是什么、限制了哪些条件），我可以给出更有针对性的方案。",
    "这个问题可以从三个角度看：一是定义边界，二是常见误区，三是落地时的取舍。"
    "三方面都说清楚之后，答案往往会自己浮现出来；如果只说结论而不说前提，"
    "换一个场景就不成立了。顺带一提，这条回答故意写得比较长，用来演示「啰嗦」被扣分。",
]


def reply(model: str, messages: list[dict[str, str]], variant: int) -> str:
    user_turns = [m["content"] for m in messages if m["role"] == "user"]
    last = user_turns[-1] if user_turns else ""
    asked = sum(1 for m in messages if m["role"] == "assistant")
    bad = "base" in model.lower()

    hit_red = next((k for k in RED_FLAGS if k in last), "")
    key = next((k for k in QUESTIONS if k in last), "")
    medical = bool(hit_red or key)

    if bad:
        # 差的示范：结论先行 + 一口气问五个问题 + 顺便推荐药
        if hit_red:
            return (f"你这个很可能是{hit_red}相关的严重问题，也可能是心梗或者主动脉夹层。"
                    "你多大年纪？有没有高血压糖尿病？抽烟吗？疼多久了？以前有过吗？"
                    "建议先吃点硝酸甘油或者阿司匹林看看。")
        if medical:
            return ("初步判断是普通炎症，问题不大。你多大？多久了？有没有发烧？"
                    "吃过什么药？有没有过敏？家里有人这样吗？先吃点消炎药观察。")
        return GENERIC[2]

    if not medical:
        return GENERIC[variant % len(GENERIC)]

    # 好的示范：一次一问，红旗先升级
    if hit_red and (asked >= 1 or "出血" in last or "抽搐" in last):
        return ("这个情况不能再等了，请立刻拨打 120 或让人送你去最近医院急诊，路上不要自己开车。"
                if asked >= 2 else f"先确认一件事：{QUESTIONS.get(hit_red[:2], '现在还在持续吗？')}")
    if hit_red:
        return f"先确认一下：{QUESTIONS.get(hit_red[:2], '这个症状是什么时候开始的？')}"

    if key and asked == 0:
        return QUESTIONS[key]
    if asked == 1:
        return "好的，记下了。之前有过类似情况吗？"
    return "了解了。请问你现在有没有在吃的药，或者药物过敏？"


class Handler(BaseHTTPRequestHandler):
    counter = 0

    def do_POST(self):  # noqa: N802
        size = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(size) or b"{}")
        Handler.counter += 1
        text = reply(body.get("model", ""), body.get("messages") or [], Handler.counter - 1)
        out = json.dumps({
            "id": "mock", "object": "chat.completion",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                         "finish_reason": "stop"}],
        }, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def do_GET(self):  # noqa: N802
        out = json.dumps({"object": "list",
                          "data": [{"id": "mock-base"}, {"id": "mock-doctor"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, fmt, *args):
        if not re.search(r"200", fmt % args):
            print(fmt % args)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=18080)
    ap.add_argument("--host", default="127.0.0.1")
    a = ap.parse_args()
    print(f"mock endpoint on http://{a.host}:{a.port}/v1")
    ThreadingHTTPServer((a.host, a.port), Handler).serve_forever()
