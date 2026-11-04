"""语义召回：选病症 + 降级。

锁三件事：
1. 关键词命中不了时，语义能把它捞回来（"胃里翻江倒海还拉稀" → 腹痛域）；
2. 语义命中只改**槽位顺序**，不改槽位集合——问什么仍由配置决定；
3. 后端不可用（服务没起、库被删）时静默退回关键词选域，且**只试一次**，
   不能每轮去撞一次超时。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from knowtune.config import load_config
from knowtune.generate.llm import LLMClient
from knowtune.previsit import KnowtuneLLMAdapter
from knowtune.previsit.config import load_previsit
from previsit.engine import PrevisitEngine
from previsit.retriever import Hit, SemanticRetriever
from previsit.store import Store

SERVICE = "http://127.0.0.1:8000"
HAS_KB = True          # 知识库由 scripts/ingest_previsit_kb.py 灌，跑过就有


class _FakeRetriever:
    """假召回器：直接返回预设命中，不碰网络。"""

    def __init__(self, hits: list[Hit]) -> None:
        self.hits = hits
        self.ok = True
        self.calls = 0

    def search(self, query: str) -> list[Hit]:
        self.calls += 1
        return self.hits


def _engine(tmp_path: Path, *, retrieval: bool = False) -> PrevisitEngine:
    cfg = load_config()
    pv = load_previsit()
    pv["timeout_s"] = 0
    pv["kg"] = dict(pv.get("kg") or {})
    pv["kg"]["enabled"] = False
    if not retrieval:
        pv["retrieval"] = {"enabled": False}
    return PrevisitEngine(pv, Store(tmp_path / "previsit.db"),
                          KnowtuneLLMAdapter(LLMClient(cfg)))


class TestDomainSelection:
    def test_semantic_picks_domain_when_keywords_miss(self, tmp_path):
        """关键词全没命中，语义说它是腹痛域 → 按语义来。"""
        eng = _engine(tmp_path)
        eng.retriever = _FakeRetriever([Hit("腹痛", "site", "要点…", 0.8)])
        v = eng.start("胃里翻江倒海，还拉稀", {"age": "35", "gender": "男"})
        assert v["domain"] == "abd"
        assert v["semantic"]["domain"] == "abd"
        assert v["semantic"]["hits"] >= 1

    def test_semantic_reorders_slots_only(self, tmp_path):
        """召回命中"部位"→ 它排到最前，但槽位集合一个不少。"""
        eng = _engine(tmp_path)
        base = eng._slots_for("abd")
        eng.retriever = _FakeRetriever([Hit("腹痛", "companions", "要点…", 0.9)])
        eng.start("胃里翻江倒海", {"age": "35", "gender": "男"})
        ordered = eng._slots_for("abd", ["companions"])
        assert ordered[0]["key"] == "companions"
        assert {s["key"] for s in ordered} == {s["key"] for s in base}

    def test_falls_back_to_keywords_when_no_hits(self, tmp_path):
        """召回啥也没有 → 退回关键词，流程不变。"""
        eng = _engine(tmp_path)
        eng.retriever = _FakeRetriever([])
        v = eng.start("肚子疼两天了", {"age": "35", "gender": "男"})
        assert v["domain"] == "abd"
        assert v["semantic"]["degraded"] is True

    def test_disabled_by_config(self, tmp_path):
        eng = _engine(tmp_path, retrieval=False)
        assert eng.retriever is None
        v = eng.start("肚子疼两天了", {"age": "35", "gender": "男"})
        assert v["domain"] == "abd"
        assert v["semantic"] == {}


class TestRetrieverDegradation:
    def test_unreachable_backend_disables_itself(self):
        """连不上只试一次：之后的每一轮都不再撞超时。"""
        r = SemanticRetriever("http://127.0.0.1:9", kb="预问诊知识库",
                              timeout_s=0.2)
        assert r.search("肚子疼") == []
        assert r.disabled is True
        n = r.calls if hasattr(r, "calls") else 0      # 类型无关，仅占位
        assert r.search("肚子疼") == []                 # 第二次不再打网络

    def test_from_config_off(self):
        assert SemanticRetriever.from_config({"enabled": False}) is None
        assert SemanticRetriever.from_config({}) is None
        assert SemanticRetriever.from_config({"enabled": True, "url": ""}) is None

    def test_label_to_key_prefers_specific(self, tmp_path):
        eng = _engine(tmp_path)
        assert eng._domain_key_by_label("儿童发热") == "child_fever"
        assert eng._domain_key_by_label("发热") == "fever"
        assert eng._domain_key_by_label("") == ""


@pytest.mark.skipif(not HAS_KB, reason="需要后端知识库已灌入预问诊条目")
class TestLiveRetrieval:
    def test_live_service_recalls_abdominal(self, tmp_path):
        """真服务在跑时，语义召回要能捞出腹痛域（冒烟，服务不在就跳过）。"""
        import httpx

        try:
            httpx.get(f"{SERVICE}/api/kb", timeout=2.0)
        except Exception:
            pytest.skip("主服务未启动")
        r = SemanticRetriever(SERVICE, kb="预问诊知识库", top_k=6, timeout_s=3.0)
        hits = r.search("胃里翻江倒海还拉稀")
        if not hits:                      # 库被删了不算失败，是降级
            pytest.skip("知识库里还没有预问诊条目")
        dom, weight, slots = SemanticRetriever.best_domain(hits)
        assert dom in ("腹痛", "腹泻", "呕吐", "一般不适"), dom


class TestMedRagMerge:
    """MedRAG 按系统归并后的解析与去重。

    归并前是一病一篇，病名写在 doc_title 里；归并后 doc_title 记的是系统，
    病名下沉到 heading_path。这两个名字必须分清——"呼吸系统"当病名喂给
    best_diseases，追问就会去查一个不存在的病。
    """

    def test_disease_name_comes_from_heading(self):
        from previsit.retriever import KIND_DISEASE, _split_parts

        kind, name, _ = _split_parts(
            "MedRAG · 呼吸系统", "MedRAG · 呼吸系统 > 肺炎")
        assert (kind, name) == (KIND_DISEASE, "肺炎")

    def test_system_layer_is_not_a_disease(self):
        """块没落到具体病上（heading 只到系统层）→ 丢弃，别把系统当病。"""
        from previsit.retriever import _split_parts

        _, name, _ = _split_parts("MedRAG · 呼吸系统", "MedRAG · 呼吸系统")
        assert name == ""

    def test_legacy_one_doc_per_disease_still_parses(self):
        from previsit.retriever import _split_parts

        _, name, _ = _split_parts("MedRAG · 肺栓塞", "MedRAG · 肺栓塞 > 症状确认（symptoms）")
        assert name == "肺栓塞"

    def test_dedupe_caps_per_document(self):
        """同一个域最多留两块：问"头晕"时前三条全是「头晕」的三个槽位，
        不去重的话 top_k 被一个域吃干，病种一条都进不来。"""
        from previsit.retriever import _dedupe

        hits = [Hit("头晕", "trigger", "", 1.0, doc="预问诊 · 头晕"),
                Hit("头晕", "history", "", 0.9, doc="预问诊 · 头晕"),
                Hit("头晕", "duration", "", 0.8, doc="预问诊 · 头晕"),
                Hit("头痛", "site", "", 0.7, doc="预问诊 · 头痛"),
                Hit("贫血", "", "", 0.6, kind="disease", doc="MedRAG · 皮肤免疫与血液")]
        out = _dedupe(hits, max_per_doc=2, limit=8)
        assert [h.slot or h.domain for h in out] == ["trigger", "history", "site", "贫血"]

    def test_inprocess_overfetch_then_trim(self):
        """进程内路径要先多要几条再去重，否则去重后可能只剩两条。"""
        from previsit.retriever import InProcessRetriever

        seen: list[int] = []

        def fake(query, k):
            seen.append(k)
            return [{"doc_title": "预问诊 · 头晕", "heading_path": "预问诊 · 头晕 > 头晕 · trigger（trigger）",
                     "text": "", "score": 1.0},
                    {"doc_title": "预问诊 · 头晕", "heading_path": "预问诊 · 头晕 > 头晕 · history（history）",
                     "text": "", "score": 0.9},
                    {"doc_title": "预问诊 · 头晕", "heading_path": "预问诊 · 头晕 > 头晕 · duration（duration）",
                     "text": "", "score": 0.8},
                    {"doc_title": "预问诊 · 头痛", "heading_path": "预问诊 · 头痛 > 头痛 · site（site）",
                     "text": "", "score": 0.7}]

        r = InProcessRetriever(fake, top_k=8, max_per_doc=2)
        hits = r.search("头晕")
        assert seen == [24]                       # 8 * 3
        assert [h.slot for h in hits] == ["trigger", "history", "site"]
