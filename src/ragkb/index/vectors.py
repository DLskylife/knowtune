"""向量检索：内存矩阵 + 暴力点积。

D1 已确认规模 ≤10 万 chunk → 10万 × 512 × 4B ≈ 196MB。这个量级上
ANN 索引（FAISS/HNSW）是负资产：多一层近似误差、多一套序列化、
多一个"索引和原数据不同步"的故障点，换来的只是把 30ms 变成 3ms。

向量存在 embedding 表（BLOB），与 chunk 同库同事务——宁可多占 200MB 内存，
也不要"db 和 .f32 文件对不上"这种需要人工排查的一致性问题。
"""

from __future__ import annotations

import sqlite3
import threading

import numpy as np

from ..config import Config
from ..models import Hit


class VectorStore:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._ids = np.zeros(0, dtype=np.int64)
        self._doc_ids = np.zeros(0, dtype=np.int64)
        self._kb_ids = np.zeros(0, dtype=np.int64)
        self._mat = np.zeros((0, cfg.embedding.dim), dtype=np.float32)
        self._doc_meta: dict[int, tuple[str, str]] = {}
        self._lock = threading.RLock()
        self._loaded = False
        self._skipped = 0

    @property
    def stats(self) -> dict:
        return {"vectors": int(self._ids.shape[0]), "dim": int(self._mat.shape[1]),
                "skipped_dim_mismatch": self._skipped}

    def invalidate(self) -> None:
        with self._lock:
            self._loaded = False

    def _load(self, conn: sqlite3.Connection) -> None:
        rows = conn.execute(
            """
            SELECT c.id AS chunk_id, c.doc_id, d.kb_id, e.vec, e.dim
            FROM chunk c
            JOIN document d ON d.id = c.doc_id
            JOIN embedding e ON e.content_hash = c.content_hash AND e.model = ?
            WHERE c.generation = d.active_gen
              AND c.is_hidden = 0
              AND d.status = 'ready'
            """,
            (self._model_key(),),
        ).fetchall()

        dim = self.cfg.embedding.dim
        ids: list[int] = []
        doc_ids: list[int] = []
        kb_ids: list[int] = []
        vectors: list[np.ndarray] = []
        skipped = 0

        for r in rows:
            if int(r["dim"]) != dim:
                skipped += 1
                continue
            vec = np.frombuffer(r["vec"], dtype=np.float32)
            if vec.shape[0] != dim:
                skipped += 1
                continue
            ids.append(int(r["chunk_id"]))
            doc_ids.append(int(r["doc_id"]))
            kb_ids.append(int(r["kb_id"]))
            vectors.append(vec.copy())

        if vectors:
            mat = np.vstack(vectors).astype(np.float32, copy=False)
        else:
            mat = np.zeros((0, dim), dtype=np.float32)

        self._ids = np.asarray(ids, dtype=np.int64)
        self._doc_ids = np.asarray(doc_ids, dtype=np.int64)
        self._kb_ids = np.asarray(kb_ids, dtype=np.int64)
        self._mat = mat
        self._skipped = skipped
        # 元数据过滤（F2.7）只按文档粒度做，所以存一份 doc_id → (来源类型, 导入时间)
        self._doc_meta = {
            int(r["id"]): (r["source_type"], r["imported_at"] or "")
            for r in conn.execute("SELECT id, source_type, imported_at FROM document").fetchall()
        }
        self._loaded = True

    def _allowed_docs(
        self,
        source_types: list[str] | None,
        imported_after: str | None,
        imported_before: str | None,
    ) -> np.ndarray | None:
        if not (source_types or imported_after or imported_before):
            return None
        allowed = [
            did
            for did, (stype, imported) in self._doc_meta.items()
            if (not source_types or stype in source_types)
            and (not imported_after or imported >= imported_after)
            and (not imported_before or imported <= imported_before)
        ]
        # 用空数组以外的方式表示"全被过滤掉"：返回 -1 哨兵，保证 mask 全 False
        return np.asarray(allowed or [-1], dtype=np.int64)

    def _model_key(self) -> str:
        return f"{self.cfg.embedding.model}@{self.cfg.embedding.dim}"

    def ensure(self, conn: sqlite3.Connection) -> None:
        with self._lock:
            if not self._loaded:
                self._load(conn)

    def search(
        self,
        conn: sqlite3.Connection,
        qvec: np.ndarray,
        *,
        top_k: int,
        kb_id: int | None = None,
        doc_ids: list[int] | None = None,
        min_score: float = -1.0,
        source_types: list[str] | None = None,
        imported_after: str | None = None,
        imported_before: str | None = None,
    ) -> list[Hit]:
        self.ensure(conn)
        if self._ids.shape[0] == 0:
            return []

        scores = self._mat @ qvec.astype(np.float32, copy=False)

        mask = np.ones(scores.shape[0], dtype=bool)
        if kb_id is not None:
            mask &= self._kb_ids == kb_id
        if doc_ids:
            mask &= np.isin(self._doc_ids, np.asarray(doc_ids, dtype=np.int64))
        allowed = self._allowed_docs(source_types, imported_after, imported_before)
        if allowed is not None:
            mask &= np.isin(self._doc_ids, allowed)
        if min_score > -1.0:
            mask &= scores >= min_score

        idx = np.flatnonzero(mask)
        if idx.size == 0:
            return []

        candidate_scores = scores[idx]
        k = min(top_k, idx.size)
        if k < idx.size:
            top_local = np.argpartition(-candidate_scores, k - 1)[:k]
        else:
            top_local = np.arange(idx.size)
        ordered = top_local[np.argsort(-candidate_scores[top_local])]

        return [
            Hit(
                chunk_id=int(self._ids[idx[pos]]),
                score=float(candidate_scores[pos]),
                rank=i + 1,
                label="dense",
            )
            for i, pos in enumerate(ordered)
        ]

    def vectors_of(self, conn: sqlite3.Connection, chunk_ids: list[int]) -> dict[int, np.ndarray]:
        """MMR 需要候选的真实向量；没进矩阵的 chunk 直接忽略。"""
        self.ensure(conn)
        if not chunk_ids or self._ids.shape[0] == 0:
            return {}
        wanted = np.asarray(chunk_ids, dtype=np.int64)
        positions = np.flatnonzero(np.isin(self._ids, wanted))
        return {int(self._ids[p]): self._mat[p] for p in positions}
