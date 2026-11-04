"""把预问诊的分域追问知识灌进主项目的 RAG 知识库（语义召回用）。

为什么灌两遍（yaml 一份、知识库一份）：两者干的活不同。

- `config/previsit_kb.yaml`：引擎的**配置**，负责"问法原文"——可审计、可改、
  离线可用，知识库挂了也照常问出这句话。
- 知识库：负责**语义召回**——患者说"胃里翻江倒海还拉稀"，关键词一个没命中，
  但语义上它就是腹痛域；这时候靠向量把它捞回来，再决定问什么。

每篇文档 = 一个症状域，每个条目 = 一节（标题里带 `域·槽位(key)`），
召回后引擎从 doc_title / heading 解析出域与槽位，问法仍回配置里取原文。

用法：./.venv/Scripts/python.exe scripts/ingest_previsit_kb.py [--base http://127.0.0.1:8000]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from previsit.config import load_previsit  # noqa: E402

KB_NAME = "预问诊知识库"


def _call(base: str, path: str, payload: dict | None = None, method: str = "POST"):
    data = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
    req = urllib.request.Request(
        base + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode())


def ensure_kb(base: str) -> int:
    kbs = _call(base, "/api/kb", None, method="GET")
    items = kbs if isinstance(kbs, list) else (kbs.get("kbs") or kbs.get("items") or [])
    for k in items:
        if k.get("name") == KB_NAME:
            return int(k["id"])
    created = _call(base, "/api/kb", {"name": KB_NAME, "description": "预问诊分域追问知识（语义召回）"})
    return int(created["id"])


def doc_markdown(dom: dict) -> str:
    """一个症状域一篇文档。

    结构上刻意让"关键词 + 要点"排在前面——语义召回靠这段匹配患者原话；
    `问法` 排在条目末尾，是给人看的备份（引擎实际取问法走配置，不靠它）。
    """
    label = dom["label"]
    lines = [
        f"# 预问诊 · {label}",
        "",
        "适用主诉：" + "、".join(dom.get("keywords") or []),
        "",
    ]
    for e in dom.get("entries") or []:
        slot_label = _slot_label(dom, e["slot"])
        lines.append(f"## {label} · {slot_label}（{e['slot']}）")
        if e.get("keywords"):
            lines.append("关键词：" + "、".join(e["keywords"]))
        lines.append("要点：" + (e.get("text") or "").strip())
        if e.get("ask"):
            lines.append("问法：" + e["ask"])
        lines.append("")
    return "\n".join(lines)


def _slot_label(dom: dict, key: str) -> str:
    hints = dom.get("slot_hints") or {}
    return str(key)          # 槽位显示名在全局配置里，这里没有；标题用 key 足够


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--kb-path", default="")
    args = ap.parse_args()

    pv = load_previsit(kb_path=args.kb_path or None)
    domains = pv.get("domains") or []
    if not domains:
        print("config/previsit_kb.yaml 里没有分域知识，先建知识库再跑这个脚本")
        return 1

    try:
        kb_id = ensure_kb(args.base)
    except urllib.error.URLError as exc:
        print(f"连不上服务 {args.base}：{exc}（先 knowtune serve）")
        return 1
    print(f"知识库「{KB_NAME}」id={kb_id}")

    ok = 0
    for dom in domains:
        title = f"预问诊 · {dom['label']}"
        try:
            res = _call(args.base, "/api/import/text",
                        {"kb_id": kb_id, "title": title, "text": doc_markdown(dom)})
        except urllib.error.HTTPError as exc:
            print(f"  ✗ {title}：HTTP {exc.code} {exc.read()[:200]!r}")
            continue
        status = res.get("status")
        print(f"  {'✓' if status == 'ready' else '·'} {title} → {status} "
              f"({res.get('message') or ''})")
        if status == "ready":
            ok += 1
        time.sleep(0.05)

    docs = _call(args.base, f"/api/kb/{kb_id}/docs", None, method="GET")
    items = docs if isinstance(docs, list) else (docs.get("docs") or docs.get("items") or [])
    chunks = sum(int(d.get("n_chunks") or 0) for d in items)
    print(f"\n导入完成：{ok}/{len(domains)} 篇，共 {len(items)} 篇文档 / {chunks} 个分块")
    return 0 if ok == len(domains) else 2


if __name__ == "__main__":
    raise SystemExit(main())
