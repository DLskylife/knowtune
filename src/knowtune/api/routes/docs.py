"""文档路由：导入（路径/文本/URL/上传/分片）、清洗预览、重索引、移动、删除、
预览、完整性自检、chunk 查询与就地编辑。"""

from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ... import logbus
from ... import paths as paths_mod
from ...db import utcnow as _utcnow
from ...ingest import loader as loader_mod
from ...ingest.cleaner import clean_loaded, plan_from_dict, rules_payload
from ...ingest.pipeline import display_title
from ..common import (
    UPLOAD_DIR,
    _CHUNK_SIZE,
    _IMPORT_TASKS,
    _TASK_LOCK,
    _UID_RE,
    _UPLOAD_CHUNK,
    _discard_parts,
    _inside,
    _quiet_unlink,
    _read_meta,
    _received_parts,
    _save_upload,
    _source_missing,
    _start_import_task,
    _stored_plan,
    _task_public,
    _upload_dir,
    _write_meta,
)
from ..coverage import _coverage_report
from ..state import state

router = APIRouter()


class PathImport(BaseModel):
    kb_id: int
    path: str
    plan: dict[str, object] | None = None


class TextImport(BaseModel):
    kb_id: int
    title: str = ""
    text: str = Field(min_length=1)
    plan: dict[str, object] | None = None


class UrlImport(BaseModel):
    kb_id: int
    urls: list[str] = Field(min_length=1, max_length=50)
    plan: dict[str, object] | None = None


class ReindexReq(BaseModel):
    # 不传 = 沿用上次导入时用的那套清洗方案；传空 dict = 这次不清洗
    plan: dict[str, object] | None = None


class DocMove(BaseModel):
    """把文档换到另一个知识库。`doc_ids` 给批量用，单篇走路径参数。"""
    kb_id: int = Field(gt=0)
    doc_ids: list[int] = Field(default_factory=list, max_length=200)


@router.get("/api/import/task/{task_id}")
def import_task(task_id: str) -> dict:
    task = _IMPORT_TASKS.get(task_id)
    if task is None:
        # 进程重启后旧 task_id 一律失效（任务表只在内存里），说清楚而不是 404 装死
        raise HTTPException(404, "任务不存在或服务已重启，请重新上传")
    return _task_public(task)


@router.get("/api/import/tasks")
def import_tasks() -> dict:
    """当前全部后台任务（前端切回页面时用它补齐：轮询只在任务发起的那一刻
    开始，切走再回来，先前那些任务的卡片已经没人喂数据了）。"""
    with _TASK_LOCK:
        items = [_task_public(t) for t in _IMPORT_TASKS.values()]
    items.sort(key=lambda t: t.get("started_at") or "")
    return {
        "items": items,
        "running": sum(1 for t in items if t["state"] == "running"),
        "queued": sum(1 for t in items if t["state"] == "queued"),
    }


# ---------------------------------------------------------------- 清洗
#
# 清洗 = "导入前把不该进库的内容剔掉"。两条硬约束：
#   ① 源文件与上传副本都只当原料，一个字节都不写回（写回会让重索引拿到
#      "洗过一遍"的输入，二次清洗结果不可预期）；
#   ② 每条规则都要报账单 —— 删错就是永久丢失，用户得在导入前看到删了什么。

@router.get("/api/clean/rules")
def clean_rules_api() -> dict:
    """规则定义、默认值、参数规格全部由后端下发，前端不写死任何一条。"""
    return rules_payload()


def _parse_plan(raw: str | None) -> "object":
    if raw is None or not str(raw).strip():
        return None
    try:
        return json.loads(raw)
    except Exception as exc:
        raise HTTPException(400, f"清洗方案不是合法 JSON：{exc}") from exc


@router.post("/api/clean/preview")
async def clean_preview(
    file: UploadFile | None = None,
    plan: str | None = Form(None),
    text: str | None = Form(None),
    path: str | None = Form(None),
    staged: str | None = Form(None),
    doc_id: int | None = Form(None),
    url: str | None = Form(None),
) -> dict:
    """不入库，只报告"这套规则会删掉什么"。

    上传的文件会落盘并回一个 staged 路径：预览完直接导入就不用再传一次字节，
    而且改规则重新预览走的还是同一份文件（否则每次勾选都要重传，本地文件
    倒还好，稍大一点就是几十秒）。
    """
    parsed = plan_from_dict(_parse_plan(plan))
    try:
        parsed.validate()
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    target: Path | None = None
    title = ""
    if file is not None and (file.filename or ""):
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        name = Path(file.filename or "upload.bin").name
        target = UPLOAD_DIR / f"{uuid.uuid4().hex[:8]}-{name}"
        await _save_upload(file, target, state.cfg.ingest.max_file_mb)
        title = name
    elif staged:
        target = paths_mod.resolve_source(staged)
        # staged 只认 uploads 里的路径：这是个落盘路径参数，不校验等于把
        # 任意文件读取能力开放出去。
        if not _inside(target, UPLOAD_DIR) or not target.exists():
            raise HTTPException(400, "预览用的文件已失效，请重新选择文件")
        title = display_title(str(target))
    elif path:
        target = paths_mod.resolve_source(path)
        if not target.exists():
            raise HTTPException(400, f"路径不存在：{path}")
        title = target.name
    elif doc_id:
        row = state.conn.execute(
            "SELECT source_type, source_uri, title FROM document WHERE id=?", (doc_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "文档不存在")
        if row["source_type"] != "file" or _source_missing("file", row["source_uri"] or ""):
            raise HTTPException(400, "这份文档的原始文件已不在，无法预览清洗效果")
        target = paths_mod.resolve_source(row["source_uri"])
        title = row["title"] or target.name
    elif url:
        ucfg = state.cfg.ingest.url
        try:
            loaded = loader_mod.load_url(
                url.strip(), allowed_domains=ucfg.allowed_domains or None
            )
        except loader_mod.ExtractError as exc:
            raise HTTPException(400, str(exc)) from exc
        title = loaded.title or url
        cleaned, report = clean_loaded(loaded, parsed)
        return {
            "ok": True, "title": title, "staged": None,
            "report": report.as_dict(), "cleaned_text": cleaned.text[:6000],
            "cleaned_chars": len(cleaned.text),
        }

    try:
        if target is not None:
            loaded = loader_mod.load_path(
                target, min_pdf_text_chars=state.cfg.ingest.pdf.min_text_chars_per_page
            )
        else:
            if not (text or "").strip():
                raise HTTPException(400, "没有可预览的内容：给文件或文本")
            loaded = loader_mod.load_inline_text(text or "", title or "粘贴文本")
            title = title or "粘贴文本"
    except loader_mod.ExtractError as exc:
        raise HTTPException(400, str(exc)) from exc

    cleaned, report = clean_loaded(loaded, parsed)
    return {
        "ok": True,
        "title": title,
        "staged": paths_mod.stored_path(target)
        if target is not None and _inside(target, UPLOAD_DIR) else None,
        "report": report.as_dict(),
        # 前 6000 字够判断"洗得对不对"了，全量文本回来只会让弹窗卡一下
        "cleaned_text": cleaned.text[:6000],
        "cleaned_chars": len(cleaned.text),
    }


@router.post("/api/import/path")
def import_path(req: PathImport) -> dict:
    p = paths_mod.resolve_source(req.path)
    if not p.exists():
        raise HTTPException(400, f"路径不存在：{req.path}")
    cfg = state.effective_cfg(req.kb_id)  # 分块参数按库生效
    if p.is_dir():
        results = []
        files = [f for f in p.rglob("*") if f.is_file() and f.suffix.lower() in loader_mod.SUPPORTED_EXTS]
        for f in files[:500]:
            results.append(
                state.pipeline.import_path(req.kb_id, f, cfg, clean=req.plan).as_dict())
        ok = sum(1 for r in results if r["status"] == "ready")
        logbus.note(f"目录导入 {p}：{ok}/{len(results)} 成功", source="ingest")
        return {"mode": "dir", "total": len(results), "ready": ok, "results": results}
    result = state.pipeline.import_path(req.kb_id, p, cfg, clean=req.plan).as_dict()
    logbus.note(
        f"导入 {p.name}：{result['message']}",
        level="INFO" if result["status"] == "ready" else "ERROR",
        source="ingest",
    )
    return result


@router.post("/api/import/text")
def import_text(req: TextImport) -> dict:
    result = state.pipeline.import_text(
        req.kb_id, req.text, req.title, state.effective_cfg(req.kb_id), clean=req.plan
    ).as_dict()
    logbus.note(
        f"粘贴文本《{req.title or '未命名'}》：{result['message']}",
        level="INFO" if result["status"] == "ready" else "ERROR",
        source="ingest",
    )
    return result


@router.post("/api/import/url")
def import_url(req: UrlImport) -> dict:
    """单条或批量。逐条独立失败——一条 URL 挂掉不该让整批回滚。"""
    cfg = state.effective_cfg(req.kb_id)
    results = []
    for raw in req.urls[:50]:
        url = raw.strip()
        if not url:
            continue
        item = state.pipeline.import_url(req.kb_id, url, cfg, clean=req.plan).as_dict()
        item["url"] = url
        results.append(item)
    if not results:
        raise HTTPException(400, "没有有效的 URL")
    ready = sum(1 for r in results if r["status"] == "ready")
    logbus.note(f"URL 导入：{ready}/{len(results)} 成功", source="ingest")
    return {"total": len(results), "ready": ready, "results": results}


# ---------------------------------------------------------------- 分片上传 API
#
# 四个动作对应传输的一生：init 拿分片方案 → put 逐片推进 → finish 合并成
# 一份 staged 文件 → （断了就）abort 清掉。
#
# finish 产出的路径直接喂给 /api/import/upload 的 `staged` 参数，导入链路
# 一个字都不用改：分片只是"另一种把字节放上磁盘的方式"，放完就退场。


class UploadInitReq(BaseModel):
    name: str
    size: int
    # 带上 kb_id 才能用这个库自己的覆盖值：库级把上限调小了，分片就得在这里
    # 拦住，否则传完 500MB 才在导入那一步被拒，白等一场。
    kb_id: int | None = None
    # 前端算的指纹（名字+大小+修改时间）。同一文件重传能接上已收的分片；
    # 不给就每次新开一个 id，退化成普通分片上传。
    fingerprint: str | None = None


@router.post("/api/upload/init")
def upload_init(req: UploadInitReq) -> dict:
    name = Path(req.name or "upload.bin").name
    size = int(req.size)
    cfg = state.effective_cfg(req.kb_id) if req.kb_id else state.cfg
    limit_mb = int(cfg.ingest.max_file_mb)
    limit = max(1, limit_mb) * 1024 * 1024
    if size <= 0:
        raise HTTPException(400, "文件是空的")
    if size > limit:
        raise HTTPException(
            413, f"文件 {size / 1048576:.0f}MB，超过上限 {limit_mb}MB"
        )

    fp = (req.fingerprint or "").lower()
    upload_id = fp if _UID_RE.match(fp) else __import__("hashlib").sha1(
        f"{name}|{size}|{time.time()}".encode()
    ).hexdigest()[:32]
    folder = _upload_dir(upload_id)
    meta = _read_meta(folder)

    # 同名同大小但内容不同（编辑过又存回去）会撞同一个 id。size 变了直接重来；
    # 大小没变就没法分辨，照旧续传——总比每次从零开始好，finish 还有总字节校验。
    if meta and int(meta.get("size", -1)) != size:
        _discard_parts(folder)
        meta = None

    total = max(1, (size + _CHUNK_SIZE - 1) // _CHUNK_SIZE)
    if not meta:
        meta = {"name": name, "size": size, "chunk_size": _CHUNK_SIZE,
                "chunks": total, "created_at": time.time()}
        _write_meta(folder, meta)

    received = _received_parts(folder)
    return {
        "upload_id": upload_id,
        "chunk_size": int(meta["chunk_size"]),
        "total_chunks": int(meta["chunks"]),
        "received": received,
        "name": meta.get("name") or name,
    }


@router.put("/api/upload/{upload_id}/part/{index}")
async def upload_part(upload_id: str, index: int, request: Request) -> dict:
    """收一片。body 就是裸字节，不走 multipart。

    multipart 会在每片外面套一层边界+头，服务端还得先解析再落盘；裸字节直接
    边收边写，省一次内存拷贝。代价是不能在这个请求里带别的字段——但分片本来
    就不需要别的字段，元信息在 init 时已经定了。
    """
    folder = _upload_dir(upload_id)
    meta = _read_meta(folder)
    if not meta:
        raise HTTPException(404, "上传已过期或不存在，请重新开始")
    if not (0 <= index < int(meta["chunks"])):
        raise HTTPException(400, f"分片序号越界：{index}")

    # 单片允许比标准片稍大一点（前端按字节切，末片永远更小），但不给"一片
    # 装下整个文件"留口子——那样分片就白做了。
    cap = int(meta["chunk_size"]) + (1 << 20)
    tmp = folder / f"{index}.part.tmp"
    written = 0
    try:
        with tmp.open("wb") as fh:
            async for block in request.stream():
                written += len(block)
                if written > cap:
                    raise HTTPException(413, "单片超过大小上限")
                fh.write(block)
        if written == 0:
            raise HTTPException(400, "分片是空的")
        os.replace(tmp, folder / f"{index}.part")
    except BaseException:
        _quiet_unlink(tmp)
        raise

    meta["updated_at"] = time.time()
    _write_meta(folder, meta)
    return {"received": len(_received_parts(folder)), "chunks": int(meta["chunks"])}


@router.post("/api/upload/{upload_id}/finish")
def upload_finish(upload_id: str) -> dict:
    """按序合并，落地成一份 staged 文件。

    总字节数必须等于 init 时声明的 size：不等就说明少片或有多余数据，拼出来
    是个**看起来正常、实际被截断**的文件——这种文件进了库比导入失败糟得多，
    检索永远少一截，还没有任何迹象。
    """
    folder = _upload_dir(upload_id)
    meta = _read_meta(folder)
    if not meta:
        raise HTTPException(404, "上传已过期或不存在，请重新开始")

    chunks = int(meta["chunks"])
    got = _received_parts(folder)
    missing = [i for i in range(chunks) if i not in set(got)]
    if missing:
        raise HTTPException(
            400, f"还缺 {len(missing)} 片（第一个是第 {missing[0] + 1} 片）"
        )

    name = Path(str(meta.get("name") or "upload.bin")).name
    # 目录名带 uuid 前缀：同名文件重复上传不会互相覆盖，而 display_title 会
    # 把这个前缀去掉，界面上看到的还是原名
    target = UPLOAD_DIR / f"{uuid.uuid4().hex[:8]}-{name}"
    tmp = target.with_name(target.name + ".part")
    total = 0
    try:
        with tmp.open("wb") as out:
            for i in range(chunks):
                with (folder / f"{i}.part").open("rb") as fh:
                    shutil.copyfileobj(fh, out, _UPLOAD_CHUNK)
                total += (folder / f"{i}.part").stat().st_size
        if total != int(meta["size"]):
            raise HTTPException(
                400,
                f"合并后 {total} 字节，与声明的 {meta['size']} 不符，请重传",
            )
        os.replace(tmp, target)
    except BaseException:
        _quiet_unlink(tmp)
        raise
    _discard_parts(folder)
    return {"staged": paths_mod.stored_path(target), "name": name, "size": total}


@router.delete("/api/upload/{upload_id}")
def upload_abort(upload_id: str) -> dict:
    folder = _upload_dir(upload_id)
    _discard_parts(folder)
    return {"ok": True}


@router.post("/api/import/upload")
async def import_upload(
    kb_id: int,
    file: UploadFile | None = None,
    plan: str | None = Form(None),
    staged: str | None = Form(None),
) -> dict:
    parsed = plan_from_dict(_parse_plan(plan))
    try:
        parsed.validate()
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    if staged:
        # 预览已经把字节收过了，别再传一次
        dest = paths_mod.resolve_source(staged)
        if not _inside(dest, UPLOAD_DIR) or not dest.exists():
            raise HTTPException(400, "预览用的文件已失效，请重新选择文件")
        title = display_title(str(dest))
    else:
        if file is None:
            raise HTTPException(400, "没有收到文件")
        name = Path(file.filename or "upload.bin").name
        dest = UPLOAD_DIR / f"{uuid.uuid4().hex[:8]}-{name}"
        await _save_upload(file, dest, state.cfg.ingest.max_file_mb)
        title = name
    # 这里**故意不删**落盘副本：document.source_uri 指向它，删了就等于
    # 让这个文档永远"重索引失败"（之前正是这么做的，用户点重索引必然报
    # 文件不存在）。上传文件本来也不大，留着当代价换可重索引是划算的。
    unlink_on_fail = not staged  # 预览阶段的落盘副本已确认要导入，失败也留着
    cfg = state.effective_cfg(kb_id)

    def run(progress):
        try:
            result = state.pipeline.import_path(
                kb_id, dest, cfg, progress=progress, clean=parsed
            ).as_dict()
        except Exception:
            if unlink_on_fail:
                _quiet_unlink(dest)  # 连文档行都没建起来，留个孤儿文件没意义
            raise
        # 标题由流水线按落盘名去前缀生成（见 pipeline.display_title），
        # 这样重索引时也不会退回带 uuid 的版本。返回值与库里一样用相对路径。
        result["source_uri"] = paths_mod.stored_path(dest)
        return result

    # 立刻返回 task_id：文件字节已经收完了，剩下的解析/向量化放到后台线程，
    # 前端逐帧轮询进度。同步跑完再返回的话，这段时间只能给一个假的转圈。
    return _start_import_task(title, run)


@router.post("/api/doc/{doc_id}/source")
async def restore_source(doc_id: int, file: UploadFile) -> dict:
    """给丢失了原始文件的文档补回文件，并立即重索引。

    起因：上传副本曾被删掉，`source_uri` 成了死路径，重索引 100% 失败。
    对这批历史文档，除了重新导入（等于换一个新 doc_id、丢掉已有的引用编号），
    更合理的是把文件放回原位。
    """
    row = state.conn.execute(
        "SELECT kb_id, source_type, source_uri, title, clean_json FROM document WHERE id=?",
        (doc_id,),
    ).fetchone()
    if not row:
        raise HTTPException(404, "文档不存在")
    if row["source_type"] != "file":
        raise HTTPException(400, "只有文件来源需要补原始文件；粘贴/URL 内容请重新导入")

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    target = paths_mod.resolve_source(row["source_uri"] or "")
    if not target.name or not _inside(target, UPLOAD_DIR):
        # 原路径在 uploads 之外（手工导入的外部文件被挪走了）→ 收进 uploads，
        # 保证以后重索引有个稳定副本，再把 source_uri 改过来。
        target = UPLOAD_DIR / f"{uuid.uuid4().hex[:8]}-{Path(file.filename or 'upload.bin').name}"
        state.conn.execute(
            "UPDATE document SET source_uri=? WHERE id=?",
            (paths_mod.stored_path(target), doc_id),
        )
    n_bytes = await _save_upload(file, target, state.cfg.ingest.max_file_mb)
    if n_bytes == 0:
        # 空文件没有任何索引价值，但刚落下的空副本要清掉，否则 source_uri
        # 指向一个 0 字节文件，重索引会报"未检测到文本层"这种误导性原因。
        _quiet_unlink(target)
        raise HTTPException(400, "文件是空的")
    state.conn.execute("UPDATE document SET content_hash='' WHERE id=?", (doc_id,))
    kb_id = int(row["kb_id"])
    cfg = state.effective_cfg(kb_id)

    def run(progress):
        result = state.pipeline.import_path(
            kb_id, target, cfg, progress=progress, clean=_stored_plan(row["clean_json"])
        ).as_dict()
        result["source_uri"] = paths_mod.stored_path(target)
        logbus.note(
            f"补回原始文件 doc#{doc_id} {target.name}：{result['message']}",
            level="INFO" if result["status"] == "ready" else "ERROR",
            source="ingest",
        )
        return result

    return _start_import_task(f"补回 {row['title'] or target.name}", run)


@router.post("/api/doc/{doc_id}/reindex")
def reindex(doc_id: int, req: ReindexReq | None = None) -> dict:
    row = state.conn.execute(
        "SELECT source_type, source_uri, kb_id, title, clean_json FROM document WHERE id=?",
        (doc_id,),
    ).fetchone()
    if not row:
        raise HTTPException(404, "文档不存在")
    if row["source_type"] != "file":
        raise HTTPException(400, "只有文件来源支持重新索引；粘贴内容请删除后重新导入")
    if _source_missing(row["source_type"], row["source_uri"] or ""):
        # 409 而不是 500：这不是系统故障，是"源文件没了"，前端据此改成重新上传入口
        return JSONResponse(
            status_code=409,
            content={
                "error": f"原始文件已不在：{row['source_uri']}",
                "missing_source": True,
                "doc_id": doc_id,
                "source_uri": row["source_uri"],
                "hint": "点「重新上传」选同一个文件，会放回原位并重新索引",
            },
        )
    # 不传 plan = 沿用上次那套清洗规则：重索引悄悄把清洗撤掉，等于库里的内容
    # 跟用户当初确认过的那份不是一回事，而且没有任何提示。
    plan = req.plan if (req and req.plan is not None) else _stored_plan(row["clean_json"])
    try:
        parsed = plan_from_dict(plan)
        parsed.validate()
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    kb_id = int(row["kb_id"])
    cfg = state.effective_cfg(kb_id)
    state.conn.execute("UPDATE document SET content_hash='' WHERE id=?", (doc_id,))

    def run(progress):
        result = state.pipeline.import_path(
            kb_id, row["source_uri"], cfg, progress=progress, clean=parsed
        ).as_dict()
        logbus.note(f"重索引 {doc_id}：{result['message']}", source="ingest")
        return result

    return _start_import_task(f"重索引 {row['title'] or doc_id}", run)


def _reindex_kb(kb_id: int) -> dict | None:
    """重建整个库的索引（分块参数改了之后自动调用）。

    走的是同一个串行任务队列：不管库里有 3 篇还是 30 篇，都只占一个队列位置，
    前端也只有一张进度卡。逐个文档起任务会让任务坞瞬间堆满卡片，反而看不出
    整体进度。
    """
    rows = state.conn.execute(
        "SELECT id, title, source_type, source_uri, clean_json FROM document"
        " WHERE kb_id=? ORDER BY id", (kb_id,)
    ).fetchall()
    targets = [
        r for r in rows
        if r["source_type"] == "file"
        and not _source_missing(r["source_type"], r["source_uri"] or "")
    ]
    if not targets:
        return None

    skipped = len(rows) - len(targets)

    def run(progress):
        conn = state.conn      # worker 线程：必须重新取（state.conn 是 thread-local）
        cfg = state.effective_cfg(kb_id)
        n = len(targets)
        done = failed = 0
        chunks = 0
        notes: list[str] = []
        for i, r in enumerate(targets):
            title = r["title"] or f"doc#{r['id']}"
            progress("重建索引", i / n, f"{i + 1}/{n} · {title}")

            def sub(stage, pct, note="", _i=i, _title=title):
                progress(stage, (_i + max(0.0, min(1.0, float(pct)))) / n,
                         f"{_i + 1}/{n} · {note or _title}")

            try:
                conn.execute("UPDATE document SET content_hash='' WHERE id=?", (r["id"],))
                result = state.pipeline.import_path(
                    kb_id, r["source_uri"], cfg, progress=sub,
                    # 沿用这篇自己的清洗方案：改分块参数触发的重建不该顺手
                    # 把清洗撤掉
                    clean=_stored_plan(r["clean_json"]),
                ).as_dict()
                if result.get("status") == "ready":
                    done += 1
                    chunks += int(result.get("n_chunks") or 0)
                else:
                    failed += 1
                    notes.append(f"{title}：{result.get('message') or '失败'}")
            except Exception as exc:                      # 一篇炸了不能带走整批
                failed += 1
                notes.append(f"{title}：{type(exc).__name__}: {exc}")
        msg = f"重建完成：{done} 篇 / {chunks} 块" + (f"，{failed} 篇失败" if failed else "")
        if skipped:
            msg += f"（{skipped} 篇非文件或源文件缺失，已跳过）"
        logbus.note(f"知识库 #{kb_id} 重建索引：{msg}",
                    level="INFO" if not failed else "WARNING", source="ingest")
        return {
            "status": "ready" if done else "failed",
            "message": msg,
            "n_chunks": chunks,
            "notes": notes[:10],
        }

    started = _start_import_task(f"重建索引 · 库 #{kb_id}", run)
    started["docs"] = len(targets)
    started["skipped"] = skipped
    return started


@router.get("/api/doc/{doc_id}/preview")
def preview_doc(doc_id: int, offset: int = 0, limit: int = 200) -> dict:
    """文档预览：元信息 + 当前代次的 chunk 列表（分页）。

    只读 `active_gen` 那一代：重索引过程中新旧两代同时存在，混着展示会把
    同一段文字显示两遍，用户会以为切分出了重复块。

    为什么必须分页而不是"最多 500 块"：一本 40 万字的书能切出 1280 块，
    硬截断之后预览只显示前 39%，用户看到的是"我的文件没全进去"——而库里
    其实一块不少。截断本身不是问题，**静默**截断才是。
    """
    conn = state.conn
    doc = conn.execute(
        "SELECT d.id, d.kb_id, d.title, d.source_type, d.source_uri, d.extractor,"
        " d.status, d.fail_reason, d.n_pages, d.n_chunks, d.active_gen,"
        " d.imported_at, d.updated_at, k.name AS kb_name"
        " FROM document d LEFT JOIN kb k ON k.id = d.kb_id WHERE d.id=?",
        (doc_id,),
    ).fetchone()
    if doc is None:
        raise HTTPException(404, "文档不存在")

    cap = max(1, min(int(limit), 500))
    off = max(0, int(offset))
    rows = conn.execute(
        "SELECT id, seq, text, token_count, page_no, heading_path, char_start, is_hidden"
        " FROM chunk WHERE doc_id=? AND generation=?"
        " ORDER BY seq LIMIT ? OFFSET ?",
        (doc_id, int(doc["active_gen"]), cap, off),
    ).fetchall()

    items = [
        {
            "id": r["id"],
            "seq": r["seq"],
            "text": r["text"],
            "chars": len(r["text"] or ""),
            "tokens": r["token_count"],
            "page_no": r["page_no"],
            "heading_path": r["heading_path"],
            "char_start": r["char_start"],
            "hidden": bool(r["is_hidden"]),
        }
        for r in rows
    ]
    total_chars = sum(it["chars"] for it in items)
    total = int(doc["n_chunks"] or 0)
    return {
        "doc": dict(doc),
        "chunks": items,
        "total": total,
        "shown": len(items),
        "offset": off,
        "loaded": off + len(items),
        "has_more": off + len(items) < total,
        "truncated": total > off + len(items),
        "chars": total_chars,
        "tokens": sum(int(it["tokens"] or 0) for it in items),
    }


@router.get("/api/doc/{doc_id}/coverage")
def doc_coverage(doc_id: int) -> dict:
    """完整性自检：源文件里的文字，有多少真的进了库。

    "文件是不是全进去了"这个问题不能靠感觉回答。做法是把源文件重新抽一遍
    拿到原文，再用滑动窗口检查每个窗口是否出现在当前代次的 chunk 拼接里，
    未命中的连续区间就是要给用户看的具体证据（而不只是一个百分比）。

    只比对空白归一化后的文本：换行/缩进差异不是内容丢失。
    """
    conn = state.conn
    if conn.execute("SELECT id FROM document WHERE id=?", (doc_id,)).fetchone() is None:
        raise HTTPException(404, "文档不存在")
    return _coverage_report(doc_id)


def _move_docs(doc_ids: list[int], kb_id: int) -> dict:
    """把文档整体挪到另一个知识库，索引一行都不用重建。

    chunk 挂在 doc_id 上、FTS 的 rowid 与 chunk.id 对齐、embedding 按
    content_hash 全局共用——文档换库不动这三样，引用编号和切分结果原样保留。
    唯一必须刷掉的是 VectorStore 里那份 (chunk → kb_id) 内存掩码：它是加载时
    的快照，不刷会让 dense 路继续按旧库过滤，表现成"文档明明在库里却搜不到"。
    """
    ids = sorted({int(i) for i in doc_ids if int(i) > 0})
    if not ids:
        raise HTTPException(400, "没有指定要移动的文档")
    conn = state.conn
    target = conn.execute("SELECT id, name FROM kb WHERE id=?", (kb_id,)).fetchone()
    if target is None:
        raise HTTPException(404, f"目标知识库 #{kb_id} 不存在")
    marks = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT id, kb_id, title, source_uri FROM document WHERE id IN ({marks})", ids
    ).fetchall()
    if len(rows) != len(ids):
        missing = sorted(set(ids) - {int(r["id"]) for r in rows})
        raise HTTPException(404, f"这些文档不存在：{missing}")
    # document 上有 UNIQUE(kb_id, source_uri)。目标库里已有同一个源文件时
    # 不能硬插，否则 SQLite 抛 IntegrityError，用户只看到一句 500。
    for r in rows:
        dup = conn.execute(
            "SELECT id FROM document WHERE kb_id=? AND source_uri=?",
            (kb_id, r["source_uri"]),
        ).fetchone()
        if dup and int(dup["id"]) != int(r["id"]):
            raise HTTPException(
                409, f"「{r['title']}」在「{target['name']}」里已有一份（文档 #{dup['id']}）")
    todo = [int(r["id"]) for r in rows if int(r["kb_id"]) != kb_id]
    if not todo:
        return {"moved": 0, "kb_id": kb_id, "kb_name": target["name"], "ids": []}
    try:
        conn.execute("BEGIN")
        conn.executemany(
            "UPDATE document SET kb_id=? WHERE id=?", [(kb_id, i) for i in todo])
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    state.vectors.invalidate()
    logbus.note(
        f"移动 {len(todo)} 篇文档到知识库「{target['name']}」#{kb_id}（索引未重建）",
        source="kb",
    )
    return {"moved": len(todo), "kb_id": kb_id, "kb_name": target["name"], "ids": todo}


@router.post("/api/docs/move")
def move_docs(req: DocMove) -> dict:
    """批量移动。给脚本/一次性整理用，UI 上逐篇点「移动」走下面那个。"""
    return _move_docs(req.doc_ids, req.kb_id)


@router.post("/api/doc/{doc_id}/move")
def move_doc(doc_id: int, req: DocMove) -> dict:
    return _move_docs([doc_id, *req.doc_ids], req.kb_id)


@router.delete("/api/doc/{doc_id}")
def delete_doc(doc_id: int) -> dict:
    """删除走**后台任务**：几千块的文档要逐条清 FTS + 级联删 chunk，前端
    不能干等一个不返回的请求。任务进度跟导入/重索引同一条链路。"""
    conn = state.conn  # 请求线程自己的连接，只用来查这一下行元信息
    row = conn.execute(
        "SELECT title, source_type, source_uri FROM document WHERE id=?", (doc_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(404, "文档不存在")
    label = row["title"] or f"doc#{doc_id}"

    def run(progress) -> dict:
        # state.conn 是 thread-local 的：任务跑在 worker 线程，必须在这里重新取，
        # 直接复用上面请求线程的连接会报 "created in a thread can only be used
        # in that same thread"。
        conn = state.conn
        ids = [r["id"] for r in conn.execute("SELECT id FROM chunk WHERE doc_id=?", (doc_id,))]
        total = len(ids)
        if total:
            # 分批清 FTS：一次 executemany 三千条没问题，但分批才能报出真进度
            B = 800
            for i in range(0, total, B):
                state.sparse.delete(conn, ids[i : i + B])
                done = min(i + B, total)
                progress("清理索引", 0.05 + 0.65 * done / total, f"{done}/{total} 块")
        progress("删除文档", 0.8)
        conn.execute("DELETE FROM document WHERE id=?", (doc_id,))  # chunk 级联删除
        state.vectors.invalidate()
        # 上传副本要跟着走：留着就是纯垃圾（没有 document 行指向它，重索引也用不上）。
        # 只删 uploads 里的，外部路径（手工导入的源文件）绝对不能碰。
        removed_copy = False
        if row["source_type"] == "file" and row["source_uri"]:
            p = paths_mod.resolve_source(row["source_uri"])
            if _inside(p, UPLOAD_DIR):
                removed_copy = _quiet_unlink(p)
        progress("完成", 1.0)
        return {
            "status": "ready",
            "message": f"已删除{f' {total} 块' if total else ''}" + ("，源文件副本已清理" if removed_copy else ""),
            "deleted": doc_id,
        }

    return _start_import_task(f"删除 {label}", run)


@router.get("/api/chunks")
def get_chunks(ids: str) -> list[dict]:
    """批量取原文。检索面板要同时展示稠密/稀疏/融合三组结果，逐条请求是 N 次往返，
    而这里一次就够（面板一屏最多几十条）。"""
    parsed: list[int] = []
    for part in ids.split(","):
        part = part.strip()
        if part.isdigit():
            parsed.append(int(part))
    if not parsed:
        return []
    parsed = parsed[:80]
    placeholders = ",".join("?" for _ in parsed)
    rows = state.conn.execute(
        f"SELECT c.id, c.doc_id, c.seq, c.text, c.page_no, c.heading_path, c.token_count,"
        f" d.title AS doc_title, d.source_uri"
        f" FROM chunk c JOIN document d ON d.id=c.doc_id WHERE c.id IN ({placeholders})",
        parsed,
    ).fetchall()
    return [
        {
            "id": r["id"],
            "doc_id": r["doc_id"],
            "doc_title": r["doc_title"],
            "source_uri": r["source_uri"],
            "seq": r["seq"],
            "text": r["text"],
            "chars": len(r["text"] or ""),
            "tokens": r["token_count"],
            "page_no": r["page_no"],
            "heading_path": r["heading_path"],
        }
        for r in rows
    ]


@router.get("/api/chunk/{chunk_id}")
def get_chunk(chunk_id: int) -> dict:
    row = state.conn.execute(
        """
        SELECT c.id, c.doc_id, c.seq, c.text, c.page_no, c.heading_path, c.token_count,
               c.is_hidden, d.title, d.source_uri
        FROM chunk c JOIN document d ON d.id=c.doc_id WHERE c.id=?
        """,
        (chunk_id,),
    ).fetchone()
    if not row:
        raise HTTPException(404, "chunk 不存在（可能已因重新索引被替换）")
    return dict(row)


# ---------------------------------------------------------------- chunk 编辑
#
# 手动编辑 chunk 是"检索质量最后一道人工阀门"：自动分块一定会切坏一些东西
# （表格被切断、标题与正文分家、代码块被拦腰截断）。与其逼用户去调参数再整体
# 重建，不如允许就地改一条——改完只重算这一条的 FTS 与向量，代价是毫秒级。


def _reindex_chunk(conn, chunk_id: int, text: str) -> None:
    """改一条 chunk 的文本，并同步 text_seg / content_hash / 嵌入 / FTS。"""
    import hashlib as _hashlib

    from ...ingest.chunker import estimate_tokens

    text = text.strip()
    if not text:
        raise HTTPException(400, "chunk 内容不能为空")

    digest = _hashlib.sha256(text.encode("utf-8")).hexdigest()
    seg = state.sparse.segment(text)
    conn.execute(
        "UPDATE chunk SET text=?, text_seg=?, token_count=?, content_hash=?, is_hidden=0"
        " WHERE id=?",
        (text, seg, estimate_tokens(text), digest, chunk_id),
    )
    state.sparse.delete(conn, [chunk_id])
    state.sparse.index(conn, [(chunk_id, text)])

    model_key = f"{state.cfg.embedding.model}@{state.cfg.embedding.dim}"
    has = conn.execute(
        "SELECT 1 FROM embedding WHERE content_hash=? AND model=?", (digest, model_key)
    ).fetchone()
    if not has and state.embedder.available:
        import numpy as np

        vec = state.embedder.encode([text])[0]
        conn.execute(
            "INSERT OR REPLACE INTO embedding (content_hash, model, dim, vec) VALUES (?,?,?,?)",
            (digest, model_key, int(state.cfg.embedding.dim),
             np.asarray(vec, dtype=np.float32).tobytes()),
        )
    state.vectors.invalidate()


def _active_chunk(conn, chunk_id: int):
    row = conn.execute(
        "SELECT c.*, d.active_gen FROM chunk c JOIN document d ON d.id=c.doc_id WHERE c.id=?",
        (chunk_id,),
    ).fetchone()
    if not row:
        raise HTTPException(404, "chunk 不存在")
    if row["generation"] != row["active_gen"]:
        raise HTTPException(409, "该 chunk 属于旧代次，已被重新索引替换，不能编辑")
    return row


def _renumber(conn, doc_id: int, generation: int, order: list[int] | None = None) -> None:
    """按给定顺序重排 seq。

    UNIQUE(doc_id, generation, seq) 意味着边改边写一定会撞，所以先整体写负数
    占位、再写正数落位——两步都不会出现重复值。
    """
    if order is None:
        order = [
            int(r["id"])
            for r in conn.execute(
                "SELECT id FROM chunk WHERE doc_id=? AND generation=? ORDER BY seq, id",
                (doc_id, generation),
            ).fetchall()
        ]
    # 占位值必须远离任何可能存在的 seq（新插入的块用 -1 占位），否则第一轮写入
    # 就会自己跟自己撞唯一约束
    base = -1_000_000
    conn.executemany(
        "UPDATE chunk SET seq=? WHERE id=?",
        [(base - i, cid) for i, cid in enumerate(order)],
    )
    conn.executemany("UPDATE chunk SET seq=? WHERE id=?", [(i, cid) for i, cid in enumerate(order)])
    visible = conn.execute(
        "SELECT count(*) c FROM chunk WHERE doc_id=? AND generation=? AND is_hidden=0",
        (doc_id, generation),
    ).fetchone()["c"]
    conn.execute(
        "UPDATE document SET n_chunks=?, updated_at=? WHERE id=?",
        (int(visible), _utcnow(), doc_id),
    )


class ChunkPatch(BaseModel):
    text: str = Field(min_length=1, max_length=20000)


@router.patch("/api/chunk/{chunk_id}")
def patch_chunk(chunk_id: int, req: ChunkPatch) -> dict:
    conn = state.conn
    _active_chunk(conn, chunk_id)
    _reindex_chunk(conn, chunk_id, req.text)
    return {"ok": True, "chunk_id": chunk_id, "chars": len(req.text.strip())}


class ChunkSplit(BaseModel):
    offset: int | None = Field(default=None, ge=1)


@router.post("/api/chunk/{chunk_id}/split")
def split_chunk(chunk_id: int, req: ChunkSplit) -> dict:
    conn = state.conn
    row = _active_chunk(conn, chunk_id)
    text = row["text"]
    offset = req.offset or len(text) // 2
    if not (1 <= offset < len(text)):
        raise HTTPException(400, f"切分位置超出范围（1..{len(text)}）")

    left, right = text[:offset].rstrip(), text[offset:].lstrip()
    if not left or not right:
        raise HTTPException(400, "切分后有一半为空，换个位置")

    _reindex_chunk(conn, chunk_id, left)
    # 先给新块一个临时的负 seq（避开唯一约束），落库后再按顺序整体重排
    cur = conn.execute(
        "INSERT INTO chunk (doc_id, generation, seq, text, text_seg, token_count,"
        " page_no, heading_path, char_start, char_end, content_hash)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (row["doc_id"], row["generation"], -1, right, "", 0,
         row["page_no"], row["heading_path"], None, None, ""),
    )
    new_id = int(cur.lastrowid)
    _reindex_chunk(conn, new_id, right)

    order = [
        int(r["id"])
        for r in conn.execute(
            "SELECT id FROM chunk WHERE doc_id=? AND generation=? AND id<>? ORDER BY seq, id",
            (row["doc_id"], row["generation"], new_id),
        ).fetchall()
    ]
    order.insert(order.index(chunk_id) + 1, new_id)
    _renumber(conn, row["doc_id"], row["generation"], order)
    return {"ok": True, "left_id": chunk_id, "right_id": new_id}


class ChunkMerge(BaseModel):
    ids: list[int] = Field(min_length=2, max_length=20)


@router.post("/api/chunk/merge")
def merge_chunks(req: ChunkMerge) -> dict:
    conn = state.conn
    rows = [_active_chunk(conn, cid) for cid in req.ids]
    doc_ids = {int(r["doc_id"]) for r in rows}
    gens = {int(r["generation"]) for r in rows}
    if len(doc_ids) > 1 or len(gens) > 1:
        raise HTTPException(400, "只能合并同一文档、同一代次的 chunk")

    rows.sort(key=lambda r: (r["seq"], r["id"]))
    merged = "\n\n".join(r["text"].strip() for r in rows)
    keep = int(rows[0]["id"])
    _reindex_chunk(conn, keep, merged)

    drop = [int(r["id"]) for r in rows[1:]]
    state.sparse.delete(conn, drop)
    conn.execute(
        f"UPDATE chunk SET is_hidden=1 WHERE id IN ({','.join('?' for _ in drop)})", drop
    )
    _renumber(conn, rows[0]["doc_id"], rows[0]["generation"])
    return {"ok": True, "chunk_id": keep, "merged": len(rows)}


@router.delete("/api/chunk/{chunk_id}")
def delete_chunk(chunk_id: int) -> dict:
    """软删除：置 is_hidden=1 并从 FTS 摘掉。

    硬删会让同一代次里的 seq 出现空洞，而 seq 是"引用位置"的锚点；
    软删除同时保留回滚可能——用户手滑删错了，改数据库就能救回来。
    """
    conn = state.conn
    row = _active_chunk(conn, chunk_id)
    conn.execute("UPDATE chunk SET is_hidden=1 WHERE id=?", (chunk_id,))
    state.sparse.delete(conn, [chunk_id])
    _renumber(conn, row["doc_id"], row["generation"])
    state.vectors.invalidate()
    return {"ok": True, "chunk_id": chunk_id, "hidden": True}