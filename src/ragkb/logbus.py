"""内存环形日志 —— 首页"系统日志"的数据源。

为什么不用文件：个人单机、单用户，用户想看的不是 500MB 的 access.log，而是
"刚才那次导入为什么失败"。所以只保留最近 400 条，进程重启即清空。真要看历史
检索过程，query_log 表里存着每次请求的完整快照（候选、耗时、拒答判定），
那才是需要长期留存的证据，而这个环形缓冲区是"现场"。
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from datetime import datetime
from typing import Any

LEVEL_ORDER = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}


def _now() -> str:
    return datetime.now().strftime("%H:%M:%S")


class RingBuffer(logging.Handler):
    def __init__(self, capacity: int = 400) -> None:
        super().__init__()
        self._items: deque[dict[str, Any]] = deque(maxlen=capacity)
        self._lock = threading.Lock()
        self._seq = 0

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D102
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - 格式化失败也要留一条痕迹
            message = str(record.msg)
        if record.exc_info:
            message = f"{message} | {record.exc_info[0].__name__ if record.exc_info[0] else ''}"
        self.note(
            message,
            level=record.levelname,
            source=record.name,
            ts=time.time(),
        )

    def note(
        self,
        message: str,
        *,
        level: str = "INFO",
        source: str = "app",
        ts: float | None = None,
    ) -> None:
        """手工写一条。source 用短名（app / ingest / models / settings），
        它决定日志表左侧那个标签，比一堆 "uvicorn.error" 有用得多。"""
        level = (level or "INFO").upper()
        with self._lock:
            self._seq += 1
            seq = self._seq
            self._items.appendleft({
                "seq": seq,
                "at": _now() if ts is None else datetime.fromtimestamp(ts).strftime("%H:%M:%S"),
                "ts": ts if ts is not None else time.time(),
                "level": level,
                "source": source,
                "message": message[:600],
            })

    def entries(
        self, limit: int = 100, level: str | None = None, source: str | None = None
    ) -> list[dict[str, Any]]:
        floor = LEVEL_ORDER.get((level or "").upper(), 0)
        with self._lock:
            items = list(self._items)
        out = [
            i for i in items
            if LEVEL_ORDER.get(i["level"], 0) >= floor
            and (not source or i["source"] == source)
        ]
        return out[: max(1, min(limit, 400))]

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


BUS = RingBuffer()

_installed = False


def install() -> None:
    """挂到 root 与 uvicorn 两个 logger 上。

    只挂 root 会漏掉 uvicorn 的访问日志——uvicorn 配置日志时把 propagate 关了，
    而启动失败、端口占用恰恰都是从那两个 logger 报出来的，正是最需要看的东西。
    """
    global _installed
    if _installed:
        return
    BUS.setLevel(logging.DEBUG)
    for name in ("", "uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name) if name else logging.getLogger()
        if any(isinstance(h, RingBuffer) for h in logger.handlers):
            continue
        logger.addHandler(BUS)
    _installed = True


def note(message: str, *, level: str = "INFO", source: str = "app") -> None:
    BUS.note(message, level=level, source=source)


def entries(limit: int = 100, level: str | None = None) -> list[dict[str, Any]]:
    return BUS.entries(limit=limit, level=level)
