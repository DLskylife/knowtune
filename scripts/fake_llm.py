"""假的 OpenAI 兼容端点，专门用来验证 RAG 的「生成 → 引用 → 支撑度」这一段。

不是给真人看的回答生成器。它只做一件事：把**送进来的资料**原样抄一句出来
并挂上引用编号，或者故意编一句带数字的假话。两种模式各验证一条链路：

    good  抄资料 → 支撑度应判 supported，引用编号应被保留
    bad   编数字 → 支撑度应判 weak/unsupported，锚点应抓到数字对不上

    python fake_llm.py --port 18080 --mode good
    python fake_llm.py --port 18080 --mode bad
"""

from __future__ import annotations

import argparse
import json
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODE = "good"


def pick_evidence(prompt: str) -> list[str]:
    """从 prompt 里把编号资料捞出来。knowtune 的上下文形如 [1] 正文…"""
    blocks = re.split(r"\n(?=\[\d+\])", prompt)
    out = []
    for b in blocks:
        m = re.match(r"\[(\d+)\]\s*(.+)", b, re.S)
        if m:
            out.append((m.group(1), m.group(2).strip()))
    return out


def build_answer(prompt: str) -> str:
    ev = pick_evidence(prompt)
    if not ev:
        return "资料里没有找到相关内容，无法回答这个问题。"
    n, text = ev[0]
    # 取资料里的第一个完整句子：这正是"贴着资料说"的形态
    sent = re.split(r"(?<=[。！？])", text.replace("\n", ""))[0][:120]
    if MODE == "good":
        # 引用要紧贴句末标点之前——这才是模型的正常写法。写成「……疗程。[1]。」
        # 会让引用标记单独成句，被"事实句≥25字"的规则筛掉，覆盖率假性归零。
        body = sent.rstrip("。！？") 
        return f"根据资料，{body}[{n}]。以上内容出自第 {n} 条资料。"
    # bad：主题词全对、数字全错。余弦对这一层近乎无感，只有锚点能抓。
    fake = sent.replace("15 分钟", "45 分钟").replace("30 天", "7 天")
    if fake == sent:
        fake = sent + "每次治疗需要 200 毫升药液，连续 90 天为一个疗程。"
    body = fake.rstrip("。！？")
    return f"根据资料，{body}[{n}]。"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # 静音
        pass

    def do_GET(self):
        body = json.dumps({"data": [{"id": "fake-rag", "object": "model"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        req = json.loads(self.rfile.read(n) or b"{}")
        prompt = "\n".join(m.get("content", "") for m in req.get("messages", []))
        answer = build_answer(prompt)
        if not req.get("stream"):
            body = json.dumps({
                "id": "fake", "object": "chat.completion", "model": "fake-rag",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": answer},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": len(prompt), "completion_tokens": len(answer),
                          "total_tokens": len(prompt) + len(answer)},
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        step = 12
        for i in range(0, len(answer), step):
            chunk = {
                "id": "fake", "object": "chat.completion.chunk", "model": "fake-rag",
                "choices": [{"index": 0, "delta": {"content": answer[i:i + step]},
                             "finish_reason": None}],
            }
            self.wfile.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode())
            self.wfile.flush()
            time.sleep(0.01)
        tail = {"id": "fake", "object": "chat.completion.chunk", "model": "fake-rag",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": len(prompt), "completion_tokens": len(answer),
                          "total_tokens": len(prompt) + len(answer)}}
        self.wfile.write(f"data: {json.dumps(tail, ensure_ascii=False)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=18080)
    ap.add_argument("--mode", choices=["good", "bad"], default="good")
    a = ap.parse_args()
    MODE = a.mode
    print(f"fake LLM on :{a.port} mode={MODE}")
    ThreadingHTTPServer(("127.0.0.1", a.port), Handler).serve_forever()
