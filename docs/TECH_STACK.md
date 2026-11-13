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
| **前端层** | 后台管理台 + 前台问答站，纯原生 HTML/CSS/JS，零前端框架、零打包 | `src/knowtune/api/static/{index.html,app.js,style.css}`（后台，构建号 0.9.69）；`src/knowtune/frontsite/static/{index.html,app.js,style.css}`（前台，构建号 1.7.37） |
| **服务层** | 启动装配、全部 HTTP 接口、站点线程、预问诊桥接、配置与角色/提示词解析 | `src/knowtune/cli.py`、`src/knowtune/api/app.py`（装配入口）+ `src/knowtune/api/routes/` 8 个模块（94 个端点）、`api/common.py` / `state.py` / `coverage.py`、`src/knowtune/frontsite/{instcfg,runner,site}.py`、`src/knowtune/{config,kbconfig,roles,promptcfg,modelcfg,settings,secrets}.py`、`src/knowtune/previsit/app.py`（桥接装配）、`src/previsit/`（预问诊实现真身，v0.4.0，零 knowtune 依赖） |
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
| 核心 | fastapi | ≥ 0.115.0 | REST API（`api/app.py` 装配 + `api/routes/` 8 模块，94 端点） |
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
| dev | pytest / pytest-asyncio | ≥ 8.3.0 / ≥ 0.24.0 | 单测（仓库自带 `tests/`：相对路径闸门、预问诊语言、包安装冒烟） |
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

- **后台统一 `0.9.69`**（`src/knowtune/api/static/`）。
- **前台现行 `1.7.37`、后台现行 `0.9.69`。** 2026-11-07 全量重写时曾统一为 `1.7.18`：当时核对到三处不一致（`index.html` 的 `style.css?v=1.7.18`、`app.js?v=1.7.7`、`index.html:18` 与 `app.js:25` 的 `1.7.17`）。同日补 `progress` 进度事件改了两个前端 JS，按下面规则升为 `1.7.19`；后台同步升 `0.9.55`，同日站点配置弹窗重排再升为 `0.9.56`；同日移除预问诊病历卡、收网改为只给分诊结论，前台升 `1.7.20`；同日预问诊整体退化为「提示词 + 模型」纯对话（删除槽位/分诊/病历/检索），前端删掉结论卡并把重开移到侧栏「+」，前台升 `1.7.27`；同日修复维语模式下预问诊仍用中文作答（前端补发 `ui_lang`、引擎按界面语言作答并对维语走双向翻译），前台升 `1.7.28`；同日后台「模型与提示词」矩阵补预问诊的两个模型位（翻译 / 推理，写到 `previsit.overrides.yaml` 并即时重载引擎），后台升 `0.9.62`；同日再修维语会话翻译链断掉后静默把中文推给患者（翻译客户端改为引擎自检懒装配、双客户端链 + 译文质量闸门、拿不到合格译文直接报错；面板运行时改报真实翻译状态），前台升 `1.7.29`、后台升 `0.9.63`；同日三修：语言切换把进行中的维语问诊打成中文（`reply` 改为会话语言优先、开诊语言钉进 `pv.lang`、切语言重开问诊），前台升 `1.7.30`；同日四修：中文电脑打开站点直接落在满屏维语界面、服务预热期 `/api/info` 连不上时摊一个双语错误页且不再重试（用户原话「根本用不了」）。改为界面语言按「上次手动选择 > 浏览器语言 > 站点主语言」决定、启动期先显示等待遮罩再自动重试与刷新，前台升 `1.7.32`；同日五修：开诊请求在途时切换语言，中文那次响应仍会落地画进维语界面（`pv.gen` 代号丢弃过期响应、切语言无条件重开、进入预问诊时语言不符即重开；预问诊气泡改存 `content` / `content_int` 两份并按界面语言挑一份渲染），前台升 `1.7.33`；同日六修：把预问诊那套自建语言系统并回问答板块的口径（作答语言按请求算、每条 turn 记 `lang` / `translated`、双语气泡按 `altMode` 渲染、切语言只重画不重开、翻译失败降级为原文 + 本地化提示而不再 503），前台升 `1.7.34`；同日七修：进预问诊先空等一次模型生成 + 一次翻译才看到开场问候（用户原话「你这个不是固定的话吗，别读时间了」）。开场白改回界面固定文案（`t.pvHello`，与另两个板块的欢迎语同源），进入即显示、切语言原地跟着换、不发任何请求；患者说出第一句才 POST `/start` 建会话；`start` 空主诉不再调模型；系统提示词同步删掉「开场先问候」的指令，模型不再重复自我介绍，前台升 `1.7.35`；同日八修：切语言时已有回答没有目标语言版本，患者看到的是「中文气泡 + 某某语作答」甚至空白（用户原话「这两个切换每次内容不统一，我让你参照维医问答的方式，实现翻译」）。按维医问答的口径改成**翻译同一句话**：`turn` 新增 `content_ug`（维语版本），切到维语时 `ChatEngine.localize()` 把缺译文的答复并发译过去写回库、只译一次，前端 `pvLocalize()` 异步补齐、期间挂「正在译为维吾尔语…」；语言标记改为「没有目标语言版本才挂」，译好即消失。顺带修掉首句发出后输入框不清空，前台升 `1.7.37`（现行）。
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
