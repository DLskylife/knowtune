"""答案质量评测（LLM-as-judge）单元测试。

核心逻辑——judge_answer 的 JSON 解析与降级、run_eval_answer 的编排与聚合——
都能用假模型覆盖，不需要真实 LLM 密钥。真正的端到端（含生成答案）走
`knowtune eval --answer`，那一步才需要配置后端。
"""

from __future__ import annotations

from types import SimpleNamespace

import knowtune.evaluate as E
from knowtune.evaluate import AnswerEvalReport, EvalReport, judge_answer, run_eval_answer


class _Out:
    def __init__(self, text: str = "") -> None:
        self.text = text


class _Judge:
    def __init__(self, text: str) -> None:
        self._text = text

    def complete(self, msgs, **kw):
        return _Out(self._text)


class _Cand:
    def __init__(self, doc_title: str, text: str, final_rank: int = 1) -> None:
        self.doc_title = doc_title
        self.text = text
        self.heading_path = ""
        self.final_rank = final_rank


class _Res:
    def __init__(self, candidates, refused: bool = False) -> None:
        self.candidates = candidates
        self.refusal = SimpleNamespace(refused=refused, top_score=0.5, margin=0.1)


class _Engine:
    def search(self, conn, question, kb_id=None, top_k=5, rerank=None, mmr=None):
        return _Res([_Cand("DOC", "上下文：X 的相关内容")])


def _dummy_report() -> EvalReport:
    return EvalReport(
        k=5,
        metrics={"n_cases": 1, "n_answerable": 1, "recall_at_k": 1.0,
                 "mrr": 1.0, "hit_at_1": 1.0, "latency_ms_p50": 10,
                 "latency_ms_p95": 10, "latency_ms_max": 10},
        refusal={"n_should_refuse": 0, "missed_refusal": 0, "missed_refusal_rate": 0.0,
                 "n_answerable": 1, "false_refusal": 0, "false_refusal_rate": 0.0,
                 "missed_ids": [], "false_refusal_ids": []},
        cases=[],
        fingerprint={},
    )


def test_judge_parses_json() -> None:
    j = _Judge('{"faithful":0.9,"relevant":0.8,"reason":"全部有出处"}')
    f, r, reason, err = judge_answer(j, question="q", answer="a",
                                     context=["上下文"])
    assert err is None
    assert f == 0.9 and r == 0.8
    assert "出处" in reason


def test_judge_returns_none_on_garbage() -> None:
    j = _Judge("这答案不太行，但没给 JSON")
    f, r, _, err = judge_answer(j, question="q", answer="a", context=[])
    assert f is None and r is None
    assert err == "no_json"


def test_judge_none_llm_is_explicit() -> None:
    f, r, _, err = judge_answer(None, question="q", answer="a", context=[])
    assert f is None and r is None and err == "no_llm"


def test_judge_clamps_and_tolerates_surrounding_text() -> None:
    j = _Judge('好的，评判如下：\n{"faithful": 1.7, "relevant": -0.2, "reason": "x"}\n以上。')
    f, r, _, err = judge_answer(j, question="q", answer="a", context=["c"])
    assert err is None
    # 越界值被夹到 [0,1]
    assert f == 1.0 and r == 0.0


def test_run_eval_answer_aggregates(monkeypatch) -> None:
    monkeypatch.setattr(E, "run_eval", lambda *a, **k: _dummy_report())
    judge = _Judge('{"faithful":0.85,"relevant":0.7,"reason":"ok"}')
    cases = [{"id": "q1", "question": "x", "expect": {"doc": "DOC", "keys": ["X"]}}]

    report = run_eval_answer(
        cfg=SimpleNamespace(eval=SimpleNamespace(dataset="", k=5)),
        conn=None, engine=_Engine(), llm=judge, cases=cases,
        gen=lambda q, c: "这是基于上下文 X 的答案",
    )
    assert isinstance(report, AnswerEvalReport)
    c = report.cases[0]
    assert c.faithful == 0.85
    assert c.relevant == 0.7
    assert c.answer and not c.judge_error
    agg = report.as_dict()["answer"]
    assert agg["mean_faithful"] == 0.85
    assert agg["n_answered"] == 1


def test_run_eval_answer_no_gen_skips_answer(monkeypatch) -> None:
    """gen 为 None 时只出检索指标、不做答案评测（检索-only 路径不受影响）。"""
    monkeypatch.setattr(E, "run_eval", lambda *a, **k: _dummy_report())
    cases = [{"id": "q1", "question": "x", "expect": {"doc": "DOC", "keys": ["X"]}}]
    report = run_eval_answer(
        cfg=SimpleNamespace(eval=SimpleNamespace(dataset="", k=5)),
        conn=None, engine=_Engine(), llm=_Judge("{}"), cases=cases, gen=None,
    )
    assert report.cases[0].answer == ""
    assert report.cases[0].faithful is None
    # 检索指标仍在
    assert report.retrieval.metrics["n_cases"] == 1
