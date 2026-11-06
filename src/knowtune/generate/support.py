"""答案支撑度：把「引用了」变成「引到了」。

## 为什么需要它

`cite.py` 算的覆盖率只验证格式：这句话里有没有一个方括号数字，编号有没有
越界。模型写「GPU 推理需要 A100[1]」，只要上下文里存在 [1]，覆盖率就算它
被支持了——哪怕 [1] 讲的是节点调度。于是「覆盖率 100%」和「整段胡说」可以
同时成立，界面上两个信号互相打架。

支撑度换一个问法：**这句话的意思，在它引用的那段资料里找得到吗？**

做法是把句子和它引用的块各自编码，算余弦。够高 = 这段资料确实在说这件事；
够低 = 编号挂上去了，内容没对上。这一项不依赖任何大模型，本地嵌入模型就够，
所以它能进每次问答的运行时链路，而不是只在离线评测里存在。

## 两个指标，别混

- `score`   只看带引用的句子：引用块是否真的支撑它。答「引用对不对」。
- `grounded` 看全部事实句：每句能否在证据里找到支撑（没引用的句子允许在
  全部块里找）。答「答案整体有没有落地」。

`score` 低 + `grounded` 高 = 内容有据但编号乱挂，格式问题；
两个都低 = 内容本身没落地，这才是该担心的。

## 第三个信号：事实锚点

主题相邻的问题（库里有 K8s、问的是 GPU 推理）里，答案会带着一堆具体事实：
型号、版本号、命令行参数、显存规格。这些「锚点」如果没在证据原文里出现过，
基本就是模型拿通用知识补的。锚点比对是纯字符串操作，零成本，专治这种
「话题对得上、事实对不上」的情况。
"""

from __future__ import annotations

import re
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Callable, Mapping, Sequence

from .cite import CITE_RE, SENT_SPLIT

# 进统计的最短句长。比 cite.py 的 25 字低：支撑度要抓的恰恰是「短句里塞一个
# 具体结论」这种形态，门槛定高了等于把它放过去。
FACT_MIN_CHARS = 12
# 结构性文字不算事实句，否则「如确认」「可能的原因」会把分数稀释掉。
# 只收**纯引导语**：「建议你直接上 A100」这种句子带着具体主张，必须计入——
# 把它当结构文字跳过，等于放过一句最容易编的内容。
STRUCT_PREFIX = ("可能的原因", "如确认", "1.", "2.", "3.", "-")

# 事实锚点：这些 token 出现在答案里、却不在证据里，就是「模型自己补的」。
_EN_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9_.\-]{1,}")
_VERSION = re.compile(r"\d+\.\d+(?:\.\d+)*")
_NUM_UNIT = re.compile(r"\d+(?:\.\d+)?\s*(?:%|GB|MB|TB|KB|ms|核|张|块|节点|路|条)")
_CLI_FLAG = re.compile(r"--[A-Za-z][\w-]*")
# 文件名不是事实锚点：答案里写《xxx.pdf》而证据正文不含文件名，是正常的，
# 不该算"编造"。第一版实测就被 luckclub.pdf 这类误报拉低了锚点命中率。
_FILE_NAME = re.compile(r"\.(pdf|docx?|xlsx?|pptx?|md|txt|csv|json|html?|zip)$", re.I)
# 送进编码器的块长度上限。块常被切成多窗口再平均，一次问答要多付几百毫秒
# （实测 5 块 + 7 句要 1.2 秒）。判定"这句话有没有被这块支撑"用不到整块——
# 答案句对应的通常就是其中一段，首 600 字（约一个 512 token 窗口）足够。
BLOCK_CHARS = 600

# 块向量缓存。同一批文档会被反复问到，缓存命中后这块成本归零。
# 按文本哈希而不是 chunk_id：文档重切后 id 会变，文本没变就不该重算。
_CACHE: "OrderedDict[str, object]" = OrderedDict()
_CACHE_MAX = 256


def _cached(keys: Sequence[str], encode: Callable[[Sequence[str]], Sequence[Sequence[float]]]):
    """编码并缓存。只缓存证据块，答案句每次都不同，缓存没有意义。"""
    import numpy as np

    todo = [k for k in keys if k not in _CACHE]
    if todo:
        fresh = np.asarray(encode(todo), dtype=np.float32)
        for i, k in enumerate(todo):
            _CACHE[k] = fresh[i]
            if len(_CACHE) > _CACHE_MAX:
                _CACHE.popitem(last=False)
    out = np.asarray([_CACHE[k] for k in keys], dtype=np.float32)
    return out


def _norm(text: str) -> str:
    """比对用的归一：小写、去空白、去千分位逗号。

    证据原文和答案里的空格差异（PDF 抽出来的文本空格很随机）不该影响判定，
    否则「A100 80GB」和「A10080GB」会被当成两个东西。
    """
    return re.sub(r"\s+", "", text.lower()).replace(",", "")


def split_facts(answer: str) -> list[str]:
    """切出事实句。带不带引用都算——`grounded` 要覆盖整个答案。

    剔掉两类不是句子的东西：
    - OCR 碎片：PDF 抽出来的标题常带字间空格（「古 籍 典 藏 · 原 文」）。它的
      相似度天然偏低，留着会把分母撑大、把分数压低，看起来像"答案没依据"；
    - 结构性引导语：见 STRUCT_PREFIX。
    """
    out: list[str] = []
    for raw in SENT_SPLIT.split(answer or ""):
        s = raw.strip()
        if len(s) < FACT_MIN_CHARS:
            continue
        if s.startswith(STRUCT_PREFIX):
            continue
        if s.count(" ") / len(s) > 0.25:
            continue
        out.append(s)
    return out


def anchors_of(text: str) -> list[str]:
    """抽事实锚点。只收「具体到能被证伪」的形态。

    普通数字（「3 个步骤」）刻意不收：它们既高频又没区分度，收进来只会让
    缺失率恒为正、指标失去意义。

    按长度去重：`80GB` 会同时被英文 token 和数字单位两条规则抓到，再被拆出
    一个 `GB` 碎片。碎片留在列表里会让"缺失 4 个"看起来比实际严重。
    """
    toks: list[str] = []
    for pat in (_CLI_FLAG, _VERSION, _NUM_UNIT, _EN_TOKEN):
        toks.extend(m.group(0).strip() for m in pat.finditer(text or ""))

    uniq = sorted({t for t in toks if len(_norm(t)) >= 2}, key=len, reverse=True)
    out: list[str] = []
    for tok in uniq:
        if _FILE_NAME.search(tok):
            continue
        norm = _norm(tok)
        if any(norm in _norm(kept) for kept in out):
            continue
        out.append(tok)
    return out


def _cited_ids(sentence: str) -> list[int]:
    return [int(m.group(1)) for m in CITE_RE.finditer(sentence)]


@dataclass(slots=True)
class SupportReport:
    score: float | None = None          # 带引用句中被支撑的比例
    grounded: float | None = None        # 全部事实句中有证据支撑的比例
    n_facts: int = 0
    n_cited: int = 0
    n_supported: int = 0
    threshold: float = 0.55
    anchors: list[str] = field(default_factory=list)
    missed: list[str] = field(default_factory=list)
    anchor_rate: float | None = None
    weak: list[dict] = field(default_factory=list)
    skipped: str | None = None
    # 阈值本身就是经验值，没标定过就别说成结论。界面据此决定要不要加「参考」二字。
    calibrated: bool = False

    def verdict(self) -> str:
        if self.skipped or self.grounded is None:
            return "unknown"
        anchor_ok = self.anchor_rate is None or self.anchor_rate >= 0.8
        if self.grounded >= 0.6 and anchor_ok:
            return "grounded"
        if self.grounded >= 0.3:
            return "weak"
        return "unsupported"

    def as_dict(self) -> dict:
        return {
            "score": None if self.score is None else round(self.score, 3),
            "grounded": None if self.grounded is None else round(self.grounded, 3),
            "n_facts": self.n_facts,
            "n_cited": self.n_cited,
            "n_supported": self.n_supported,
            "threshold": self.threshold,
            "anchors": self.anchors[:20],
            "missed": self.missed[:20],
            "anchor_rate": None if self.anchor_rate is None else round(self.anchor_rate, 3),
            "weak": self.weak[:3],
            "verdict": self.verdict(),
            "calibrated": self.calibrated,
            "skipped": self.skipped,
        }


def evaluate(
    answer: str,
    evidence: Mapping[int, str],
    encode: Callable[[Sequence[str]], Sequence[Sequence[float]]],
    *,
    threshold: float = 0.55,
    max_sentences: int = 24,
    calibrated: bool = False,
    min_answer_chars: int = 40,
) -> SupportReport:
    """算一句话有没有被它引用的资料接住。

    evidence 是 `{引用编号: 块原文}`，编号与答案里的 [n] 对应。
    encode 由调用方注入（本地嵌入模型 / 测试里的假编码器），本模块不绑任何后端。
    """
    report = SupportReport(threshold=threshold, calibrated=calibrated)
    text = (answer or "").strip()
    if len(text) < min_answer_chars:
        report.skipped = "答案太短，不判定"
        return report
    if not evidence:
        report.skipped = "没有可用证据"
        return report

    facts = split_facts(text)[:max_sentences]
    if not facts:
        report.skipped = "没有可判定的事实句"
        return report

    # 锚点先算：纯字符串，不依赖模型。模型加载失败时它仍然有效。
    ev_norm = _norm(" ".join(evidence.values()))
    report.anchors = anchors_of(text)
    report.missed = [a for a in report.anchors if _norm(a) not in ev_norm]
    if report.anchors:
        report.anchor_rate = 1 - len(report.missed) / len(report.anchors)

    report.n_facts = len(facts)

    # 一次编码：句子每次都不一样，块走缓存。块只取前 BLOCK_CHARS——
    # 超窗的块会被切成多窗口逐个编码再平均，一次问答要多付几百毫秒，
    # 而"这句有没有被这块支撑"用不到整块。
    import numpy as np

    pool_ids = sorted(evidence)
    pool_texts = [(evidence[i] or "")[:BLOCK_CHARS] for i in pool_ids]
    try:
        sent_arr = np.asarray(encode(list(facts)), dtype=np.float32)
        pool_arr = _cached(pool_texts, encode)
    except Exception as exc:  # 模型没装/加载失败：降级到只有锚点信号
        report.skipped = f"无法计算语义支撑（{type(exc).__name__}）"
        return report

    if sent_arr.ndim != 2 or len(sent_arr) != len(facts) or pool_arr.ndim != 2:
        report.skipped = "编码结果与输入数量不符"
        return report
    # 归一化，让点积等于余弦。这里自己归一而不是信任后端：
    # 有的编码器 normalize=False，混用会让分数跨过 1。
    sent_vecs = sent_arr / np.maximum(np.linalg.norm(sent_arr, axis=1, keepdims=True), 1e-9)
    pool_vecs = pool_arr / np.maximum(np.linalg.norm(pool_arr, axis=1, keepdims=True), 1e-9)
    sims = sent_vecs @ pool_vecs.T

    supported = 0
    grounded_hits = 0
    weak: list[dict] = []
    for idx, sent in enumerate(facts):
        ids = _cited_ids(sent)
        if ids:
            cols = [pool_ids.index(i) for i in ids if i in evidence]
        else:
            cols = list(range(len(pool_ids)))
        if not cols:
            # 引用的编号全越界：cite.verify 已经把编号剥掉了，这里按「无支撑」处理
            best, best_id = 0.0, None
        else:
            row = sims[idx][cols]
            j = int(np.argmax(row))
            best = float(row[j])
            best_id = pool_ids[cols[j]]

        hit = best >= threshold
        grounded_hits += int(hit)
        if ids:
            report.n_cited += 1
            supported += int(hit)
        if not hit:
            weak.append({
                "text": sent[:80],
                "score": round(best, 3),
                "ref": best_id,
                "cited": bool(ids),
            })

    report.n_supported = supported
    report.score = (supported / report.n_cited) if report.n_cited else None
    report.grounded = grounded_hits / len(facts)
    weak.sort(key=lambda w: w["score"])
    report.weak = weak
    return report
