"""引用校验：把"模型说它引用了"变成"确认它引用了"。

三件事：
1. 越界编号剥离——模型偶尔会编出 [7]，此时上下文只有 5 条；
2. 覆盖率统计——有多少事实句是带引用的，这是 F3.2 唯一可量化的指标；
3. 不做重写。校验器只删不改，任何改写都等于二次创作。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

CITE_RE = re.compile(r"\[(\d{1,2})\]")
SENT_SPLIT = re.compile(r"(?<=[。！？!?\n])")


@dataclass
class CiteReport:
    cleaned: str
    cited_ids: list[int] = field(default_factory=list)
    invalid_ids: list[int] = field(default_factory=list)
    uncited_sentences: list[str] = field(default_factory=list)
    coverage: float = 0.0

    @property
    def ok(self) -> bool:
        return not self.invalid_ids


def verify(answer: str, n_context: int) -> CiteReport:
    ids = [int(m.group(1)) for m in CITE_RE.finditer(answer)]
    invalid = sorted({i for i in ids if i < 1 or i > n_context})

    cleaned = answer
    if invalid:
        bad = {str(i) for i in invalid}
        cleaned = CITE_RE.sub(
            lambda m: "" if m.group(1) in bad else m.group(0), answer
        )

    sentences = [s.strip() for s in SENT_SPLIT.split(cleaned) if s.strip()]
    # 只统计"事实性长句"：短句、"可能的原因"这类结构性文字不计入分母，
    # 否则覆盖率会被格式文字稀释成一个永远不及格的数字。
    factual = [
        s
        for s in sentences
        if len(s) >= 25 and not s.startswith(("可能的原因", "如确认", "1.", "2.", "3.", "-"))
    ]
    uncited = [s for s in factual if not CITE_RE.search(s)]
    coverage = (len(factual) - len(uncited)) / len(factual) if factual else (1.0 if ids else 0.0)

    return CiteReport(
        cleaned=cleaned,
        cited_ids=sorted({i for i in ids if i not in invalid}),
        invalid_ids=invalid,
        uncited_sentences=uncited[:5],
        coverage=round(coverage, 3),
    )
