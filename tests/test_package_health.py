"""安装健康闸门：不注入 sys.path，验证包真的装进了当前解释器。

其它测试文件为了跑得快会自己把 ``src/`` 插进 ``sys.path``，这会把「editable
安装掉了」完全遮住：包没装、venv 里只剩一个孤儿 ``knowtune.exe``，pytest 照样
全绿，但 ``knowtune serve`` 直接 ModuleNotFoundError。所以这里必须用独立进程，
并且不能继承 pytest 进程的 sys.path。

同时守住配置可移植性：models.yaml 的模型 path 一旦写回盘符绝对路径，clone 到
别处就是死链，还会让界面在 local 端点下启动时回写 base_url 污染预问诊。
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _clean(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, *args], cwd=ROOT,
        capture_output=True, text=True, timeout=60,
    )


def test_editable_install_is_importable():
    r = _clean("-c", "import knowtune, knowtune.cli; print(knowtune.__file__)")
    assert r.returncode == 0, f"干净解释器无法 import knowtune：{r.stderr.strip()}"
    assert "knowtune" in r.stdout


def test_cli_module_runs():
    r = _clean("-m", "knowtune.cli", "--help")
    assert r.returncode == 0, f"CLI 入口不可用：{r.stderr.strip()}"
    assert "serve" in r.stdout


def test_models_yaml_paths_are_portable():
    for p in (ROOT / "config").glob("*.yaml"):
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            assert not re.search(r"^\s*path:\s*[A-Za-z]:[\\/]", line), (
                f"{p.name}:{i} 写了盘符绝对路径，换机器即失效：{line.strip()}"
            )
