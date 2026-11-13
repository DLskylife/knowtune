"""文档完整性自检（覆盖率比对）。

从 `app.py` 拆出来的独立模块：`common.py` 里的导入任务结束后要调
`_coverage_digest`，而 `docs` 路由的「完整性自检」要调 `_coverage_report`。
放独立模块让这两处都只朝一个方向依赖，避免 common -> routes 的环。
"""

from __future__ import annotations

import hashlib
import re

from .. import paths as paths_mod
from ..ingest import loader as loader_mod
from ..ingest.chunker import repair_chunk_primitives
from ..ingest.cleaner import clean_loaded, plan_from_dict
from .common import _source_missing, _stored_plan
from .state import state


def _coverage_report(doc_id: int) -> dict:
    """算一份覆盖率结论（约 100ms，源文件还在就能算）。

    抽成函数是因为它有两处调用：手动点「完整性自检」，以及**导入/重索引任务
    结束时自动跑一次**。后者才是关键 —— 让人手动去点一个藏得很深的按钮，
    等于把"到底导全没有"继续留给感觉回答。
    """
    conn = state.conn
    doc = conn.execute(
        "SELECT id, title, source_type, source_uri, active_gen, status, extractor,"
        " content_hash, clean_json FROM document WHERE id=?", (doc_id,),
    ).fetchone()
    if doc is None:
        return {"ok": False, "reason": "文档不存在"}
    if doc["status"] != "ready":
        return {"ok": False, "reason": f"文档状态是 {doc['status']}，先成功索引再校验"}
    if _source_missing(doc["source_type"], doc["source_uri"] or ""):
        return {"ok": False, "reason": "源文件已不在原路径，无法比对（重新上传后可校验）"}

    try:
        loaded = loader_mod.load_path(
            paths_mod.resolve_source(doc["source_uri"]),
            min_pdf_text_chars=state.cfg.ingest.pdf.min_text_chars_per_page,
        )
    except Exception as exc:
        return {"ok": False, "reason": f"重新抽取源文件失败：{type(exc).__name__}: {exc}"}

    # 清洗过的文档，库里存的是"洗完之后"的文本。不先跑同一套规则就比对，
    # 每篇都会报成"源文件改过"——那句提示会彻底失去意义。
    plan = plan_from_dict(_stored_plan(doc["clean_json"]))
    cleaned_note = None
    if plan.any_enabled():
        loaded, report = clean_loaded(loaded, plan)
        cleaned_note = report.as_dict()

    rows = conn.execute(
        "SELECT text FROM chunk WHERE doc_id=? AND generation=? ORDER BY seq",
        (doc_id, int(doc["active_gen"])),
    ).fetchall()
    cov = _coverage_of(loaded.text, [r["text"] or "" for r in rows])
    # 源文件在导入之后被改过，是"覆盖不全"最常见的解释——不是切分丢了，
    # 而是库里存的是上一版。不区分这两者，用户会以为系统把内容吃掉了。
    now_hash = hashlib.sha256(loaded.text.encode("utf-8")).hexdigest()
    stale_source = bool(doc["content_hash"]) and now_hash != doc["content_hash"]
    cov["stale_source"] = stale_source
    cov["cleaned"] = cleaned_note
    return {
        **{"ok": True}, **cov,
        # 报"源文件字数"还是"清洗后字数"：后者才是库里应有的量，前者会让人
        # 以为清洗删掉的部分是"没导进去"
        "source_chars": len(loaded.text),
        "extractor": loaded.extractor,
        "doc_extractor": doc["extractor"],
    }


def _coverage_digest(doc_id: int) -> dict | None:
    """任务结束时附在结果里的那一小段结论：能算就算，算不了也不许拖累任务。"""
    try:
        r = _coverage_report(doc_id)
    except Exception:
        return None
    if not r.get("ok"):
        return None
    return {
        "ratio": r.get("ratio"),
        "big_gap_count": r.get("big_gap_count") or 0,
        "max_gap": r.get("max_gap") or 0,
        "stale_source": bool(r.get("stale_source")),
        "source_chars": r.get("source_chars") or 0,
        "chunks_chars": r.get("checked") or 0,
    }


def _cmp_norm(text: str) -> str:
    """比对用的归一化：清洗 + 去空白 + 去 Markdown 标记。

    三件事都在消除"看起来不一样、其实一个字没少"的假警报：
      · repair —— 切分本就会清掉控制字符与超长空白，那是清洗不是丢失；
      · 空白 —— 换行缩进差异不是内容差异；
      · `#|*~>` —— 抽取时标题的 `##` 会被摘掉、表格会重排成 `列 | 列`，
        若不忽略这些标记，每个标题行和表格行都会被报成"缺了一段"，
        用户看到的是满屏假缺口，反而不知道到底缺没缺。
    """
    text = repair_chunk_primitives(text or "")
    text = re.sub(r"\s+", "", text)
    return re.sub(r"[#|*~>`]", "", text)


def _coverage_of(source: str, chunks: list[str], k: int = 24) -> dict:
    """返回原文被 chunk 覆盖的比例与未覆盖区间样本。"""
    src = _cmp_norm(source)
    joined = _cmp_norm("".join(chunks))
    if not src:
        return {"ratio": 1.0, "gaps": [], "gap_chars": 0, "checked": 0}
    seen = {joined[i : i + k] for i in range(0, max(0, len(joined) - k + 1))}

    gaps: list[dict] = []
    gap_chars = 0
    i = 0
    n = len(src)
    while i < n:
        w = src[i : i + k]
        if len(w) < k:  # 尾巴不足一个窗口，用能比的部分兜底
            w = src[i:]
            if not w or w in joined:
                break
            gaps.append({"start": i, "end": n, "text": source[i : i + 60]})
            gap_chars += len(w)
            break
        if w in seen:
            i += k
            continue
        j = i + 1
        while j < n and src[j : j + k] not in seen:
            j += 1
        end = min(j + k, n)
        gap_chars += end - i
        gaps.append({"start": i, "end": end, "text": src[max(0, i - 10) : end + 10][:120]})
        i = end + k
    covered = max(0, n - gap_chars)
    # 短缺口基本都是页眉页码、标记符号这类"顺序/清洗"差异，不是真丢了内容。
    # 只有成段（>= 60 字）的缺口才值得当成证据摆给用户，其余只进百分比。
    big = [g for g in gaps if g["end"] - g["start"] >= 60]
    return {
        "ratio": round(covered / n, 4) if n else 1.0,
        "gap_chars": gap_chars,
        "checked": n,
        "gaps": big[:5],
        "gap_count": len(gaps),
        "big_gap_count": len(big),
        "max_gap": max((g["end"] - g["start"] for g in gaps), default=0),
    }