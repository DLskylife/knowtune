"""诊断"漏召回"的性质：是压根没进候选池，还是进了池但没排进 Top-K。

对每道失败题，把候选池放大到 --pool 条重跑一次，报告：
  - 期望片段在池中的名次（若有）
  - 若池里都没有，再查一遍全库：这段原文到底在不在索引里（在 = 排序问题，
    不在 = 分块/嵌入/清洗环节就丢了）

用法：
    python scripts/diag_misses.py --kb 2 --ids c04,c11,c19,c20,c21
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from knowtune.cli import _bootstrap  # noqa: E402
from knowtune.evaluate import _match, load_cases  # noqa: E402
from knowtune.textnorm import fold  # noqa: E402
from knowtune import kbconfig  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default=str(ROOT / "evals" / "cases_classics.jsonl"))
    ap.add_argument("--kb", type=int, default=0)
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--pool", type=int, default=50, help="放大后的候选池")
    ap.add_argument("--ids", default="", help="只看这些题，逗号分隔；空 = 全部失败题")
    args = ap.parse_args()

    cfg, db, engine, _ = _bootstrap()
    conn = db.conn()
    if args.kb:
        cfg = kbconfig.effective(cfg, kbconfig.load(conn, args.kb))
        engine.cfg = cfg

    cases = load_cases(Path(args.dataset))
    if args.ids:
        want = {s.strip() for s in args.ids.split(",") if s.strip()}
        cases = [c for c in cases if c.get("id") in want]

    for case in cases:
        q = case["question"]
        expect = case.get("expect") or {}
        res = engine.search(conn, q, kb_id=args.kb or None, top_k=args.pool)
        hit = None
        for cand in res.candidates:
            if _match(cand, expect):
                hit = cand
                break
        # 池里没有 -> 查原文到底在不在索引里
        in_index = None
        if hit is None:
            keys = expect.get("keys") or []
            if keys:
                like = f"%{keys[0]}%"
                in_index = conn.execute(
                    "SELECT COUNT(*) FROM chunk WHERE text LIKE ?", (like,)
                ).fetchone()[0]

        pool_n = len(res.candidates)
        if hit is not None:
            verdict = f"池内第 {hit.final_rank} 名（池 {pool_n} 条）→ 排序问题，Top-{args.k} 被挤掉"
        elif in_index:
            verdict = f"池 {pool_n} 条内无；但全库有 {in_index} 个块含关键词 → 检索层没召回"
        else:
            verdict = "池内无，且全库零命中 → 该内容根本不在索引里（分块/清洗/导入丢的）"

        print(f"[{case.get('id')}] {q}")
        print(f"    expect={expect}")
        print(f"    判定：{verdict}")
        for cand in res.candidates[:3]:
            print(f"    #{cand.final_rank} {fold(cand.text)[:60].replace(chr(10), ' ')}"
                  f"  <{cand.doc_title}>")
        print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
