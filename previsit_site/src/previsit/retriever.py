"""语义召回：问后端知识库"患者这句话像哪个症状域 / 哪个病种"。

预问诊自己**不带向量模型**（那会把 torch / sentence-transformers 拖回来，
独立部署就成了空话）。它只做一件事：把患者原话发给后端知识库的检索接口，
拿回命中的分块，从标题里解析出「域 + 槽位」或「病种 + 小节」。

知识库里混着两类文档，职责完全不同：

| 文档 | 标题 | 用途 |
|---|---|---|
| 分域问法（previsit_kb.yaml 灌的） | `预问诊 · 腹泻` | 选病症、决定**问哪个槽位** |
| MedRAG 病种（xlsx 灌的） | `MedRAG · 肺炎` | 拿到**该病还该确认什么**，供生成追问 |

问法原文仍从 `previsit_kb.yaml` 取：可审计、可改、离线可用；MedRAG 那边
给的是素材（"发热、咳痰和胸膜炎性胸痛"），引擎才决定怎么问。

后端不可用时（独立部署、服务没起、库被删）一律静默降级回关键词选域，
并且**只试一次**——每轮都去撞一次 2.5 秒的超时，患者等不起。
"""

from __future__ import annotations

import os
import re
import time
from typing import Any

import httpx

# "预问诊 · 腹泻" → "腹泻"；"腹泻 · history（history）" → 槽位 key
_TITLE = re.compile(r"预问诊\s*·\s*(.+)")
_SLOT = re.compile(r"（([a-z_]+)）")
# MedRAG 文档的 section key：症状确认（symptoms）→ symptoms
_SECTION = re.compile(r"（(symptoms|exposure|history|lifestyle)）")

KIND_SLOT = "slot"          # 分域问法：决定问哪个槽位
KIND_DISEASE = "disease"    # MedRAG 病种：决定该确认什么

# MedRAG 归并后的六个系统文档名（scripts/ingest_medrag_kb.py 的 SYSTEMS）。
# 归并之后文档标题记的是**系统**不是病，病名在 heading_path 里；这两个名字
# 必须区分开，否则"呼吸系统"会被当成一个病名塞进 best_diseases。
SYSTEM_DOCS = frozenset({
    "呼吸系统", "心血管系统", "消化系统",
    "神经与精神", "皮肤免疫与血液", "耳鼻喉与感染",
})

# MedRAG 文档里的四类小节。文档通常很短（一个病 200~400 字），分块器会把
# 整篇当成一块，heading_path 就只剩文档标题——小节得从正文里认。
_SEC_HEAD = re.compile(
    r"(症状确认|危险因素|病史线索|生活背景)\s*（(symptoms|exposure|history|lifestyle)）")


def _parse_sections(text: str) -> dict[str, list[str]]:
    """从 MedRAG 文档正文里切出 {小节key: [条目…]}。

    `表现：…` 那一行单独处理成 symptoms——它是灌库时把全部症状拼在一起
    的摘要行，语义召回主要靠它命中，也是最直白的一份"该确认什么"。
    """
    out: dict[str, list[str]] = {}
    cur = ""
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        m = _SEC_HEAD.search(line)
        if m:
            cur = m.group(2)
            out.setdefault(cur, [])
            continue
        if line.startswith("表现："):
            val = line[3:].strip()
            if val:
                out.setdefault("symptoms", []).append(val)
            continue
        if line.startswith("- ") and cur:
            val = line[2:].strip()
            if val and val not in out[cur]:
                out[cur].append(val)
    return out


def _section_key(heading: str) -> str:
    head = str(heading or "")
    if ">" in head:
        head = head.split(">")[-1]
    m = _SECTION.search(head)
    return m.group(1) if m else ""


def _split_parts(doc_title: str, heading: str) -> tuple[str, str, str]:
    """从文档标题/章节路径解析 (类型, 域或病名, 槽位或小节)。

    后端返回的真实形态：
        doc_title    = "预问诊 · 腹泻"
        heading_path = "预问诊 · 腹泻 > 腹泻 · history（history）"

    以及 MedRAG 归并后的：
        doc_title    = "MedRAG · 呼吸系统"
        heading_path = " MedRAG · 呼吸系统 > 肺炎"   （病名在小节标题里）

    域一定从 **doc_title** 取——heading 里"预问诊"也在第一段，直接拿会被
    当成域名（实测判成"预问诊"，等于没判）。病名反过来，必须从 heading 取：
    归并后 doc_title 记的是系统名，"呼吸系统"不是病。
    """
    title = str(doc_title or "").strip()
    head = str(heading or "")
    if title.startswith("MedRAG"):
        name = ""
        for part in reversed([p.strip() for p in head.split(">") if p.strip()]):
            if part in SYSTEM_DOCS or part.startswith("MedRAG"):
                continue          # 系统名那两层，跳过
            if _SECTION.search(part):
                continue          # 小节标题（症状确认（symptoms）那类），跳过
            name = part
            break
        if not name:
            # 兼容一病一篇的旧格式（doc_title 后半就是病名）。要是后半是系统名
            # 说明这条块没落到具体病上，宁可丢弃也不能把系统当病。
            tail = title.split("·", 1)[1].strip() if "·" in title else ""
            name = "" if tail in SYSTEM_DOCS else tail
        return KIND_DISEASE, name, _section_key(head)
    m = _TITLE.search(title)
    domain = m.group(1).strip() if m else title
    slot = ""
    if ">" in head:
        head = head.split(">")[-1]
    hm = _SLOT.search(head)
    if hm:
        slot = hm.group(1).strip()
        # 标题里没解析出域（旧格式文档）时，退而求其次用 heading 的第一段
        if not domain:
            dm = re.match(r"\s*(.+?)\s*·", head)
            domain = dm.group(1).strip() if dm else ""
    return KIND_SLOT, domain, slot


def _dedupe(hits: list[Hit], max_per_doc: int, limit: int) -> list[Hit]:
    """同一个文档最多留 max_per_doc 条，防止一个域把 top_k 全占满。

    `预问诊 · 头晕` 一篇有 3 块（trigger / history / duration），语义上和
    "头晕站不稳"都很像，融合后往往连号排在一起——实测问"头晕"时 top6 前三条
    全是它，剩下的位置只够塞三个病种，等于这条路白搭。按文档限量后，同一个
    域只贡献它最像的那两块，省下的位置留给别的域和 MedRAG 病种。
    """
    if max_per_doc <= 0:
        return hits[:limit]
    out: list[Hit] = []
    seen: dict[str, int] = {}
    for h in hits:
        key = h.doc or h.domain
        if seen.get(key, 0) >= max_per_doc:
            continue
        seen[key] = seen.get(key, 0) + 1
        out.append(h)
        if len(out) >= limit:
            break
    return out


class InProcessRetriever:
    """进程内直调（主服务挂载预问诊时用）。

    预问诊挂在后台同一个 FastAPI 进程里，再绕一圈 HTTP 是纯浪费：这个服务
    每个请求有 1.8 秒的固定开销（连 `GET /api/kb` 都是），而检索本身只花
    12 毫秒。主服务把"怎么查库"这个函数交进来，召回就变成一次函数调用。

    独立部署（previsit_site）没有主服务，走 SemanticRetriever 的 HTTP 路径；
    两条路径接口一致，引擎不用区分。
    """

    def __init__(self, search_fn: Any, top_k: int = 8, max_per_doc: int = 2) -> None:
        self._fn = search_fn
        self.top_k = int(top_k or 8)
        self.max_per_doc = int(max_per_doc or 0)
        self.disabled = False
        self.last_error = ""

    @property
    def ok(self) -> bool:
        return callable(self._fn) and not self.disabled

    def search(self, query: str) -> list[Hit]:
        if not self.ok or not (query or "").strip():
            return []
        try:
            # 多要几倍再按文档限量：只要 top_k 条的话，去重后可能只剩两条
            raw = self._fn(query, self.top_k * 3) or []
        except Exception as exc:                      # 主服务侧任何异常都不传染
            self.disabled = True
            self.last_error = f"{type(exc).__name__}: {exc}"
            return []
        hits: list[Hit] = []
        for i, c in enumerate(raw):
            kind, domain, slot = _split_parts(c.get("doc_title") or "",
                                              c.get("heading_path") or "")
            if not domain:
                continue
            score = c.get("score")
            hits.append(Hit(domain, slot, str(c.get("text") or "")[:400],
                            float(score) if isinstance(score, (int, float))
                            else 1.0 / (i + 1), kind,
                            str(c.get("doc_title") or "")))
        return _dedupe(hits, self.max_per_doc, self.top_k)


class Hit:
    """一条召回结果。只保留引擎用得上的几个字段。

    `kind` 决定这条命中怎么用：`slot` 是分域问法（选病症、定槽位），
    `disease` 是 MedRAG 病种（找"这个病还该确认什么"）。默认 slot 是为了
    兼容旧调用方——它们本来就只关心域和槽位。

    `doc` 是命中来源的文档标题，只用于同文档去重（见 `_dedupe`），不参与
    任何判断：域看 domain，病看 domain，文档只是"这两条是不是同一篇"的凭据。
    """

    __slots__ = ("domain", "slot", "text", "score", "kind", "doc")

    def __init__(self, domain: str, slot: str, text: str, score: float,
                 kind: str = KIND_SLOT, doc: str = "") -> None:
        self.domain = domain
        self.slot = slot
        self.text = text
        self.score = score
        self.kind = kind
        self.doc = doc

    def as_dict(self) -> dict[str, Any]:
        return {"domain": self.domain, "slot": self.slot, "kind": self.kind,
                "doc": self.doc, "text": self.text, "score": round(self.score, 4)}


class SemanticRetriever:
    """后端知识库的语义检索客户端。

    kb 用**名字**而不是 id：id 会随建库/删库漂移，写死成数字早晚失效；
    名字是配置里人能读懂的那一个。
    """

    def __init__(self, url: str, kb: str = "预问诊知识库", top_k: int = 8,
                 timeout_s: float = 2.5, min_score: float = 0.0,
                 rerank: bool = True, max_per_doc: int = 2) -> None:
        self.url = (url or "").rstrip("/")
        self.kb = kb
        self.top_k = int(top_k or 8)
        self.timeout_s = float(timeout_s or 2.5)
        self.min_score = float(min_score or 0.0)
        self.rerank = bool(rerank)
        self.max_per_doc = int(max_per_doc or 0)
        self._kb_id: int | None = None
        self.disabled = False
        self.last_error = ""

    @classmethod
    def from_config(cls, cfg: dict[str, Any] | None) -> "SemanticRetriever | None":
        """配置关掉/没配 url → 返回 None，引擎走关键词老路。

        `PREVISIT_KB_URL` 环境变量优先于配置：独立部署（previsit_site）没有
        后端知识库服务，部署时把它设成空串就能彻底关掉这一路，省得每场问诊
        去撞一次连不上的地址。
        """
        cfg = cfg or {}
        if not cfg.get("enabled"):
            return None
        raw = os.environ.get("PREVISIT_KB_URL")
        url = str(cfg.get("url") or "").strip() if raw is None else str(raw).strip()
        if url.lower() in ("", "0", "none", "off"):
            return None
        return cls(
            url,
            kb=str(cfg.get("kb") or "预问诊知识库"),
            top_k=int(cfg.get("top_k") or 8),
            timeout_s=float(cfg.get("timeout_s") or 2.5),
            min_score=float(cfg.get("min_score") or 0.0),
            rerank=bool(cfg.get("rerank", True)),
            max_per_doc=int(cfg.get("max_per_doc") or 2),
        )

    @property
    def ok(self) -> bool:
        return not self.disabled and bool(self.url)

    def _resolve(self) -> int | None:
        if self._kb_id is not None:
            return self._kb_id
        try:
            resp = httpx.get(f"{self.url}/api/kb", timeout=self.timeout_s)
            if resp.status_code >= 400:
                raise RuntimeError(f"HTTP {resp.status_code}")
            items = resp.json()
            items = items if isinstance(items, list) else (items.get("kbs") or [])
            for k in items:
                if str(k.get("name") or "").strip() == self.kb:
                    self._kb_id = int(k["id"])
                    return self._kb_id
            raise RuntimeError(f"知识库「{self.kb}」不存在")
        except Exception as exc:                      # 解析失败也算不可用
            self.disabled = True
            self.last_error = f"定位知识库失败：{type(exc).__name__}: {exc}"
            return None

    def search(self, query: str) -> list[Hit]:
        """语义召回。任何问题都返回空列表（不抛），调用方自然降级。"""
        if not self.ok or not (query or "").strip():
            return []
        kb_id = self._resolve()
        if kb_id is None:
            return []
        try:
            resp = httpx.post(
                f"{self.url}/api/search/debug",
                json={"question": query[:500], "kb_id": kb_id,
                      # 多要几倍再按文档限量，理由同 InProcessRetriever
                      "top_k": self.top_k * 3, "rerank": self.rerank},
                timeout=self.timeout_s + 3.0,
            )
            if resp.status_code >= 400:
                raise RuntimeError(f"HTTP {resp.status_code}")
            payload = resp.json()
        except Exception as exc:
            self.disabled = True
            self.last_error = f"检索失败：{type(exc).__name__}: {exc}"
            return []
        hits: list[Hit] = []
        for i, c in enumerate(payload.get("final") or []):
            kind, domain, slot = self._parse(c.get("doc_title") or "",
                                             c.get("heading_path") or "")
            if not domain:
                continue
            score = self._score(c, i)
            if score < self.min_score:
                continue
            hits.append(Hit(domain, slot, str(c.get("preview") or ""), score, kind,
                            str(c.get("doc_title") or "")))
        return _dedupe(hits, self.max_per_doc, self.top_k)

    @staticmethod
    def _parse(doc_title: str, heading: str) -> tuple[str, str, str]:
        return _split_parts(doc_title, heading)

    @staticmethod
    def _score(chunk: dict[str, Any], rank: int) -> float:
        """排序分：精排分 > 稠密分 > 名次倒数。

        精排分是 CrossEncoder 给的绝对分（0~1 量级），优先考虑；没有就退回
        名次——名次本身就是融合后的顺序，比生造一个分数诚实。
        """
        for key in ("rerank_score", "dense_score"):
            v = chunk.get(key)
            if isinstance(v, (int, float)):
                return float(v)
        return 1.0 / (rank + 1)

    # 给 engine 的聚合：按域累计"权重"，取最高的那个域
    @staticmethod
    def best_domain(hits: list[Hit]) -> tuple[str, float, list[str]]:
        """返回 (域标签, 累计权重, 该域里被命中的槽位 key 列表，按命中顺序)。

        只吃 `kind=slot` 的命中——MedRAG 病种文档的 domain 字段是病名，
        混进来会把"肺炎"当成一个症状域去查配置，查不到就白跑一趟。
        """
        if not hits:
            return "", 0.0, []
        weights: dict[str, float] = {}
        slots: dict[str, list[str]] = {}
        for i, h in enumerate(hits):
            if not h.domain or h.kind != KIND_SLOT:
                continue
            weights[h.domain] = weights.get(h.domain, 0.0) + h.score * (1.0 / (i + 1))
            if h.slot:
                slots.setdefault(h.domain, [])
                if h.slot not in slots[h.domain]:
                    slots[h.domain].append(h.slot)
        if not weights:
            return "", 0.0, []
        # 长标签优先：命中"发热"时可能是"儿童发热"的子串，取更具体的那个
        dom = max(weights, key=lambda d: (weights[d], len(d)))
        return dom, weights[dom], slots.get(dom) or []

    @staticmethod
    def best_diseases(hits: list[Hit], limit: int = 3) -> list[dict[str, Any]]:
        """路 B：命中的 MedRAG 病种，按累计权重排序。

        返回 `[{"disease","score","sections":{symptoms:[...], ...}}]`，
        只保留有**症状确认**内容的病种——只有"危险因素""生活方式"的命中
        对追问没用（问患者"你接触过污染物吗"在预问诊里是噪声）。

        `sections` 里存的是召回分块的原文片段，引擎拿它当"该确认什么"的
        线索，不是拿来直接念给患者听的。
        """
        if not hits:
            return []
        acc: dict[str, dict[str, Any]] = {}
        for i, h in enumerate(hits):
            if h.kind != KIND_DISEASE or not h.domain:
                continue
            item = acc.setdefault(h.domain, {"disease": h.domain, "score": 0.0,
                                             "sections": {}})
            item["score"] += h.score * (1.0 / (i + 1))
            secs = _parse_sections(h.text)
            if not secs and h.slot:
                # 老格式（分块器按标题切了）还能拿到 section key，兜住
                secs = {h.slot: [h.text]}
            for key, vals in secs.items():
                bucket = item["sections"].setdefault(key, [])
                for v in vals:
                    if v not in bucket:
                        bucket.append(v)
        out = [v for v in acc.values() if v["sections"].get("symptoms")]
        out.sort(key=lambda x: -x["score"])
        for v in out:
            v["score"] = round(v["score"], 4)
        return out[:limit]
