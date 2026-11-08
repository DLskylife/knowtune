#!/usr/bin/env python
"""KnowTune 一键部署：clone 之后跑这一条就够了。

    python scripts/setup.py            # 建环境、装依赖、初始化空库、检查模型
    python scripts/setup.py --start    # 上面全部 + 直接起服务（Ctrl+C 停止）
    python scripts/setup.py --gpu      # 已有 CUDA 版 torch，跳过 CPU 专用安装

设计约束：
1. 只用标准库 —— 建 venv 之前本机可能什么都没装。
2. 幂等 —— 已有 venv / 依赖 / 数据库就跳过，重复跑等价于体检。
3. 不替你下载 1.2 GB 模型权重，只检查并打印两条下载命令。
4. 不碰密钥 —— 密钥在后台「模型」页填，或走 .env（见 .env.example）。

安装顺序不能反：local extra 里的 sentence-transformers 依赖 torch，
先装本体会拉默认 CUDA 版（体积约 10 倍），再装 CPU 版只是白下几个 GB。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VENV = ROOT / ".venv"
IS_WIN = os.name == "nt"
BIN = VENV / ("Scripts" if IS_WIN else "bin")
PY = BIN / ("python.exe" if IS_WIN else "python")
DB = ROOT / "data" / "library.db"

MODELS = (
    ("嵌入", ROOT / "data/models/bge-small-zh-v1.5", "BAAI/bge-small-zh-v1.5", "约 92 MB"),
    ("精排", ROOT / "data/models/BAAI--bge-reranker-base", "BAAI/bge-reranker-base", "约 1.1 GB"),
)


def step(n: int, total: int, text: str) -> None:
    print(f"\n[{n}/{total}] {text}", flush=True)


def run(cmd: list[str]) -> None:
    print("  $", " ".join(cmd), flush=True)
    subprocess.run(cmd, cwd=ROOT, check=True)


def can_import(module: str) -> bool:
    return subprocess.run(
        [str(PY), "-c", f"import {module}"], cwd=ROOT,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    ).returncode == 0


def has_weights(path: Path) -> bool:
    return path.is_dir() and (
        any(path.glob("*.safetensors")) or (path / "pytorch_model.bin").exists()
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="KnowTune 一键部署")
    ap.add_argument("--start", action="store_true", help="装好后直接起服务（前台阻塞）")
    ap.add_argument("--gpu", action="store_true", help="已有 CUDA 版 torch，跳过 CPU 专用安装")
    args = ap.parse_args()

    if sys.version_info < (3, 11):
        print(f"需要 Python >= 3.11，当前是 {sys.version.split()[0]}。")
        return 2

    total = 5
    print(f"KnowTune 部署向导  |  项目根目录：{ROOT}")

    # 1. venv
    step(1, total, "虚拟环境 .venv/")
    if PY.exists():
        print("  已存在，跳过。")
    else:
        run([sys.executable, "-m", "venv", str(VENV)])
        print("  已创建。")

    # 2. CPU torch（顺序关键）
    step(2, total, "CPU 版 torch（必须先于本体）")
    if can_import("torch"):
        print("  已安装，跳过。")
    elif args.gpu:
        print("  --gpu：跳过 CPU 专用安装，请自行确保 CUDA 版 torch 可用。")
    else:
        run([str(PY), "-m", "pip", "install", "torch",
             "--index-url", "https://download.pytorch.org/whl/cpu"])

    # 3. 本体 + 抽取 + 本地模型依赖
    step(3, total, "安装 knowtune 本体（extract + local）")
    if can_import("knowtune") and can_import("sentence_transformers"):
        print("  已安装，跳过。")
    else:
        run([str(PY), "-m", "pip", "install", "-e", ".[extract,local]"])

    # 4. 初始化空库
    step(4, total, "初始化数据库")
    if DB.exists():
        print(f"  {DB.relative_to(ROOT).as_posix()} 已存在，跳过（不会覆盖你的数据）。")
    else:
        run([str(PY), "-m", "knowtune.cli", "init"])

    # 5. 模型检查（只提示，不下载）
    step(5, total, "检查本地模型")
    missing = [(label, rel, repo, size) for label, rel, repo, size in MODELS
               if not has_weights(rel)]
    if not missing:
        print("  两个模型权重都在，检索与精排可全速跑。")
    else:
        print("  以下模型尚未就位（脚本不自动下载，避免拉 1.2 GB）：")
        for label, rel, repo, size in missing:
            print(f"    - {label}：{rel.relative_to(ROOT).as_posix()}  （{size}）")
        print("\n  国内网络先设镜像：export HF_ENDPOINT=https://hf-mirror.com")
        print("  然后执行（pip install -U huggingface_hub 装下载工具）：")
        for _, rel, repo, _ in missing:
            print(f"    hf download {repo} --local-dir {rel.relative_to(ROOT).as_posix()}")
        print("  详见 data/models/README.md。")

    # 收尾
    py = PY.relative_to(ROOT).as_posix()
    print("\n" + "=" * 62)
    print("环境就绪。接下来：")
    print("  1. 起服务： " + f"{py} -m knowtune.cli serve --port 8013")
    print("     （或激活 venv 后直接 knowtune serve --port 8013）")
    print("  2. 浏览器开 http://127.0.0.1:8013/ ，在「模型」页填后端密钥；")
    print("     前台问答站 http://127.0.0.1:8800/ 随主服务自动起。")
    print("  3. 首次启动嵌入模型预热约 43-93 秒，看 /api/status 的 warmup.state=ready 再提问。")
    print("  完整说明：README.md「快速开始」/ docs/RUNBOOK.md")
    print("=" * 62)

    if args.start:
        print("\n启动服务中（Ctrl+C 停止）...", flush=True)
        run([str(PY), "-m", "knowtune.cli", "serve", "--port", "8013"])
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except subprocess.CalledProcessError as exc:
        print(f"\n命令失败（退出码 {exc.returncode}），已停止。")
        print("先看上面的报错；网络问题可把 pip 换国内镜像（CPU torch 那步仍须走官方 index）。")
        sys.exit(1)
