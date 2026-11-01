"""精排的开销/质量权衡：候选数 × 输入长度。

精排是 CPU 上唯一"秒级"的环节：20 个 900 字的候选过一遍 base 档 cross-encoder
要 8 秒多，用户点发送得瞪着屏幕。但砍候选、砍长度都会掉指标，所以要实测。

这个脚本固定评测集，只变这两个旋钮，把 Recall@5 / Hit@1 / p50 一起打出来，
让取舍有数可依——而不是拍脑袋定个 max_length=512 然后抱怨慢。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ragkb.cli import _bootstrap  # noqa: E402
from ragkb.evaluate import load_cases, run_eval  # noqa: E402
from ragkb.retrieve.engine import RetrievalEngine  # noqa: E402
from ragkb.retrieve.rerank import Reranker  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default=str(ROOT / "evals" / "cases_classics.jsonl"))
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument(
        "--grid",
        default="512x20,256x20,256x10,128x20,128x10,384x12",
        help="max_length x 候选数，逗号分隔",
    )
    args = ap.parse_args()

    cfg, db, engine, _ = _bootstrap()
    conn = db.conn()
    cases = load_cases(Path(args.dataset))
    base = engine

    print(f"{'max_len':>7} {'pool':>5} {'Recall@5':>9} {'MRR':>6} {'Hit@1':>6} {'p50ms':>7} {'maxms':>7}")
    for cell in args.grid.split(","):
        ml, pool = (int(x) for x in cell.lower().split("x"))
        c = cfg.model_copy(deep=True)
        c.retrieval.rerank.max_length = ml
        c.retrieval.fusion.top_k = pool
        eng = RetrievalEngine(c, base.embedder, base.vectors, base.sparse, Reranker(c))
        # 第一次调用含模型加载，先暖一遍，避免它把 p50 拉爆
        eng.reranker.rerank("预热", ["一段用来触发模型加载的文本"])
        t0 = time.perf_counter()
        rep = run_eval(c, conn, eng, k=args.k, cases=cases)
        wall = time.perf_counter() - t0
        m = rep.metrics
        print(
            f"{ml:>7} {pool:>5} {m['recall_at_k']:>9.1%} {m['mrr']:>6.3f} "
            f"{m['hit_at_1']:>6.1%} {m['latency_ms_p50']:>7} "
            f"{m['latency_ms_max']:>7}   ({wall:.0f}s/{len(cases)}题)"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
