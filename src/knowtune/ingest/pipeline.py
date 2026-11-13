"""导入流水线：抽取 → 切分 → 分词 → 嵌入 → 落库 → 原子切换。

状态机（F1.6 / F1.7）：
  pending → indexing → ready | failed
旧代次（generation）在新代次完全就绪之前一直是 active，检索永远只读
active_gen，所以"重新索引期间检索到半成品"这种情况在结构上就不存在。
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Callable

from .. import paths as paths_mod
from ..config import Config
from ..db import Database, utcnow as _utcnow
from ..index.embedder import Embedder, EmbedderUnavailable
from ..index.sparse import SparseIndex
from ..index.vectors import VectorStore
from ..models import ChunkDraft, LoadedDoc
from . import loader as loader_mod
from .chunker import chunk_blocks, repair_chunk_primitives
from .cleaner import CleanPlan, clean_loaded, plan_from_dict


# 进度回调：(阶段名, 0~1, 附注)。附注是给人看的具体数字，比如 "128/300 块"。
# 之所以要这个：上传进度条到 100% 只说明"字节传完了"，而解析 + 切分 + 向量化
# 才是真正耗时的那几十秒。只给一个转圈的斜纹，用户无法判断是"在跑"还是"死了"。
ProgressFn = Callable[[str, float, str], None]

# 上传落盘名带 8 位十六进制前缀防撞名（"23970fa2-zyyw_update.docx"）。这个前缀
# 只对文件系统有意义，露在文档标题里就是"这系统把文件名搞坏了"。去掉它。
_UPLOAD_PREFIX = re.compile(r"^[0-9a-f]{8}-")


def display_title(source_uri: str) -> str:
    name = Path(source_uri).name
    return _UPLOAD_PREFIX.sub("", name)


class IngestResult:
    def __init__(self, doc_id: int, status: str, n_chunks: int, message: str,
                 reused_vectors: int = 0, computed_vectors: int = 0,
                 clean: dict | None = None) -> None:
        self.doc_id = doc_id
        self.status = status
        self.n_chunks = n_chunks
        self.message = message
        self.reused_vectors = reused_vectors
        self.computed_vectors = computed_vectors
        self.clean = clean

    def as_dict(self) -> dict:
        return {
            "doc_id": self.doc_id,
            "status": self.status,
            "n_chunks": self.n_chunks,
            "message": self.message,
            "reused_vectors": self.reused_vectors,
            "computed_vectors": self.computed_vectors,
            "clean": self.clean,
        }


class IngestPipeline:
    def __init__(
        self,
        cfg: Config,
        db: Database,
        embedder: Embedder,
        sparse: SparseIndex,
        vectors: VectorStore,
    ) -> None:
        self.cfg = cfg
        self.db = db
        self.embedder = embedder
        self.sparse = sparse
        self.vectors = vectors

    # ------------------------------------------------------------------ 对外

    def import_path(
        self, kb_id: int, path: str | Path, cfg: Config | None = None,
        progress: ProgressFn | None = None, clean: dict | CleanPlan | None = None,
    ) -> IngestResult:
        # 解析仍按项目根做基准（从任意 cwd 起服务都一样），但落库只写相对路径：
        # 绝对路径会把这台机器的盘符和目录名焊死在 source_uri 里，目录一改名
        # 全部文档立刻变成"源文件缺失"。项目根之外的文件由 stored_path 兜底。
        p = paths_mod.resolve_source(path).resolve()
        return self._import(
            kb_id=kb_id,
            source_type="file",
            source_uri=paths_mod.stored_path(p),
            load=lambda: loader_mod.load_path(
                p, min_pdf_text_chars=(cfg or self.cfg).ingest.pdf.min_text_chars_per_page
            ),
            cfg=cfg,
            progress=progress,
            clean=clean,
        )

    def import_text(
        self, kb_id: int, text: str, title: str, cfg: Config | None = None,
        progress: ProgressFn | None = None, clean: dict | CleanPlan | None = None,
    ) -> IngestResult:
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
        return self._import(
            kb_id=kb_id,
            source_type="text",
            source_uri=f"text://{title or digest}/{digest}",
            load=lambda: loader_mod.load_inline_text(text, title),
            cfg=cfg,
            progress=progress,
            clean=clean,
        )

    def import_url(
        self, kb_id: int, url: str, cfg: Config | None = None,
        progress: ProgressFn | None = None, clean: dict | CleanPlan | None = None,
    ) -> IngestResult:
        target = url.strip()
        ucfg = (cfg or self.cfg).ingest.url
        return self._import(
            kb_id=kb_id,
            source_type="url",
            source_uri=target,
            load=lambda: loader_mod.load_url(
                target, allowed_domains=ucfg.allowed_domains or None
            ),
            cfg=cfg,
            progress=progress,
            clean=clean,
        )

    # ------------------------------------------------------------------ 内部

    def _import(
        self,
        *,
        kb_id: int,
        source_type: str,
        source_uri: str,
        load,
        cfg: Config | None = None,
        progress: ProgressFn | None = None,
        clean: dict | CleanPlan | None = None,
    ) -> IngestResult:
        # cfg 可以是"知识库级覆盖后的副本"：分块长度是按库记的，用全局的分块
        # 参数去索引一个自定义了 chunk_size 的库，等于参数面板上的数字是假的。
        cfg = cfg or self.cfg
        conn = self.db.conn()
        # 清洗方案可以被复用（重索引时默认沿用上次那套），所以统一在这里归一化
        plan = clean if isinstance(clean, CleanPlan) else plan_from_dict(clean)

        def tell(stage: str, pct: float, note: str = "") -> None:
            if progress is None:
                return
            try:
                progress(stage, pct, note)
            except Exception:  # 进度只是观感，绝不能因为它把导入搞挂
                pass

        tell("抽取原文", 0.02)

        row = conn.execute(
            "SELECT id FROM document WHERE kb_id = ? AND source_uri = ?", (kb_id, source_uri)
        ).fetchone()
        doc_id = int(row["id"]) if row else None
        if doc_id is None:
            cur = conn.execute(
                "INSERT INTO document (kb_id, source_type, source_uri, title, content_hash,"
                " extractor, status, imported_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (kb_id, source_type, source_uri, display_title(source_uri), "",
                 "pending", "pending", _utcnow(), _utcnow()),
            )
            doc_id = int(cur.lastrowid)
        self._set_status(conn, doc_id, "indexing")

        try:
            loaded = load()
        except loader_mod.ExtractError as exc:
            self._fail(conn, doc_id, str(exc))
            return IngestResult(doc_id, "failed", 0, str(exc))
        except Exception as exc:
            msg = f"抽取异常：{type(exc).__name__}: {exc}"
            self._fail(conn, doc_id, msg)
            return IngestResult(doc_id, "failed", 0, msg)

        # 清洗：动的是内存里的块，源文件（以及上传的那份副本）一个字节都不改。
        # 放在算 content_hash 之前——清洗过的内容本来就该有新的指纹。
        clean_summary = None
        if plan is not None and plan.any_enabled():
            try:
                loaded, report = clean_loaded(loaded, plan)
            except Exception as exc:
                msg = f"清洗失败：{type(exc).__name__}: {exc}"
                self._fail(conn, doc_id, msg)
                return IngestResult(doc_id, "failed", 0, msg)
            clean_summary = report.as_dict()
            tell("清洗", 0.17, f"删掉 {report.removed_chars} 字 · 剩 {report.after_blocks} 块")

        content_hash = hashlib.sha256(loaded.text.encode("utf-8")).hexdigest()
        tell("抽取原文", 0.15, f"{loaded.n_pages} 页 · {len(loaded.text)} 字"
             if loaded.n_pages else f"{len(loaded.text)} 字")
        existing = conn.execute(
            "SELECT content_hash, status FROM document WHERE id = ?", (doc_id,)
        ).fetchone()
        if existing and existing["content_hash"] == content_hash and existing["status"] == "ready":
            conn.execute(
                "UPDATE document SET updated_at = ? WHERE id = ?", (_utcnow(), doc_id)
            )
            n = conn.execute(
                "SELECT count(*) c FROM chunk c JOIN document d ON d.id=c.doc_id"
                " WHERE c.doc_id=? AND c.generation=d.active_gen", (doc_id,)
            ).fetchone()["c"]
            return IngestResult(doc_id, "ready", int(n), "内容未变化，跳过重新索引")

        try:
            tell("切分", 0.18)
            drafts = self._prepare_chunks(loaded, cfg)
        except Exception as exc:
            msg = f"切分失败：{type(exc).__name__}: {exc}"
            self._fail(conn, doc_id, msg)
            return IngestResult(doc_id, "failed", 0, msg)

        if not drafts:
            msg = "切分后没有产生任何 chunk（内容可能全是空白或格式标记）"
            self._fail(conn, doc_id, msg)
            return IngestResult(doc_id, "failed", 0, msg)
        tell("切分", 0.28, f"{len(drafts)} 块")

        try:
            computed, reused = self._ensure_embeddings(conn, drafts, tell)
        except EmbedderUnavailable as exc:
            # 没有嵌入模型时不写半成品：chunk 落库但没向量，会造成"看起来成功
            # 但永远检索不到"，这是最坏的用户体验
            msg = f"嵌入失败：{exc}"
            self._fail(conn, doc_id, msg)
            return IngestResult(doc_id, "failed", 0, msg)
        except Exception as exc:
            msg = f"嵌入异常：{type(exc).__name__}: {exc}"
            self._fail(conn, doc_id, msg)
            return IngestResult(doc_id, "failed", 0, msg)

        try:
            tell("写入", 0.94)
            n_chunks = self._commit(
                conn, doc_id, drafts, loaded, content_hash, source_type,
                title=display_title(source_uri) if source_type == "file" else loaded.title,
                clean_json=json.dumps(plan.as_dict(), ensure_ascii=False) if plan else None,
            )
        except Exception as exc:
            msg = f"写入失败：{type(exc).__name__}: {exc}"
            self._fail(conn, doc_id, msg)
            return IngestResult(doc_id, "failed", 0, msg)

        self.vectors.invalidate()
        tell("完成", 1.0, f"{n_chunks} 块")
        note = f"已索引 {n_chunks} 个 chunk"
        if clean_summary:
            # 清洗删了什么要出现在结果里：一句"已索引 300 块"会让人以为
            # 源文件就是这个体量，等发现某段搜不到时已经无从对照。
            note += f"（清洗删掉 {clean_summary['removed_chars']} 字 / "
            note += f"{clean_summary['before']['blocks'] - clean_summary['after']['blocks']} 块"
            note += f"，-{clean_summary['removed_pct']}%）"
        if computed or reused:
            note += f"（新算向量 {computed}，复用 {reused}）"
        return IngestResult(doc_id, "ready", n_chunks, note, reused, computed, clean_summary)

    def _prepare_chunks(self, loaded: LoadedDoc, cfg: Config) -> list[ChunkDraft]:
        drafts = chunk_blocks(loaded.blocks, cfg.chunking)
        for d in drafts:
            d.text = repair_chunk_primitives(d.text)
            d.text_seg = self.sparse.segment(d.text)
        return [d for d in drafts if d.text.strip()]

    def _ensure_embeddings(
        self,
        conn: sqlite3.Connection,
        drafts: list[ChunkDraft],
        tell: Callable[[str, float, str], None] | None = None,
    ) -> tuple[int, int]:
        model_key = f"{self.cfg.embedding.model}@{self.cfg.embedding.dim}"
        unique = {d.content_hash: d.text for d in drafts}

        hashes = list(unique)
        placeholders = ",".join("?" for _ in hashes)
        existing = {
            r["content_hash"]
            for r in conn.execute(
                f"SELECT content_hash FROM embedding WHERE model = ? AND content_hash IN ({placeholders})",
                [model_key, *hashes],
            ).fetchall()
        }
        missing = [(h, t) for h, t in unique.items() if h not in existing]
        report = tell or (lambda *a: None)

        if missing:
            import numpy as np

            texts = [t for _, t in missing]
            self.embedder.load()  # 预热不计入吞吐统计，否则日志会骗人
            started = time.perf_counter()
            # 分批编码：整批一次 encode 拿不到任何中间信号，而这一步恰恰是
            # 最慢的（本机约 21 块/秒，几百块就是一分钟）。按 batch 切之后
            # 每批报一次进度，用户看到的是"128/300"而不是一个转圈。
            step = max(1, int(self.cfg.embedding.batch_size or 32))
            chunks: list[np.ndarray] = []
            done = 0
            for i in range(0, len(texts), step):
                part = texts[i : i + step]
                chunks.append(np.asarray(self.embedder.encode(part), dtype=np.float32))
                done += len(part)
                report("嵌入", 0.28 + 0.62 * (done / len(texts)), f"{done}/{len(texts)} 块")
            vectors = np.vstack(chunks) if chunks else np.zeros((0, self.cfg.embedding.dim))
            elapsed = time.perf_counter() - started
            rows = [
                (h, model_key, int(self.cfg.embedding.dim),
                 np.asarray(vec, dtype=np.float32).tobytes())
                for (h, _), vec in zip(missing, vectors)
            ]
            with Database.tx(conn):
                conn.executemany(
                    "INSERT OR REPLACE INTO embedding (content_hash, model, dim, vec)"
                    " VALUES (?,?,?,?)",
                    rows,
                )
            rate = len(texts) / elapsed if elapsed > 0 else 0
            print(f"[ingest] embedded {len(texts)} chunks in {elapsed:.2f}s ({rate:.1f}/s)")
        else:
            # 全命中缓存时也得给个交代，否则进度条会从 28% 直接跳到 94%
            report("复用向量", 0.9, f"{len(unique)} 块已存在")

        return len(missing), len(existing & set(unique))

    def _commit(
        self,
        conn: sqlite3.Connection,
        doc_id: int,
        drafts: list[ChunkDraft],
        loaded: LoadedDoc,
        content_hash: str,
        source_type: str,
        title: str | None = None,
        clean_json: str | None = None,
    ) -> int:
        next_gen = int(
            conn.execute(
                "SELECT COALESCE(MAX(generation), 0) + 1 AS g FROM chunk WHERE doc_id = ?",
                (doc_id,),
            ).fetchone()["g"]
        )
        old_ids = [
            int(r["id"])
            for r in conn.execute("SELECT id FROM chunk WHERE doc_id = ?", (doc_id,)).fetchall()
        ]

        with Database.tx(conn):
            conn.executemany(
                "INSERT INTO chunk (doc_id, generation, seq, text, text_seg, token_count,"
                " page_no, heading_path, char_start, char_end, content_hash)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (doc_id, next_gen, d.seq, d.text, d.text_seg, d.token_count,
                     d.page_no, d.heading_path, d.char_start, d.char_end, d.content_hash)
                    for d in drafts
                ],
            )
            new_ids = [
                int(r["id"])
                for r in conn.execute(
                    "SELECT id FROM chunk WHERE doc_id = ? AND generation = ? ORDER BY seq",
                    (doc_id, next_gen),
                ).fetchall()
            ]
            self.sparse.delete(conn, old_ids)
            self.sparse.index(conn, list(zip(new_ids, [d.text for d in drafts])))

            conn.execute(
                "UPDATE document SET status='ready', fail_reason=NULL, content_hash=?,"
                " title=?, extractor=?, n_pages=?, n_chunks=?, active_gen=?, source_type=?,"
                " clean_json=?, updated_at=? WHERE id=?",
                (content_hash, title or loaded.title, loaded.extractor, loaded.n_pages, len(drafts),
                 next_gen, source_type, clean_json, _utcnow(), doc_id),
            )
            self._gc_old_generations(conn, doc_id, next_gen)

        return len(drafts)

    def _gc_old_generations(self, conn: sqlite3.Connection, doc_id: int, active_gen: int) -> None:
        keep = max(0, self.cfg.storage.keep_generations)
        if keep:
            floor_gen = active_gen - keep
            conn.execute(
                "DELETE FROM chunk WHERE doc_id = ? AND generation < ?", (doc_id, floor_gen)
            )
        else:
            conn.execute(
                "DELETE FROM chunk WHERE doc_id = ? AND generation < ?", (doc_id, active_gen)
            )

    # ------------------------------------------------------------------ 状态

    @staticmethod
    def _set_status(conn: sqlite3.Connection, doc_id: int, status: str) -> None:
        conn.execute(
            "UPDATE document SET status = ?, fail_reason = NULL, updated_at = ? WHERE id = ?",
            (status, _utcnow(), doc_id),
        )

    @staticmethod
    def _fail(conn: sqlite3.Connection, doc_id: int, reason: str) -> None:
        conn.execute(
            "UPDATE document SET status='failed', fail_reason=?, updated_at=? WHERE id=?",
            (reason[:800], _utcnow(), doc_id),
        )