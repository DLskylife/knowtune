"""清掉数据库里的死数据，把 library.db 瘦回去。

为什么会长胖：每次重新索引都会写新一代（generation+1）的 chunk，旧代从此
再也没人读——全项目所有查询都带 `generation = active_gen`。但没人删它们，
于是旧块连同它们独占的向量一起留在库里，越积越多。embedding 表更狠：它按
content_hash 存、没有指向 chunk 的外键，所以 chunk 删了它也跟着赖着不走。

清理两类：
  1. 非活跃 generation 的 chunk（以及文档已被删的孤儿 chunk）
  2. embedding 中不再被任何活跃块引用的向量

安全边界：**只删检索永远读不到的行**。删之前会实测一遍"活跃块是否都有向量"，
覆盖率不是 100% 就直接拒绝执行——宁可不瘦身，也不能把库清成残废。
embedding 是向量的唯一存储（chunk 表没有 vec 列），误删等于毁索引。

用法：
    python scripts/prune_db.py            # dry-run，只报数不落地
    python scripts/prune_db.py --apply    # 真正删 + VACUUM
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "library.db"


def _scalar(conn: sqlite3.Connection, sql: str) -> int:
    return int(conn.execute(sql).fetchone()[0])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正执行删除并 VACUUM")
    ap.add_argument("--db", default=str(DB))
    args = ap.parse_args()

    db = Path(args.db)
    if not db.exists():
        print(f"找不到 {db}")
        return 1

    before = db.stat().st_size
    conn = sqlite3.connect(str(db))
    conn.execute("PRAGMA foreign_keys=ON")

    active = _scalar(conn, """
        SELECT COUNT(*) FROM chunk c JOIN document d ON d.id=c.doc_id
        WHERE c.generation = d.active_gen
    """)
    covered = _scalar(conn, """
        SELECT COUNT(*) FROM chunk c JOIN document d ON d.id=c.doc_id
        WHERE c.generation = d.active_gen
          AND EXISTS(SELECT 1 FROM embedding e WHERE e.content_hash=c.content_hash)
    """)
    stale_chunks = _scalar(conn, """
        SELECT COUNT(*) FROM chunk c JOIN document d ON d.id=c.doc_id
        WHERE c.generation <> d.active_gen
    """)
    orphan_chunks = _scalar(conn, """
        SELECT COUNT(*) FROM chunk WHERE doc_id NOT IN (SELECT id FROM document)
    """)
    stale_vecs = _scalar(conn, """
        SELECT COUNT(*) FROM embedding WHERE content_hash NOT IN (
            SELECT DISTINCT c.content_hash FROM chunk c
            JOIN document d ON d.id=c.doc_id WHERE c.generation=d.active_gen
        )
    """)
    total_vecs = _scalar(conn, "SELECT COUNT(*) FROM embedding")

    print(f"库文件        {db}  {before/1e6:.1f} MB")
    print(f"活跃块        {active}")
    print(f"  有向量覆盖  {covered}  ({covered/active:.1%})" if active else "  无活跃块")
    print(f"待删 旧代块   {stale_chunks}")
    print(f"待删 孤儿块   {orphan_chunks}")
    print(f"待删 孤儿向量 {stale_vecs} / {total_vecs}")

    # 硬闸门：活跃块必须 100% 有向量，否则删了就等于毁索引
    if active and covered != active:
        print("\n拒绝执行：活跃块向量覆盖不足 100%，说明 embedding 表状态异常。")
        print("先跑一次完整重索引再清理。")
        return 2

    if not args.apply:
        print("\n（dry-run，加 --apply 才落地）")
        return 0

    # 备份一份再动刀：库本身不大，代价远小于清错的后果
    bak = db.with_suffix(db.suffix + ".bak_prune")
    shutil.copy2(db, bak)
    print(f"\n已备份 -> {bak.name}")

    with conn:
        conn.execute("""
            DELETE FROM chunk WHERE id IN (
                SELECT c.id FROM chunk c JOIN document d ON d.id=c.doc_id
                WHERE c.generation <> d.active_gen
            )
        """)
        conn.execute("DELETE FROM chunk WHERE doc_id NOT IN (SELECT id FROM document)")
        conn.execute("""
            DELETE FROM embedding WHERE content_hash NOT IN (
                SELECT DISTINCT c.content_hash FROM chunk c
                JOIN document d ON d.id=c.doc_id WHERE c.generation=d.active_gen
            )
        """)

    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.execute("VACUUM")
    conn.close()

    after = db.stat().st_size
    print(f"清理完成      {before/1e6:.1f} MB -> {after/1e6:.1f} MB "
          f"(省 {(before-after)/1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
