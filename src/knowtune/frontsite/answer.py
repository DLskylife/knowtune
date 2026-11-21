"""前台答案的展示后处理。

这里不参与检索或模型调用，只清理模型常见的检索报告式收尾，
让站点请求处理保持在 HTTP 和业务编排层。
"""

from __future__ import annotations

import re


_SUMMARY_OPENERS = (
    "综上所述", "总之", "总的来说", "总的来看", "总体来说", "总体来看",
    "概括起来", "概括来说", "简而言之", "综上",
)
_REPORT_STARTS = (
    "根据资料", "资料显示", "资料中提到", "资料中未找到", "资料里没有",
    "资料并未", "资料主要", "资料未提及", "以上资料", "资料不足以",
)
_ID_RE = re.compile(r"\[\d+\]")


def tidy_answer(text: str) -> str:
    """删除没有引用且没有实际内容的检索报告句和末尾总结。"""
    if not text.strip():
        return text
    sentences = re.split(r"(?<=[。！？])", text)
    kept = [
        sentence for sentence in sentences
        if not (
            sentence.strip()
            and sentence.strip().startswith(_REPORT_STARTS)
            and len(sentence.strip()) <= 100
            and not _ID_RE.search(sentence)
        )
    ]
    out = "".join(kept)
    if not out.strip():
        return text

    paragraphs = out.split("\n\n")
    while len(paragraphs) > 1:
        tail = paragraphs[-1].strip()
        if len(tail) <= 160 and tail.startswith(_SUMMARY_OPENERS):
            paragraphs.pop()
            continue
        break

    out = "\n\n".join(paragraphs).rstrip()
    start = max(0, len(out) - 160)
    tail = out[start:]
    hit = None
    for opener in _SUMMARY_OPENERS:
        index = tail.find(opener)
        if index >= 0 and (
            index == 0
            or tail[:index].strip()
            and tail[:index].rstrip()[-1] in "。！？\n]"
        ):
            hit = index if hit is None else min(hit, index)
    if hit is not None:
        out = out[: start + hit].rstrip()
    return out or text
