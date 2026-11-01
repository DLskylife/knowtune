"""剥掉模型吐出来的思考块（2026-10-28）。

vLLM 上跑的思考型模型会把推理过程包在 <think>…</think> 里一起返回。这段东西
直接进答案会有两个后果：

  1. 用户看到一堆自言自语，而不是答案；
  2. 更阴的——草稿是中文、容器是 RTL 的维吾尔语时，这段中文被双向算法一搅，
     在界面上就是一坨看起来"倒着"的乱字。之前有人在状态栏看到倒着的中文，
     根子就在这儿。

放在 LLM 层做，stream / complete 两条路都盖住，调用方不用各自记得处理。
"""

from __future__ import annotations

_OPEN = "<think>"
_CLOSE = "</think>"

# 流式时标签可能被切在两个 chunk 之间，所以每次留一段尾巴不吐，
# 等下一个 chunk 来了再判断这是不是一个完整的开/闭标签。
_HOLD = len(_OPEN) - 1


def _find(haystack: str, needle: str) -> int:
    """大小写不敏感查找——模型偶尔会写 <Think>。"""
    return haystack.lower().find(needle)


class ThinkFilter:
    """增量过滤：feed 进去原始 chunk，吐出剥掉思考块之后的内容。"""

    def __init__(self) -> None:
        self._in_think = False
        self._buf = ""

    def feed(self, chunk: str) -> str:
        if not chunk:
            return ""
        self._buf += chunk
        out: list[str] = []
        while True:
            if self._in_think:
                idx = _find(self._buf, _CLOSE)
                if idx < 0:
                    # 闭合标签还没来，只留下可能是半个标签的尾巴
                    self._buf = self._buf[-_HOLD:] if len(self._buf) > _HOLD else self._buf
                    break
                self._buf = self._buf[idx + len(_CLOSE):]
                self._in_think = False
                continue
            idx = _find(self._buf, _OPEN)
            if idx >= 0:
                out.append(self._buf[:idx])
                self._in_think = True
                self._buf = self._buf[idx + len(_OPEN):]
                continue
            # 没开标签却来了闭合标签：要么是模板在提示里偷偷开了思考块，
            # 要么是模型吐脏字。两种情况下这个标签本身都不该给用户看。
            close = _find(self._buf, _CLOSE)
            if close >= 0:
                self._buf = self._buf[:close] + self._buf[close + len(_CLOSE):]
                continue
            if len(self._buf) > _HOLD:
                out.append(self._buf[:-_HOLD])
                self._buf = self._buf[-_HOLD:]
            break
        return "".join(out)

    def flush(self) -> str:
        """流结束时把尾巴放出来。思考块没闭合 = 模型整段都在推理，整段丢弃。"""
        tail = "" if self._in_think else self._buf
        self._buf = ""
        self._in_think = False
        return tail


def strip(text: str | None) -> str:
    """非流式场景：一次性剥干净。"""
    if not text or "<" not in text:
        return text or ""
    f = ThinkFilter()
    return f.feed(text) + f.flush()


def filtered(chunks):
    """把一串 chunk 过一遍过滤器，yield 出非空的部分。"""
    f = ThinkFilter()
    for c in chunks:
        piece = f.feed(c)
        if piece:
            yield piece
    tail = f.flush()
    if tail:
        yield tail
