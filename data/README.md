# data/ 目录说明

clone 下来这些目录就已在，程序不会因为「目录不存在」报错。除下表标注的文件外，`data/` 下的一切都不进版本库。

| 目录 / 文件 | 用途 | 谁生成 |
|---|---|---|
| `library.db` | SQLite 主库：知识库、文档、chunk、会话、检索日志 | `knowtune init` / 首次启动自动建表 |
| `vectors/` | 向量索引文件 | 导入文档时自动写 |
| `uploads/` | 通过界面导入的源文档 | 用户导入时自动写 |
| `corpus/` | 整理后的语料（可选） | 用户自备 |
| `models/` | 本地嵌入与精排权重 | 按 [`models/README.md`](models/README.md) 下载 |
| `calibration/` | 按库标定结果 `kb_<id>.json` | 评测台产出；仓库带一份 `kb_2.json` 作示例 |
| `tmp/` | 运行期临时文件 | 程序自建 |
| `secrets.json` | API 密钥密文（Windows DPAPI，绑账户与本机） | 界面填写或环境变量，**绝不入库** |

**不进版本库**：数据库（含问答历史）、源文档、模型权重、`secrets.json`、`*.db*`、`*.bak-*`。它们要么含本机内容，要么体积超过 GitHub 单文件 100 MB 上限，要么两者都有。

一条命令补齐环境与空库：

```bash
python scripts/setup.py
```

详细步骤见 [`docs/RUNBOOK.md`](../docs/RUNBOOK.md)，日常使用见 [`docs/USER_GUIDE.md`](../docs/USER_GUIDE.md)。
