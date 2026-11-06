"""分块：结构优先，字符兜底。

为什么要结构感知：中文技术文档里一个论点常常横跨 3-5 个自然段，按固定窗口
硬切会把结论和前提劈开，向量化之后两边都不像任何问题。先按标题/段落聚合，
只有单段超长时才退化为字符切分。

为什么分块策略比换模型更重要：换嵌入模型通常带来 3-5 个点的召回变化，
chunk size 从 480 调到 240 能带来 10-20 个点的变化（且方向不确定）。
所以 chunk 参数必须进评估集回归，不能凭手感。
"""

from __future__ import annotations

import hashlib
import re

from ..config import ChunkingCfg
from ..models import Block, ChunkDraft

SENT_END = "。！？；!?;\n"


def estimate_tokens(text: str) -> int:
    """中英混合的粗略估算：CJK 约 1 字 1 token，其他约 4 字符 1 token。

    不用 tiktoken：它面向 GPT 系列词表，对中文会高估 30%+，
    反而让上下文预算算不准。真要精确就用模型自带 tokenizer，S2 再说。
    """
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    other = len(text) - cjk
    return cjk + ((other + 3) // 4)


def _hash(text: str, size: int, overlap: int) -> str:
    payload = f"{text}|s={size}|o={overlap}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _hard_split(text: str, size: int) -> list[str]:
    """单段超过 size 时按句子边界切，实在没有边界才按字符切。

    用游标滑动，不要 `rest = rest[cut:]`：那样每切一块就把剩余全文复制一遍，
    总代价是 n²/(2·size)。一份 1473 万字的书按 480 字切，光复制就 2×10¹¹ 字符，
    实测 4M 字要 17 秒、1M 字只要 1.4 秒——平方增长，大文件直接卡死。
    """
    parts: list[str] = []
    n = len(text)
    i = 0
    while n - i > size:
        window = text[i : i + size]
        cut = max(window.rfind(ch) for ch in SENT_END)
        if cut < size // 2:  # 边界太靠前等于没切，宁可硬切
            cut = size
        else:
            cut += 1
        parts.append(text[i : i + cut])
        i += cut
    if i < n:
        parts.append(text[i:])
    return parts


def chunk_blocks(blocks: list[Block], cfg: ChunkingCfg) -> list[ChunkDraft]:
    drafts: list[ChunkDraft] = []
    buf: list[str] = []
    buf_len = 0
    buf_page: int | None = None
    buf_heading: str | None = None
    cursor = 0
    strategy = (getattr(cfg, "strategy", "size") or "size").strip().lower()
    if strategy not in ("size", "heading", "paragraph"):
        strategy = "size"

    def emit(body: str, page: int | None, heading: str | None, start: int) -> None:
        body = body.strip()
        if not body:
            return
        drafts.append(
            ChunkDraft(
                seq=len(drafts),
                text=body,
                text_seg="",  # 由 sparse 索引层填，切分器不关心分词实现
                token_count=estimate_tokens(body),
                page_no=page,
                heading_path=heading,
                char_start=start,
                char_end=start + len(body),
                content_hash=_hash(body, cfg.size, cfg.overlap),
            )
        )

    def flush(keep_overlap: bool = True) -> None:
        nonlocal buf, buf_len, buf_page, buf_heading, cursor
        if not buf:
            return
        body = "\n\n".join(buf)
        emit(body, buf_page, buf_heading, cursor)
        cursor += len(body)
        # overlap：留上一块尾部作下一块前缀，保证跨块语义连续。
        # 但必须从句子边界起——从字符中间切出来的半句话会直接污染向量，
        # 让这块永远匹配不到任何完整的问题。
        # paragraph 模式不重叠：那种模式下"一块 = 一个段落"是用户明确要的边界，
        # 再拖一段尾巴进去等于把别人的段落缝到自己身上。
        tail = body[-cfg.overlap :] if (cfg.overlap > 0 and keep_overlap) else ""
        for i, ch in enumerate(tail):
            if ch in SENT_END:
                tail = tail[i + 1 :]
                break
        buf = [tail] if tail.strip() else []
        buf_len = len(tail) if tail.strip() else 0
        buf_page = None
        buf_heading = None

    for block in blocks:
        piece = block.text.strip()
        if not piece:
            continue

        # 章节边界：heading 策略每次都断，size 策略只在已经装得差不多时才断。
        # 后者是为了不让一个 50 字的小节把上一块的 430 字硬拆开——块太碎，
        # 检索命中后拿到的证据就只剩半句话。
        if buf and buf_heading is not None and block.heading_path != buf_heading:
            if strategy == "heading":
                flush()
            elif strategy == "size" and cfg.respect_headings and buf_len >= cfg.size * 0.6:
                flush()

        pieces = _hard_split(piece, cfg.size) if len(piece) > cfg.size else [piece]
        for sub in pieces:
            over = buf_len + (2 if buf else 0) + len(sub) > cfg.size
            if buf and over:
                flush()
                # flush 之后 buf 里是上一块留下的 overlap 尾巴，尾巴 + 新块可能
                # 仍然超长（实测 80+480=560 > 512 的嵌入窗口，向量化时被悄悄
                # 截断）。此时尾巴让位给正文：丢掉它重开一块，损失一点跨块
                # 连续性，换"每一块都完整进向量"。尾巴的内容上一块里已经有了，
                # 这不是丢数据。
                if buf and buf_len + (2 if buf else 0) + len(sub) > cfg.size:
                    buf, buf_len = [], 0
                    buf_page, buf_heading = None, None
            # 注意用 buf_heading is None 判断而不是 not buf：flush 后 buf 里可能
            # 只剩 overlap 尾巴，此时 heading 仍需取当前块，否则整块会丢章节归属
            if buf_heading is None:
                buf_page = block.page_no
                buf_heading = block.heading_path
            buf.append(sub)
            buf_len += (2 if len(buf) > 1 else 0) + len(sub)

        if strategy == "paragraph":
            flush(keep_overlap=False)

    if buf:
        body = "\n\n".join(buf).strip()
        # 尾块太短就并进前一块，避免产生"孤立标题"这种纯噪声 chunk
        if drafts and 0 < len(body) < cfg.min_chunk:
            prev = drafts[-1]
            merged = f"{prev.text}\n\n{body}"
            drafts[-1] = ChunkDraft(
                seq=prev.seq,
                text=merged,
                text_seg="",
                token_count=estimate_tokens(merged),
                page_no=prev.page_no,
                heading_path=prev.heading_path,
                char_start=prev.char_start,
                char_end=prev.char_start + len(merged),
                content_hash=_hash(merged, cfg.size, cfg.overlap),
            )
        else:
            emit(body, buf_page, buf_heading, cursor)

    # seq 必须连续，否则 UNIQUE(doc_id, generation, seq) 会挡住重新索引
    for i, d in enumerate(drafts):
        d.seq = i
    return drafts


def repair_chunk_primitives(text: str) -> str:
    """去掉控制字符与超长空白，它们会污染 FTS 分词并撑爆 token 估算。"""
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
    text = re.sub(r"[ \t]{3,}", "  ", text)
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    return text.strip()
