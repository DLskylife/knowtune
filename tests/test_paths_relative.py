"""路径口径闸门：库里只存项目根相对路径，解析不依赖进程工作目录。

2026-11-08 之前 `document.source_uri` 存的是本机绝对路径。项目目录从
`sky_agent` 改名成 `knowtune`，32 条记录一夜之间全变死链，重索引、清洗预览、
删除副本整条链路跟着坏。这个文件守的就是三件事：写相对、读按项目根、
项目根之外的文件保留绝对路径兜底。
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from knowtune import paths  # noqa: E402
from knowtune.ingest.pipeline import IngestPipeline  # noqa: E402


def test_stored_path_is_relative_to_project_root():
    target = ROOT / "data" / "uploads" / "weiyi" / "a.pdf"
    assert paths.stored_path(target) == "data/uploads/weiyi/a.pdf"


def test_resolve_source_ignores_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert paths.resolve_source("pyproject.toml") == ROOT / "pyproject.toml"
    assert paths.resolve_source("pyproject.toml").exists()


def test_source_missing_with_relative_uri(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert paths.source_missing("file", "pyproject.toml") is False
    assert paths.source_missing("file", "__no_such_file__.pdf") is True
    # URL / 粘贴文本没有本地副本，本来就不该判缺失
    assert paths.source_missing("url", "https://example.com/a") is False
    assert paths.source_missing("text", "text://a/b") is False


def test_external_absolute_path_is_kept(tmp_path):
    outside = tmp_path / "outside.gguf"
    outside.write_bytes(b"x")
    stored = paths.stored_path(outside)
    assert Path(stored).is_absolute()
    assert paths.resolve_source(stored).exists()


def test_relative_path_with_parent_is_not_rewritten_absolute():
    """界面保存模型配置时不许把 `../../...` 算回绝对路径。"""
    rel = "../../llamafactory/models/x.gguf"
    assert paths.stored_path(rel) == rel
    assert not Path(paths.stored_path(rel)).is_absolute()


def test_pipeline_writes_relative_source_uri():
    """新导入的文档不许再往库里写绝对路径。"""
    pipeline = object.__new__(IngestPipeline)  # 绕开 DB / 嵌入器装配
    captured: dict = {}

    def fake_import(**kw):
        captured.update(kw)
        return Mock()

    pipeline._import = fake_import
    pipeline.import_path(1, "pyproject.toml")
    assert captured["source_type"] == "file"
    assert captured["source_uri"] == "pyproject.toml"
