"""融合权重网格搜索：在真实语料上找 w_dense / w_sparse 的合理区间。

背景：ARCHITECTURE.md 那套默认值（dense 0.5 / sparse 0.35）是在技术文档上
定的——技术文档里 query 和文档用同一套术语，BM25 很强。换成古籍那种"白话问、
文言答"的语料，词面几乎不重合，BM25 那一路召回的全是噪声，却因为权重更大
把 dense 的正确结果压了下去。

这个脚本不改代码，只是把每组的 Recall@5 / MRR 打出来，让人看清"权重到底
有多大影响"，而不是凭感觉调。

用法：
    python scripts/sweep_fusion.py --dataset evals/cases_classics.jsonl
"""

from __future__ import annotations

import argparse
import itertools
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ragkb.cli import _bootstrap  # noqa: E402
from ragkb.evaluate import load_cases, run_eval  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default=str(ROOT / "evals" / "cases_classics.jsonl"))
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--topk", default="20,30,50", help="融合候选数，逗号分隔")
    ap.add_argument("--rerank", type=int, default=0,
                    help="1=带上精排（慢 ~300 倍，只用于最终确认）")
    args = ap.parse_args()

    cfg, db, engine, _ = _bootstrap()
    conn = db.conn()
    cases = load_cases(Path(args.dataset))

    rows = []
    for wd, ws, topk in itertools.product(
        (0.35, 0.5, 0.65, 0.8, 1.0), (0.0, 0.15, 0.35, 0.5), [int(x) for x in args.topk.split(",")]
    ):
        c = cfg.model_copy(deep=True)
        c.retrieval.fusion.w_dense = wd
        c.retrieval.fusion.w_sparse = ws
        c.retrieval.fusion.top_k = topk
        # 默认关精排：它把每组从 ~0.3s 抬到 ~8s，60 组就是八分钟，而融合权重
        # 的作用是决定"谁进候选池"——那是精排之前的事，两件事得分开看。
        c.retrieval.rerank.enabled = bool(args.rerank)
        # engine.search 收 cfg 参数，正好用来跑"不落盘的候选配置"
        orig = engine.cfg
        engine.cfg = c
        try:
            rep = run_eval(c, conn, engine, k=args.k, cases=cases)
        finally:
            engine.cfg = orig
        m = rep.metrics
        rows.append((m["recall_at_k"], m["mrr"], m["hit_at_1"], wd, ws, topk))

    rows.sort(key=lambda r: (-r[0], -r[1]))
    print(f"{'Recall@'+str(args.k):>9} {'MRR':>6} {'Hit@1':>6} {'w_dense':>8} {'w_sparse':>9} {'top_k':>6}")
    for rec, mrr, h1, wd, ws, topk in rows[:15]:
        print(f"{rec:>9.1%} {mrr:>6.3f} {h1:>6.1%} {wd:>8.2f} {ws:>9.2f} {topk:>6}")
    print("\n末尾三组（对比用）：")
    for rec, mrr, h1, wd, ws, topk in rows[-3:]:
        print(f"{rec:>9.1%} {mrr:>6.3f} {h1:>6.1%} {wd:>8.2f} {ws:>9.2f} {topk:>6}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
