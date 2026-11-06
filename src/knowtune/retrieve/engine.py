"""检索编排：双路召回 → 加权 RRF → 精排 → MMR → 拒答判定。

这条链上每一段都可以失败，且失败后都必须有明确去向（降级 + 打标），
不允许"某一路静默返回空"这种看起来正常、实际召回腰斩的状态。
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field

import numpy as np

from ..config import Config
from ..index.embedder import Embedder, EmbedderUnavailable
from ..index.sparse import SparseIndex
from ..index.vectors import VectorStore
from ..models import Candidate, Hit, RefusalDecision
from ..textnorm import fold_query
from .rerank import Reranker, mmr_select

# 低置信的判据。不是"要不要答"，而是"答了要提醒一句"。
# 0.10 这个余量来自实测分布：可答问题最低 0.560，硬阈值 0.35，
# 中间这段（0.35~0.45）基本是"沾点边但没有直接依据"，值得提醒。
# 实际取值在 refusal.soft_margin / refusal.soft_floor（可调参数面板）；
# 这里只留一份出厂默认，给"没走配置"的旧调用路径兜底。
SOFT_SCORE_MARGIN = 0.10
SOFT_MARGIN_FLOOR = 0.015


def _soft(top: float | None, margin: float | None, rcfg) -> tuple[bool, str | None]:
    """答完之后的置信度评估。返回值直接喂给 RefusalDecision 的两个尾巴字段。"""
    if top is None:
        return False, None
    margin_add = float(getattr(rcfg, "soft_margin", SOFT_SCORE_MARGIN))
    floor = float(rcfg.min_score) + margin_add
    if top < floor:
        return True, f"最高相关度 {top:.2f}，低于 {floor:.2f}"
    floor_eps = float(getattr(rcfg, "soft_floor", SOFT_MARGIN_FLOOR))
    if margin is not None and margin < floor_eps:
        return True, f"前几条候选分数几乎持平（差 {margin:.3f}）"
    return False, None


@dataclass
class RetrievalResult:
    query: str
    candidates: list[Candidate]
    dense_hits: list[Hit] = field(default_factory=list)
    sparse_hits: list[Hit] = field(default_factory=list)
    fused: list[dict] = field(default_factory=list)
    timings: dict[str, int] = field(default_factory=dict)
    degraded: list[str] = field(default_factory=list)
    rerank_model: str | None = None
    rerank_used: bool = False
    # 精排里有多少对（问题, 片段）命中了进程内缓存、没走模型。
    # rerank_ms 突然从 3000 掉到 1 时，看这个字段就知道是缓存还是出了别的事。
    rerank_cached: int = 0
    refusal: RefusalDecision | None = None


def _norm(hits: list[Hit], higher_is_better: bool = True) -> dict[int, float]:
    """把一路的原始分数压到 0~1（min-max，按本次查询的候选池归一）。

    RRF 只认名次：BM25 里"断崖式第一"和"险胜第二"在它眼里只差一个名次，
    于是稠密路那堆"语义沾边但不对"的候选能靠名次把决定性命中挤下去——
    实测单路 BM25 的 R@5 是 0.98，融合完反而掉到 0.75，就是这么丢的。
    归一化的意义是把"这一路有多确定"这个信息带进融合。

    `higher_is_better=False` 用于 bm25：它的分数是负的、越小越相关，
    不翻符号的话归一化会把最不相关的那条变成 1.0（第一版就是这个 bug，
    加权融合的 R@5 直接掉到 0.32）。
    """
    if not hits:
        return {}
    vals = [h.score if higher_is_better else -h.score for h in hits]
    lo, hi = min(vals), max(vals)
    if hi - lo < 1e-9:  # 整路分数持平 = 这一路给不出区分度，全给 0.5
        return {h.chunk_id: 0.5 for h in hits}
    return {
        h.chunk_id: ((h.score if higher_is_better else -h.score) - lo) / (hi - lo)
        for h in hits
    }


def _apply_quota(
    ordered: list[dict],
    rankings: list[list[Hit]],
    weights: list[float],
    quota: int,
) -> list[dict]:
    """保底配额：每一路的前 quota 名无条件进候选池。

    为什么需要：两路分数各自的量纲不同，融合前都要先归一化，而归一化是
    min-max —— 它只保留"这一路内部谁最高"，抹掉了"这一路到底有多可信"。
    于是稀疏路那条**完全无关**的第一名也拿到 1.0，乘以权重就压在稠密路第 9
    名头上。实测在古籍语料上就是这样：正确答案 dense 排第 9，融合后掉到第 15
    甚至池外，而排它前面的一堆稀疏噪声在原文里根本找不到对应内容。

    配额把这件事掰回来：不管分数怎么算，两路各自的前 quota 名都必须被看到，
    相关与否交给精排去判。没有精排时它也只是"顺序微调"，不会把噪声塞进前列
    —— 噪声候选本来就在各路尾部。
    """
    if quota <= 0:
        return ordered
    keep: set[int] = set()
    for hits, w in zip(rankings, weights):
        if w <= 0:
            continue
        keep.update(h.chunk_id for h in hits[:quota])
    head = [it for it in ordered if it["chunk_id"] in keep]
    tail = [it for it in ordered if it["chunk_id"] not in keep]
    return head + tail


def fuse_weighted(
    rankings: list[list[Hit]],
    weights: list[float],
    *,
    top_k: int,
    quota: int = 0,
) -> list[dict]:
    """归一化加权融合。与 RRF 互斥，由 retrieval.fusion.method 选择。"""
    total = sum(w for w in weights if w > 0) or 1.0
    merged: dict[int, float] = {}
    ranks: dict[int, dict[str, int]] = {}
    for hits, weight in zip(rankings, weights):
        if weight <= 0:
            continue
        # bm25 返回负值、越小越相关；靠 label 判断而不是让调用方传参数，
        # 是因为"这一路的方向"本来就是这一路自己的属性
        flip = bool(hits) and hits[0].label == "sparse"
        norm = _norm(hits, higher_is_better=not flip)
        for h in hits:
            merged[h.chunk_id] = merged.get(h.chunk_id, 0.0) + (weight / total) * norm.get(
                h.chunk_id, 0.0
            )
            ranks.setdefault(h.chunk_id, {})[h.label] = h.rank

    ordered = sorted(merged.items(), key=lambda kv: -kv[1])
    items = [
        {
            "chunk_id": cid,
            "rrf": score,  # 字段名沿用，只是数值含义换成"加权归一化分"
            "dense_rank": ranks.get(cid, {}).get("dense"),
            "sparse_rank": ranks.get(cid, {}).get("sparse"),
        }
        for cid, score in ordered
    ]
    return _apply_quota(items, rankings, weights, quota)[:top_k]


def fuse_rrf(
    rankings: list[list[Hit]],
    weights: list[float],
    *,
    k: int,
    top_k: int,
    quota: int = 0,
) -> list[dict]:
    """加权 RRF。权重只表达相对比例，之和不要求为 1——整体缩放不改排序。

    返回 [{chunk_id, rrf, dense_rank, sparse_rank}]，rank 从 1 起算。
    """
    scores: dict[int, float] = {}
    ranks: dict[int, dict[str, int]] = {}

    for hits, weight in zip(rankings, weights):
        if weight <= 0:
            continue
        for h in hits:
            scores[h.chunk_id] = scores.get(h.chunk_id, 0.0) + weight / (k + h.rank)
            ranks.setdefault(h.chunk_id, {})[h.label] = h.rank

    ordered = sorted(scores.items(), key=lambda kv: -kv[1])
    items = [
        {
            "chunk_id": cid,
            "rrf": score,
            "dense_rank": ranks.get(cid, {}).get("dense"),
            "sparse_rank": ranks.get(cid, {}).get("sparse"),
        }
        for cid, score in ordered
    ]
    return _apply_quota(items, rankings, weights, quota)[:top_k]


class RetrievalEngine:
    def __init__(
        self,
        cfg: Config,
        embedder: Embedder,
        vectors: VectorStore,
        sparse: SparseIndex,
        reranker: Reranker,
    ) -> None:
        self.cfg = cfg
        self.embedder = embedder
        self.vectors = vectors
        self.sparse = sparse
        self.reranker = reranker

    # ------------------------------------------------------------------ 主流程

    def search(
        self,
        conn: sqlite3.Connection,
        query: str,
        *,
        kb_id: int | None = None,
        doc_ids: list[int] | None = None,
        top_k: int | None = None,
        rerank: bool | None = None,
        rerank_model: str | None = None,
        mmr: bool | None = None,
        filters: dict | None = None,
        cfg: Config | None = None,
    ) -> RetrievalResult:
        # cfg 允许按请求传入：知识库级参数覆盖生成的副本从这里进来。
        # 绝不改写 self.cfg —— 它被多个库共享，就地改会把 A 库的口径串到 B 库。
        cfg = cfg or self.cfg
        rcfg = cfg.retrieval
        top_k = top_k or rcfg.rerank.top_k
        use_rerank = rcfg.rerank.enabled if rerank is None else rerank
        use_mmr = rcfg.mmr.enabled if mmr is None else mmr

        # 元数据过滤（F2.7）：先过滤后检索，两路用同一组条件，
        # 否则会出现"向量路过滤了、BM25 没过滤"的隐性漏检
        f = filters or {}
        doc_ids = doc_ids or f.get("doc_ids")
        f_source = f.get("source_types")
        f_after = f.get("imported_after")
        f_before = f.get("imported_before")

        degraded: list[str] = []
        timings: dict[str, int] = {}
        # 查询和语料必须用同一套码位规则。用户可能从 PDF 里复制"⽓"来搜
        # （那本来就是文档里的字符），不折就等于拿另一个字去比，两路全废。
        query = fold_query(query)
        result = RetrievalResult(query=query, candidates=[], degraded=degraded, timings=timings)

        # --- 1. dense 召回
        dense_hits: list[Hit] = []
        if rcfg.dense.enabled:
            try:
                # 模型冷启动（import torch + 读权重）单独计时：把它混进 dense_ms
                # 会让检索延迟看起来是 40 秒，之后所有性能判断都失去基准
                if not self.embedder.available:
                    t_load = time.perf_counter()
                    self.embedder.load()
                    timings["embed_load_ms"] = int((time.perf_counter() - t_load) * 1000)
                t0 = time.perf_counter()
                qvec = self.embedder.encode([self._query_text(query, cfg)])[0]
                dense_hits = self.vectors.search(
                    conn, qvec, top_k=rcfg.dense.top_k, kb_id=kb_id, doc_ids=doc_ids,
                    source_types=f_source, imported_after=f_after, imported_before=f_before,
                )
                timings["dense_ms"] = int((time.perf_counter() - t0) * 1000)
            except EmbedderUnavailable as exc:
                degraded.append("dense_unavailable")
                timings["dense_ms"] = 0
                self._note_embed_error(conn, str(exc))
        else:
            degraded.append("dense_disabled")
            timings["dense_ms"] = 0

        # --- 2. sparse 召回
        sparse_hits: list[Hit] = []
        if rcfg.sparse.enabled:
            try:
                if not self.sparse.ready:
                    t_seg = time.perf_counter()
                    self.sparse.warmup()
                    timings["segmenter_load_ms"] = int((time.perf_counter() - t_seg) * 1000)
                t0 = time.perf_counter()
                sparse_hits = self.sparse.search(
                    conn, query, top_k=rcfg.sparse.top_k, kb_id=kb_id, doc_ids=doc_ids,
                    source_types=f_source, imported_after=f_after, imported_before=f_before,
                )
                timings["sparse_ms"] = int((time.perf_counter() - t0) * 1000)
            except Exception as exc:
                degraded.append("sparse_failed")
                timings["sparse_ms"] = 0
                self._note(conn, f"sparse 检索失败：{type(exc).__name__}: {exc}")
        else:
            timings["sparse_ms"] = 0

        result.dense_hits = dense_hits
        result.sparse_hits = sparse_hits

        # --- 3. 融合。只有一路可用时权重归一，否则排序会被那一路的权重整体缩放
        t0 = time.perf_counter()
        rankings, weights = [], []
        if dense_hits:
            rankings.append(dense_hits)
            weights.append(rcfg.fusion.w_dense)
        if sparse_hits:
            rankings.append(sparse_hits)
            weights.append(rcfg.fusion.w_sparse)
        if not rankings:
            timings["fusion_ms"] = 0
            result.refusal = RefusalDecision(
                refused=True, reason="empty", top_score=None, margin=None,
                calibrated=self._calibrated(cfg),
            )
            return result

        if rcfg.fusion.method == "weighted":
            fused = fuse_weighted(rankings, weights, top_k=rcfg.fusion.top_k,
                                  quota=rcfg.fusion.quota)
        else:
            fused = fuse_rrf(rankings, weights, k=rcfg.fusion.k, top_k=rcfg.fusion.top_k,
                             quota=rcfg.fusion.quota)
        timings["fusion_ms"] = int((time.perf_counter() - t0) * 1000)
        result.fused = fused

        # --- 4. 取回元数据
        meta = self._load_meta(conn, [f["chunk_id"] for f in fused])
        candidates: list[Candidate] = []
        for rank, item in enumerate(fused, start=1):
            m = meta.get(item["chunk_id"])
            if not m:
                continue  # 索引刚被替换掉，静默跳过是正确行为
            candidates.append(
                Candidate(
                    chunk_id=item["chunk_id"],
                    doc_id=int(m["doc_id"]),
                    doc_title=m["title"] or m["source_uri"],
                    source_uri=m["source_uri"],
                    page_no=m["page_no"],
                    heading_path=m["heading_path"],
                    text=m["text"],
                    dense_rank=item["dense_rank"],
                    sparse_rank=item["sparse_rank"],
                    dense_score=self._score_of(dense_hits, item["chunk_id"]),
                    sparse_score=self._score_of(sparse_hits, item["chunk_id"]),
                    rrf_score=item["rrf"],
                    final_rank=rank,
                )
            )

        # --- 4b. 邻块扩展（可选）
        if rcfg.expand.enabled and candidates:
            self._expand_neighbors(conn, candidates, meta, rcfg.expand)

        # --- 5. 精排
        t0 = time.perf_counter()
        if use_rerank and candidates:
            doc_texts = [c.text for c in candidates]
            rr = self.reranker.rerank(
                query, doc_texts, model_name=rerank_model or rcfg.rerank.model
            )
            result.rerank_model = rr.model
            result.rerank_cached = rr.cached
            if rr.scores is None:
                degraded.append(rr.degraded or "rerank_failed")
                result.rerank_used = False
            else:
                for cand, score in zip(candidates, rr.scores):
                    cand.rerank_score = float(score)
                candidates.sort(key=lambda c: -(c.rerank_score or 0.0))
                result.rerank_used = True
        timings["rerank_ms"] = int((time.perf_counter() - t0) * 1000)

        # --- 6. MMR
        if use_mmr and len(candidates) > top_k:
            try:
                ordered_ids = [c.chunk_id for c in candidates]
                vecs = self.vectors.vectors_of(conn, ordered_ids)
                picked = mmr_select(
                    ordered_ids, vecs, k=top_k, lambda_=cfg.retrieval.mmr.lambda_
                )
                by_id = {c.chunk_id: c for c in candidates}
                candidates = [by_id[cid] for cid in picked if cid in by_id]
            except Exception as exc:
                degraded.append("mmr_failed")
                self._note(conn, f"MMR 失败：{type(exc).__name__}: {exc}")

        candidates = candidates[:top_k]
        for i, c in enumerate(candidates, start=1):
            c.final_rank = i
        result.candidates = candidates

        # --- 7. 拒答判定
        result.refusal = self._decide_refusal(candidates, degraded, dense_hits, cfg)
        timings["total_ms"] = sum(v for k, v in timings.items() if k.endswith("_ms"))
        return result

    # ------------------------------------------------------------------ 辅助

    @staticmethod
    def _query_text(query: str, cfg: Config) -> str:
        """查询侧指令前缀。

        bge-zh-v1.5 是对称模型，但官方对"短查询检索长段落"要求给查询加一句
        指令。关键是**只加在查询侧**：语料侧一旦也加上，两边一起平移，等于没加。
        """
        instr = (cfg.embedding.query_instruction or "").strip()
        if not instr or query.startswith(instr):
            return query
        return instr + query

    def _expand_neighbors(
        self,
        conn: sqlite3.Connection,
        candidates: list[Candidate],
        meta: dict[int, sqlite3.Row],
        ecfg,
    ) -> None:
        """把命中块的前后邻块补进来（就地修改 candidates 并重新排序）。

        切分会把一个论点劈在边界上：边界那句一半在这块、一半在邻块。邻块
        因为缺头/缺尾，向量分往往不够自己进 Top-K，但它装着的可能正是答案
        的另一半。折价是为了不让它把真正的命中块挤掉。
        """
        seeds = candidates[: ecfg.seed_top]
        if not seeds:
            return
        have = {c.chunk_id for c in candidates}

        want: list[int] = []
        for c in seeds:
            row = meta.get(c.chunk_id)
            if row is None:
                continue
            seq = int(row["seq"])
            sql = (
                "SELECT id, seq FROM chunk WHERE doc_id = ? AND generation = ?"
                "  AND seq BETWEEN ? AND ? AND is_hidden = 0 ORDER BY seq"
            )
            for r in conn.execute(
                sql, (int(row["doc_id"]), int(row["generation"]),
                      seq - ecfg.neighbors, seq + ecfg.neighbors)
            ).fetchall():
                if int(r["id"]) not in have:
                    want.append(int(r["id"]))
        if not want:
            return

        extra = self._load_meta(conn, want)
        # 邻块的分数 = 种子块分数 × 折扣，按这个分数重排，保证种子仍在前面
        seed_score = max((c.rrf_score or 0.0) for c in seeds)
        for cid, row in extra.items():
            candidates.append(
                Candidate(
                    chunk_id=cid,
                    doc_id=int(row["doc_id"]),
                    doc_title=row["title"] or row["source_uri"],
                    source_uri=row["source_uri"],
                    page_no=row["page_no"],
                    heading_path=row["heading_path"],
                    text=row["text"],
                    dense_rank=None,
                    sparse_rank=None,
                    dense_score=None,
                    sparse_score=None,
                    rrf_score=round(seed_score * float(ecfg.discount), 6),
                    final_rank=len(candidates) + 1,
                    expanded=True,
                )
            )
        candidates.sort(key=lambda c: -(c.rrf_score or 0.0))
        # 上限只用来防止极端情况（宽 seed_top）把列表撑爆；正常不该触发，
        # 因为精排需要足够的候选池，砍太狠等于把扩展的意义抹掉。
        cap = max(1, int(getattr(ecfg, "max_candidates", 60)))
        del candidates[cap:]

    def _calibrated(self, cfg: Config | None = None) -> bool:
        return bool((cfg or self.cfg).refusal.calibrated_at)

    def _decide_refusal(
        self,
        candidates: list[Candidate],
        degraded: list[str],
        dense_hits: list[Hit],
        cfg: Config | None = None,
    ) -> RefusalDecision:
        rcfg = (cfg or self.cfg).refusal
        calibrated = self._calibrated(cfg)

        # 分数必须先算：即便拒答被关闭，top/margin 也是"阈值标定"唯一的数据来源。
        # 早先在这里把 enabled=False 直接短路返回，导致标定脚本拿到的全是 None，
        # 扫出来的阈值毫无意义——这种"关掉功能顺手把数据也关掉"的写法最坑。
        window = max(1, int(getattr(rcfg, "margin_window", 5)))
        scores = [h.score for h in dense_hits[:window]]
        top = scores[0] if scores else None
        margin = None
        if len(scores) >= 2:
            margin = round(scores[0] - float(np.mean(scores[1:])), 4)

        if not candidates:
            return RefusalDecision(True, "empty", top, margin, calibrated)
        if not rcfg.enabled:
            return RefusalDecision(False, None, top, margin, calibrated, *_soft(top, margin, rcfg))

        if top is None:
            degraded.append("refusal_uncalibrated")
            return RefusalDecision(False, None, None, margin, False)

        if top < rcfg.min_score:
            return RefusalDecision(True, "low_score", top, margin, calibrated)
        if margin is not None and margin < rcfg.min_margin:
            # 分数全挤在一起 = 模型对哪条都没把握，top1 是抽签结果。
            # 注意 min_margin 默认 0.0：实测本语料的 margin 分布和"该不该拒"
            # 不相关（该拒的 0.034 vs 该答的 0.031），开着它只会误伤。
            return RefusalDecision(True, "low_margin", top, margin, calibrated)
        return RefusalDecision(False, None, top, margin, calibrated, *_soft(top, margin, rcfg))

    @staticmethod
    def _score_of(hits: list[Hit], chunk_id: int) -> float | None:
        for h in hits:
            if h.chunk_id == chunk_id:
                return h.score
        return None

    @staticmethod
    def _load_meta(conn: sqlite3.Connection, chunk_ids: list[int]) -> dict[int, sqlite3.Row]:
        if not chunk_ids:
            return {}
        placeholders = ",".join("?" for _ in chunk_ids)
        rows = conn.execute(
            f"""
            SELECT c.id, c.doc_id, c.text, c.page_no, c.heading_path,
                   c.seq, c.generation, d.title, d.source_uri
            FROM chunk c
            JOIN document d ON d.id = c.doc_id
            WHERE c.id IN ({placeholders})
              AND c.generation = d.active_gen
              AND c.is_hidden = 0
            """,
            chunk_ids,
        ).fetchall()
        return {int(r["id"]): r for r in rows}

    @staticmethod
    def _note(conn: sqlite3.Connection, message: str) -> None:
        try:
            conn.execute(
                "INSERT INTO query_log(trace_id, question_digest, created_at, degraded)"
                " VALUES (?, ?, strftime('%Y-%m-%dT%H:%M:%SZ','now'), ?)",
                (f"sys-{time.time_ns()}", "system", message[:300]),
            )
        except sqlite3.Error:
            pass  # 诊断写入失败绝不能影响主流程

    _note_embed_error = _note
