# llama-server 放这里

`tools/llama/` 的骨架已在仓库里，二进制不入库（约 100 MB，且按平台/后端分版本）。

在本机跑 7B 模型（预问诊默认走本机模型）时下载：

```bash
python scripts/get_llama.py                 # 自动选：Windows 优先 Vulkan
python scripts/get_llama.py --backend cpu   # 没有独显 / 想最小体积
python scripts/get_llama.py --list          # 只看这个 tag 有哪些资产
```

`config/models.yaml` 里 `local` 后端的 `path` 写**项目根相对路径**（用 `/` 分隔，可用 `../` 指向仓库外），不写盘符；clone 后把示例路径改成你自己的 `.gguf`。不用本机模型则整步跳过，走云端后端即可。

## 从自己的 LoRA 微调产物做一个能跑的 GGUF

仓库不带任何训练权重：`.gguf` / `.safetensors` 都被忽略，且最小的 Q4 版本也有约 1.8 GB，超 GitHub 单文件上限。手上有 LLaMA-Factory 的 LoRA adapter 时，在本机这样产出一个能直接跑的文件：

```bash
# 1. 合并 adapter 到 base：对每个目标权重做 W += B @ A * (lora_alpha / r)
#    输出标准 HF 目录（config.json + tokenizer + *.safetensors + index.json）
python merge_lora.py            # 只需 torch + safetensors，不必装 peft

# 2. HF -> F16 GGUF（PYTHONPATH 指向 llama.cpp 里的 gguf-py；还需 sentencepiece）
python <llama.cpp>/convert_hf_to_gguf.py <merged_dir> \
  --outfile model-f16.gguf --outtype f16

# 3. 量化（本目录的 llama-quantize.exe，8 线程）
./llama-quantize.exe model-f16.gguf model-Q4_K_M.gguf Q4_K_M 8

# 4. 在 config/models.yaml 的 local 后端加一条 id / 相对 path / port，
#    后台「模型」页点「启动」即可
```

3B 模型 Q4_K_M 约 1.8 GB、4.99 BPW：6 GB 显存可全量 offload（`-ngl 99`），纯 CPU（`-ngl 0`）实测约 16 tok/s。量化只解决「跑得动」，不改善模型本身的能力；上线前仍要按自己的业务场景做质量评测。
