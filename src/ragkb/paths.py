"""路径判定小工具。

单独成模块是为了可测：`app.py` 一 import 就会建 AppState、开库、启预热线程，
把两行纯函数塞在那里等于让测试去背整个应用的启动成本。
"""

from __future__ import annotations

from pathlib import Path


def source_missing(source_type: str, source_uri: str) -> bool:
    """文件来源的原始文件是否还在。

    只判断 `file`：URL 与粘贴文本没有本地副本，谈不上"缺失"。
    """
    if source_type != "file":
        return False
    try:
        return not Path(source_uri).exists()
    except (OSError, ValueError):
        return True


def inside(path: Path, root: Path) -> bool:
    """path 是否落在 root 之内（挡住 `../` 写到上传目录外面）。"""
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except (ValueError, OSError):
        return False
