# 技术栈与架构速查

本文是 KnowTune 的「技术清单 + 架构主线」速查，内容从 `pyproject.toml`、源码与配置核出。数据模型、参数取舍与实测推导见 [ARCHITECTURE.md](ARCHITECTURE.md)；逐文件地图见 [PROJECT_MAP.md](PROJECT_MAP.md)；从零部署见 [RUNBOOK.md](RUNBOOK.md)；日常使用见 [USER_GUIDE.md](USER_GUIDE.md)；项目入口见 [README.md](../README.md)。

## 1. 一句话主线

**导入 → 切块 → 向量(BLOB) + FTS5 双写 → 双路召回 → 加权融合 → 精排 → MMR(关) → 拒答 → LLM 带引用生成 → 引用校验 / 支撑度。**

- 稠密（`dense`）路用 `BAAI/bge-small-zh-v1.5`（512 维），稀疏（`sparse`）路用 jieba 预分词写进 FTS5 BM25；两路各取 top 50，`weighted` 融合（w_dense 0.35 / w_sparse 0.50 / w_third 0），前 10 条送精排。
- 精排（`rerank`）用 `BAAI/bge-reranker-base` CrossEncoder，三个库的 `rerank.enabled` 均为 `true`、**精排实际在跑**；MMR（最大边际相关）、邻块扩展、查询改写三项均未启用。
- 引用校验（`cite.verify`）给引用覆盖率，是形式指标。**支撑度（`support`）只在后台 `/api/ask` 链路计算**，前台 `/api/ask` 不调用 `support.evaluate`；`/v1/chat/completions` 是纯转发网关，不进这条链。前台 `/api/ask` 与 `/v1` 网关都不算支撑度。
- 每一层可独立失败、降级并打 `degraded`（降级标记），不存在「某路静默返回空」这种看着正常、实际召回腰斩的状态。

## 2. 分层架构

| 层 | 职责 | 真实文件路径 |
|---|---|---|
| **前端层** | 后台管理台 + 前台问答站，纯原生 HTML/CSS/JS，零前端框架、零打包 | `src/knowtune/api/static/{index.html,app.js,style.css}`（后台，构建号 0.9.54）；`src/knowtune/frontsite/static/{index.html,app.js,style.css}`（前台） |
| **服务层** | 启动装配、全部 HTTP 接口、站点线程、预问诊桥接、配置与角色/提示词解析 | `src/knowtune/cli.py`、`src/knowtune/api/app.py`（约 4400 行，唯一大文件）、`src/knowtune/frontsite/{instcfg,runner,site}.py`、`src/knowtune/{config,kbconfig,roles,promptcfg,modelcfg,settings,secrets}.py`、`src/knowtune/previsit/app.py`（桥接装配）、`src/previsit/`（预问诊实现真身，v0.3.0，零 knowtune 依赖） |
| **能力层** | 导入 / 索引 / 检索 / 生成四类算法 | `src/knowtune/ingest/{loader,cleaner,chunker,pipeline}.py`、`src/knowtune/index/{embedder,vectors,sparse}.py`、`src/knowtune/retrieve/{engine,rerank}.py`、`src/knowtune/generate/{prompt,llm,cite,evidence,support,think}.py` |
| **存储层** | 单文件库 + 密钥密文 + 运行资产（均不进版本库） | `data/library.db`（SQLite WAL，8 表 + 1 张 FTS5 虚表 + 5 张影子表）、`data/secrets.json`（Windows DPAPI）、`data/models/`、`data/calibration/`、`data/uploads/` |

两套 `previsit` 不是重复实现：`src/previsit/` 是实现真身，`src/knowtune/previsit/` 是五个同名文件纯再导出 + `app.py` 装配层，运行时由 `frontsite/site.py` 挂到 `/previsit` 并用 `_SectionGate` 按 `pv` 开关做 404 拦截。

**分层约束**：依赖只允许从上往下（前端层 → 服务层 → 能力层 → 存储层），下层不反向 import 上层。服务层的 `AppState` 是唯一知道全局的地方，它只装配、不实现算法；能力层的 `ingest` / `index` / `retrieve` / `generate` 四个包互不调用，只靠数据结构对话，所以各层能单独喂数据、不连模型地验证。前台与后台两套静态资源的样式与文案完全独立，改后台不会动到前台。`pyproject.toml` 的 wheel 打包 `src/knowtune` 与 `src/previsit` 两个包。

## 3. 完整技术清单

`pyproject.toml` 写的是「实测可用的最低版本」，没锁 patch；要可复现就装好后 `pip freeze > requirements.lock` 自己留一份（每台机器 CPU/CUDA 组合不同）。`all = knowtune[extract,local,dev]`，**不含 onnx**。

| 分组 | 依赖 | 版本下限 | 用途 |
|---|---|---|---|
| 运行时 | Python | ≥ 3.11 | 语言运行时 |
| 构建 | hatchling | ≥ 1.25.0 | 构建后端；wheel 打包 `src/knowtune` 与 `src/previsit`，入口 `knowtune = knowtune.cli:main`，MIT |
| 核心 | fastapi | ≥ 0.115.0 | REST API（`api/app.py`） |
| 核心 | uvicorn[standard] | ≥ 0.32.0 | ASGI 服务器 |
| 核心 | pydantic / pydantic-settings | ≥ 2.9.0 / ≥ 2.5.0 | 数据校验与配置模型 |
| 核心 | PyYAML | ≥ 6.0.2 | 读 `config/*.yaml` |
| 核心 | httpx | ≥ 0.27.2 | LLM 客户端，`trust_env=False` 绕代理 |
| 核心 | tenacity | ≥ 9.0.0 | LLM 调用指数退避重试 |
| 核心 | numpy | ≥ 1.26.4 | 向量矩阵暴力点积检索 |
| 核心 | jieba | ≥ 0.42.1 | 中文 BM25 预分词 |
| 核心 | structlog | ≥ 24.4.0 | 结构化日志 |
| 核心 | python-multipart | ≥ 0.0.9 | 文件上传（含分片） |
| 核心 | psutil | ≥ 6.0.0 | 端口占用者（PID / 进程名）排查 |
| extract | pypdf | ≥ 5.1.0 | PDF 抽取 |
| extract | python-docx | ≥ 1.1.2 | Word 抽取 |
| extract | openpyxl | ≥ 3.1.5 | Excel 抽取 |
| extract | python-pptx | ≥ 1.0.2 | PPT 抽取 |
| extract | beautifulsoup4 | ≥ 4.12.3 | HTML 解析 |
| extract | trafilatura | ≥ 1.12.0 | 网页正文抽取 |
| local | sentence-transformers | ≥ 3.3.0 | 本地嵌入（Bi-Encoder）与精排（Cross-Encoder） |
| onnx | onnxruntime | ≥ 1.20.0 | **未启用**：可选加速路径，待实测 |
| onnx | fastembed | ≥ 0.4.0 | **未启用**：bge-reranker-v2-m3 在其中的可用性待核实，不必装 |
| dev | pytest / pytest-asyncio | ≥ 8.3.0 / ≥ 0.24.0 | 单测（本仓库不含 `tests/`，需要时自建） |
| dev | ruff / mypy | ≥ 0.7.0 / ≥ 1.13.0 | lint / 类型检查 |

`onnx` 组整体**未启用**：没有代码路径加载它，装了也不生效，别当可用加速。

**另有一项不在 `pyproject.toml`**：CPU 版 `torch` 必须从 PyTorch 官方 index 先装（`pip install torch --index-url https://download.pytorch.org/whl/cpu`），再 `pip install -e ".[extract,local]"`。写进依赖会被解析成 CUDA 版（体积差约 10 倍），顺序反了前面白装。

## 4. 三个关键架构决策

### 4.1 不用向量数据库 —— SQLite BLOB + numpy 暴力点积够用

向量存 `embedding(vec BLOB)`，查询时全量读进内存做精确矩阵点积（`@` + `argpartition` top-k），不建 ANN 索引。当前 28020 条 512 维向量（2026-11-07 采集）就是这个量级：多一层 ANN 只会多一套近似误差、多一个「索引与数据不同步」故障点，换来的只是把 30 ms 级变成 3 ms 级。**什么时候必须换 ANN / `sqlite-vec` 未验证**——本仓库没有更大量级的实测，不给切换阈值。

### 4.2 Bi-Encoder 召回 + Cross-Encoder 精排（两阶段）

召回用 `BAAI/bge-small-zh-v1.5`（Bi-Encoder）：query / doc 独立编码，doc 向量入库常驻，查询只编码一次，可预计算，适合全库粗筛。精排用 `BAAI/bge-reranker-base`（Cross-Encoder）：`(query, doc)` 配对同场推理出相关性分，不可预计算，只适合少量 top-K。Cross-Encoder 不能做检索——全库逐条配对会慢 100~1000 倍，两阶段各司其职。代价是精排为整链最重环节：维医库 22 道口语题 `10×384` 实测 3.07 s（CPU，2026-11-07），打分有 4096 条进程内 LRU 缓存（键为「模型, 问题摘要, 片段摘要」），换问法不命中、重启即空。

### 4.3 中文 BM25 必须 jieba 预分词

FTS5 默认 `unicode61` 把整句中文当一个 token，2 字查询命中 0 条；`trigram` 要求查询 ≥3 字符，2 字词同样全失效。唯一正路：**先 jieba 预分词、空格拼串写入 FTS5，查询侧同样分词**（`index/sparse.py`）；`bm25()` 返回负数、越小越相关，排序用 `ORDER BY bm25(chunk_fts) ASC`。这是 `weighted` 融合里稀疏路能起作用的前提。

## 5. 启动要点速记

- **CPU torch 顺序**：`pip install torch --index-url https://download.pytorch.org/whl/cpu` → `pip install -e ".[extract,local]"`。反了会拉 CUDA 版。
- **两个模型目录名不一样**（都在 `data/models/`）：嵌入 `data/models/bge-small-zh-v1.5`（约 92 MB，**无** `BAAI--` 前缀，与 `embedding.local_path` 字符串一致）；精排 `data/models/BAAI--bge-reranker-base`（约 1.1 GB，**有** `BAAI--` 前缀，由 `model` 里的 `/` 替换而来）。`allow_download: false`，缺权重只降级、不联网补。
- **8013 / 8800**：`knowtune serve --port 8013`（`cli.py` 默认 `8000`）；后台 `http://127.0.0.1:8013/` 与 `/v1` 网关，前台 `http://127.0.0.1:8800/`（`instances.yaml` 的 `port` + `autostart: true`，进程内 daemon 线程，不另起进程）。`KNOWTUNE_GATEWAY` 由 `cli.py:234` 按实际 `--port` 写入。
- **前台端口范围 8800–8899**（`instcfg.py` 的 `PORT_MIN/MAX`）；两个后台同时 `serve` 会撞 8800，先关掉多余的那个，别换端口绕。
- **43–93 秒预热**：16 核 CPU，2026-11-07 实测热盘 43 s / 冷盘首次约 93 s；期间 `embedder.available=false`、只有 BM25 一路，`GET /api/status` 的 `warmup.state=ready` 才算双路齐全。

## 6. 构建号与缓存失效

- **后台统一 `0.9.54`**（`src/knowtune/api/static/`）。
- **前台本次统一为 `1.7.18`**。2026-11-07 核对时曾记录三处不一致：`index.html` 的 `style.css?v=1.7.18`、`app.js?v=1.7.7`、`index.html:18` 与 `app.js:25` 的 `1.7.17`；按统一口径以 `1.7.18` 为准。
- 为什么要这个号：HTML 是 no-store 的，但 WebView 可能把旧 JS 留在缓存里。三处要一致（`index.html` 的 `?v=`、`index.html` 的自检常量、`app.js` 的常量），编号对不上时页面会强制换一次，否则出现「新按钮在、点了没反应」。改前端静态资源必须同步升号。

## 与其他文档的分工

本文只给**技术清单、分层、三个关键决策、启动速记、构建号**；按分工不写需求对照（在 [ARCHITECTURE.md](ARCHITECTURE.md) §13，且该处有三套统计口径，别只引一套）、不写操作步骤、不写实测指标全表。

- [README.md](../README.md)：项目入口、事实卡、能力速览、文档地图、已知边界摘要。
- [RUNBOOK.md](RUNBOOK.md)：从零 clone 到三板块可用、逐条判据、排障、备份。
- [USER_GUIDE.md](USER_GUIDE.md)：建库导入、读引用与支撑度、参数含义、标定核对、指标现状、使用侧排障与限制、本地 `.gguf`。
- [ARCHITECTURE.md](ARCHITECTURE.md)：现行架构、数据模型、检索/生成链、关键决策实测推导、需求对照三口径、代码已知不一致、历史归档。
- [PROJECT_MAP.md](PROJECT_MAP.md)：逐目录逐文件说明、启动顺序、配置优先级、改动落点。
- [WBS.html](WBS.html)：交付时工作包与范围缺口**快照**，标「历史」，不冒充现行状态表。

状态栏口径、标定现行值、需求三口径这几处最易写歪的事实，以 [ARCHITECTURE.md](ARCHITECTURE.md) 与 [RUNBOOK.md](RUNBOOK.md) 正文为准。
