"""在真实知识库上跑评估集，可切换"折叠关/开"以对比改前改后。

为什么单独一个脚本而不是走 `ragkb eval`：CLI 的评估集路径钉在配置里
（默认 evals/cases.jsonl），而这次要对比的不是"同一份配置"，而是**同一批
问题在两种码位处理下的检索质量**。用脚本可以直接传 cases、可以 monkeypatch
fold，把"改前"这个本来已经消失的状态复现出来。

用法：
    python scripts/eval_classics.py                 # 当前代码（折叠开）
    python scripts/eval_classics.py --nofold        # 复现旧行为（折叠关）
    python scripts/eval_classics.py --kb 2          # 套用该库的库级覆盖（线上真实口径）
    python scripts/eval_classics.py --show          # 逐题打印排名与首条标题

注意：默认跑的是**全局配置**。库级覆盖（rerank 开关、chunk size 等）只在该库
生效，所以要复现"用户实际问到的效果"必须带 --kb，否则会拿全局的精排开/关
去算，得出和线上对不上的数——这个坑踩过一次。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import ragkb.retrieve.engine as engine_mod  # noqa: E402
from ragkb.cli import _bootstrap  # noqa: E402
from ragkb.evaluate import load_cases, run_eval  # noqa: E402
from ragkb import kbconfig  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default=str(ROOT / "evals" / "cases_classics.jsonl"))
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--nofold", action="store_true", help="查询侧不折叠，复现旧行为")
    ap.add_argument("--kb", type=int, default=0, help="套用该知识库的库级覆盖")
    ap.add_argument("--show", action="store_true", help="逐题打印")
    ap.add_argument("--out", default="", help="把结果 json 写到这个路径")
    args = ap.parse_args()

    if args.nofold:
        # engine 里是 from ..textnorm import fold_query，改这个名字即可
        engine_mod.fold_query = lambda s: s

    cfg, db, engine, _ = _bootstrap()
    conn = db.conn()
    tag_extra = ""
    if args.kb:
        overrides = kbconfig.load(conn, args.kb)
        cfg = kbconfig.effective(cfg, overrides)
        engine.cfg = cfg
        on = bool(getattr(cfg.retrieval.rerank, "enabled", False))
        tag_extra = f" | kb{args.kb}覆盖(精排{'开' if on else '关'})"
    cases = load_cases(Path(args.dataset))
    report = run_eval(cfg, conn, engine, k=args.k, cases=cases)
    d = report.as_dict()

    tag = ("旧行为(不折叠)" if args.nofold else "当前(折叠)") + tag_extra
    print(f"=== {tag} | k={args.k} | {len(cases)} 题 ===")
    m = d["metrics"]
    print(f"Recall@{args.k} = {m['recall_at_k']:.1%}   MRR = {m['mrr']:.3f}   "
          f"Hit@1 = {m['hit_at_1']:.1%}   p50 = {m['latency_ms_p50']}ms")
    r = d["refusal"]
    print(f"拒答：漏拒 {r['missed_refusal']}/{r['n_should_refuse']}   "
          f"误拒 {r['false_refusal']}/{r['n_answerable']}")

    if args.show:
        print("-" * 78)
        for c in report.cases:
            flag = "✓" if c.rank else ("拒答" if c.refused else "✗")
            print(f"{c.id:4s} {flag:4s} rank={c.rank}  {c.question[:26]:28s} {(c.top_doc or '')[:18]}")
    if args.out:
        Path(args.out).write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"已写入 {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
