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

## 本机 7B（可选，只影响预问诊）

`config/models.yaml` 里 `local` 后端的 `path` 写**项目根相对路径**（`/` 分隔，基准是仓库根目录）。仓库里给的是开发机示例 `../../llamafactory/...gguf`；要用本机模型，把它改成你自己的 `.gguf`（推荐放进本目录，写成 `data/models/xxx.gguf`），或删掉这个后端。相对路径按仓库根还原，从哪个工作目录启动服务都一样。不用本机模型就走云端 `siliconflow` / `deepseek`，在界面填密钥即可，这个文件不用改。

`llama-server` 可执行文件放 `tools/llama/`，用 `python scripts/get_llama.py` 下载。
