"""把 MedRAG（DDXPlus 62 病种精编）的中文素材灌进 RAG 知识库。

**数据来源**：`data/previsit/ddxplus_en.xlsx`（英文原文）+ `data/previsit/
medrag_zh.json`（逐句校验过的译文）。不再读 kg.db 里那份机翻 `disease_desc`
——原因见 `scripts/medrag_source.py` 顶部：12 个病名在 OpenCMKG 里查不到、
8 处句子错译、行序错乱。宁可灌不进去，也不把错译喂给追问链路。

**按系统归并成 6 篇，不是一病一篇。** 一病一篇试过，48 篇占库 71% 的容量，
实测 10 个主诉的 top6 里只命中 7 个位置（11.7%），6 个查询颗粒无收——原因
是 20 篇分域问法每篇有 3~4 块，同一个域的块会连号刷屏（问"头晕"时前三条
全是「头晕·trigger / 头晕·history / 预问诊·头晕」），top6 塞满后病种文档
根本挤不进去。归并成系统级文档后，每篇内部按 `## 病名` 切块，一块一个病。

它在系统里的位置是**路 B：追问提示的来源**：

    患者原话 ──语义召回──> 命中的 MedRAG 病种 ──> "这个病还该确认什么"

跟路 A（本地 SQLite 的 OpenCMKG 8685 病）分工不同：路 A 负责"往哪个方向想"
（科室倾向、候选病），路 B 负责"下一句问什么"。

**灌进去的是素材，不是问句**：直接把"呼吸困难加重、慢性咳嗽伴咳痰"发给患者，
那是病历不是问诊。引擎按槽位把它转成问法，原文仍取配置。

用法：
    ./.venv/Scripts/python.exe scripts/build_medrag_zh.py   # 改译文后才需要
    ./.venv/Scripts/python.exe scripts/ingest_medrag_kb.py
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

from medrag_source import build_docs  # noqa: E402

KB_NAME = "预问诊知识库"
DB = ROOT / "data" / "previsit" / "kg.db"
# 一块 = 一个病，所以必须按标题切块、且不能带重叠：
# overlap 会把上一块的尾巴带进下一块（标签写"慢阻肺加重"、正文开头是哮吼的"犬吠样咳嗽"），
# 症状就串到隔壁病头上了。
KB_CONFIG = {"chunking.strategy": "heading", "chunking.overlap": 0,
             "chunking.min_chunk": 20}


def _call(base: str, path: str, payload: dict | None = None, method: str = "POST"):
    data = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
    req = urllib.request.Request(base + path, data=data, method=method,
                                headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as resp:
        return json.loads(resp.read().decode())


def ensure_kb(base: str) -> int:
    kbs = _call(base, "/api/kb", None, method="GET")
    items = kbs if isinstance(kbs, list) else (kbs.get("kbs") or kbs.get("items") or [])
    for k in items:
        if k.get("name") == KB_NAME:
            return int(k["id"])
    created = _call(base, "/api/kb",
                    {"name": KB_NAME, "description": "预问诊追问知识（分域问法 + MedRAG 病种素材）"})
    return int(created["id"])


def docs_list(base: str, kb_id: int) -> list[dict]:
    got = _call(base, f"/api/kb/{kb_id}/docs", None, method="GET")
    return got if isinstance(got, list) else (got.get("docs") or got.get("items") or [])


def ensure_chunking(base: str, kb_id: int) -> None:
    """把库切到 heading 分块。脚本自己保证这件事，别指望人手点过一次。"""
    try:
        _call(base, f"/api/kb/{kb_id}/config",
              {"values": KB_CONFIG, "replace": False}, method="PATCH")
    except urllib.error.HTTPError as exc:          # 老版本没有这个接口，不致命
        print(f"  切分块策略失败（{exc.code}），继续按现有策略灌")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--dry-run", action="store_true", help="只打印生成结果，不灌库")
    ap.add_argument("--db", default=str(DB), help="kg.db，用来取病名的科室")
    args = ap.parse_args()

    docs = build_docs(Path(args.db))
    if not docs:
        print("没生成任何文档：检查 medrag_zh.json 与 ddxplus_en.xlsx")
        return 1
    if args.dry_run:
        n = sum(t.count("\n## ") for _, t in docs)
        print(f"共 {len(docs)} 篇、{n} 个病种。首篇预览：\n")
        print(docs[0][0])
        print("-" * 60)
        print(docs[0][1][:1200])
        return 0

    try:
        kb_id = ensure_kb(args.base)
    except urllib.error.URLError as exc:
        print(f"连不上服务 {args.base}：{exc}（先 knowtune serve）")
        return 1
    ensure_chunking(args.base, kb_id)

    # 同名先删，重跑不会变成 12 篇
    old = [d for d in docs_list(args.base, kb_id)
           if str(d.get("title", "")).startswith("MedRAG")]
    for d in old:
        try:
            _call(args.base, f"/api/doc/{d['id']}", None, method="DELETE")
        except urllib.error.HTTPError as exc:
            print(f"  删除旧文档 {d.get('title')} 失败：{exc.code}")
    print(f"知识库「{KB_NAME}」id={kb_id}，清掉旧文档 {len(old)} 篇，导入 {len(docs)} 篇")

    ok = chunks = 0
    for title, text in docs:
        try:
            r = _call(args.base, "/api/import/text",
                      {"kb_id": kb_id, "title": title, "text": text})
        except urllib.error.HTTPError as exc:
            print(f"  {title} 导入失败：{exc.code} {exc.read()[:160]}")
            continue
        n = int(r.get("n_chunks") or 1)
        ok += 1
        chunks += n
        print(f"  {title}: {n} 块")
    print(f"完成：{ok}/{len(docs)} 篇，{chunks} 块")

    # 顺带核对病名是否都能在 OpenCMKG 里找到科室——找不到就是译文又飘了
    try:
        con = sqlite3.connect(f"file:{Path(args.db).as_posix()}?mode=ro", uri=True)
        names = {r[0] for r in con.execute("SELECT disease FROM disease_dept")}
        con.close()
        lost = []
        for _, text in docs:
            for line in text.splitlines():
                if line.startswith("## "):
                    n = line[3:].strip()
                    if n not in names:
                        lost.append(n)
        if lost:
            print(f"提示：{len(lost)} 个病名在 OpenCMKG 里没有科室记录"
                  f"（追问仍可用，科室投票靠症状）：{'、'.join(lost)}")
    except sqlite3.Error:
        pass
    return 0 if ok == len(docs) else 1


if __name__ == "__main__":
    raise SystemExit(main())
