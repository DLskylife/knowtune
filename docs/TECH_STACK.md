# 技术栈与架构

> 本文件是 KnowTune 的「技术清单 + 架构主线」速查。所有内容均从 `pyproject.toml`、源码与配置中核出，不是泛泛而谈。
> 数据模型、参数取舍、实测数据见 [ARCHITECTURE.md](ARCHITECTURE.md)；目录级说明见 [PROJECT_MAP.md](PROJECT_MAP.md)；
> 从零部署与完整运行步骤见 [RUNBOOK.md](RUNBOOK.md)。

## 1. 一句话主线

**导入 → 切块 → 向量(BLOB)+FTS5 双写 → 查询双路召回 → 融合 → 精排 → MMR → 拒答 → LLM 带引用生成 → 可量化评估。**

每一层都可独立失败、降级、打标，不存在「某路静默返回空」这种看起来正常、实际召回腰斩的状态。

## 2. 分层架构

| 层 | 职责 | 关键模块 |
|---|---|---|
| **入口层** | CLI / REST API / 前端 | `cli.py`、`api/app.py`(8000)、`frontsite/`(8800)、`index.html`/`app.js`/`style.css`(原生 JS，零前端框架) |
| **子系统** | 导入 / 检索 / 生成 / 评估 | `ingest/`、`retrieve/`、`generate/`、`evaluate.py` |
| **能力层(算法)** | 混合检索、嵌入、精排、LLM | `index/embedder.py`、`index/vectors.py`、`index/sparse.py`、`retrieve/rerank.py`、`generate/llm.py` |
| **存储层** | 单文件库 + 密钥 | `data/library.db`(SQLite)、`data/secrets.json`(DPAPI) |
| **外部依赖** | 模型算力 | 云端 API(硅流/DeepSeek/混元)、本地 torch(CPU via sentence-transformers) |

## 3. 完整技术清单

### 语言与运行时
- **Python ≥3.11**（部署机为 3.13，项目用独立 venv，系统 Python 未装包）
- **构建/分发**：`hatchling` 后端，可编辑安装 `pip install -e .`，入口 `knowtune` → `cli:main`
- **依赖分区**(optional-extras，锁 patch 用 `requirements.lock`)：`extract`(文档抽取) / `local`(本地 torch 嵌入) / `onnx`(ONNX 加速，待实测) / `dev`(测试/lint)

### Web / API（11 个核心依赖，零向量库、零前端框架）
- `fastapi` —— REST API（`api/app.py`，8000）
- `uvicorn[standard]` —— ASGI 服务器（8800 由 `config/instances.yaml` 的 `autostart` 拉起）
- `pydantic` + `pydantic-settings` —— 数据校验 + 配置模型
- `python-multipart` —— 文件上传
- 前端：**纯原生 HTML/CSS/JS**，直接调 API

### 存储
- **SQLite**（单文件 `data/library.db`）：三重角色 —— 关系表(`document`/`chunk`/`embedding(content_hash,model,dim,vec BLOB)`)、**FTS5 倒排索引**(`chunk_fts`，中文 BM25 真相源)、元数据/日志(`query_log`)
- **文件系统**：源文档、`config/*.yaml`、`data/secrets.json`
- **DPAPI**(Windows 原生)：密钥密文落 `data/secrets.json`，默认持久化，绑当前账户+本机

### 导入（Ingest）
- **多格式 loader**：PDF(`pypdf`)、Word(`python-docx`)、Excel(`openpyxl`)、PPT(`python-pptx`)、HTML(`beautifulsoup4`+`trafilatura`)、Markdown
- `chunker`(有序切块，`seq` 支撑邻块扩展) / `cleaner`(清洗) / `pipeline`(编排+双写) / `textnorm`(码位归一 `fold_query`，解决 PDF 异体字对齐)

### 检索（Retrieve）—— 核心算法层
- **Dense 路**：`BAAI/bge-small-zh-v1.5`(512 维，**Bi-Encoder**) → **numpy 矩阵暴力点积**(`N×512` 常驻 RAM，`@` + `argpartition` top-k)，**精确检索，无向量库**
- **Sparse 路**：`jieba` 预分词(`cut_for_search`) + **SQLite FTS5** + `bm25()` 排序（中文必须预分词，否则 FTS5 把整句当一个 token）
- **融合**：`weighted`(min-max 归一化) 或 **RRF**(Reciprocal Rank Fusion) + 保底配额(防某路噪声挤掉真命中)
- **精排**：`BAAI/bge-reranker-base`(**Cross-Encoder**)
- **MMR**：`mmr_select`，`λ` 去冗余
- **拒答判定**：`min_score` + margin + soft 置信度（低置信只提醒不拒）
- **邻块扩展**：`expand_neighbors`，补回被切分劈开的论点另一半

### 生成（Generate）
- **LLM 客户端**(`generate/llm.py`)：**OpenAI 兼容协议**，三档后端 —— 云端(硅流/SiliconFlow、DeepSeek、混元)、本地 vLLM、本地 LM Studio
- `httpx` 同步客户端：显式 `connect/read/write/pool` 超时，`trust_env=False` 绕过公司代理
- `tenacity`：指数退避重试
- `think`(推理模型节流) / `cite`(引用覆盖率 `verify()`，形式指标非忠实度) / `evidence` / `prompt`

### 评估与可观测
- `dashboard.py`(运行仪表盘) / `chatctx.py`(会话上下文规则，不依赖 torch 可单测)
- `structlog` + `logbus`：结构化日志

### 配置体系
- `config.py`(Pydantic 根) / `settings.py` / `kbconfig`(知识库级覆盖) / `modelcfg`(模型) / `promptcfg`(提示词) / `frontsite/instcfg`(实例)
- 全部 **YAML 驱动**(`default.yaml`/`models.yaml`/`instances.yaml`)
- 密钥三级优先级：**本次填写 > 已存密文 > 环境变量**(`api_key_env`)

### 质量保障
- `pytest` + `pytest-asyncio`：**259 条单测全绿**
- `ruff`(lint) + `mypy`(类型)

## 4. 三个关键架构决策（为什么这么选）

### 4.1 不用向量数据库 —— 用 numpy 暴力检索
- 向量存 SQLite `embedding(vec BLOB)`，**查询时全量读进内存做精确矩阵点积**，不建 ANN 索引。
- 规模 ≤10 万 chunk 时 ANN 是**负资产**：多一层近似误差、多一套序列化、多一个「索引与数据不同步」故障点，换来的只是把 30ms 变 3ms。
- 切换阈值：**超过 20 万 chunk**（内存先撞墙），那时按库切 `sqlite-vec` / mmap。
- 实测：1,120 向量 / dim 512 / 2.2MB / 加载 34ms / 单次检索 0.48ms(点积 0.28ms)。

### 4.2 Bi-Encoder 召回 + Cross-Encoder 精排（两阶段）
- **召回用 Bi-Encoder**(`embedder.py` 的 `SentenceTransformer`)：query/doc 独立编码，doc 向量入库常驻，查询只编码一次 → 可预计算，适合全库粗筛。
- **精排用 Cross-Encoder**(`rerank.py` 的 `CrossEncoder`)：`(query, doc)` 配对同场推理出相关性分 → 不可预计算，只适合少量(top-K)精排。
- cross-encoder 不能做检索：每对都要跑一次 transformer 前向，全库逐条配对会慢 100~1000 倍。两阶段各司其职是行业标准。
- 代价：精排是整链最重环节（依赖 torch、单线程执行器、10s 超时降级）。

### 4.3 中文 BM25 必须预分词
- FTS5 默认 `unicode61` 把整句中文当一个 token、「检索」命中数为 0；`trigram` 要求查询 ≥3 字符、2 字词全失效。
- 唯一正路：**先 jieba 分词、空格拼串写入** FTS5，查询侧同样分词。虚词在查询侧过滤但不删索引侧（两侧同步重索引成本高，而查询侧改动即时生效）。

## 5. 启动要点（速记）
- 必须走 venv：`./.venv/Scripts/knowtune.exe serve --port 8000`（端口自定，系统 Python 无包）
- 8800 前端由 `instances.yaml` 的 `autostart: true` 自动拉起，不用手动起
- **冷启动约 93 秒**（嵌入模型 `import torch`+读权重），期间 `embedder.available=false`，只有 BM25 稀疏检索；`/api/status` 的 `warmup.state` 变 `ready` 才算双路齐全，此前别急着提问
- 后台进程绑定会话，会话结束即关；常驻需另配系统服务
