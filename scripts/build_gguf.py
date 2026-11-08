"""HF 权重 -> F16 GGUF -> 量化 GGUF，一条命令跑完。

串联 llama.cpp 的 convert_hf_to_gguf.py 与 tools/llama/llama-quantize。
脚本不下载任何东西：缺 llama.cpp、缺量化器、缺 sentencepiece 都直接
给安装命令，不会偷偷往环境里装包。

路径口径：相对路径按**仓库根目录**还原（与 config/models.yaml 一致）。

示例：

    python scripts/build_gguf.py \
      --hf data/tmp/qwen-3b-doctor-weiyi-light-merged \
      --llama-cpp ../../llama-cpp \
      --name qwen-3b-doctor-weiyi-light \
      --out data/models/Qwen-3b-doctor-weiyi-light-Q4_K_M.gguf

F16 中间文件放在 out 同级目录，已存在就跳过转换（方便直接重算 Q5/Q8），
默认保留；产物已存在时拒绝覆盖。加 --dry-run 只打印将执行的命令。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def resolve(raw: str) -> Path:
    p = Path(raw).expanduser()
    return (p if p.is_absolute() else ROOT / p).resolve()


def find_quantizer(explicit: str) -> Path:
    if explicit:
        return resolve(explicit)
    for name in ("llama-quantize.exe", "llama-quantize"):
        p = ROOT / "tools" / "llama" / name
        if p.is_file():
            return p
    return ROOT / "tools" / "llama" / "llama-quantize.exe"


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hf", required=True, help="合并后的 HF 目录（scripts/merge_lora.py 的 --out）")
    ap.add_argument("--llama-cpp", required=True, help="llama.cpp 源码根目录（含 convert_hf_to_gguf.py 和 gguf-py/）")
    ap.add_argument("--name", default="", help="写进 GGUF 的 general.name；默认取 --out 文件名")
    ap.add_argument("--out", default="", help="最终量化 GGUF；默认 data/models/<name>-Q4_K_M.gguf")
    ap.add_argument("--quant", default="Q4_K_M", help="量化规格，默认 Q4_K_M")
    ap.add_argument("--threads", type=int, default=8, help="量化线程数，默认 8")
    ap.add_argument("--quantizer", default="", help="量化器路径；默认 tools/llama/llama-quantize(.exe)")
    ap.add_argument("--extra-pythonpath", default="", help="额外的 site-packages 目录（如隔离装的 sentencepiece）")
    ap.add_argument("--drop-f16", action="store_true", help="量化成功后删除 F16 中间文件（默认保留）")
    ap.add_argument("--dry-run", action="store_true", help="只打印命令，不执行")
    return ap.parse_args()


def run(cmd: list[str], env: dict[str, str], dry: bool) -> None:
    printable = " ".join(f'"{c}"' if " " in c else c for c in cmd)
    print(("+ " if dry else "$ ") + printable, flush=True)
    if not dry:
        subprocess.run(cmd, check=True, env=env)


def main() -> int:
    a = parse_args()
    hf, llama_cpp = resolve(a.hf), resolve(a.llama_cpp)
    name = a.name or (Path(a.out).stem if a.out else "")
    if not name:
        print("请给 --name 或 --out，用来给模型命名。", file=sys.stderr)
        return 2
    out = resolve(a.out) if a.out else ROOT / "data" / "models" / f"{name}-{a.quant}.gguf"
    out.parent.mkdir(parents=True, exist_ok=True)
    f16 = out.parent / f"{name}-F16.gguf"

    convert = llama_cpp / "convert_hf_to_gguf.py"
    gguf_py = llama_cpp / "gguf-py"
    quantizer = find_quantizer(a.quantizer)
    problems = []
    if not hf.is_dir():
        problems.append(f"HF 目录不存在: {hf}")
    if not convert.is_file():
        problems.append(f"缺转换脚本: {convert}")
    if not gguf_py.is_dir():
        problems.append(f"缺 gguf-py: {gguf_py}")
    if not quantizer.is_file():
        problems.append(f"缺量化器: {quantizer}（先跑 python scripts/get_llama.py）")
    if out.exists():
        problems.append(f"产物已存在，拒绝覆盖: {out}")
    if problems and not a.dry_run:
        for p in problems:
            print("× " + p, file=sys.stderr)
        return 1
    for p in problems:
        print("! " + p + ("（--dry-run 只打印命令，不阻断）" if a.dry_run else ""))

    parts = [str(gguf_py)]
    if a.extra_pythonpath:
        parts.append(str(resolve(a.extra_pythonpath)))
    if os.environ.get("PYTHONPATH"):
        parts.append(os.environ["PYTHONPATH"])
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(parts)

    if not a.dry_run:
        probe = subprocess.run(
            [sys.executable, "-c", "import sentencepiece"], env=env,
            capture_output=True, text=True,
        )
        if probe.returncode != 0:
            print("× convert_hf_to_gguf.py 需要 sentencepiece。安装后重跑：", file=sys.stderr)
            print(f"    {sys.executable} -m pip install sentencepiece", file=sys.stderr)
            print("  不想装进当前环境就用 --extra-pythonpath 指向隔离安装目录。", file=sys.stderr)
            return 2

    print(f"HF      : {hf}")
    print(f"F16     : {f16}{'（已存在，跳过转换）' if f16.is_file() else ''}")
    print(f"量化产物: {out} ({a.quant})")

    if not f16.is_file():
        run([sys.executable, str(convert), str(hf), "--outfile", str(f16),
             "--outtype", "f16", "--model-name", name], env, a.dry_run)
    run([str(quantizer), str(f16), str(out), a.quant, str(a.threads)], env, a.dry_run)

    if a.dry_run:
        print("\n--dry-run：以上命令均未执行。")
        return 0

    if out.is_file():
        print(f"\n完成: {out} | {out.stat().st_size / 1024**3:.2f} GB")
    else:
        print("量化器返回成功但产物不存在，检查上面的输出。", file=sys.stderr)
        return 1
    if a.drop_f16 and f16.is_file():
        f16.unlink()
        print(f"已删除 F16 中间文件: {f16}")
    else:
        print(f"F16 中间文件保留在: {f16}（重算其它量化规格可直接复用；不需要可自行删除）")
    print(f"校验: sha256sum {out}")
    print("接入: config/models.yaml 里建独立 local 端点，path 写项目根相对路径。")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as exc:
        print(f"\n命令失败（退出码 {exc.returncode}），已停止。", file=sys.stderr)
        raise SystemExit(1)
