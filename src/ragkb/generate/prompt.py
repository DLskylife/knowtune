"""上下文装填与提示词。

两条硬规则：
1. 超预算时**按相关度从低到高丢整条**，绝不从中间截断单条——
   截断会产生"半句话"，模型一定会用想象补全后半句，这是最隐蔽的幻觉来源。
2. 编号一旦确定就不再重排。前端点击 [3] 必须能精确回到第 3 条，
   中途重排会让引用指错地方，比没有引用更坏。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .. import promptcfg
from ..lang import answer_instruction
from ..models import Candidate, Citation
from . import evidence as evidence_rule

# 提示词资产的实际归属在 ragkb/promptcfg.py（它要同时管 generate 与 translate
# 两个角色、以及后台可编辑的覆盖层）。这里保留同名符号，是为了别处 `from
# ragkb.generate.prompt import SYSTEM_PROMPT_FALLBACK / PROMPT_DIR` 的老引用不断。
PROMPT_DIR = promptcfg.PROMPT_DIR
SYSTEM_PROMPT_FALLBACK = promptcfg.SYSTEM_PROMPT_FALLBACK

REFUSE_TEMPLATE = (
    "当前知识库中未找到足够依据回答这个问题。\n\n"
    "可能的原因：\n"
    "1. 相关知识还没有导入到这个知识库；\n"
    "2. 问题表述与文档用词差异较大（可以换一种说法再试）；\n"
    "3. 检索到的内容相关性不足，不足以支撑可靠回答。\n\n"
    "如确认文档已导入，可在右侧检索调试面板查看实际召回的片段。"
)


@dataclass
class ContextBundle:
    system_prompt: str
    user_prompt: str
    citations: list[Citation] = field(default_factory=list)
    used: int = 0
    dropped: int = 0
    tokens_used: int = 0
    # 真正装进【资料】的块原文。used 是**个数**（int），拿不到内容——照抄检测
    # 要逐句比对原文，只能另存一份。别改成把 used 变成列表：它的 int 语义
    # 已经写进 done 事件的 context_used，前端按数字渲染。
    used_texts: list[str] = field(default_factory=list)

    def messages(self, question: str, history: list[dict] | None = None) -> list[dict]:
        msgs: list[dict] = [{"role": "system", "content": self.system_prompt}]
        for h in history or []:
            msgs.append({"role": h["role"], "content": h["content"]})
        msgs.append({"role": "user", "content": self.user_prompt})
        return msgs


def load_system_prompt(version: str, answer_lang: str | None = None,
                       provider: str = "") -> str:
    """取系统提示。回答语言不是中文时，优先用对应的语言版本（qa_ug.md）。

    语言指令不写进提示文件里、而是在后面追加，是因为提示文件是版本化的资产，
    而"这个站点回答什么语言"是运行期实例的配置——两者耦合在一起，改个站点
    语言就得动提示版本，回滚时也会一起被卷进去。

    `provider` 决定要不要取该后端专属的覆盖（后台「模型 → 提示词」里配的）。
    传空串就是"角色级/内置"这一路，老调用点的行为不变。
    """
    return promptcfg.system_prompt(version, answer_lang, provider)


def _header(n: int, c: Candidate) -> str:
    bits = [f"[{n}]", f"《{c.doc_title}》"]
    if c.page_no:
        bits.append(f"第 {c.page_no} 页")
    if c.heading_path:
        bits.append(c.heading_path)
    return " · ".join(bits)


def build_context(
    candidates: list[Candidate],
    *,
    question: str,
    token_budget: int,
    prompt_version: str = "qa_v1",
    history_text_tokens: int = 0,
    answer_lang: str | None = None,
    evidence: str | None = None,
    provider: str = "",
    min_context_chars: int = 200,
) -> ContextBundle:
    # provider 只影响"取哪一份 system 提示"，后面两段追加（语言指令、证据例外）
    # 仍由运行期决定——它们是行为约束，不该被一段可编辑的文本覆盖掉。
    system_prompt = load_system_prompt(prompt_version, answer_lang, provider)
    if answer_lang:
        system_prompt += answer_instruction(answer_lang)
    # 证据不足时必须在系统提示里点名作废"禁止外部知识"那条，写在用户消息里不管用
    if evidence and evidence != "high":
        system_prompt += evidence_rule.override(evidence)
    # 地板值与 chatctx 共用 generation.min_context_chars：两边若各写一个数，
    # 前端提示的"还剩多少"和真正装填的结果就会对不上。
    floor = max(1, int(min_context_chars))
    budget = max(floor, token_budget - history_text_tokens)

    used: list[tuple[int, Candidate]] = []
    spent = 0
    for idx, cand in enumerate(candidates, start=1):
        cost = cand_tokens(cand)
        if spent + cost > budget and used:
            break
        used.append((idx, cand))
        spent += cost

    if not used and candidates:
        # 一条都装不下时的兜底：宁可只放第一条并截断，也不要给模型空上下文。
        # 截断长度就是 budget（已 >= floor），不再另设 100 的魔法数——
        # 那样在 floor < 100 时反而会超出预算。
        first = candidates[0]
        truncated = first.text[:budget]
        first = Candidate(**{**first.__dict__, "text": truncated})
        used = [(1, first)]
        spent = cand_tokens(first)

    blocks: list[str] = []
    citations: list[Citation] = []
    for n, cand in used:
        blocks.append(f"{_header(n, cand)}\n{cand.text}")
        citations.append(
            Citation(
                n=n,
                chunk_id=cand.chunk_id,
                doc_title=cand.doc_title,
                page_no=cand.page_no,
                heading_path=cand.heading_path,
                preview=cand.text[:160],
            )
        )

    dropped = len(candidates) - len(used)
    tail = (
        "请依据上述资料作答，并按要求标注来源编号。"
        if (not evidence or evidence == "high")
        # 资料不够硬时不能再催它"依据资料"——那就等于逼它拿不沾边的资料硬凑
        else "请按系统提示中的【本轮例外】条款作答：资料里有的带 [n] 编号，"
             "资料里没有的用自身知识答完并标明。"
    )
    user_prompt = (
        "【资料】\n"
        + "\n\n".join(blocks)
        + "\n\n【问题】\n"
        + question
        + "\n\n"
        + tail
    )
    return ContextBundle(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        citations=citations,
        used=len(used),
        dropped=dropped,
        tokens_used=spent,
        used_texts=[cand.text for _, cand in used],
    )


def cand_tokens(c: Candidate) -> int:
    from ..ingest.chunker import estimate_tokens

    return estimate_tokens(c.text) + 12
