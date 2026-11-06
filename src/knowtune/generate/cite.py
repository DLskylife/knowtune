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

# 写在句号**之后**的引用标记。中文写作里「……适应症。[1]」才是最自然的写法，
# 真实模型（本地 7B / DeepSeek）都这么写；而 SENT_SPLIT 按句号切，[1] 会落到
# 下一段的开头。后果是每一句都"没有引用"：实测一篇 5 句全带 [1] 的答案，
# 覆盖率算出 0.0、n_cited 算出 0，而支撑度又显示 grounded 0.6——同一篇答案
# 两个指标互相打架。这不是模型写错了，是分句器把引用算到了别人头上。
_LEADING_CITES = re.compile(r"^\s*(?:\[\d{1,2}\]\s*)+")


def split_sentences(text: str) -> list[str]:
    """按句切分，并把「句号后的引用编号」归回它真正标注的那一句。

    引用校验和支撑度都必须用这一份切法：两处各写一遍，就会一边认得出引用、
    另一边认不出，指标再次自相矛盾。
    """
    out: list[str] = []
    for piece in SENT_SPLIT.split(text or ""):
        if not piece or not piece.strip():
            continue
        m = _LEADING_CITES.match(piece)
        if m and out:
            out[-1] = out[-1].rstrip() + m.group(0).strip()
            piece = piece[m.end():]
            if not piece.strip():
                continue
        out.append(piece)
    return [s.strip() for s in out if s.strip()]

# 假引用的两种典型形态（2026-11-01 实测抓到的：问词库外的"甲信"，模型答
# "资料中未找到相关依据"却挂满 [1][2][3][4][5]，覆盖率照算 100%——指标被
# "礼貌性全引"轻松绕过，界面上"低置信"和"覆盖 100%"同时出现，信号自相矛盾）：
# 1. 对冲式引用：答案明说"没找到依据"，编号是装饰；
# 2. 笼统引用：单句连挂 3 个以上编号，等于"我全看了"，没有指向性。
HEDGE_RE = re.compile(
    r"未找到|未检索到|未出现|未提及|没有相关|资料中未|无相关依据|找不到相关|未提供具体"
)
RUN_RE = re.compile(r"(?:\[\d{1,2}\]\s*){3,}")

# 谈自己怎么引用的句子。
# 「以上信息均来自《……》资料。」这种句子不含事实主张，只描述引用动作，
# 放到分母里必然找不到语义支撑，等于每答一次自扣一分。原来只认「第 N 条资料」
# 这一种写法（见 support._REF_POINTER），换成"以上信息均来自"就漏了。
META_RE = re.compile(
    r"^(?:以上|上述|前述|本(?:文|回答|答案|次回答))[^。！？]{0,24}"
    r"(?:来自|出自|依据|参考|基于|根据)"
)


def is_fact_sentence(s: str) -> bool:
    """这句是不是一条可核对的事实主张。

    三类不算：
    - 引导语：以「：」结尾的空壳（「沙疗的适应证和禁忌证如下：」），真正的内容
      在它后面的列表里；把它算进分母，覆盖率永远差一句；
    - 元话语：见 META_RE；
    - 结构性套话：见 STRUCT_LEAD（"可能的原因"这类）。
    """
    if s.endswith(("：", ":")):
        return False
    if META_RE.match(s):
        return False
    return not s.startswith(STRUCT_LEAD)


STRUCT_LEAD = ("可能的原因", "如确认")


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
    sents = [s for s in split_sentences(answer) if len(s) >= 12]
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

    sentences = split_sentences(cleaned)
    # 只统计"事实性长句"：短句、"可能的原因"这类结构性文字不计入分母，
    # 否则覆盖率会被格式文字稀释成一个永远不及格的数字。
    #
    # 前缀表里原来还列了 "1." "2." "3." "-"——那是按"排除纯编号行"的意图写的，
    # 但 startswith 把**整条列表项**一起排掉了。真实模型几乎只用编号列表回答，
    # 于是分母只剩下开头那句和结尾那句（实测一篇 5 段的答案只认出 2 句）。
    # 纯编号行长度不到 25，早被下面的长度条件滤掉，这几条纯属误伤。
    factual = [s for s in sentences if len(s) >= 20 and is_fact_sentence(s)]
    uncited = [s for s in factual if not CITE_RE.search(s)]
    if factual:
        coverage = (len(factual) - len(uncited)) / len(factual)
    else:
        # 一句够长的都没有：要么是"好的，我明白了"这种寒暄，要么是把编号挂在
        # 碎片上（「未找到依据[1][2][3]」）。两种都不该白拿 100%。原来这里
        # 兜底成 `1.0 if ids else 0.0`，于是"每句都短、每句都挂编号"这一路
        # 完全绕过指标——比没有指标更糟，因为它看起来是满的。
        coverage = 0.0

    # 引用存疑的两种形态。
    #
    # 对冲式改成**句级**判定。原来是全文扫一遍 hedge 词、只要有任何编号就把
    # 覆盖率强制归零——而最理想的回答恰恰长这样：
    #   「资料中未找到沙疗治白癜风的依据。」          （无编号，本来就该无）
    #   「白癜风资料里写的是药物和光疗[1][2]。」      （有编号，真指涉）
    # 全文匹配把这种答案判成"装饰引用"，等于专门惩罚说实话的模型。
    # 现在只认"编号挂在没依据的那句上"：句子自己一边说没有、一边挂 [n]。
    hedged = [s for s in factual if HEDGE_RE.search(s)]
    solid = [s for s in factual if CITE_RE.search(s) and not HEDGE_RE.search(s)]
    blanket: str | None = None
    if not factual and ids:
        blanket = "答案里没有可统计的完整事实句（句子都太短），编号却不少，覆盖率按 0 计"
    if hedged and not solid and ids:
        blanket = "引用编号挂在「未找到依据」这类句子上，是装饰不是证据，覆盖率按 0 计"
        coverage = 0.0
        uncited = factual
    elif hedged:
        # 自证"没有依据"的句子不需要编号，也不该算缺口。
        covered = len(solid) + len(hedged)
        coverage = covered / len(factual)
        uncited = [s for s in factual if not CITE_RE.search(s) and s not in hedged]
    if blanket is None:
        # 连续 ≥3 个编号本身不是罪证：逐条指明出处的长句也会这样。
        # 真正可疑的是"编号堆成串、句子却没剩多少内容"，那才是"我全看了"。
        for s in sentences:
            if RUN_RE.search(s) and len(CITE_RE.sub("", s).strip()) < 25:
                blanket = "单句连挂 3 个以上引用编号且几乎没内容（笼统引用），指向性存疑"
                break

    return CiteReport(
        cleaned=cleaned,
        cited_ids=sorted({i for i in ids if i not in invalid}),
        invalid_ids=invalid,
        uncited_sentences=uncited[:5],
        coverage=round(coverage, 3),
        blanket=blanket,
    )
