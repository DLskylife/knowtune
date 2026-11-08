"""把 LLaMA-Factory 的 LoRA adapter 合并进 base 权重，产出标准 HF 目录。

不依赖 peft / LLaMA-Factory，直接用 safetensors + torch 做
W += B @ A * (lora_alpha / r)。输出目录可直接喂给 llama.cpp 的
convert_hf_to_gguf.py，再交给 scripts/build_gguf.py 量化。

路径口径：相对路径一律以**仓库根目录**为基准（与 config/models.yaml 的
model.path、数据库里的 source_uri 一致），不认进程 cwd，也不写死盘符。

示例（本机 LLaMA-Factory 在仓库同级盘下）：

    python scripts/merge_lora.py \
      --base ../../llamafactory/models/Qwen2.5-3B-Instruct \
      --adapter ../../llamafactory/LLaMA-Factory/saves/qwen-3b-doctor-weiyi-light/saves/qwen-3b-doctor-weiyi \
      --out data/tmp/qwen-3b-doctor-weiyi-light-merged

输出目录已存在时直接拒绝，绝不覆盖已有权重。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

import torch
from safetensors import safe_open
from safetensors.torch import save_file

ROOT = Path(__file__).resolve().parents[1]

# HF 目录里除权重之外必须带上的文件。优先取 adapter 目录（微调可能改过
# tokenizer / chat_template），没有就回落到 base。
COPY_FILES = (
    "config.json",
    "generation_config.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "vocab.json",
    "merges.txt",
    "special_tokens_map.json",
    "added_tokens.json",
    "chat_template.jinja",
)


def resolve(raw: str) -> Path:
    """相对路径按仓库根还原，绝对路径原样返回。"""
    p = Path(raw).expanduser()
    return (p if p.is_absolute() else ROOT / p).resolve()


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", required=True, help="base 模型的 HF 目录（含 model.safetensors.index.json）")
    ap.add_argument("--adapter", required=True, help="LoRA 目录（含 adapter_config.json 与 adapter_model.safetensors）")
    ap.add_argument("--out", required=True, help="合并后的 HF 目录，必须不存在")
    ap.add_argument("--prefix", default="base_model.model.", help="adapter 张量名前缀，默认 LLaMA-Factory 口径")
    ap.add_argument("--scale", type=float, default=None, help="覆盖 lora_alpha/r；默认从 adapter_config.json 读")
    return ap.parse_args()


def main() -> int:
    a = parse_args()
    base, adapter_dir, out = resolve(a.base), resolve(a.adapter), resolve(a.out)

    for label, p in (("base", base), ("adapter", adapter_dir)):
        if not p.is_dir():
            print(f"{label} 目录不存在: {p}", file=sys.stderr)
            return 2
    adapter_file = adapter_dir / "adapter_model.safetensors"
    cfg_file = adapter_dir / "adapter_config.json"
    index_file = base / "model.safetensors.index.json"
    for p in (adapter_file, cfg_file, index_file):
        if not p.is_file():
            print(f"缺少文件: {p}", file=sys.stderr)
            return 2
    if out.exists():
        print(f"输出目录已存在，拒绝覆盖: {out}", file=sys.stderr)
        print("确认要重建就先自行移走该目录。", file=sys.stderr)
        return 1

    cfg = json.loads(cfg_file.read_text(encoding="utf-8"))
    scale = a.scale if a.scale is not None else cfg["lora_alpha"] / cfg["r"]
    print(f"LoRA r={cfg.get('r')} alpha={cfg.get('lora_alpha')} -> scale={scale}")
    print(f"base   : {base}")
    print(f"adapter: {adapter_dir}")
    print(f"out    : {out}")

    t0 = time.time()
    with safe_open(adapter_file, framework="pt") as f:
        lora = {k: f.get_tensor(k) for k in f.keys()}

    suffix_a, suffix_b = ".lora_A.weight", ".lora_B.weight"
    pairs: dict[str, tuple[torch.Tensor, torch.Tensor]] = {}
    for k, mat_a in lora.items():
        if not k.endswith(suffix_a):
            continue
        if not k.startswith(a.prefix):
            print(f"adapter 张量前缀不符（期望 {a.prefix!r}）: {k}", file=sys.stderr)
            return 2
        stem = k[len(a.prefix) : -len(suffix_a)]
        b_key = a.prefix + stem + suffix_b
        if b_key not in lora:
            print(f"缺少配对的 lora_B: {b_key}", file=sys.stderr)
            return 2
        pairs[stem + ".weight"] = (mat_a.float(), lora[b_key].float())
    if not pairs:
        print("adapter 里没有找到任何 lora_A/lora_B 配对，退出。", file=sys.stderr)
        return 2
    print(f"adapter: {len(lora)} 张量 -> {len(pairs)} 组待合并权重", flush=True)

    index = json.loads(index_file.read_text(encoding="utf-8"))
    shards = sorted(set(index["weight_map"].values()))
    out.mkdir(parents=True)
    merged_count = 0
    try:
        for shard in shards:
            src = base / shard
            if not src.is_file():
                raise RuntimeError(f"base 缺少分片: {src}")
            print(f"处理分片 {shard} ({src.stat().st_size / 1024**3:.2f} GB) ...", flush=True)
            with safe_open(src, framework="pt") as f:
                tensors = {k: f.get_tensor(k) for k in f.keys()}
            for k in list(tensors):
                if k not in pairs:
                    continue
                mat_a, mat_b = pairs[k]
                delta = (mat_b @ mat_a) * scale
                w = tensors[k]
                if tuple(delta.shape) != tuple(w.shape):
                    raise RuntimeError(f"形状不匹配 {k}: {tuple(delta.shape)} vs {tuple(w.shape)}")
                tensors[k] = (w.float() + delta).to(w.dtype).contiguous()
                merged_count += 1
            save_file(tensors, str(out / shard), metadata={"format": "pt"})
            del tensors
            print(f"  已写出，累计合并 {merged_count} 个权重", flush=True)
    except Exception:
        # 半截目录留着会让人误以为合并成功，直接清掉（只清本次新建的目录）。
        shutil.rmtree(out, ignore_errors=True)
        raise

    if merged_count != len(pairs):
        shutil.rmtree(out, ignore_errors=True)
        print(f"合并数不符: {merged_count} != {len(pairs)}，已丢弃输出。", file=sys.stderr)
        return 1
    shutil.copy2(index_file, out / index_file.name)
    for name in COPY_FILES:
        src = adapter_dir / name if (adapter_dir / name).is_file() else base / name
        if src.is_file():
            shutil.copy2(src, out / name)

    size = sum(p.stat().st_size for p in out.iterdir() if p.is_file())
    print(f"完成: {out} | {size / 1024**3:.2f} GB | {merged_count} 个权重 | {time.time() - t0:.1f}s")
    print("\n下一步：")
    print(f"  python scripts/build_gguf.py --hf {a.out} \\")
    print("    --llama-cpp ../../llama-cpp --name <模型名>")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
