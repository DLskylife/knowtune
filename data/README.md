# data/ 目录说明

clone 下来这些目录就已在，程序不会因为「目录不存在」报错。除下表标注的文件外，`data/` 下的一切都不进版本库。

| 目录 / 文件 | 用途 | 谁生成 |
|---|---|---|
| `library.db` | SQLite 主库：知识库、文档、chunk、会话、检索日志 | `knowtune init` / 首次启动自动建表 |
| `vectors/` | 向量索引文件 | 导入文档时自动写 |
| `uploads/` | 用户导入的源文档；仓库自带 26 篇公开维医指南作样例，见 [`uploads/weiyi/README.md`](uploads/weiyi/README.md) | 界面 / `knowtune ingest` 写入 |
| `corpus/` | 整理后的语料：`现代医学/` 下 20 篇常见病参考 + `现代医学_来源清单.md`，**随仓库发布**（364 KB）。`_raw/`（抓取原始件，与 `uploads/weiyi/` 同版重复）与 `临床药理/`（39 MB 整本教材 OCR，来源不可核）不入库 | 本地脚本 `scripts/fetch_msd.py` |
| `models/` | 本地嵌入与精排权重 | 按 [`models/README.md`](models/README.md) 下载 |
| `calibration/` | 按库标定结果 `kb_<id>.json` | 评测台产出；仓库带一份 `kb_2.json` 作示例 |
| `tmp/` | 运行期临时文件 | 程序自建 |
| `secrets.json` | API 密钥密文（Windows DPAPI，绑账户与本机） | 界面填写或环境变量，**绝不入库** |

## 路径口径

`library.db` 里文档的 `source_uri`、`config/models.yaml` 里的模型 `path`，一律写**项目根相对路径**（如 `data/uploads/weiyi/xxx.pdf`，用 `/` 分隔）。基准是仓库根目录，不是进程的工作目录：整个仓库改名、换盘符、拷到另一台机器，只要目录结构不变就不会失效。程序读取时按仓库根还原，新导入的文档也自动写成相对路径；项目根之外的外部文件无法可移植，会保留绝对路径兜底。

**不进版本库**：数据库（含问答历史）、本机上传的源文档、模型权重、`secrets.json`、`*.db*`、`*.bak-*`。它们要么含本机内容，要么体积超过 GitHub 单文件 100 MB 上限。

**例外是随仓库发布的两批样例语料**，clone 下来即可直接导入：

| 位置 | 内容 | 体积 | 来源性质 |
|---|---|---|---|
| `uploads/weiyi/` | 26 篇维医诊疗指南与疗法规范（PDF + 转好的 Markdown） | 4.5 MB | 新疆卫健委政府公开发布，带来源清单 |
| `corpus/现代医学/` | 20 篇常见病参考摘要 + `现代医学_来源清单.md` | 364 KB | 《默沙东诊疗手册》大众版公开正文，逐篇带原地址与抓取日期 |

这两批走的就是普通上传/整理路径，没有单独的目录流程。其余语料（古籍 PDF 等本机材料、`corpus/_raw/` 抓取原始件、`corpus/临床药理/` 的整本教材 OCR）留在本机：前两类与已发布的同版重复或含第三方版权，后者 39 MB 且来源不可核。


一条命令补齐环境与空库：

```bash
python scripts/setup.py
```

详细步骤见 [`docs/RUNBOOK.md`](../docs/RUNBOOK.md)，日常使用见 [`docs/USER_GUIDE.md`](../docs/USER_GUIDE.md)。
