"""跨路由共享的常量、工具与导入任务队列。

把 upload/分片上传、导入后台任务队列这些「多块路由都要用」的东西放这里，
是为了让拆到 `routes/` 里的各模块只朝这一个方向依赖：路由模块 import 这里，
这里不 import 任何路由模块（`_coverage_digest` 那处延迟 import 也只在运行时、
且 app 早已加载之后才发生），从而彻底避免循环 import。
"""

from __future__ import annotations

import json
import os
import queue as _queue
import re
import threading
import time
import uuid
from pathlib import Path

from fastapi import HTTPException, UploadFile

from .. import logbus
from .. import paths as paths_mod
from ..db import utcnow as _utcnow

STATIC_DIR = Path(__file__).resolve().parent / "static"
UPLOAD_DIR = Path(__file__).resolve().parents[3] / "data" / "uploads"


def _source_missing(source_type: str, source_uri: str) -> bool:
    """文件来源的原始文件是否还在（判定逻辑见 ..paths）。"""
    return paths_mod.source_missing(source_type, source_uri)


def _stored_plan(raw: str | None) -> dict | None:
    """把库里存成 JSON 的清洗计划安全地还原成 dict（空/坏数据返回 None）。"""
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def _inside(path: Path, root: Path) -> bool:
    """path 是否落在 root 之内（防 `../` 之类写到别处）。"""
    return paths_mod.inside(path, root)


_UPLOAD_CHUNK = 1 << 20  # 1MiB，够大不碎、够小不吃内存


async def _save_upload(file: UploadFile, target: Path, limit_mb: int) -> int:
    """把上传边读边写，返回字节数。

    不用 `await file.read()` 一次吃进内存：那样 `max_file_mb` 拦的是"读完
    之后的拒绝"，不是"不读超过限制的部分"。一个 5GB 的请求进来，进程会先
    把 5GB 装进内存再回 413——限制形同虚设，而且这种请求本身就能把机器打趴。

    两道闸（任一命中就拒）：
      1. `file.size`（starlette 从 Content-Length 或已缓冲部分算出来的）；
      2. 逐块累加计数。
    第二道是必须的：分块传输（chunked）没有 Content-Length，`size` 为 None，
    只靠第一道等于对这类请求完全不设防。

    超限时**删掉半截文件**再抛错：uploads 里留一个残缺副本比不留更糟，后续
    "补原始文件 / 重索引"会把它当成有效来源，导进来一堆截断的内容。

    写的是 `*.part` 临时文件、成功后才 `os.replace` 成目标名。这不只是洁癖：
    "补原始文件"会覆盖已有的落盘副本，如果直接往目标文件写、写到一半断了，
    旧副本就没了，而这个文档的重索引全靠它——一次失败的上传顺手毁掉一份
    本来完好的原始文件，是最难排查的那类损坏。
    """
    limit = max(1, int(limit_mb)) * 1024 * 1024
    declared = getattr(file, "size", None)
    if declared is not None and int(declared) > limit:
        raise HTTPException(413, f"文件超过 {limit_mb}MB")

    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".part")
    written = 0
    try:
        with tmp.open("wb") as fh:
            while True:
                block = await file.read(_UPLOAD_CHUNK)
                if not block:
                    break
                written += len(block)
                if written > limit:
                    raise HTTPException(413, f"文件超过 {limit_mb}MB")
                fh.write(block)
        os.replace(tmp, target)
    except BaseException:
        # HTTPException、客户端中途断开、磁盘满都走这里：临时文件一律不留，
        # target 保持原样（可能是上一次成功上传的那份）
        _quiet_unlink(tmp)
        raise
    return written


# --------------------------------------------------------------- 分片上传
#
# 单请求上传的天花板不在服务端逻辑，而在链路上：浏览器对长时间无进展的请求
# 会判超时，反向代理有 request body 上限，HTTP/1.1 还得整个 body 传完才开始
# 处理。几十 MB 就开始不稳，几百 MB 基本必挂——这时 `max_file_mb` 设多大都
# 没意义，因为根本传不到判大小的那一步。
#
# 分片把"一次大传输"拆成"多次小传输"，换来三件事：
#   ① 失败代价从"整个文件重来"降到"重传一片"；
#   ② 单片小到不会触发中间层的体积/超时限制；
#   ③ 进度是真实字节累计，不是猜的。
#
# upload_id 由前端的 fingerprint（名字+大小+修改时间）决定，所以浏览器刷新、
# 甚至明天再来传同一个文件，已收的分片还在，能接着传。这不是花活——中断在
# 大文件上传里是常态，不能续传的分片上传只是把一次失败拆成十次失败。
_PARTS_DIR = UPLOAD_DIR / ".parts"
_CHUNK_SIZE = 4 * 1024 * 1024     # 4MiB：请求数不至于太多，重传又不至于太贵
_PART_TTL_H = 24                  # 未完成的分片留一天；再久就是垃圾
_UID_RE = re.compile(r"^[0-9a-f]{16,64}$")


def _upload_dir(upload_id: str) -> Path:
    """分片目录。id 必须先过白名单——它被拼进路径，不校验等于任意目录读写。"""
    if not _UID_RE.match(upload_id or ""):
        raise HTTPException(400, "上传标识非法")
    return _PARTS_DIR / upload_id


def _quiet_unlink(p: Path) -> bool:
    """删单个文件，删不掉就认了。

    两条原则。一，删除是收尾动作：文件已经合并落地、请求已经成功，这时删不掉
    一块临时文件只该留点垃圾，不该把成功改判成 500。二，除了 OSError 还要接
    SystemExit——宿主环境（沙箱、审计钩子、企业管控）会在删除时直接抛它硬拦，
    它不属于 Exception，`ignore_errors=True` 也盖不住，真见过把整个服务带走。
    """
    try:
        p.unlink(missing_ok=True)
    except (OSError, SystemExit):
        return False
    return True


def _discard_parts(folder: Path) -> bool:
    """丢掉一整个分片目录，返回是否真的清掉了。

    逐个删，不用 shutil.rmtree：rmtree 会先把整棵树数一遍再整体交给回收站，
    几十片就直接撞上批量删除守卫；逐个删每个目标只有一个文件，既不会误伤，
    失败也只损失一个分片。而且任何失败都不外抛（见 _quiet_unlink）。
    """
    try:
        if not folder.exists():
            return True
        for p in folder.iterdir():
            _quiet_unlink(p)
        folder.rmdir()
    except (OSError, SystemExit):
        return False
    return not folder.exists()


def _read_meta(folder: Path) -> dict | None:
    try:
        return json.loads((folder / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_meta(folder: Path, meta: dict) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False), encoding="utf-8"
    )


def _received_parts(folder: Path) -> list[int]:
    """已收的分片序号。按文件名取，不信任 meta 里记的那份——meta 是缓存，
    磁盘才是事实；两者不一致时以磁盘为准，否则续传会漏掉一片而 finish 才报错。"""
    got: list[int] = []
    try:
        for p in folder.iterdir():
            if p.suffix == ".part" and p.stem.isdigit():
                got.append(int(p.stem))
    except OSError:
        return []
    return sorted(got)


# --------------------------------------------------------------------- 导入任务
#
# 导入为什么变成"后台任务 + 轮询"：
# 上传进度条走到 100% 只说明字节传完了，真正的耗时在解析 / 切分 / 向量化
# （本机嵌入约 21 块/秒）。原来这段时间只有一个转圈的斜纹，用户没法判断
# 是"在跑"还是"死了"。改成任务制之后，前端能拿到 stage + pct + "128/300 块"
# 这种具体数字；顺带把重索引、补回源文件也统一到同一条链路。

_IMPORT_TASKS: dict[str, dict] = {}
# 用**可重入**锁：`import_tasks` 在持锁的情况下逐个调 `_task_public`，而后者
# 也要看一眼任务表来算排队位置。普通 Lock 会在那里把自己锁死（实测：请求
# 直接挂住不返回）。同一模块内的组合调用，重入是这里真正想要的语义。
_TASK_LOCK = threading.RLock()
_TASK_KEEP = 40
# 任务**串行**执行：解析/切分/向量化会同时写 SQLite、同时占用嵌入模型，
# 并行跑两个不只是进度条互相打架（两个任务往同一张卡上写百分比），还容易
# 把嵌入模型挤爆。排队执行总耗时几乎一样，但每个任务的进度是真的。
_TASK_Q: _queue.Queue[str] = _queue.Queue()
_TASK_SEQ = 0


def _task_worker() -> None:
    """单线程消费任务队列。任务里抛异常由 work() 自己兜住，这里只管状态切换。"""
    while True:
        tid = _TASK_Q.get()
        with _TASK_LOCK:
            task = _IMPORT_TASKS.get(tid)
            if task is not None:
                task["state"] = "running"
                task["stage"] = "启动"
        if task is None:
            continue
        try:
            task["_work"]()
        finally:
            _TASK_Q.task_done()


threading.Thread(target=_task_worker, daemon=True, name="import-worker").start()


def _task_public(task: dict) -> dict:
    """去掉下划线开头的内部字段（_work 是个函数，不能进 JSON），并补排队位置。"""
    with _TASK_LOCK:
        seq = task.get("_seq", 0)
        ahead = 0
        if task["state"] == "queued":
            ahead = sum(
                1
                for t in _IMPORT_TASKS.values()
                if t.get("_seq", 0) < seq and t["state"] in ("queued", "running")
            )
    out = {k: v for k, v in task.items() if not k.startswith("_")}
    out["queue_pos"] = ahead
    return out


def _start_import_task(label: str, fn) -> dict:
    """fn(progress) -> dict（IngestResult.as_dict() 的形状）。

    返回里带 `queue_pos`：前面还有几个任务。不给这个数的话，用户连点三个
    重索引只会看到两个纹丝不动的 0%，像是卡死了。
    """
    global _TASK_SEQ
    tid = uuid.uuid4().hex[:10]
    with _TASK_LOCK:
        _TASK_SEQ += 1
        seq = _TASK_SEQ
    task: dict = {
        "task_id": tid,
        "label": label,
        "state": "queued",
        "stage": "排队",
        "pct": 0.0,
        "note": "",
        "message": None,
        "result": None,
        "started_at": _utcnow(),
        "took_ms": None,
        "_seq": seq,
    }
    with _TASK_LOCK:
        _IMPORT_TASKS[tid] = task
        while len(_IMPORT_TASKS) > _TASK_KEEP:
            _IMPORT_TASKS.pop(next(iter(_IMPORT_TASKS)))

    def progress(stage: str, pct: float, note: str = "") -> None:
        task["stage"] = stage
        task["pct"] = round(float(pct), 4)
        task["note"] = note or ""

    def work() -> None:
        started = time.perf_counter()
        try:
            result = fn(progress)
            # 索引刚建完，源文件还在手边 —— 这时候顺手做一次完整性自检最便宜：
            # 之后文件可能被挪走/改掉，就再也比不了了。结论直接挂在任务结果里，
            # 用户不用去预览弹窗里翻「完整性自检」那个按钮才知道导全了没有。
            if isinstance(result, dict) and result.get("status") == "ready" and result.get("doc_id"):
                # 延迟 import 只为避开 common -> app 的环：此刻 app 早已随路由
                # 装配完毕，拿到的就是已加载的那份模块。
                from .coverage import _coverage_digest

                digest = _coverage_digest(int(result["doc_id"]))
                if digest:
                    result["coverage"] = digest
                    if digest["big_gap_count"]:
                        logbus.note(
                            f"导入自检：doc#{result['doc_id']} 成段缺失 {digest['big_gap_count']} 处"
                            f"（最大 {digest['max_gap']} 字）",
                            level="WARN", source="ingest",
                        )
            task["result"] = result
            task["state"] = "ready" if result.get("status") == "ready" else "failed"
            task["message"] = result.get("message")
            task["stage"] = "完成" if task["state"] == "ready" else "失败"
            task["pct"] = 1.0 if task["state"] == "ready" else task["pct"]
        except Exception as exc:  # 任务线程里抛什么都得落到任务上，否则前端永远等
            task["state"] = "failed"
            task["message"] = f"{type(exc).__name__}: {exc}"
            logbus.note(f"导入任务失败：{task['message']}", level="ERROR", source="ingest")
        finally:
            task["took_ms"] = int((time.perf_counter() - started) * 1000)

    task["_work"] = work
    _TASK_Q.put(tid)
    return _task_public(task)