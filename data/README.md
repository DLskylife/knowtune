# data/ 目录说明

clone 下来这些目录就已在，程序不会因为「目录不存在」报错。除下表标注的文件外，`data/` 下的一切都不进版本库。

| 目录 / 文件 | 用途 | 谁生成 |
|---|---|---|
| `library.db` | SQLite 主库：知识库、文档、chunk、会话、检索日志 | `knowtune init` / 首次启动自动建表 |
| `vectors/` | 向量索引文件 | 导入文档时自动写 |
| `uploads/` | 用户导入的源文档；仓库自带 26 篇公开维医指南作样例，见 [`uploads/weiyi/README.md`](uploads/weiyi/README.md) | 界面 / `knowtune ingest` 写入 |
| `corpus/` | 本机整理产物，仓库只留空骨架，内容不入库、不参与运行 | 本地脚本（可选） |
| `models/` | 本地嵌入与精排权重 | 按 [`models/README.md`](models/README.md) 下载 |
| `calibration/` | 按库标定结果 `kb_<id>.json` | 评测台产出；仓库带一份 `kb_2.json` 作示例 |
| `tmp/` | 运行期临时文件 | 程序自建 |
| `secrets.json` | API 密钥密文（Windows DPAPI，绑账户与本机） | 界面填写或环境变量，**绝不入库** |

## 路径口径

`library.db` 里文档的 `source_uri`、`config/models.yaml` 里的模型 `path`，一律写**项目根相对路径**（如 `data/uploads/weiyi/xxx.pdf`，用 `/` 分隔）。基准是仓库根目录，不是进程的工作目录：整个仓库改名、换盘符、拷到另一台机器，只要目录结构不变就不会失效。程序读取时按仓库根还原，新导入的文档也自动写成相对路径；项目根之外的外部文件无法可移植，会保留绝对路径兜底。

**不进版本库**：数据库（含问答历史）、本机上传的源文档、模型权重、`secrets.json`、`*.db*`、`*.bak-*`。它们要么含本机内容，要么体积超过 GitHub 单文件 100 MB 上限。**唯一例外是 `uploads/weiyi/` 的 26 篇维医指南**（4.5 MB、政府公开发布、带来源清单），它们作为可直接导入的样例语料随仓库发布，走的就是普通上传文件路径，没有单独目录或单独流程。

一条命令补齐环境与空库：

```bash
python scripts/setup.py
```

详细步骤见 [`docs/RUNBOOK.md`](../docs/RUNBOOK.md)，日常使用见 [`docs/USER_GUIDE.md`](../docs/USER_GUIDE.md)。
