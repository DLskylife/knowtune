"""追问要点检索。

知识库按「症状域 × 槽位」组织（`config/previsit_kb.yaml`）：
每个域下面，每个槽位对应若干条 `{text: 知识点, ask: 现成问法}`。

三件事：

1. `detect_domain` —— 主诉命中哪个域。命中总字数最多的域胜出，
   因为越长的词越具体（"下腹痛"比"腹痛"更能说明是妇科问题）。
2. `for_slot` —— 取某个域、某个槽位最相关的一条知识。追问的画风、
   要点、兜底话术全部从这条知识里来。
3. `retrieve` —— 旧的平铺条目检索，保留给没有配 domains 的配置用。

为什么不上向量库：
  1. 条目是关键词强相关的规则型知识（"问部位要区分上腹/脐周/右下腹"），
     词重叠比语义向量更可控、更可解释；
  2. 预问诊不该被 90 秒的嵌入模型预热堵住——患者第一句话就要有回应；
  3. 和主库一样，能不引新依赖就不引。

jieba 是主项目已有的核心依赖，这里直接复用，不额外引入。
"""

from __future__ import annotations

from typing import Any

import jieba


def _tokens(text: str) -> set[str]:
    return {t.strip() for t in jieba.cut(text or "") if len(t.strip()) >= 2}


def _hits(keywords: list[str], text: str, toks: set[str]) -> int:
    """关键词命中数。

    双向包含（`kw in text` 与 `kw 与某个 token 互为子串`）是为了吃下
    "肚子疼"与"肚子痛"、"拉肚子"与"腹泻"这类分词切不准的情况。但**不做**
    `t in kw` 这种单字母/单字回退——那会让"热"命中"发热"、"疼"命中所有含疼
    的词，几十个域互相串味。分词 token 至少 2 字，已经是下限。
    """
    n = 0
    for kw in keywords:
        kw = str(kw or "").strip()
        if not kw:
            continue
        if kw in text or any(kw == t or (len(t) >= 2 and t in kw) for t in toks):
            n += 1
    return n


def _domain_score(keywords: list[str], text: str) -> int:
    """域的关键词得分 = 命中关键词的字数之和，且**不重复计算被包含的词**。

    腹痛域同时有「肚子」「肚子疼」「小肚子疼」，一句话能把三个都命中，
    累加会让它压过任何更贴切的域（"小肚子疼还出血"被判成腹痛而不是妇科）。
    所以按长度降序取词，短词若已被命中过的长词包含就跳过——
    "小肚子疼"命中了就不再算"肚子疼"和"肚子"的分。
    """
    hits = sorted({kw for kw in keywords if kw and kw in text}, key=len, reverse=True)
    kept: list[str] = []
    for kw in hits:
        if any(kw != k and kw in k for k in kept):
            continue
        kept.append(kw)
    return sum(len(k) for k in kept)


def detect_domain(domains: list[dict[str, Any]], text: str) -> str:
    """判断主诉属于哪个症状域；一个都没命中返回空串。

    打分用**命中关键词的字数之和**而不是条数：命中的词越长越具体，
    越能代表主诉。这样"下腹痛"会判给妇科而不是普适的腹痛域，
    "宝宝发烧"会判给儿童发热而不是成人发热域。
    """
    if not domains or not text:
        return ""
    best_key, best_score = "", 0
    for d in domains:
        score = _domain_score([str(k or "") for k in (d.get("keywords") or [])], text)
        if score > best_score:
            best_key, best_score = d["key"], score
    return best_key


def for_slot(domain: dict[str, Any] | None, slot: str, query: str,
             top_k: int = 1) -> list[dict[str, Any]]:
    """取某域某槽位的知识条目，按与 query 的相关度排序。

    同一槽位可能有多条（腹痛的「部位」有"定位"和"是否游走"两条），
    靠条目自己的 keywords 决定这次该用哪条。都没命中就按配置顺序取第一条——
    配置里第一条永远是该槽位最通用、最该先问的那条。
    """
    if not domain:
        return []
    entries = [e for e in (domain.get("entries") or []) if e.get("slot") == slot]
    if not entries:
        return []
    toks = _tokens(query)
    scored = [(_hits(e.get("keywords") or [], query, toks), -i, e)
              for i, e in enumerate(entries)]
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return [e for _, _, e in scored[:top_k]]


def ask_of(domain: dict[str, Any] | None, slot: str, query: str) -> str:
    """该域该槽位的现成问法；没有配置就返回空串，由调用方兜底。"""
    got = for_slot(domain, slot, query)
    return str(got[0].get("ask") or "").strip() if got else ""


def retrieve(entries: list[dict[str, Any]], query: str, top_k: int = 3) -> list[dict[str, Any]]:
    """旧的平铺检索：按关键词命中数排序，无命中时用 universal 条目兜底。

    只在配置里没有 `domains`（旧格式）时被调用。
    """
    if not entries:
        return []
    q = query or ""
    toks = _tokens(q)
    scored: list[tuple[int, int, dict[str, Any]]] = []
    for i, e in enumerate(entries):
        if not isinstance(e, dict):
            continue
        n = _hits(e.get("keywords") or [], q, toks)
        if n:
            scored.append((n, i, e))
    scored.sort(key=lambda x: (-x[0], x[1]))
    picked = [e for _, _, e in scored[:top_k]]
    if picked:
        return picked
    return [e for e in entries if isinstance(e, dict) and e.get("universal")][:top_k]
