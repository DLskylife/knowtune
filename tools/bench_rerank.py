"""精排延迟/质量探针：同一批候选，换不同 max_length 看耗时与 top1 命中。

只回答一个问题：把 rerank 打开，到底要付多少毫秒，以及能不能少付。
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

API = "http://127.0.0.1:8013/api/search/debug"
DB = ROOT / "data" / "library.db"


def candidates(q: str, kb: int, n: int = 10) -> tuple[list[str], str]:
    body = json.dumps({"question": q, "kb_id": kb, "rerank": False, "top_k": n}).encode()
    req = urllib.request.Request(API, data=body, headers={"Content-Type": "application/json"})
    out = json.load(urllib.request.urlopen(req, timeout=120))
    ids = [f["chunk_id"] for f in out["final"]]
    conn = sqlite3.connect(DB)
    ph = ",".join("?" for _ in ids)
    texts = {r[0]: r[1] for r in conn.execute(f"select id,text from chunk where id in ({ph})", ids)}
    gold = str(out["final"][0]["doc_title"] or "") if out["final"] else ""
    return [texts[i] for i in ids if i in texts], gold


def main() -> None:
    calib = json.loads((ROOT / "data/calibration/kb_2.json").read_text(encoding="utf-8"))
    samples = calib["refusal"]["samples"][:8]
    batch = []
    for s in samples:
        texts, _ = candidates(s["q"], 2)
        batch.append((s["q"], texts, s["truth"].replace(".pdf", "").replace(".md", "")))

    from sentence_transformers import CrossEncoder

    ref = str(ROOT / "data/models/BAAI--bge-reranker-base")
    for mlen in (96, 128, 192, 256, 384):
        model = CrossEncoder(ref, max_length=mlen, device="cpu")
        model.predict([(batch[0][0], batch[0][1][0])])  # 预热，别把首对初始化算进去
        t0 = time.perf_counter()
        for q, texts, _truth in batch:
            model.predict([(q, t) for t in texts])
        dt = (time.perf_counter() - t0) * 1000
        print(f"max_length={mlen:<4} {dt:7.0f}ms total  {dt/len(batch):6.0f}ms/问")

    # 质量：用文档标题做命中判定，跟配置注释里的 doc 级 top1 同口径
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    qs = [s["q"] for s in samples]
    truths = [s["truth"] for s in samples]
    for mlen in (128, 192, 384):
        model = CrossEncoder(ref, max_length=mlen, device="cpu")
        hit1 = hit3 = 0
        for q, truth in zip(qs, truths):
            body = json.dumps({"question": q, "kb_id": 2, "top_k": 10, "rerank": False}).encode()
            req = urllib.request.Request(API, data=body, headers={"Content-Type": "application/json"})
            out = json.load(urllib.request.urlopen(req, timeout=120))
            ids = [f["chunk_id"] for f in out["final"]]
            ph = ",".join("?" for _ in ids)
            rows = {r["id"]: dict(r) for r in conn.execute(
                f"select c.id,c.text,d.title from chunk c join document d on d.id=c.doc_id "
                f"where c.id in ({ph})", ids).fetchall()}
            items = [(rows[i]["text"], str(rows[i]["title"] or "")) for i in ids if i in rows]
            scores = model.predict([(q, t) for t, _ in items])
            order = sorted(range(len(items)), key=lambda i: -scores[i])
            titles = [items[i][1] for i in order]
            if titles and truth in titles[0]:
                hit1 += 1
            if any(truth in t for t in titles[:3]):
                hit3 += 1
        print(f"max_length={mlen:<4} doc-top1={hit1}/{len(qs)}  doc-top3={hit3}/{len(qs)}")


if __name__ == "__main__":
    main()
