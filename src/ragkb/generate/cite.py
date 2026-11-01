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

# 假引用的两种典型形态（2026-11-01 实测抓到的：问词库外的"甲信"，模型答
# "资料中未找到相关依据"却挂满 [1][2][3][4][5]，覆盖率照算 100%——指标被
# "礼貌性全引"轻松绕过，界面上"低置信"和"覆盖 100%"同时出现，信号自相矛盾）：
# 1. 对冲式引用：答案明说"没找到依据"，编号是装饰；
# 2. 笼统引用：单句连挂 3 个以上编号，等于"我全看了"，没有指向性。
HEDGE_RE = re.compile(
    r"未找到|未检索到|未出现|未提及|没有相关|资料中未|无相关依据|找不到相关|未提供具体"
)
RUN_RE = re.compile(r"(?:\[\d{1,2}\]\s*){3,}")


@dataclass
class CiteReport:
    cleaned: str
    cited_ids: list[int] = field(default_factory=list)
    invalid_ids: list[int] = field(default_factory=list)
    uncited_sentences: list[str] = field(default_factory=list)
    coverage: float = 0.0
    # 非空 = 引用存疑的原因，直接给界面展示，别让用户自己对 0%/100% 犯疑心
    blanket: str | None = None

    @property
    def ok(self) -> bool:
        return not self.invalid_ids


def verbatim_ratio(answer: str, sources: list[str]) -> float:
    """答案里"逐字照抄资料"的句子占比。

    小模型（本地 7B 及以下）常常不听"不要复述原文"那一条：把检索到的块整段
    搬出来，连块里的 OCR 乱码、条目编号、页眉一起抄。用户看到的是"直接截断
    的文本"，而不是答案——这比拒答更伤信任，因为界面上看着像正常回答。
    这里量化它，供界面标注，不改写答案（校验器只判不改）。
    """
    if not answer or not sources:
        return 0.0
    sents = [s.strip() for s in SENT_SPLIT.split(answer) if len(s.strip()) >= 12]
    if not sents:
        return 0.0
    pool = [c for c in sources if c]
    hit = 0
    for s in sents:
        if any(s in c for c in pool):
            hit += 1
    return hit / len(sents)


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

    # 引用存疑的两种形态。对冲式直接把覆盖率归零：模型自己都说没依据了，
    # 这些编号不构成任何证据支撑，留着 100% 只会跟"低置信"提醒打架。
    blanket: str | None = None
    if HEDGE_RE.search(answer) and ids:
        blanket = "回答声称资料中未找到依据，引用编号是装饰，覆盖率按 0 计"
        coverage = 0.0
        uncited = factual or uncited
    elif RUN_RE.search(answer):
        blanket = "单句连挂 3 个以上引用编号（笼统引用），指向性存疑"

    return CiteReport(
        cleaned=cleaned,
        cited_ids=sorted({i for i in ids if i not in invalid}),
        invalid_ids=invalid,
        uncited_sentences=uncited[:5],
        coverage=round(coverage, 3),
        blanket=blanket,
    )
