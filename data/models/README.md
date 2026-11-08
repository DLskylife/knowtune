# 本地模型放这里

两个模型**目录名不一样**，写错只会静默降级（检索只剩 BM25 一路 / 精排不生效），不会报错。

| 模型 | 目录名 | 体积（实测） | 命名规则 |
|---|---|---|---|
| 嵌入 `BAAI/bge-small-zh-v1.5` | `bge-small-zh-v1.5` | 约 92 MB | **无** `BAAI--` 前缀，`config/default.yaml` 的 `embedding.local_path` 写死这个字符串 |
| 精排 `BAAI/bge-reranker-base` | `BAAI--bge-reranker-base` | 约 1.1 GB | **有** 前缀，程序把 `model` 里的 `/` 换成 `--` 自动拼路径 |

## 下载（两条命令）

```bash
pip install -U huggingface_hub

# 国内网络先设镜像
# export HF_ENDPOINT=https://hf-mirror.com

hf download BAAI/bge-small-zh-v1.5 \
  --local-dir data/models/bge-small-zh-v1.5

hf download BAAI/bge-reranker-base \
  --local-dir data/models/BAAI--bge-reranker-base
```

旧版 CLI 叫 `huggingface-cli download`，参数相同；也可以从 HuggingFace / ModelScope 手动下载仓库全部文件放进对应目录。

## 校验

两个目录里都应有 `config.json`、`tokenizer.json`、`model.safetensors`（或 `pytorch_model.bin`）。`python scripts/setup.py` 会检查目录是否存在，但**不会替你下载 1.2 GB 权重**。

精排默认开启（`retrieval.rerank.enabled: true`、`allow_download: false`），本地缺权重不会联网补下，只会打 `degraded=rerank_model_missing`。看 `GET /api/status` 的 `rerank.last_error` 是否为 `null`。

## 可选的生成后端 GGUF（本机模型）

这两个跟上面检索用的嵌入 / 精排无关，是给「在本机跑生成模型」用的。仓库不含任何权重，`*.gguf` 全被 gitignore 排除；标准落位**就是本目录**，文件名与 `config/models.yaml` 一一对应：

| 端点 | 文件名 | 体积 | 用途 |
|---|---|---|---|
| `local`（8097，`autostart: true`） | `Qwen2.5-7B-Instruct-Q4_K_M.gguf` | 4.68 GB | 官方 7B Q4，预问诊默认走它 |
| `local3b`（8098，按需启动） | `Qwen-3b-doctor-weiyi-light-Q4_K_M.gguf` | 1.83 GB | 3B 维医微调 Q4_K_M，可选 |

把文件放进本目录即可，配置里已经是 `data/models/xxx.gguf`（项目根相对，从哪个工作目录启动都一样），不用改路径。没有这两个文件也能用：走云端 `siliconflow` / `deepseek` 填密钥即可；不想看 `local` 自启失败，就把 `config/models.yaml` 里它的 `autostart` 删掉。

3B 那份的校验值（本机 2026-11-08 量化产物）：

```
sha256  9608faa15fef5b218510a6663afdb5867fd1a235f90af5d846e6ffa4da7a9bf2
```

**怎么拿到**：仓库放不下 1.83 GB 权重（GitHub 单文件上限 100 MB）。两条路——① 手上有 LoRA adapter，按 [`tools/llama/README.md`](../../tools/llama/README.md) 的两条脚本命令自己合并 + 量化，产物直接写进本目录；② 从自己的 Hugging Face / 网盘拷入，用上面的 sha256 校验。别把权重提交进 git。

`llama-server` 可执行文件放 `tools/llama/`，用 `python scripts/get_llama.py` 下载。
