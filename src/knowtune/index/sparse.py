"""稀疏检索：jieba 预分词 + SQLite FTS5。

为什么必须预分词（实测，见 docs/ARCHITECTURE.md §4.3）：
- FTS5 默认 unicode61 把整句中文当一个 token，"检索"这种词命中数为 0；
- trigram 分词器要求查询 ≥3 字符，中文高频 2 字词全部失效；
- 只有"先分词、再用空格拼成串写入"这条路能让 2 字词正常命中。

bm25() 返回负数，越小越相关 → 必须 ORDER BY bm25(...) ASC。
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from ..config import SparseCfg
from ..models import Hit

# 查询侧停用词。只删"本身没有检索意义"的虚词，不做大而全的词表——
# 删多了会把"是什么"这种短查询清成空查询。索引侧照旧全量入库：
# 两侧都过滤必须同步重索引，而这个改动只影响查询，改了立刻生效。
#
# 为什么值得单列一份：中文 BM25 的噪声主要来自虚词。像"太阳病的**主要症状**
# 是什么"，分词后是「太阳 / 病 / 的 / 主要 / 症状 / 主要症状 / 是 / 什么」，
# 8 个词里 4 个是虚词。"病"这种高频字在古籍里几乎每页都有，idf 接近 0，
# 剩下的权重全被"的 / 是 / 什么"这种更没用的词摊平，排序就变成掷骰子。
_STOPWORDS = frozenset("""
的 了 着 过 和 与 及 或 是 在 有 就 都 而 也 很 太 更 最 这 那 之 其 为 以 于 对 从 到 被 把 让 给
吗 呢 吧 啊 呀 么 哦 嗯 的话 一些 一个 这个 那个 这些 那些
什么 怎么 怎样 怎么样 如何 为什么 哪些 哪个 哪里 多少 是否 能否 可以 应该 需要 有没有
我们 你们 他们 她们 它们 请问 一下 告诉 帮我 麻烦
""".split())


class SparseIndex:
    def __init__(self, cfg: SparseCfg) -> None:
        self.cfg = cfg
        self._jieba = None
        self._lock = threading.Lock()

    def _segmenter(self):
        if self._jieba is not None:
            return self._jieba
        with self._lock:
            if self._jieba is not None:
                return self._jieba
            import jieba

            jieba.setLogLevel(60)  # 关掉构建词典时的 INFO 噪声
            userdict = self.cfg.userdict
            if userdict and Path(userdict).exists():
                jieba.load_userdict(userdict)
            self._jieba = jieba
            return jieba

    @property
    def ready(self) -> bool:
        return self._jieba is not None

    def warmup(self) -> None:
        """jieba 的默认词典是懒初始化的：setLogLevel / load_userdict 都不会触发，
        必须真的分一次词，否则这 1 秒会漏到第一次查询的 sparse_ms 里。"""
        jieba = self._segmenter()
        list(jieba.cut_for_search("预热"));

    def segment(self, text: str) -> str:
        """cut_for_search：细粒度 + 长词都吐，召回率优先。"""
        jieba = self._segmenter()
        tokens = [t.strip() for t in jieba.cut_for_search(text)]
        return " ".join(t for t in tokens if t)

    # ------------------------------------------------------------------ 写入

    def index(self, conn: sqlite3.Connection, rows: list[tuple[int, str]]) -> None:
        """rows = [(chunk_id, text)]。rowid 必须与 chunk.id 对齐。"""
        if not rows:
            return
        payload = [(chunk_id, self.segment(text)) for chunk_id, text in rows]
        conn.executemany("INSERT INTO chunk_fts(rowid, text_seg) VALUES (?, ?)", payload)

    def delete(self, conn: sqlite3.Connection, chunk_ids: list[int]) -> None:
        if not chunk_ids:
            return
        conn.executemany("DELETE FROM chunk_fts WHERE rowid = ?", [(cid,) for cid in chunk_ids])

    # ------------------------------------------------------------------ 检索

    def search(
        self,
        conn: sqlite3.Connection,
        query: str,
        *,
        top_k: int,
        kb_id: int | None = None,
        doc_ids: list[int] | None = None,
        source_types: list[str] | None = None,
        imported_after: str | None = None,
        imported_before: str | None = None,
    ) -> list[Hit]:
        seg = self.segment(query)
        terms = [t for t in seg.split() if t]
        if not terms:
            return []
        # 滤虚词，但留后手：整句都是虚词（"是什么"）时保留原样，
        # 否则一个正常提问会被过滤成空查询，直接变成"库里没有"
        keep = [t for t in terms if t not in _STOPWORDS]
        if keep:
            terms = keep

        # 用 OR 而非隐式 AND：中文查询分词后容易带入无意义的连接词，
        # 一旦 AND 就整体零命中。相关性由 bm25 排序保证，不靠 AND 硬过滤。
        match_expr = " OR ".join(f'"{t}"' for t in terms)

        sql = [
            "SELECT f.rowid AS chunk_id, bm25(chunk_fts) AS score",
            "FROM chunk_fts f",
            "JOIN chunk c ON c.id = f.rowid",
            "JOIN document d ON d.id = c.doc_id",
            "WHERE chunk_fts MATCH ?",
            "  AND c.generation = d.active_gen",
            "  AND c.is_hidden = 0",
            "  AND d.status = 'ready'",
        ]
        params: list[object] = [match_expr]
        if kb_id is not None:
            sql.append("  AND d.kb_id = ?")
            params.append(kb_id)
        if doc_ids:
            placeholders = ",".join("?" for _ in doc_ids)
            sql.append(f"  AND d.id IN ({placeholders})")
            params.extend(doc_ids)
        if source_types:
            placeholders = ",".join("?" for _ in source_types)
            sql.append(f"  AND d.source_type IN ({placeholders})")
            params.extend(source_types)
        # imported_at 是 ISO8601 定长字符串，字典序即时间序，可以直接比较
        if imported_after:
            sql.append("  AND d.imported_at >= ?")
            params.append(imported_after)
        if imported_before:
            sql.append("  AND d.imported_at <= ?")
            params.append(imported_before)
        sql.append("ORDER BY score ASC LIMIT ?")
        params.append(top_k)

        try:
            rows = conn.execute("\n".join(sql), params).fetchall()
        except sqlite3.OperationalError:
            # 查询串里的引号/括号会让 FTS5 语法解析失败，退化为全部词组 OR 的安全形式
            safe = " OR ".join(
                "".join(ch for ch in t if ch.isalnum() or "\u4e00" <= ch <= "\u9fff") for t in terms
            )
            safe = " OR ".join(f'"{t}"' for t in safe.split() if t)
            if not safe:
                return []
            rows = conn.execute("\n".join(sql), [safe, *params[1:]]).fetchall()

        return [
            Hit(chunk_id=int(r["chunk_id"]), score=float(r["score"]), rank=i + 1, label="sparse")
            for i, r in enumerate(rows)
        ]
