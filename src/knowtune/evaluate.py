"""评估：Recall@K / MRR / 拒答混淆矩阵。

为什么必须有这个模块：没有基线的 RAG，每次"感觉好像变好了"都可能是噪声。
改 chunk size、换嵌入模型、调融合权重、开关精排——这四类改动每天都在发生，
没有一套能在几分钟内跑完的评估，调参就是掷骰子。

设计上的两个硬要求：
1. **不依赖 LLM**（skip_generation）——生成评估慢且贵，检索质量必须能独立测量。
2. **期望来源用"文档 + 关键词全命中"表示，不用 chunk_id**——chunk_id 会随
   重新分块整体漂移，一旦绑死 chunk_id，改一次 chunk size 评估集就全废。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .config import Config
from .textnorm import fold

DEFAULT_K = 5


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_cases(path: Path) -> list[dict]:
    if not Path(path).exists():
        raise FileNotFoundError(f"评估集不存在：{path}")
    cases: list[dict] = []
    for lineno, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            cases.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{lineno} 不是合法 JSON：{exc}") from exc
    if not cases:
        raise ValueError(f"评估集为空：{path}")
    return cases


def _match(cand, expect: dict) -> bool:
    """期望来源的柔性匹配：文档名命中 + 全部关键词出现在正文或章节路径里。

    用关键词而不是 chunk_id，是为了让评估集在重新分块后依然有效。
    """
    doc = expect.get("doc")
    if doc and doc.lower() not in (cand.doc_title or "").lower():
        return False
    keys = expect.get("keys") or []
    if not keys:
        return True
    # 判定前折叠码位：评估集是人写的（正常汉字），而历史索引里可能存着
    # 康熙部首形态的同一个字。这属于"人工判定应该忽略的编码差异"，
    # 不该让一条本来正确的召回被判成没召回。
    hay = fold(f"{cand.heading_path or ''}\n{cand.text}")
    return all(k in hay for k in keys)


@dataclass
class CaseResult:
    id: str
    question: str
    refused: bool
    expect_refusal: bool | None
    rank: int | None = None
    latency_ms: int = 0
    top_doc: str | None = None
    top_score: float | None = None
    note: str = ""


@dataclass
class EvalReport:
    k: int
    metrics: dict
    refusal: dict
    cases: list[CaseResult]
    fingerprint: dict
    created_at: str = field(default_factory=_utcnow)

    def as_dict(self) -> dict:
        return {
            "created_at": self.created_at,
            "k": self.k,
            "metrics": self.metrics,
            "refusal": self.refusal,
            "fingerprint": self.fingerprint,
            "cases": [c.__dict__ for c in self.cases],
        }


def fingerprint(cfg: Config, *, rerank: bool | None = None, mmr: bool | None = None) -> dict:
    """参数指纹。基线只在指纹一致时可比，否则就是拿苹果比橘子。"""
    rcfg = cfg.retrieval
    return {
        "chunk_size": cfg.chunking.size,
        "chunk_overlap": cfg.chunking.overlap,
        "embedding": f"{cfg.embedding.model}@{cfg.embedding.dim}",
        "dense_top_k": rcfg.dense.top_k,
        "sparse_top_k": rcfg.sparse.top_k,
        "rrf_k": rcfg.fusion.k,
        "w_dense": rcfg.fusion.w_dense,
        "w_sparse": rcfg.fusion.w_sparse,
        "fusion_top_k": rcfg.fusion.top_k,
        "rerank": rcfg.rerank.enabled if rerank is None else rerank,
        "rerank_model": rcfg.rerank.model,
        "mmr": rcfg.mmr.enabled if mmr is None else mmr,
    }


def run_eval(
    cfg: Config,
    conn,
    engine,
    *,
    k: int = DEFAULT_K,
    cases: list[dict] | None = None,
    rerank: bool | None = None,
    mmr: bool | None = None,
) -> EvalReport:
    import time

    cases = cases if cases is not None else load_cases(Path(cfg.eval.dataset))
    results: list[CaseResult] = []

    for case in cases:
        question = case["question"]
        expect = case.get("expect")
        expect_refusal = case.get("expect_refusal")
        started = time.perf_counter()
        res = engine.search(
            conn, question, kb_id=case.get("kb_id"), top_k=k, rerank=rerank, mmr=mmr
        )
        latency = int((time.perf_counter() - started) * 1000)
        refused = bool(res.refusal and res.refusal.refused)

        rank = None
        if expect and not refused:
            for cand in res.candidates[:k]:
                if _match(cand, expect):
                    rank = cand.final_rank
                    break

        top = res.candidates[0] if res.candidates else None
        results.append(
            CaseResult(
                id=case.get("id") or question[:12],
                question=question,
                refused=refused,
                expect_refusal=expect_refusal,
                rank=rank,
                latency_ms=latency,
                top_doc=top.doc_title if top else None,
                top_score=res.refusal.top_score if res.refusal else None,
                note=case.get("note", ""),
            )
        )

    # 有期望来源的题（expect_refusal 不为 True）才算 answerable
    answerable = [r for r in results if r.expect_refusal is not True]
    should_refuse = [r for r in results if r.expect_refusal is True]

    recall = sum(1 for r in answerable if r.rank is not None) / len(answerable) if answerable else 0.0
    mrr = (
        sum(1.0 / r.rank for r in answerable if r.rank) / len(answerable) if answerable else 0.0
    )
    false_refusals = [r for r in answerable if r.refused]
    missed_refusals = [r for r in should_refuse if not r.refused]
    latencies = [r.latency_ms for r in results]
    latencies.sort()
    p50 = latencies[len(latencies) // 2] if latencies else 0

    metrics = {
        "n_cases": len(results),
        "n_answerable": len(answerable),
        "recall_at_k": round(recall, 4),
        "mrr": round(mrr, 4),
        "hit_at_1": round(
            sum(1 for r in answerable if r.rank == 1) / len(answerable) if answerable else 0.0, 4
        ),
        # 均值会被"首次调用含模型冷启动"的那一条拉爆（1887ms vs p95 19ms），
        # 所以主指标用 p50，避免拿假数据做优化目标
        "latency_ms_p50": p50,
        "latency_ms_p95": latencies[int(len(latencies) * 0.95) - 1] if latencies else 0,
        "latency_ms_max": latencies[-1] if latencies else 0,
    }
    refusal = {
        "n_should_refuse": len(should_refuse),
        "missed_refusal": len(missed_refusals),
        "missed_refusal_rate": round(
            len(missed_refusals) / len(should_refuse) if should_refuse else 0.0, 4
        ),
        "n_answerable": len(answerable),
        "false_refusal": len(false_refusals),
        "false_refusal_rate": round(
            len(false_refusals) / len(answerable) if answerable else 0.0, 4
        ),
        "missed_ids": [r.id for r in missed_refusals],
        "false_refusal_ids": [r.id for r in false_refusals],
    }
    return EvalReport(
        k=k,
        metrics=metrics,
        refusal=refusal,
        cases=results,
        fingerprint=fingerprint(cfg, rerank=rerank, mmr=mmr),
    )


def compare(report: EvalReport, baseline: dict | None) -> list[str]:
    """与基线对比。指纹不一致就明说"不可比"，不装作能比。"""
    if not baseline:
        return ["（无基线，本次结果可保存为基线）"]
    lines: list[str] = []
    if baseline.get("fingerprint") != report.fingerprint:
        diff = [
            f"{k}: {baseline.get('fingerprint', {}).get(k)} → {v}"
            for k, v in report.fingerprint.items()
            if baseline.get("fingerprint", {}).get(k) != v
        ]
        lines.append("⚠ 参数指纹与基线不同，指标不可直接比较：")
        lines.extend(f"    {d}" for d in diff)
        return lines

    b = baseline.get("metrics", {})
    for key in ("recall_at_k", "mrr", "hit_at_1"):
        old, new = b.get(key), report.metrics.get(key)
        if old is None or new is None:
            continue
        delta = new - old
        arrow = "↑" if delta > 1e-9 else ("↓" if delta < -1e-9 else "=")
        lines.append(f"{key}: {old:.4f} → {new:.4f}  {arrow}{abs(delta):.4f}")
    return lines


def load_baseline(path: Path) -> dict | None:
    p = Path(path)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def save_baseline(path: Path, report: EvalReport) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(report.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8")


def format_report(report: EvalReport, baseline: dict | None = None) -> str:
    m, r = report.metrics, report.refusal
    out = [
        f"评估集 {m['n_cases']} 条（可答 {m['n_answerable']}，应拒 {r['n_should_refuse']}）"
        f"   K={report.k}",
        f"Recall@{report.k} = {m['recall_at_k']:.1%}   MRR = {m['mrr']:.4f}   "
        f"Hit@1 = {m['hit_at_1']:.1%}",
        f"延迟  p50 {m['latency_ms_p50']}ms  p95 {m['latency_ms_p95']}ms  "
        f"max {m['latency_ms_max']}ms（max 通常是首次调用含模型冷启动）",
        f"拒答  误拒 {r['false_refusal']}/{r['n_answerable']} "
        f"({r['false_refusal_rate']:.1%})   "
        f"漏拒 {r['missed_refusal']}/{r['n_should_refuse']} "
        f"({r['missed_refusal_rate']:.1%})",
    ]
    misses = [c for c in report.cases if c.expect_refusal is not True and c.rank is None]
    if misses:
        out.append("")
        out.append(f"未命中 {len(misses)} 条：")
        for c in misses:
            flag = "（被拒答）" if c.refused else ""
            out.append(f"  {c.id}  top={c.top_doc or '-'}{flag}  {c.question}")
    if r["missed_ids"] or r["false_refusal_ids"]:
        out.append("")
        if r["missed_ids"]:
            out.append(f"该拒未拒：{', '.join(r['missed_ids'])}")
        if r["false_refusal_ids"]:
            out.append(f"该答被拒：{', '.join(r['false_refusal_ids'])}")
    out.append("")
    out.extend(compare(report, baseline))
    return "\n".join(out)


# ------------------------------------------------------------------ 阈值标定
#
# 需求写"低于校准阈值"——"校准"两个字是这句话里唯一重要的词。
# 拍一个 0.35 上去，要么误拒到没法用，要么形同虚设。这里拿评估集的
# (top_score, margin) 分布做网格扫描，按"误拒优先"的目标函数选点。


def _collect_scores(cfg: Config, conn, engine, cases: list[dict]) -> list[dict]:
    """关掉拒答跑一遍，拿到每题的真实 (top_score, margin, 是否应拒)。"""
    saved = cfg.refusal.enabled
    cfg.refusal.enabled = False
    rows: list[dict] = []
    try:
        for case in cases:
            # 标定只需要 (top, margin)：取 eval.k 条就够，没必要跑满精排。
            res = engine.search(
                conn, case["question"], kb_id=case.get("kb_id"),
                top_k=max(1, int(cfg.eval.k)),
            )
            decision = res.refusal
            rows.append({
                "id": case.get("id") or case["question"][:12],
                "top": decision.top_score if decision else None,
                "margin": decision.margin if decision else None,
                "expect_refusal": case.get("expect_refusal") is True,
            })
    finally:
        cfg.refusal.enabled = saved
    return rows


def calibrate(
    cfg: Config,
    conn,
    engine,
    *,
    cases: list[dict] | None = None,
    max_false_refusal: float = 0.10,
) -> dict:
    cases = cases if cases is not None else load_cases(Path(cfg.eval.dataset))
    rows = _collect_scores(cfg, conn, engine, cases)
    n_ans = sum(1 for r in rows if not r["expect_refusal"])
    n_ref = sum(1 for r in rows if r["expect_refusal"])
    if not n_ans or not n_ref:
        raise ValueError(
            f"标定需要两类样本：可答 {n_ans} 条 / 应拒 {n_ref} 条。"
            "请在评估集里补 expect_refusal: true 的题。"
        )

    usable = rows  # top=None（一路都没召回）也算"会拒答"，不能悄悄排除
    best: dict | None = None
    for score_i in range(20, 81):
        min_score = score_i / 100
        for margin_i in range(0, 25):
            min_margin = margin_i * 0.005
            fp = fn = 0
            for r in usable:
                if r["top"] is None:
                    refused = True
                else:
                    refused = r["top"] < min_score or (
                        r["margin"] is not None and r["margin"] < min_margin
                    )
                if r["expect_refusal"] and not refused:
                    fn += 1
                if not r["expect_refusal"] and refused:
                    fp += 1
            fr_rate = fp / n_ans
            mr_rate = fn / n_ref
            # 目标：误拒率达标的前提下把漏拒压到最低；同分时优先更高的 min_score
            # （更保守的拒答比"几乎不拒"更符合知识库的使用语义）
            cost = mr_rate + fr_rate + (0.5 if fr_rate > max_false_refusal else 0.0)
            key = (round(cost, 9), -min_score)
            if best is None or key < best["key"]:
                best = {
                    "key": key,
                    "min_score": round(min_score, 3),
                    "min_margin": round(min_margin, 4),
                    "false_refusal_rate": round(fr_rate, 4),
                    "missed_refusal_rate": round(mr_rate, 4),
                }

    assert best is not None
    best.pop("key", None)
    best.update({
        "calibrated_at": _utcnow(),
        "model": f"{cfg.embedding.model}@{cfg.embedding.dim}",
        "n_answerable": n_ans,
        "n_should_refuse": n_ref,
        "max_false_refusal": max_false_refusal,
        "reliable": n_ref >= 5 and n_ans >= 10,
        "samples": [{"id": r["id"], "top": r["top"], "margin": r["margin"],
                     "expect_refusal": r["expect_refusal"]} for r in rows],
    })
    return best


def save_calibration(path: Path, data: dict) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
