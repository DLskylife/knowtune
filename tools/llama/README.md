# llama-server 放这里

`tools/llama/` 的骨架已在仓库里，二进制不入库（约 100 MB，且按平台/后端分版本）。

在本机跑 7B 模型（预问诊默认走本机模型）时下载：

```bash
python scripts/get_llama.py                 # 自动选：Windows 优先 Vulkan
python scripts/get_llama.py --backend cpu   # 没有独显 / 想最小体积
python scripts/get_llama.py --list          # 只看这个 tag 有哪些资产
```

`config/models.yaml` 里本地端点的 `path` 写**项目根相对路径**（用 `/` 分隔），标准落位是 `data/models/xxx.gguf`；仓库里 `local`（8097，7B）和 `local3b`（8098，3B 微调）都已经是这个写法。文件放进 `data/models/` 就能用，不用改配置。不用本机模型就整步跳过，走云端后端即可；若 `local` 的 gguf 没放，把它的 `autostart: true` 删掉，免得启动时报自启失败。

## 从自己的 LoRA 微调产物做一个能跑的 GGUF

仓库不带任何训练权重：`.gguf` / `.safetensors` 都被忽略，最小的 Q4 版本也有约 1.8 GB，超 GitHub 单文件 100 MB 上限。手上有 LLaMA-Factory 的 LoRA adapter 时，用仓库里的两个脚本产出能直接跑的文件：

```bash
# 准备：base HF 权重、LoRA adapter 目录、llama.cpp 源码（含 gguf-py）、
#       llama-server / llama-quantize 二进制（python scripts/get_llama.py）。
#       所有路径都按仓库根相对写，不写盘符。

# 1. 合并 LoRA 到 base：W += B @ A * (lora_alpha / r)，产出标准 HF 目录
python scripts/merge_lora.py \
  --base <base_hf_dir> \
  --adapter <adapter_dir> \
  --out data/tmp/my-model-merged

# 2+3. HF -> F16 GGUF -> 量化，一条命令跑完，产物直接落标准目录
python scripts/build_gguf.py \
  --hf data/tmp/my-model-merged \
  --llama-cpp <llama_cpp_dir> \
  --name my-model \
  --out data/models/my-model-Q4_K_M.gguf
```

`merge_lora.py` 只需 `torch + safetensors`，不必装 peft；输出目录已存在会拒绝覆盖，合并数与形状不符会丢掉半成品。`build_gguf.py` 不下载任何东西：缺 `convert_hf_to_gguf.py` / `gguf-py` / `llama-quantize` / `sentencepiece` 就给安装命令，加 `--dry-run` 只打印将执行的命令。F16 中间文件默认保留，改 `--quant Q5_K_M` 重算时可直接复用；`--drop-f16` 才删。

## 接入：必须建独立端点

在 `config/models.yaml` 里建一个**独立的** local 端点，后台「模型」页点「启动」：

```yaml
  local3b:
    base_url: http://127.0.0.1:8098/v1
    model: my-model-Q4_K_M
    kind: local
    models:
    - id: my-model-Q4_K_M
      path: data/models/my-model-Q4_K_M.gguf
      port: 8098
```

**别挂在默认 `local` 端点下**：一个端点只有一个 `base_url`，点「启动」会把整个默认端点改写到新端口，预问诊等钉在默认端点上的功能会被连带切走。仓库里的 `local3b` 就是这么独立出来的，`local` 保持 8097 + 7B + autostart。注意 `modelcfg.persist` 保存配置时会重写 yaml 抹掉手写注释，原因写文档别写注释里。

3B 模型 Q4_K_M 约 1.8 GB、4.99 BPW：6 GB 显存可全量 offload（`-ngl 99`），纯 CPU（`-ngl 0`）实测约 16 tok/s。量化只解决「跑得动」，不改善模型本身的能力；上线前仍要按自己的业务场景做质量评测。
