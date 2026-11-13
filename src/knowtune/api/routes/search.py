"""检索调试、带引用问答（SSE）、请求日志路由。"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import asdict
from typing import Any, Iterator

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from ...config import Config, refusal_mode
from ...db import ensure_session, save_message as _save, utcnow as _utcnow
from ...sse import sse as _sse
from ...generate import cite as cite_mod
from ...generate import llm as llm_mod
from ...generate import support as support_mod
from ...generate.llm import LLMError
from ...generate.prompt import REFUSE_TEMPLATE, build_context, build_direct
from ...retrieve.engine import RetrievalResult
from ... import chatctx, kbconfig, logbus
from ... import roles as roles_mod
from ... import services as services_mod
from ..state import state

router = APIRouter()


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    kb_id: int | None = None
    session_id: str | None = None
    provider: str | None = None
    rerank_model: str | None = None
    rerank: bool | None = None
    top_k: int | None = Field(default=None, ge=1, le=20)
    # F2.7 元数据过滤：只在这几类文档里检索
    source_types: list[str] | None = None
    doc_ids: list[int] | None = None
    imported_after: str | None = None
    imported_before: str | None = None

    # ---- 本次提问临时生效的覆盖（不写盘，只影响这一条请求）----
    model: str | None = None
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    max_tokens: int | None = Field(default=None, ge=64, le=8192)
    stream: bool | None = None
    mmr: bool | None = None
    # 点分路径 → 值的临时参数覆盖。白名单由 kbconfig.effective 把关
    # （不在 KB_PARAM_MAP 里的键直接丢弃），前端传再多也改不动别的配置。
    overrides: dict[str, Any] | None = None
    # 不挂载知识库时：True=完全不检索、直接问模型；False=跨全部库检索。
    direct: bool = False

    def filters(self) -> dict:
        return {
            "source_types": self.source_types,
            "doc_ids": self.doc_ids,
            "imported_after": self.imported_after,
            "imported_before": self.imported_before,
        }


class DebugRequest(AskRequest):
    top_k: int | None = Field(default=None, ge=1, le=50)


class SupportProbe(BaseModel):
    """句 × 块的余弦矩阵。标定 support 阈值时用，不做任何判定。"""

    sentences: list[str] = Field(min_length=1, max_length=300)
    blocks: list[str] = Field(min_length=1, max_length=300)
    block_chars: int = Field(default=600, ge=100, le=4000)
    # >0 时按「块内滑窗、每块取窗口最大分」计算——与运行时口径一致。
    # 0 表示整块算，只用于对照实验。
    window: int = Field(default=0, ge=0, le=2000)
    step: int = Field(default=0, ge=0, le=1000)


@router.post("/api/search/debug")
def search_debug(req: DebugRequest) -> dict:
    started = time.perf_counter()
    cfg = state.effective_cfg(req.kb_id)
    result = state.engine.search(
        state.conn, req.question, kb_id=req.kb_id, top_k=req.top_k, rerank=req.rerank,
        filters=req.filters(), cfg=cfg,
    )
    payload = _debug_payload(result, cfg, state.tuned(req.kb_id))
    payload["total_ms"] = int((time.perf_counter() - started) * 1000)
    return payload


@router.post("/api/search/debug/support")
def support_probe(req: SupportProbe) -> dict:
    """把「这句有没有被那块支撑」的原始分数交出来。

    存在的理由：support 阈值只能用**你这个库的句对分布**扫，而扫的时候要
    几千对句子乘块。让标定脚本自己装一套 torch + 嵌入模型来算余弦，是把
    几十行的活变成几个 G 的依赖；这里已经把模型加载好了，直接问它。

    只返回分数，不返回向量——向量除了算余弦没别的用，传出去只是把接口
    变成一个免费的 embedding 代理。
    """
    import numpy as np

    emb = state.engine.embedder
    try:
        if not emb.available:
            emb.load()
        trimmed = [b[: req.block_chars] for b in req.blocks]
        if req.window:
            step = req.step or max(1, req.window // 2)
            win_texts: list[str] = []
            owner: list[int] = []
            for bi, b in enumerate(trimmed):
                for w in support_mod.split_windows(b, req.window, step):
                    win_texts.append(w)
                    owner.append(bi)
            probe_units = win_texts
        else:
            owner = list(range(len(trimmed)))
            probe_units = trimmed
        texts = list(req.sentences) + probe_units
        vecs = np.asarray(emb.encode(texts), dtype=np.float32)
    except Exception as exc:
        raise HTTPException(503, f"嵌入模型不可用：{type(exc).__name__}: {exc}") from exc
    if vecs.ndim != 2 or len(vecs) != len(texts):
        raise HTTPException(500, "编码结果与输入数量不符")
    vecs = vecs / np.maximum(np.linalg.norm(vecs, axis=1, keepdims=True), 1e-9)
    ns = len(req.sentences)
    raw = vecs[:ns] @ vecs[ns:].T
    if req.window:
        own = np.asarray(owner)
        sims = np.stack(
            [raw[:, own == bi].max(axis=1) for bi in range(len(req.blocks))], axis=1)
    else:
        sims = raw
    return {
        "sims": [[round(float(x), 4) for x in row] for row in sims],
        "n_sent": ns,
        "n_block": len(req.blocks),
        "n_window": int(sims.shape[1] if not req.window else len(probe_units)),
        "model": f"{state.cfg.embedding.model}@{state.cfg.embedding.dim}",
    }


def _debug_chunk_info(conn, chunk_ids: list[int]) -> dict[int, dict]:
    """批量取 chunk 的可读信息（文档名 / 章节路径 / 原文摘要）。

    调试面板如果只给 `chunk 2773 · 0.424`，用户只能看到一排编号和条形图——
    什么结论都得自己在脑内补。一次 JOIN 把 dense+sparse+fused 涉及的
    全部 id（最多 ~100 个）取回来，约几毫秒，不该省。
    """
    ids = sorted({int(c) for c in chunk_ids})
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"""
        SELECT c.id, d.title AS doc_title, c.page_no, c.heading_path, c.text
        FROM chunk c JOIN document d ON d.id = c.doc_id
        WHERE c.id IN ({placeholders})
        """,
        ids,
    ).fetchall()
    return {
        r["id"]: {
            "doc_title": r["doc_title"],
            "page_no": r["page_no"],
            "heading_path": r["heading_path"],
            "preview": (r["text"] or "").strip().replace("\n", " ")[:120],
        }
        for r in rows
    }


def _support_payload(answer: str, bundle, cfg: Config) -> dict:
    """答案支撑度：生成之后的校验。

    与检索阶段的拒答判定是两件事。拒答猜的是「库里有没有」，靠 top1 余弦——
    它量的是话题相似度，主题相邻的问题（库里有 K8s、问的是 GPU 推理）照样拿
    高分，所以那个判据天生分不开。支撑度问的是「这段话在引用的资料里找得到吗」，
    答案写出来了才判，判得准。

    先看 is_loaded 是刻意的：`encode()` 会顺手把模型加载起来（import torch +
    读权重，冷启动几十秒）。一条普通问答顺手把服务卡住半分钟，比少给一个指标
    糟得多。
    """
    if not cfg.support.enabled:
        return {"enabled": False, "skipped": "支撑度校验已关闭", "verdict": "unknown"}
    if not bundle.citations:
        return {"enabled": True, "skipped": "本次未使用资料（直答模式）", "verdict": "unknown"}
    if not state.embedder.is_loaded:
        return {"enabled": True, "skipped": "嵌入模型未加载，跳过语义支撑判定", "verdict": "unknown"}

    evidence = {c.n: t for c, t in zip(bundle.citations, bundle.used_texts)}
    started = time.perf_counter()
    rep = support_mod.evaluate(
        answer,
        evidence,
        state.embedder.encode,
        threshold=cfg.support.threshold,
        max_sentences=cfg.support.max_sentences,
        min_answer_chars=cfg.support.min_answer_chars,
    )
    out = rep.as_dict()
    out.update({
        "enabled": True,
        "weak_ratio": cfg.support.weak_ratio,
        "ms": int((time.perf_counter() - started) * 1000),
    })
    return out


def _debug_payload(result, cfg: Config | None = None, overrides: dict | None = None) -> dict:
    """一次检索的结果同时服务"回答"和"调试"两个消费者，绝不为了调试再跑一遍。"""
    cfg = cfg or state.cfg
    info = _debug_chunk_info(
        state.conn,
        [h.chunk_id for h in result.dense_hits] + [h.chunk_id for h in result.sparse_hits],
    )

    def readable(chunk_id: int) -> dict:
        # 查不到（文档刚删、跨库脏 id）时别让前端渲染出 undefined
        return info.get(
            int(chunk_id),
            {"doc_title": "?", "page_no": None, "heading_path": "", "preview": ""},
        )

    return {
        "query": result.query,
        "params": {
            "dense_top_k": cfg.retrieval.dense.top_k,
            "sparse_top_k": cfg.retrieval.sparse.top_k,
            "rrf_k": cfg.retrieval.fusion.k,
            "fusion_method": cfg.retrieval.fusion.method,
            "w_dense": cfg.retrieval.fusion.w_dense,
            "w_sparse": cfg.retrieval.fusion.w_sparse,
            "fusion_top_k": cfg.retrieval.fusion.top_k,
            "rerank_model": result.rerank_model,
            "rerank_used": result.rerank_used,
            "rerank_cached": result.rerank_cached,
            "refusal_min_score": cfg.refusal.min_score,
            "refusal_min_margin": cfg.refusal.min_margin,
            "kb_overrides": len(overrides or {}),
        },
        "stages": {
            "dense": [
                {**readable(h.chunk_id), "chunk_id": h.chunk_id,
                 "score": round(h.score, 4), "rank": h.rank}
                for h in result.dense_hits
            ],
            "sparse": [
                {**readable(h.chunk_id), "chunk_id": h.chunk_id,
                 "score": round(h.score, 4), "rank": h.rank}
                for h in result.sparse_hits
            ],
            "fused": [
                {**readable(f["chunk_id"]), "chunk_id": f["chunk_id"],
                 "rrf": round(f["rrf"], 6),
                 "dense_rank": f["dense_rank"],
                 "sparse_rank": f["sparse_rank"]}
                for f in result.fused
            ],
        },
        "final": [
            {
                "n": c.final_rank,
                "chunk_id": c.chunk_id,
                "doc_title": c.doc_title,
                "page_no": c.page_no,
                "heading_path": c.heading_path,
                "dense_rank": c.dense_rank,
                "sparse_rank": c.sparse_rank,
                "dense_score": round(c.dense_score, 4) if c.dense_score is not None else None,
                "sparse_score": round(c.sparse_score, 4) if c.sparse_score is not None else None,
                "rrf": round(c.rrf_score, 6),
                "rerank_score": round(c.rerank_score, 4) if c.rerank_score is not None else None,
                "preview": c.text[:220],
            }
            for c in result.candidates
        ],
        "timings_ms": result.timings,
        "degraded": result.degraded,
        "refusal": {
            "refused": result.refusal.refused if result.refusal else False,
            "reason": result.refusal.reason if result.refusal else None,
            "top_score": result.refusal.top_score if result.refusal else None,
            "margin": result.refusal.margin if result.refusal else None,
            "calibrated": result.refusal.calibrated if result.refusal else False,
            "low_confidence": result.refusal.low_confidence if result.refusal else False,
            "confidence_reason": result.refusal.confidence_reason if result.refusal else None,
        },
    }


def _history(conn, session_id: str, turns: int) -> list[dict]:
    rows = conn.execute(
        "SELECT role, content FROM message WHERE session_id=? ORDER BY id DESC LIMIT ?",
        (session_id, turns * 2),
    ).fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


def _context_usage(conn, session_id: str | None, history: list[dict], cfg) -> dict:
    """这一轮到底装了多少历史、还剩多少预算。

    规则本身在 chatctx（不依赖 torch，能单测）；这里只负责把"会话里一共多少条
    消息"查出来喂进去。
    """
    total_msgs = 0
    if session_id:
        row = conn.execute(
            "SELECT count(*) FROM message WHERE session_id=?", (session_id,)
        ).fetchone()
        total_msgs = int(row[0]) if row else 0
    return chatctx.usage(
        history,
        total_messages=total_msgs,
        budget=int(cfg.generation.context_token_budget),
        turns_limit=int(cfg.chat.history_turns),
        min_chars=int(cfg.generation.min_context_chars),
    )


@router.post("/api/ask")
def ask(req: AskRequest) -> Response:
    trace_id = str(uuid.uuid4())
    conn = state.conn
    # 这个库自己的一套参数（没有覆盖时就是全局对象本身，零开销）
    kb_cfg = state.effective_cfg(req.kb_id)
    # 对话页「高级参数」里的临时覆盖：只在本次请求生效，不写盘。
    # 借 kbconfig 那条链路是为了白名单和类型校验都只有一份实现。
    if req.overrides:
        kb_cfg = kbconfig.effective(kb_cfg, req.overrides)
    kb_overrides = state.tuned(req.kb_id)

    session_id = req.session_id
    ensure_session(conn, session_id, req.kb_id, req.question)
    history = _history(conn, session_id, kb_cfg.chat.history_turns) if session_id else []
    # 轮数只是第一道闸：5 轮长回答照样能把资料预算吃光。再按字符预算裁一次，
    # 从最早一轮开始整轮丢——半截对话比没有对话更容易引出错误指代。
    history = chatctx.trim_history(history, budget=int(kb_cfg.chat.history_token_budget))
    context_usage = _context_usage(conn, session_id, history, kb_cfg)

    # 本次提问真正要用的后端：对话页下拉选的那个优先，没传才落到库/系统当前后端。
    # 这个值算一次、全程复用——下面提示词、报错指路、探测缓存作废、query_log
    # 全都得指同一个后端。散着写 kb_cfg.generation.provider 的后果是：用户选了
    # deepseek 提问，401 报错却说"后端 siliconflow 密钥无效"，日志里也记成
    # siliconflow，等于"选哪个都是测当前那个"。
    gen_provider = req.provider or kb_cfg.generation.provider
    # 角色值可能带「::模型」，模型优先级：请求显式指定 > 角色值里的 > 后端默认。
    _ref_base, _ref_model = roles_mod.split(gen_provider)
    try:
        _gen_name, _gen_prov = state.llm.resolve(_ref_base or None)
        gen_model = req.model or _ref_model or _gen_prov.model
    except llm_mod.LLMError:
        # 后端名不存在：不在这里拦（下面调用会给出同一份报错），只把名字留下来
        _gen_name, gen_model = gen_provider, req.model or ""

    def stream() -> Iterator[str]:
        """StreamingResponse 会把同步生成器丢到线程池里迭代，而上面那个 conn 是在
        路由线程里创建的——sqlite3 连接不能跨线程用。所以这里必须重新取连接。

        外面这层 try 是最后一道防线：生成器里抛异常时 HTTP 200 已经发出去了，
        不兜的话前端只会看到"永远转圈"，连错误信息都拿不到。
        """
        try:
            yield from _impl()
        except Exception as exc:
            yield _sse("error", {"message": f"{type(exc).__name__}: {exc}", "trace_id": trace_id})

    def _impl() -> Iterator[str]:
        # 嵌入模型还没装进内存时，检索一定会卡在第一次 encode 上（40 秒）。
        # 与其让前端转圈到超时，不如立刻说清"在加载、还差多久"——用户能判断
        # 是等一等还是去查配置，而"一直转圈"什么信息都不给。
        if not state.embedder.is_loaded:
            w = state.warm
            if w["state"] in ("pending", "loading"):
                waited = int((time.time() - w["started_at"]))
                msg = (f"嵌入模型正在后台加载（已 {waited}s，通常 30-60 秒）。"
                       f"状态栏显示「索引就绪」后再问即可。")
            else:
                msg = f"嵌入模型加载失败：{w.get('error') or state.embedder.error or '未知原因'}"
            logbus.note(f"提问被拒：{msg}", level="WARNING", source="ask")
            yield _sse("error", {"message": msg, "trace_id": trace_id})
            return

        conn = state.db.conn()
        started = time.perf_counter()
        timings: dict[str, int] = {}
        if not req.direct:
            yield _sse("progress", {"stage": "retrieving",
                                    "label": "正在检索与精排…"})
        if req.direct:
            # 不挂载知识库 + 用户选了「直接问模型」：检索整段跳过。
            # 这里必须是"一条空结果"而不是 None——下面拒答判定、引用校验、
            # 计时统计全都按 result 走，空对象能让它们自然退化成"没资料"。
            result = RetrievalResult(query=req.question, candidates=[], timings=timings)
        else:
            result = state.engine.search(
                conn,
                req.question,
                kb_id=req.kb_id,
                top_k=req.top_k,
                rerank=req.rerank,
                rerank_model=req.rerank_model,
                mmr=req.mmr,
                filters=req.filters(),
                cfg=kb_cfg,
            )
        timings.update(result.timings)
        yield _sse("progress", {"stage": "generating",
                                "label": "检索完成，正在生成答案…"})

        if result.refusal and result.refusal.refused:
            payload_meta = {
                "trace_id": trace_id,
                "mode": "refuse",
                "reason": result.refusal.reason,
                "calibrated": result.refusal.calibrated,
                # advisory = 阈值没标定过，这次拦截只是"明显不沾边"的粗筛，
                # 不是统计上站得住的判定。前端不该把它渲染成确定结论。
                "refusal_mode": refusal_mode(kb_cfg),
                "candidates": len(result.candidates),
                "degraded": result.degraded,
                "timings_ms": timings,
                "context": context_usage,
                "debug": _debug_payload(result, kb_cfg, kb_overrides),
            }
            yield _sse("meta", payload_meta)
            for i in range(0, len(REFUSE_TEMPLATE), 12):
                yield _sse("delta", {"text": REFUSE_TEMPLATE[i : i + 12]})
            msg_id = (
                _save(conn, session_id, req.question, REFUSE_TEMPLATE, trace_id, [])
                if session_id else None
            )
            yield _sse("done", {
                "trace_id": trace_id, "refused": True,
                "provider": gen_provider, "model": gen_model,
                "citations": [],
                "cite_coverage": 0.0, "invalid_ids": [], "degraded": result.degraded,
                "answer": REFUSE_TEMPLATE,
                "message_id": msg_id,
            })
            _log(conn, trace_id, req, result, timings, "refuse", 0, 0, 0.0, history_len=len(history), provider=gen_provider)
            return

        bundle = (
            build_direct(req.question, history) if req.direct
            else build_context(
                result.candidates,
                question=req.question,
                token_budget=kb_cfg.generation.context_token_budget,
                min_context_chars=kb_cfg.generation.min_context_chars,
                prompt_version=state.cfg.prompt_version,
                history_text_tokens=sum(len(h["content"]) for h in history),
                # 这个后端待会儿真的要拿去生成，提示词就得取它那份覆盖。
                # 取成"系统当前后端"的话，界面切到别的后端后提示词会张冠李戴。
                provider=gen_provider,
            )
        )
        yield _sse("meta", {
            "trace_id": trace_id,
            "mode": "direct" if req.direct else "answer",
            "citations": [asdict(c) for c in bundle.citations],
            "context_used": bundle.used,
            "context_dropped": bundle.dropped,
            "context": context_usage,
            "degraded": result.degraded,
            "timings_ms": timings,
            "confidence": {
                "low": bool(result.refusal.low_confidence) if result.refusal else False,
                "reason": result.refusal.confidence_reason if result.refusal else None,
                "top_score": result.refusal.top_score if result.refusal else None,
            },
            "debug": _debug_payload(result, kb_cfg, kb_overrides),
            # 回显本次真正用的后端与模型：对话页下拉选了 A 却答的是 B，这件事
            # 只有服务端知道，前端不显示就没人能发现"选哪个都没区别"。
            "provider": gen_provider,
            "model": gen_model,
        })

        messages = bundle.messages(req.question, history)
        acc: list[str] = []
        error: str | None = None
        gen_started = time.perf_counter()
        try:
            for piece in state.llm.stream(
                messages,
                provider=gen_provider,
                model=req.model,
                temperature=req.temperature,
                max_tokens=req.max_tokens,
            ):
                acc.append(piece)
                yield _sse("delta", {"text": piece})
        except GeneratorExit:
            # 客户端断开（关标签页／切走／网络掉）。这时回答只生成了一半，token
            # 已经花出去了，却不会再走到下面那个 _log —— query_log 里就没有这次
            # 请求。而它恰好属于"花了钱但没人看完"那一类，统计里漏掉它，等于把
            # 最该被看见的一批藏起来（总量看着正常，成本却对不上账）。
            timings["gen_ms"] = int((time.perf_counter() - gen_started) * 1000)
            try:
                _log(conn, trace_id, req, result, timings, "disconnect", 0, 0, 0.0,
                     len(history), provider=gen_provider)
            except Exception:
                pass  # 记日志失败不能盖住断开信号本身
            raise  # GeneratorExit 必须继续往上抛，否则生成器收不干净
        except LLMError as exc:
            # 原始报错是一串英文（"You didn't provide an API key…"），不说人话
            # 也不指路。补一句"下一步该干什么"，否则用户只知道"它又坏了"。
            error = str(exc)
            hint = llm_mod.human_hint(exc, gen_provider)
            if hint:
                error = f"{error}\n\n{hint}"
            # 调用被拒 = 探测时那把钥匙已经不作数了（换过、或重启后内存里没了）。
            # 缓存不作废的话，模型页会继续显示上一把钥匙探出来的绿灯。
            if getattr(exc, "status", None) in (401, 403):
                services_mod.invalidate(gen_provider)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        gen_ms = int((time.perf_counter() - gen_started) * 1000)
        timings["gen_ms"] = gen_ms

        answer = cite_mod.normalize_cites("".join(acc))
        if error and not answer:
            yield _sse("error", {"message": error, "trace_id": trace_id})
            _log(conn, trace_id, req, result, timings, "error", 0, 0, 0.0, len(history), provider=gen_provider)
            return

        report = cite_mod.verify(answer, len(bundle.citations))
        # 照抄检测：拿本次真正装进提示词的那几块原文比对（bundle.used 才是
        # 模型看到的资料，用它而不是全库，避免"抄了别的块"也算进比例）。
        vr = cite_mod.verbatim_ratio(answer, bundle.used_texts)
        if vr >= 0.6 and not report.blanket:
            report.blanket = f"疑似照抄原文：{vr:.0%} 的句子与资料逐字相同，未归纳作答"
        # 覆盖率管格式（编号在不在、越没越界），支撑度管内容（编号指的那段是不是
        # 真在说这句话）。两个一起给：只报覆盖率时，"覆盖率 100% + 整段胡说"是
        # 可以同时成立的，界面上就成了自相矛盾的信号。
        sup = _support_payload(answer, bundle, kb_cfg)
        timings["support_ms"] = sup.get("ms", 0)
        citations = [asdict(c) for c in bundle.citations]
        msg_id = (
            _save(conn, session_id, req.question, answer, trace_id, citations)
            if session_id else None
        )
        yield _sse("done", {
            "trace_id": trace_id,
            "refused": False,
            "provider": gen_provider,
            "model": gen_model,
            "citations": citations,
            "cite_coverage": report.coverage,
            "cite_blanket": report.blanket,
            "invalid_ids": report.invalid_ids,
            "uncited": report.uncited_sentences,
            "support": sup,
            "degraded": result.degraded + ([f"llm_error:{error}"] if error else []),
            "answer": answer,
            "message_id": msg_id,
        })
        _log(conn, trace_id, req, result, timings, "ok", 0, 0, report.coverage, len(history), provider=gen_provider)
        _ = started  # 保留给 trace 扩展，避免被 lint 判定未使用

    if not (kb_cfg.generation.stream if req.stream is None else req.stream):
        # stream=false：把同一条链路整条跑完，只取 done 事件的载荷当 JSON 返回。
        #
        # 不给非流式单写一遍"检索 + 生成 + 引用校验"——两份实现必然发散，而
        # 分叉处的差异（拒答判定、引用覆盖率、message_id 落库）恰恰是最难被
        # 发现的地方。这里只是把 SSE 当内部传输格式消费掉，对外是一份完整 JSON。
        #
        # 顺带的好处：走不支持 SSE、或者会在网关处被缓冲截断的客户端时，一次
        # 性响应比流式更可靠。代价是首字延迟变成整段延迟，所以默认仍是流式。
        done_payload: dict | None = None
        meta_payload: dict | None = None
        failure: str | None = None
        for line in stream():
            if line.startswith("event: done"):
                done_payload = json.loads(line.partition("\n")[2][len("data: "):].strip())
            elif line.startswith("event: meta"):
                # meta 里是引用列表、上下文占用、调试信息——非流式客户端同样
                # 需要它们，否则只能拿到一段没有出处的文字
                meta_payload = json.loads(line.partition("\n")[2][len("data: "):].strip())
            elif line.startswith("event: error"):
                failure = json.loads(
                    line.partition("\n")[2][len("data: "):].strip()
                ).get("message", "")
        if done_payload is None:
            raise HTTPException(502, failure or "生成失败，未返回结果")
        done_payload["stream"] = False
        done_payload["meta"] = meta_payload
        return JSONResponse(done_payload, headers={"X-Trace-Id": trace_id})

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Trace-Id": trace_id,
                 "X-Accel-Buffering": "no"},
    )


def _log(conn, trace_id: str, req: AskRequest, result, timings: dict, status: str,
         prompt_tokens: int, completion_tokens: int, coverage: float,
         history_len: int, provider: str | None = None) -> None:
    digest = hashlib.sha256(req.question.encode("utf-8")).hexdigest()[:16]
    provider_name = provider or req.provider or state.cfg.generation.provider
    provider_cfg = state.cfg.generation.providers.get(provider_name)
    model_name = provider_cfg.model if provider_cfg else provider_name
    candidates = [
        {
            "chunk_id": c.chunk_id,
            "dense_rank": c.dense_rank,
            "sparse_rank": c.sparse_rank,
            "rrf": round(c.rrf_score, 6),
            "rerank": round(c.rerank_score, 4) if c.rerank_score is not None else None,
        }
        for c in result.candidates[:10]
    ]
    conn.execute(
        """INSERT INTO query_log (trace_id, kb_id, question_digest, question_chars,
             retrieve_ms, rerank_ms, gen_ms, total_ms, prompt_tokens, completion_tokens,
             model, rerank_used, rerank_model, refused, refuse_reason, top_score,
             score_margin, candidates_json, degraded, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            trace_id, req.kb_id, digest, len(req.question),
            timings.get("dense_ms", 0) + timings.get("sparse_ms", 0) + timings.get("fusion_ms", 0),
            timings.get("rerank_ms", 0), timings.get("gen_ms", 0),
            sum(v for k, v in timings.items() if k.endswith("_ms")),
            prompt_tokens, completion_tokens,
            model_name,
            1 if result.rerank_used else 0, result.rerank_model,
            1 if (result.refusal and result.refusal.refused) else 0,
            result.refusal.reason if result.refusal else None,
            result.refusal.top_score if result.refusal else None,
            result.refusal.margin if result.refusal else None,
            json.dumps(candidates, ensure_ascii=False),
            ",".join(result.degraded) if result.degraded else None,
            _utcnow(),
        ),
    )
    _ = status, coverage, history_len


@router.get("/api/logs")
def logs(limit: int = 50, kb_id: int | None = None) -> list[dict]:
    sql = "SELECT * FROM query_log"
    params: list = []
    if kb_id is not None:
        sql += " WHERE kb_id = ?"
        params.append(kb_id)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(min(limit, 500))
    return [dict(r) for r in state.conn.execute(sql, params).fetchall()]


@router.get("/api/logs/system")
def system_logs(limit: int = 120, level: str | None = None) -> list[dict]:
    """进程内的环形日志。重启即清空——要长期证据看 /api/logs 里的 query_log。"""
    return logbus.entries(limit=limit, level=level)