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


# ------------------------------------------------------------------ 答案质量评测
#
# 检索指标（Recall@K / MRR / 拒答）只能证明"捞到了对的片段"，证明不了
# "最终答案对不对、有没有编"。README 的「已知限制」里就把"逐句忠实度判定"
# 列为 P2 未做项。这里补一层 LLM-as-judge：先生成答案，再用一个模型当裁判，
# 给**忠实度**（每句结论有没有出处支撑 / 有没有幻觉）和**答案相关性**
# （有没有答到点子上）打分。
#
# 设计约束：
#   1. 这是**可选增强**——`run_eval` 的检索-only 路径完全不变、无 LLM 也能跑。
#   2. 生成实现不写死在评测模块里：`gen(question, candidates) -> str` 由调用方
#      注入（CLI 用主项目生成链路，测试用假模型），评测只负责编排与打分。
#   3. judge 解析失败 / 无 LLM 时打分记 None，不假装量到了。


_FAITHFULNESS_SYSTEM = (
    "你是严格的答案质量裁判。只依据给定的【检索上下文】评判，禁止使用你自己的知识。"
    "逐句核查：答案里有没有出现上下文没支撑的说法（幻觉）。"
    "返回严格 JSON：{\"faithful\": 0到1的浮点数, \"relevant\": 0到1的浮点数, "
    "\"reason\": \"简短中文说明\"}。"
    "faithful=1 表示答案完全由上下文支撑、无编造；relevant=1 表示答案直接回答了问题。"
)


def judge_answer(
    llm: Any,
    *,
    question: str,
    answer: str,
    context: list[str],
    attempts: int = 2,
) -> tuple[float | None, float | None, str, str]:
    """用 LLM 当裁判给答案打分。返回 `(faithful, relevant, reason, error)`。

    `llm` 为 None 或解析失败时，faithful / relevant 为 None，error 说明原因。
    调用方据此区分"量到了"和"没量到"，不会拿空数据当指标。
    """
    if llm is None:
        return None, None, "", "no_llm"
    ctx = "\n\n".join(f"[{i + 1}] {c}" for i, c in enumerate(context)) or "（无检索上下文）"
    user = (
        f"【问题】\n{question}\n\n【检索上下文】\n{ctx}\n\n【待评判答案】\n{answer}"
    )
    last_err = ""
    for _ in range(max(1, attempts)):
        try:
            out = llm.complete(
                [
                    {"role": "system", "content": _FAITHFULNESS_SYSTEM},
                    {"role": "user", "content": user},
                ],
                temperature=0.0,
                max_tokens=400,
            )
        except Exception as exc:  # 裁判挂了不能让整次评测崩
            return None, None, "", f"{type(exc).__name__}: {exc}"
        text = (getattr(out, "text", None) or "").strip()
        faithful, relevant, reason, err = _parse_judge(text)
        if err is None:
            return faithful, relevant, reason, None
        last_err = err
    return None, None, "", last_err or "judge_parse_failed"


def _parse_judge(text: str) -> tuple[float | None, float | None, str, str]:
    """从裁判输出里抠出 JSON。容忍模型在 JSON 前后多嘴。"""
    s, e = text.find("{"), text.rfind("}")
    if s == -1 or e == -1 or e <= s:
        return None, None, "", "no_json"
    try:
        obj = json.loads(text[s:e + 1])
    except json.JSONDecodeError:
        return None, None, "", "bad_json"

    def _f(v: object) -> float | None:
        try:
            return max(0.0, min(1.0, float(v)))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None

    return _f(obj.get("faithful")), _f(obj.get("relevant")), str(obj.get("reason") or ""), None


def _mean(vals: list[float | None]) -> float | None:
    xs = [v for v in vals if v is not None]
    return round(sum(xs) / len(xs), 4) if xs else None


@dataclass
class AnswerCaseResult:
    id: str
    question: str
    refused: bool
    rank: int | None
    answer: str
    faithful: float | None
    relevant: float | None
    judge_error: str
    latency_ms: int


@dataclass
class AnswerEvalReport:
    k: int
    retrieval: "EvalReport"
    cases: list[AnswerCaseResult]
    created_at: str = field(default_factory=_utcnow)

    def as_dict(self) -> dict:
        return {
            "created_at": self.created_at,
            "k": self.k,
            "retrieval_metrics": self.retrieval.metrics,
            "retrieval_refusal": self.retrieval.refusal,
            "answer": {
                "n": len(self.cases),
                "n_answered": sum(1 for c in self.cases if c.answer),
                "mean_faithful": _mean([c.faithful for c in self.cases]),
                "mean_relevant": _mean([c.relevant for c in self.cases]),
                "judge_errors": sum(1 for c in self.cases if c.judge_error),
            },
            "cases": [c.__dict__ for c in self.cases],
        }


def run_eval_answer(
    cfg: Config,
    conn,
    engine,
    llm: Any,
    *,
    k: int = DEFAULT_K,
    cases: list[dict] | None = None,
    rerank: bool | None = None,
    mmr: bool | None = None,
    gen: Any = None,
) -> AnswerEvalReport:
    """答案质量评测：生成答案 + LLM 裁判打分。需要 LLM（生成与裁判都走它）。

    `gen` 为 `Callable[[question, candidates], str]`——把"怎么生成答案"交给
    调用方，评测模块不绑死生成实现。`gen` 为 None 时只出检索指标、不做答案评测。
    """
    import time

    cases = cases if cases is not None else load_cases(Path(cfg.eval.dataset))
    # 检索指标复用同一套（单一事实来源，避免两套算法漂移）。
    retrieval = run_eval(cfg, conn, engine, k=k, cases=cases, rerank=rerank, mmr=mmr)
    results: list[AnswerCaseResult] = []
    for case in cases:
        question = case["question"]
        started = time.perf_counter()
        res = engine.search(
            conn, question, kb_id=case.get("kb_id"), top_k=k, rerank=rerank, mmr=mmr
        )
        refused = bool(res.refusal and res.refusal.refused)
        rank = None
        expect = case.get("expect")
        if expect and not refused:
            for cand in res.candidates[:k]:
                if _match(cand, expect):
                    rank = cand.final_rank
                    break

        answer = ""
        faithful = relevant = None
        judge_error = ""
        if not refused and gen is not None:
            try:
                answer = (gen(question, res.candidates) or "").strip()
            except Exception as exc:
                judge_error = f"gen:{type(exc).__name__}: {exc}"
            if answer:
                context = [f"{c.doc_title}：{c.text}" for c in res.candidates[:k]]
                faithful, relevant, _, judge_error = judge_answer(
                    llm, question=question, answer=answer, context=context
                )
        results.append(
            AnswerCaseResult(
                id=case.get("id") or question[:12],
                question=question,
                refused=refused,
                rank=rank,
                answer=answer[:500],
                faithful=faithful,
                relevant=relevant,
                judge_error=judge_error,
                latency_ms=int((time.perf_counter() - started) * 1000),
            )
        )
    return AnswerEvalReport(k=k, retrieval=retrieval, cases=results)


def format_answer_report(report: AnswerEvalReport) -> str:
    m = report.retrieval.metrics
    a = report.as_dict()["answer"]
    out = [
        "═══ 答案质量评测（LLM-as-judge）═══",
        f"评估集 {m['n_cases']} 条   K={report.k}",
        f"检索  Recall@{report.k} = {m['recall_at_k']:.1%}   MRR = {m['mrr']:.4f}",
        f"生成答案 {a['n_answered']}/{m['n_cases']} 条",
        f"忠实度 mean = {a['mean_faithful'] if a['mean_faithful'] is not None else 'N/A'}",
        f"相关性 mean = {a['mean_relevant'] if a['mean_relevant'] is not None else 'N/A'}",
        f"裁判失败 {a['judge_errors']} 条",
    ]
    bad = [c for c in report.cases if c.judge_error and not c.refused]
    if bad:
        out.append("")
        out.append("裁判异常：")
        for c in bad:
            out.append(f"  {c.id}  {c.judge_error}  {c.question}")
    low = [c for c in report.cases
           if c.faithful is not None and c.faithful < 0.6]
    if low:
        out.append("")
        out.append("忠实度偏低（<0.6，疑似编造）：")
        for c in low:
            out.append(f"  {c.id}  faithful={c.faithful}  {c.question}")
    return "\n".join(out)


def format_calibration(cal: dict) -> str:
    """把标定结果（含每题真实分数）渲染成可读报告 + 分数分布直方图。

    标定的本质是"看清楚两类样本的分数长什么样，再把刀切在它们之间的空档"。
    只给一个最优阈值、不给人看分布，等于又把"该切哪"交给用户去猜——这正是
    以前写死 0.35 招骂的原因。这里把可答 / 应拒两类的 top_score 分布画出来，
    阈值落在哪个空档一目了然。
    """
    samples = cal.get("samples") or []
    ans = [s["top"] for s in samples if not s["expect_refusal"] and s["top"] is not None]
    ref = [s["top"] for s in samples if s["expect_refusal"] and s["top"] is not None]
    buckets = 10
    lo, hi, w = 0.0, 1.0, 0.1

    def _hist(xs: list[float], label: str, lines: list[str]) -> None:
        lines.append(f"  {label}（n={len(xs)}）")
        if not xs:
            lines.append("    （无样本）")
            return
        counts = [0] * buckets
        for x in xs:
            i = min(buckets - 1, int(x // w))
            counts[i] += 1
        peak = max(counts) or 1
        for i, c in enumerate(counts):
            rng = f"{lo + i * w:.1f}–{lo + (i + 1) * w:.1f}"
            bar = "█" * c + " " * (peak - c)
            lines.append(f"    {rng} │{bar} {c}")

    lines = ["分数分布（top_score，越高越像「能答」）："]
    _hist(ans, "可答  ", lines)
    lines.append("")
    _hist(ref, "应拒  ", lines)
    lines.append("")
    lines.append(
        f"建议阈值  min_score = {cal.get('min_score')}   "
        f"min_margin = {cal.get('min_margin')}"
    )
    lines.append(
        f"误拒率 {cal.get('false_refusal_rate'):.1%}   "
        f"漏拒率 {cal.get('missed_refusal_rate'):.1%}   "
        f"（可答 {cal.get('n_answerable')} / 应拒 {cal.get('n_should_refuse')}）"
    )
    if not cal.get("reliable", True):
        lines.append(
            "⚠ 样本不足（建议 ≥10 条可答题、≥5 条应拒题），分布稀疏，阈值随样本抖。"
        )
    return "\n".join(lines)
