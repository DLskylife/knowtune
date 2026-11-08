"""路径判定与归一化小工具。

单独成模块是为了可测：`app.py` 一 import 就会建 AppState、开库、启预热线程，
把纯函数塞在那里等于让测试去背整个应用的启动成本。

**落库路径一律是「项目根相对路径」**（POSIX 分隔符），不是绝对路径。理由很实在：
项目目录一改名/换机器/换盘符，绝对路径会集体失效，数据库里 32 条 `source_uri`
瞬间全变死链，重索引、预览、删除全部跟着坏。相对路径以这里算出的 `ROOT` 为基准，
不管从哪个工作目录启动服务都解析到同一个地方。项目根之外的文件无法可移植，
保留绝对路径（`stored_path` 的兜底分支）。
"""

from __future__ import annotations

from pathlib import Path

# src/knowtune/paths.py → parents[2] 就是项目根（与 config.ROOT 同一个位置）。
# 这里不 import config，避免为了一个常量拖进 pydantic 与整套配置加载。
ROOT = Path(__file__).resolve().parents[2]


def resolve_source(uri: str | Path) -> Path:
    """把库里存的 source_uri / 配置里的路径还原成可打开的 Path。

    相对路径按项目根解析；绝对路径（含项目根之外的外部文件）原样返回。
    """
    p = Path(uri)
    return p if p.is_absolute() else ROOT / p


def stored_path(path: str | Path) -> str:
    """落库/展示用的路径：项目根内写相对路径，根外保留绝对路径。

    统一用 `/` 分隔，Windows 与 Linux 都能直接喂给 Path，数据库里也不会出现
    两套转义风格。已经是相对路径的输入（含 `../` 这种指向项目外的写法）原样
    保留——先 resolve 再 relative_to 会把它算回绝对路径，界面保存一次就把
    盘符又写回配置里了。
    """
    raw = Path(path)
    if not raw.is_absolute():
        return raw.as_posix()
    p = raw.resolve()
    try:
        return p.relative_to(ROOT).as_posix()
    except ValueError:
        return str(p)


def source_missing(source_type: str, source_uri: str) -> bool:
    """文件来源的原始文件是否还在。

    只判断 `file`：URL 与粘贴文本没有本地副本，谈不上"缺失"。
    """
    if source_type != "file":
        return False
    try:
        return not resolve_source(source_uri).exists()
    except (OSError, ValueError):
        return True


def inside(path: Path, root: Path) -> bool:
    """path 是否落在 root 之内（挡住 `../` 写到上传目录外面）。"""
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except (ValueError, OSError):
        return False
