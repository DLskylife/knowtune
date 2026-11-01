"""跨模块传递的数据结构。刻意保持轻量：不会有业务逻辑混进来。"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class Block:
    """抽取出来的结构块。分块器只认它，不认原始文件格式。"""

    text: str
    page_no: int | None = None
    heading_path: str | None = None
    kind: str = "para"  # para | heading | code | table


@dataclass(slots=True)
class LoadedDoc:
    text: str
    blocks: list[Block]
    title: str
    n_pages: int | None
    extractor: str
    meta: dict = field(default_factory=dict)


@dataclass(slots=True)
class ChunkDraft:
    seq: int
    text: str
    text_seg: str
    token_count: int
    page_no: int | None
    heading_path: str | None
    char_start: int
    char_end: int
    content_hash: str


@dataclass(slots=True)
class Hit:
    """一路检索的原始命中。score 的量纲因路而异，融合层只用 rank。"""

    chunk_id: int
    score: float
    rank: int = 0
    label: str = ""


@dataclass(slots=True)
class Candidate:
    chunk_id: int
    doc_id: int
    doc_title: str
    source_uri: str
    page_no: int | None
    heading_path: str | None
    text: str
    dense_rank: int | None = None
    sparse_rank: int | None = None
    dense_score: float | None = None
    sparse_score: float | None = None
    rrf_score: float = 0.0
    rerank_score: float | None = None
    final_rank: int = 0
    # 由"邻块扩展"补进来的（不是自己检索命中的）。UI 上要能看出来，
    # 否则用户会以为这条是模型自己找到的强相关证据。
    expanded: bool = False


@dataclass(slots=True)
class RefusalDecision:
    refused: bool
    reason: str | None
    top_score: float | None
    margin: float | None
    calibrated: bool
    # 「答了，但别太当真」。单靠一个阈值只有两种结局：要么把正确问题拒掉，
    # 要么放进来一堆不沾边的内容。所以拆成两件事——硬拒只拦"明显不沾边"，
    # 中间地带照答但标记低置信，把判断权交回给用户。
    low_confidence: bool = False
    confidence_reason: str | None = None


@dataclass(slots=True)
class Citation:
    n: int
    chunk_id: int
    doc_title: str
    page_no: int | None
    heading_path: str | None
    preview: str
