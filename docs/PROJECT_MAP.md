# PROJECT_MAP · 仓库地图

> 本篇只回答「文件在哪、各自干什么、想改功能动哪里」。架构原理见 [ARCHITECTURE.md](ARCHITECTURE.md)，安装与启动见 [RUNBOOK.md](RUNBOOK.md)，日常使用见 [USER_GUIDE.md](USER_GUIDE.md)，技术分层见 [TECH_STACK.md](TECH_STACK.md)。所有路径、模块职责、端口、优先级、状态以现行源码与配置为准；2026-10 月实现期的决策用过去时并标日期。
>
> 术语约定（英文键名首现括注中文）：provider（后端）、role（角色）、section（板块）、kb（知识库）、rerank（精排）、refusal（拒答）、support（支撑度）、degraded（降级标记）、calibration（标定）、dense（稠密向量路）、sparse（稀疏路）。FTS5 是 SQLite 全文索引虚表，WAL 是预写日志，DPAPI 是 Windows 数据保护 API，SSE 是服务端流式推送，LRU 是最近最少使用缓存，MMR 是最大边际相关去重，CrossEncoder 是交叉编码器精排模型。

## 1. 一句话

KnowTune 是本地单机知识库问答系统：一个 FastAPI 进程（`api/app.py` 装配 + `api/routes/` 8 个模块）装下后台全部 HTTP 接口与前台问答站，SQLite + FTS5 存库，本地嵌入模型 + 双路召回 + CrossEncoder 精排，外接 OpenAI 兼容后端生成带引用的答案；预问诊作为第三个板块挂在前台 `/previsit` 下。源码用 `src/knowtune`（主项目）与 `src/previsit`（预问诊独立实现）两个顶层包，由 `pyproject.toml` 一起打进 wheel，入口 `knowtune = knowtune.cli:main`。

它不是 Agent：检索链是固定流水线，分支只有若干布尔开关（精排、MMR、拒答、邻块扩展），没有让 LLM 决定下一步调什么工具。

## 2. 运行时全景

一条命令 `knowtune serve --port 8013` 只拉起**一个进程**。CLI 先按实际端口把固定网关地址写进环境变量，交给 uvicorn 加载 `knowtune.api.app:app`；模块导入期就构造好全局 `AppState`（配置、DB、嵌入模型、向量/稀疏索引、精排器、检索引擎、导入流水线、LLM 客户端、`InstanceManager`）。`lifespan` 里恢复密钥、起嵌入预热线程、调 `start_autostart()`；后者在**同一进程内**为前台起一条 daemon 线程跑第二个 uvicorn，监听 8800，并把预问诊子应用挂到该站点的 `/previsit`。所以后台与前台共享同一份嵌入模型、向量缓存、检索器与 LLM 客户端。

```
  外部资产（本机磁盘 / 网络）                         ┌────────────────────────────┐
  ┌───────────────────────────────────────────┐  │ provider：siliconflow /     │
  │ data/models/bge-small-zh-v1.5  嵌入 512 维 │  │ local(127.0.0.1:8097) /    │
  │ data/models/BAAI--bge-reranker-base  精排  │  │ deepseek  →  OpenAI 兼容    │
  │ data/library.db（WAL）  8 表 + FTS5        │  └────────────▲───────────────┘
  │ data/secrets.json（DPAPI）、calibration/   │               │ HTTP
  │ data/previsit.db、data/previsit/kg.db      │               │
  └───────────────▲───────────────────────────┘               │
                  │ 加载 / 读写                               │
  cli.py          │                                          │
  cmd_serve ──────┤ 写 KNOWTUNE_GATEWAY=<host>:8013/v1        │
                  ▼                                          │
  ┌───────────────────────────────────────────────────────────┴──┐
  │ uvicorn(127.0.0.1:8013)  knowtune.api.app:app                 │
  │   导入期 state = AppState()（api/state.py）                   │
  │     cfg / Database / Embedder / VectorStore / SparseIndex /     │
  │     Reranker / RetrievalEngine / IngestPipeline / LLMClient     │
  │     sites = InstanceManager                                     │
  │   lifespan（api/app.py）：attach 密钥 → 预热线程 → start_autostart │
  │   后台接口 /api/*（全部 HTTP）+ /v1/* 转发网关                    │
  │   后台静态 api/static/*（构建号 0.9.67）                         │
  │   /v1/chat/completions = 纯转发，不进 RAG                        │
  └───────────────────┬────────────────────────────────────────────┘
                      │ runner.start()（runner.py:296）
                      ▼
  ┌──────────────────────────────────────────────────────────────┐
  │ 前台线程：同进程 daemon uvicorn(127.0.0.1:8800)               │
  │   frontsite/site.py：/、/api/info、/api/ask、/api/feedback     │
  │   frontsite/static/*（构建号 1.7.37）                          │
  │   └─ app.mount("/previsit")                                   │
  │        _SectionGate（pv 开关未开 → 404）                        │
  │        └─ previsit.app（只有 API，没有页面）                     │
  └──────────────────────────────────────────────────────────────┘
```

**`/v1/chat/completions` 是纯转发网关**（`api/routes/models.py`）：解析请求体、按选中的 provider 拼上游地址、把 body 原样 POST 出去，流式/非流式都只是转字节，不碰检索、不碰生成链。RAG 入口只有后台 `/api/ask` 与前台 `/api/ask` 两处。

## 3. 顶层目录为什么存在

| 目录 / 文件 | 为什么存在 |
|---|---|
| `src/knowtune/` | 主项目全部代码：后台接口、检索/生成链、配置与存储、前台站点、预问诊桥接层。 |
| `src/previsit/` | 预问诊**独立实现**（v0.4.0），刻意零 `knowtune` 依赖，可单独部署；wheel 里与 `knowtune` 并列打包。 |
| `config/` | 出厂配置、本机覆盖、后端注册表、实例/板块、提示词资产与预问诊领域配置，都是随版本库走的文本。 |
| `scripts/` | 三个开发期辅助脚本（假后端、下载 llama-server、截图），不参与运行时。 |
| `docs/` | 面向人的文档，`docs/` 下 6 份 + 根 [README](../README.md) 共 7 份，各司其职见末节。 |
| `pyproject.toml` | 打包与依赖分组（`extract` / `local` / `onnx` 可选未启用 / `dev`），入口与版本号。 |
| `data/` | 运行数据：库、上传副本、本地模型、密钥、标定、会话库。**体积大且含个人内容，被 `.gitignore` 整目录排除，不进版本库**，clone 后由程序自建并自行下载模型。 |
| `tools/llama/` | llama.cpp 可执行文件（约 100MB，按需由 `scripts/get_llama.py` 下载），**被 `.gitignore` 排除**，不是项目代码。 |
| `_pm/` | 立项/需求类过程文档与内部基线，**被 `.gitignore` 排除，不是项目目录**，不参与构建与运行；`.workbuddy/`、`_shot/`、`.pytest_cache/`、`.venv/`、`.idea/` 同属本机环境或过程产物。 |

## 4. 逐文件说明（对照 `find src -type f` 真实清单）

下列表格只列源码与配置源文件。`__pycache__/*.pyc` 是字节码缓存，不列；`evaluate.py`、`frontsite/evalboard.py` 已随评测台迁出为独立项目，只残留 `.pyc`，**不是现行文件**，不要照着旧清单写。每张表一行一个文件。

### 4.1 `src/knowtune/` 顶层模块

| 文件 | 职责 | 关键约束 |
|---|---|---|
| `src/knowtune/__init__.py` | 包标记，声明 `__version__ = 0.1.0`。 | 与 `pyproject.toml` 的 `version=0.1.0` 一致。 |
| `src/knowtune/cli.py` | 命令入口 `init/status/kb/ingest/ask/debug/serve`；`cmd_serve`（:226）注入 `KNOWTUNE_GATEWAY` 后 `uvicorn.run("knowtune.api.app:app")`。 | 网关地址按实际 `--port` 写（:234），不是写死 8000；`cli.py` 默认端口是 8000，现行启动用 `--port 8013`，默认 host `127.0.0.1`。 |
| `src/knowtune/config.py` | 加载 YAML、按库标定覆盖、模型指纹校验。 | 相对路径一律相对**项目根**，不随 CWD 变；标定生效需 kb_id 非空 + 文件存在 + JSON 可读 + 指纹等于 `embedding.model@dim`（:495-510）。 |
| `src/knowtune/kbconfig.py` | 库级参数覆盖 + 出厂基线比对。 | `kb.config_json` 存**全量快照**并标 `__full__`；「用户手动改过」判据是与出厂基线值不同，不是「键存在」（`kbconfig.py`）。 |
| `src/knowtune/settings.py` | 可调参数的声明式注册表（边界/步长/说明/生效方式），供 `/api/settings` 直接渲染。 | 三层取值（高→低）：运行时覆盖 > `config/local.yaml` > `config/default.yaml`；加参数只改此文件，前端自动多一行。 |
| `src/knowtune/modelcfg.py` | `config/models.yaml` 端点注册表读写。 | 端点只分 `local` / `openai` 两类（历史上八种 kind 已砍）；**密钥不写进 models.yaml**。 |
| `src/knowtune/promptcfg.py` | 提示词覆盖层（provider / scope），带 history / rollback / preview。 | 解析链以 `effective_fields():337-344` 为准：`by_provider > by_scope > default > 内置`；仅 `generate` / `translate` 两个 role 支持前两层，`copy_ug` / `copy_zh` 只读角色级。 |
| `src/knowtune/roles.py` | 后端 / 角色解析（`translate` / `generate` / `copy_ug` / `copy_zh`）。 | 链（:81-111）：板块级 roles → 站点级 roles → 系统级 roles → 实例 provider → 默认 provider，每级用已知后端名校验，不合法继续下退。 |
| `src/knowtune/schema.sql` | 建表 DDL。 | 8 张实体表 + 1 张 FTS5 虚表 `chunk_fts`（另有 5 张 FTS5 影子表）；`journal_mode=WAL` / `foreign_keys=ON` / `synchronous=NORMAL`；时间统一 UTC ISO8601 字符串。 |
| `src/knowtune/db.py` | SQLite 连接管理。 | `threading.local` 每线程一连接（FastAPI 同步路由跑在线程池），WAL 下读写并行，不拼锁。 |
| `src/knowtune/secrets.py` | 密钥内存为主、落盘密文。 | 默认保存（2026-11-01 由「只活内存」反转）；Windows DPAPI 用户态，密文绑当前账户 + 本机，换账户/换机器/拷走都解不开；`secrets.off` 仅本次会话可关。 |
| `src/knowtune/services.py` | 后端服务探测，回答「现在到底能不能用」。 | 两段式：先打 `/models` 判端口，再发 `max_tokens=1` 补全判模型真加载（网关对 `/models` 常回 200 全列表）；结果带内存缓存且**落盘**。 |
| `src/knowtune/localmodels.py` | 扫 `.gguf`、找 / 起 / 停 llama-server。 | 一个模型一个进程一个端口；llama.cpp 不支持热切换，「选模型」在本地语境下等于「换端口」。 |
| `src/knowtune/chatctx.py` | 上下文占用核算：这轮还剩多少给历史。 | 只依赖标准库、按字符数粗略当 token，与 `generate/prompt.py` 口径一致；刻意不塞进 `app.py`（后者 import torch 要 4.5 秒）。 |
| `src/knowtune/dashboard.py` | 首页数据聚合（资产、检索健康、服务状态、日志）。 | 只读不写、不做任何网络探测；首屏须 50ms 内出。 |
| `src/knowtune/lang.py` | 语言识别与跨语言问答。 | 中文嵌入模型编码不出维吾尔语语义：维语提问必须先译成中文再检索；生成须换维语系统提示，否则模型照中文资料吐中文。 |
| `src/knowtune/logbus.py` | 内存环形日志（最近 400 条），首页「系统日志」数据源。 | 进程重启即清空；要长期留存看 `query_log` 表，环形缓冲是「现场」。 |
| `src/knowtune/models.py` | 跨模块轻量数据结构（`Block` 等 dataclass）。 | 刻意不放任何业务逻辑。 |
| `src/knowtune/paths.py` | 路径判定纯函数（如 `source_missing`）。 | 单独成模块只为可测，避免单测背 `app.py` 的启动成本。 |
| `src/knowtune/textnorm.py` | 码位归一化：把 CJK 部首区码位修回汉字。 | 不修会直接废掉检索——jieba 切不出、bge 词表无这些码位；不是排版问题。 |

### 4.2 `src/knowtune/ingest/` 导入

| 文件 | 职责 | 关键约束 |
|---|---|---|
| `src/knowtune/ingest/__init__.py` | 包标记。 | 空。 |
| `src/knowtune/ingest/loader.py` | 多格式抽取 → （纯文本 + 结构块）。 | 抽取失败必须给人能读懂的原因，绝不静默返回空文档；扫描件 PDF 无文本层是最典型的坑。 |
| `src/knowtune/ingest/cleaner.py` | 导入前清洗，作用于抽取之后、切分之前的内存块。 | **源文件一个字节都不改**；每条规则报「删了几块 / 多少字 / 哪些行」并附样例，供预览逐条核对。 |
| `src/knowtune/ingest/chunker.py` | 分块：结构优先、字符兜底。 | 先按标题/段落聚合，单段超长才退化字符切；`size/overlap` 改动必须进评估集回归，不能凭手感。 |
| `src/knowtune/ingest/pipeline.py` | 导入流水线：抽取 → 切分 → 分词 → 嵌入 → 落库 → 原子切换。 | 状态机 `pending → indexing → ready | failed`；旧 `active_gen` 在新代次完全就绪前一直是 active，结构上杜绝「检索到半成品」。 |

### 4.3 `src/knowtune/index/` 索引

| 文件 | 职责 | 关键约束 |
|---|---|---|
| `src/knowtune/index/__init__.py` | 包标记。 | 空。 |
| `src/knowtune/index/embedder.py` | 本地嵌入模型封装（默认 `BAAI/bge-small-zh-v1.5`，512 维）。 | CPU 优先；模型缺失不抛到调用方炸链路，上层把 dense 路降级、用稀疏路兜底并明确告知「当前为降级检索」。 |
| `src/knowtune/index/vectors.py` | 内存矩阵 + 暴力点积向量检索。 | 规模 ≤10 万 chunk 时 ANN（FAISS/HNSW）是负资产；向量存 `embedding` 表 BLOB，与 chunk 同库同事务，不要「db 与 .f32 对不上」。 |
| `src/knowtune/index/sparse.py` | jieba 预分词 + SQLite FTS5 稀疏检索。 | 中文必须预分词后写入 `text_seg`（FTS5 默认 unicode61 把整句当一个 token）；`bm25()` 返回负数、越小越相关，必须 `ORDER BY bm25(...) ASC`。 |

### 4.4 `src/knowtune/retrieve/` 检索

| 文件 | 职责 | 关键约束 |
|---|---|---|
| `src/knowtune/retrieve/__init__.py` | 包标记。 | 空。 |
| `src/knowtune/retrieve/engine.py` | 检索编排：双路召回 → 加权融合 → 邻块扩展 → 精排 → MMR → 拒答判定。 | 每段都可失败且失败后必须有明确去向（降级 + 打标），不允许「某一路静默返回空」这种看起来正常、实际召回腰斩的状态；融合方法以 `config/default.yaml` 现行的 `weighted` 为准。 |
| `src/knowtune/retrieve/rerank.py` | CrossEncoder 精排、MMR、4096 条进程内 LRU 打分缓存。 | 当前三个知识库 `retrieval.rerank.enabled` 均为 `true`，**精排实际在跑**，`allow_download: false` 时缺失只降级不联网补；超时把同步 `predict` 丢单线程执行器 `wait(timeout)` 后放弃等待走降级；缓存键为 (模型, 问题摘要, 片段摘要)，换问法不命中、重启即空。 |

### 4.5 `src/knowtune/generate/` 生成

| 文件 | 职责 | 关键约束 |
|---|---|---|
| `src/knowtune/generate/__init__.py` | 包标记。 | 空。 |
| `src/knowtune/generate/prompt.py` | 上下文装填与提示词（`build_context`）。 | 超预算按相关度从低到高**丢整条**，绝不从中间截断单条；编号一旦确定不再重排，前端点 [3] 要精确回到第 3 条。 |
| `src/knowtune/generate/llm.py` | LLM 客户端：一个 OpenAI 兼容协议、三档后端。 | 超时须显式数值且 read 单独给；重试只允许发生在**首 token 之前**；云端 profile 出站前做 PII 扫描、本地跳过；`disable_thinking: true`、SSE 流式。 |
| `src/knowtune/generate/cite.py` | 引用校验：越界编号剥离 + 覆盖率统计。 | 只删不改，任何改写都等于二次创作；覆盖率只验证格式，不验证内容。 |
| `src/knowtune/generate/evidence.py` | 检索证据分档，把召回质量显式告诉模型。 | 分档口径（2026-11-06 改）：资料答到 → 带 [n] 照资料说；没答到 / 只答一半 / 讲别的病 → 用自身知识把问题完整答完，不再写满屏「资料中未找到」。 |
| `src/knowtune/generate/support.py` | 答案支撑度：句子与其引用块各自编码算余弦，判「引到了没有」。 | 补 `cite.py` 只验格式之缺；**只在后台 `/api/ask` 链路调用**，前台 `/api/ask` 不调用 `support.evaluate`，不得写成「每次问答都算支撑度」。 |
| `src/knowtune/generate/think.py` | 剥掉模型吐出的 `<think>…</think>` 思考块。 | 放在 LLM 层做，`stream` / `complete` 两条路都盖住；避免中文草稿在 RTL 维语容器里显示成倒字。 |

### 4.6 `src/knowtune/api/` 后台

| 文件 | 职责 | 关键约束 |
|---|---|---|
| `src/knowtune/api/__init__.py` | 包标记。 | 空。 |
| `src/knowtune/api/app.py` | FastAPI 装配入口（约 270 行）：生命周期、静态资源、no-cache 中间件、`include_router` 注册 8 个路由模块、全局异常处理。 | 端点本体不在这里；全局 `state` 由 `api/state.py` 在导入期构造。 |
| `src/knowtune/api/routes/*.py` | 94 个端点按资源域拆成 8 个模块：meta / models / kb / docs / search / sessions / prompts / sites。 | `/v1/chat/completions` 在 `models.py`，是**纯转发网关**；「手动改过」判据在 `kbconfig.py`。 |
| `src/knowtune/api/common.py` / `state.py` / `coverage.py` | 公共层：分片上传暂存 / 全局 `AppState` / 引用覆盖率统计。 | 由各路由模块共享，不放端点逻辑。 |
| `src/knowtune/api/static/index.html` | 后台 UI 骨架，构建号 `0.9.67`（`window.ADMIN_REQ_BUILD`）。 | 与前台静态资源完全不共享；资源 URL 带 `?v=0.9.67` 破缓存。 |
| `src/knowtune/api/static/app.js` | 后台交互逻辑，构建号 `0.9.67`（`window.ADMIN_BUILD`）。 | 参数控件由 `/api/settings` 吐出的注册表渲染，前端不写业务含义。 |
| `src/knowtune/api/static/style.css` | 后台样式。 | 由 `index.html` 以 `style.css?v=0.9.67` 引用。 |

### 4.7 `src/knowtune/frontsite/` 前台问答站

| 文件 | 职责 | 关键约束 |
|---|---|---|
| `src/knowtune/frontsite/__init__.py` | 包标记 + 导出 `InstanceSpec` / `load_all` / `next_free_port` / `InstanceManager`。 | 定位「共用内核、分开表皮」：检索器/嵌入/LLM 复用后台，界面与接口另起一套。 |
| `src/knowtune/frontsite/instcfg.py` | 实例配置（一个实例 = 一个独立问答站点），读 `config/instances.yaml`。 | 端口范围 8800–8899（`PORT_MIN/MAX`）；与 `models.yaml` 分开存，避免改模型带出站点配置。 |
| `src/knowtune/frontsite/runner.py` | `InstanceManager` 启停；`start_autostart()`（:495）。 | 每实例 = 一条 daemon 线程里一个 uvicorn（:360），**同进程、不另起进程**；理由（2026-10-28）：嵌入模型只加载一份、日志/配置/队列集中、Windows 无 fork；代价是单实例崩溃带走后台，靠 uvicorn 异常隔离 + 启动前端口探测压住。 |
| `src/knowtune/frontsite/site.py` | 前台问答站点：`/`、`/api/info`、`/api/ask`、`/api/feedback`，以及挂载 `/previsit`。 | 问答流程与后台 `/api/ask` 同构但**刻意不共用代码**（不要会话预算/调试面板/审计日志）；`app.mount("/previsit", _SectionGate(...))`（:333），`_SectionGate`（:308）按 `pv` 开关做 404 拦截；挂载失败吞异常但写 ERROR 日志，不拖垮站点。 |
| `src/knowtune/frontsite/static/index.html` | 前台 UI 骨架，构建号 `1.7.37`（`window.SITE_REQ_BUILD`）。 | 维吾尔语 RTL，与后台静态资源完全隔离；资源 URL 带 `?v=1.7.37`。 |
| `src/knowtune/frontsite/static/app.js` | 前台交互逻辑，构建号 `1.7.37`（`window.SITE_BUILD`）。 | 切换语言时不允许任何元素换边；引用牌做连续编号区间合并。 |
| `src/knowtune/frontsite/static/style.css` | 前台样式。 | 由 `index.html` 以 `style.css?v=1.7.37` 引用。 |

### 4.8 两套 previsit 目录的关系（先讲清，再列表）

**不是重复实现，也不是「两套预问诊」**。`src/previsit/` 是实现真身（v0.4.0，零 `knowtune` 依赖，能独立部署）；`src/knowtune/previsit/` 是**桥接 + 装配层**：五个同名文件是纯 `from previsit.xxx import ...` 再导出，`app.py` 只负责把主项目这一侧的东西接上去。运行时实际执行的是 `src/previsit/` 的代码，由 `frontsite/site.py:331-336` 挂到 `/previsit`，并用 `_SectionGate` 按 `pv` 开关做 404 拦截；预问诊**没有独立站点**，对外全路径是 `/previsit/api/previsit/*`。

| 文件 | 职责 | 关键约束 |
|---|---|---|
| `src/knowtune/previsit/__init__.py` | 桥接包标记，说明实现已整体迁到顶层 `previsit` 包。 | 只做两件事：向后兼容再导出 + 装配，不含业务逻辑。 |
| `src/knowtune/previsit/app.py` | **主项目装配层**：LLM 适配器、密钥、会话库，建 FastAPI app。 | 一行路由都不写，全从 `previsit.app.create_app(engine)` 来；另给后台参数面板提供 `config_describe/save/reset`。 |
| `src/knowtune/previsit/config.py` | 桥接：`from previsit.config import ...` 纯再导出。 | 不是实现；独立包根目录解析优先 `PREVISIT_HOME`。 |
| `src/knowtune/previsit/engine.py` | 桥接：`from previsit.chat import ChatEngine`，并保留旧名 `PrevisitEngine = ChatEngine`。 | 不是实现。 |
| `src/knowtune/previsit/store.py` | 桥接：`from previsit.store import SCHEMA, Store` 纯再导出。 | 不是实现。 |

### 4.9 `src/previsit/` 实现真身

| 文件 | 职责 | 关键约束 |
|---|---|---|
| `src/previsit/__init__.py` | 独立包标记，声明 v0.4.0，说明数据面只有 `previsit.yaml` + `previsit.db`。 | 不 import 主项目任何东西（config / secrets / LLMClient / 检索链都不碰）。 |
| `src/previsit/app.py` | 预问诊 HTTP 入口，**只有 API 没有页面**：`/api/previsit/meta\|start\|reply\|session\|config*`。 | 界面画在维医站点第三个板块，不另开页面、不另起端口；后台 `/api/previsit/*` 调同一套函数。 |
| `src/previsit/chat.py` | **壳子本体**（约 200 行）：开会话、存流水、把系统提示词 + 完整历史交给模型。 | 没有槽位/分诊/病历；模型接不上就抛 `ChatError`，**不做规则兜底**。 |
| `src/previsit/config.py` | 读 `config/previsit.yaml`，读成普通 dict。 | 刻意不复用主 Config；唯一提示词是 `prompts.chat`，写空自动退回默认那句。 |
| `src/previsit/settings.py` | 运行时可改参数的定义（模型、上限、开关、界面文案、提示词）。 | 后台「站点 → 预问诊」改的就是这批字段，经 `apply_config()` 灌入，不直接改 `previsit.yaml`。 |
| `src/previsit/llm.py` | 自带最小 OpenAI 兼容客户端。 | 刻意不 import 主项目 LLMClient（否则「独立部署」成空话）；配置全来自环境变量 `PREVISIT_LLM_BASE_URL` / `PREVISIT_LLM_GATEWAY` / `KNOWTUNE_GATEWAY`。 |
| `src/previsit/retriever.py` | 语义召回：把患者原话发给后端知识库检索接口，从标题解析域/槽位/病种。 | 自己**不带向量模型**（不把 torch / sentence-transformers 拖回来）；`InProcessRetriever` 供主服务挂载时进程内查库，`config/previsit.yaml:86` 的 `http://127.0.0.1:8000` 只在独立部署时生效。 |
| `src/previsit/store.py` | 预问诊档案存储（SQLite 单文件 `data/previsit.db`）。 | `session` / `turn` 两表；档案用 JSON 列而非一堆字段，槽位配置驱动、加槽位不用写迁移。 |

### 4.10 `config/`

| 文件 | 职责 | 关键约束 |
|---|---|---|
| `config/default.yaml` | 出厂参数唯一权威，带「为什么是 480 不是 512」这类注释。 | 程序不写坏它；分块、检索、融合、精排、拒答、生成、会话参数都在这里。 |
| `config/local.yaml` | 用户调参结果（差异项）。 | 与 `default.yaml` 分开，出厂注释永不被程序覆盖。 |
| `config/models.yaml` | 后端注册表（OpenAI 兼容地址 + 模型清单）。 | **不写密钥**；密钥在 `data/secrets.json`。 |
| `config/instances.yaml` | 前台实例与三板块配置。 | 当前只有一个实例 `uyghur-med`：`port: 8800`、`autostart: true`、`kb_id: 2`、`classic_kb_id: 4`；`sections` 定义 `med` / `classic` / `pv`，`sections.*.roles` 当前均未配置。 |
| `config/prompts.yaml` | 提示词覆盖层（provider / scope）。 | 只记录与内置默认不同的字段；当前实配 `translate.by_provider.siliconflow.system`、`copy_zh.default.samples`，`overridden_scopes` 为空（当前没配板块级覆盖）。 |
| `config/prompts/qa_v1.md` | `generate` 角色内置中文提示词，版本化资产。 | 按 `prompt_version` 选文件，不搬进数据库（搬走就丢了版本切换）。 |
| `config/prompts/qa_ug.md` | `generate` 角色维语提示词。 | 按回答语言切到这里。 |
| `config/previsit.yaml` | 预问诊领域配置：槽位、危险词、分诊规则、提示词。 | 手写文档、字段上压着设计注释；`:86` 的 `retrieval.url` 只在独立部署时生效，模型一律跟随 `/v1` 固定入口、不在这些文件里钉后端。 |
| `config/previsit.overrides.yaml` | 预问诊运行时覆盖。 | 由后台「前台站点 → 预问诊参数」写回。 |
| `config/prompts/_history/` | 目录：提示词每次保存前的自动回滚快照。 | 提示词 history / rollback 功能写入，**非配置源、被 `.gitignore` 排除、不进版本库**。 |

### 4.11 `scripts/` 与 `docs/`

| 文件 | 职责 | 关键约束 |
|---|---|---|
| `scripts/fake_llm.py` | 假的 OpenAI 兼容端点，专门验证「生成 → 引用 → 支撑度」链路。 | `good` 模式抄资料（应判 supported）、`bad` 模式编数字（应判 weak/unsupported 并被锚点抓到）；不是给人看的回答生成器。 |
| `scripts/get_llama.py` | 下载 llama-server 到 `tools/llama`。 | 查 tag、选平台/后端资产、镜像兜底下载并解压；国内 github.com release 常 502，与 `api.github.com` 是两个网络路径。 |
| `scripts/shot_models.py` | 截「模型服务」页的本地模型扫描面板，验证 llama-server 已被认到。 | 用系统 Edge（`channel=msedge`），不额外下 Chromium。 |
| `docs/ARCHITECTURE.md` | 现行架构、数据模型、检索/生成链、实测数据、需求对照、历史归档。 | 不写操作步骤。 |
| `docs/PROJECT_MAP.md` | 本篇：逐目录逐文件、启动顺序、配置优先级、改动落点。 | 不写实测指标。 |
| `docs/RUNBOOK.md` | 从零 clone 到三板块可用：依赖顺序、模型路径、DB 初始化、绑库、判据、排障、备份。 | 不写架构原理。 |
| `docs/USER_GUIDE.md` | 日常使用：建库导入、提问读引用、参数含义、指标、使用侧限制。 | 不写安装顺序细节。 |
| `docs/TECH_STACK.md` | 技术清单与分层、三个关键决策、启动速记。 | 不写需求对照。 |
| `docs/WBS.html` | 交付时的工作包与范围缺口**快照**。 | 明确标注「历史快照」，不冒充现行状态表。 |

### 4.12 代码 / 注释已知不一致（以代码为准）

下列三类曾被记录为「注释说的」与「代码做的」不一致。文档如实点名，但**一律以代码为准**；这些注释已在本次重写中按代码口径修正，现行源码不再是旧描述：

1. **提示词优先级注释**：`src/knowtune/promptcfg.py` 早期头注释与 `save()` docstring 曾声称 scope（板块）优先于 provider（后端），`config/prompts.yaml` 头注释曾漏写 `by_scope`。以代码为准：`effective_fields():337-344` 是 `by_provider > by_scope > default > 内置`，且仅 `generate` / `translate` 支持前两层。
2. **`schema.sql` 注释**：`kb` 表 `config_json` 的早期注释曾写「只存与全局不同的项」。以代码为准：`src/knowtune/kbconfig.py` 实现是**全量快照 + `__full__`**，「用户手动改过」的判据是与出厂基线值不同（`kbconfig.py`），不是键存在。
3. **前台构建号**：前台静态资源版本号（`style.css?v` / `app.js?v` / `SITE_REQ_BUILD` / `SITE_BUILD`）曾在多处不一致。以代码为准：当前前台统一 `1.7.37`、后台统一 `0.9.67`；两套静态资源各自独立，互不影响。

## 5. 项目是怎么「构建」起来的

依赖自底向上五层，接口 / 前端是顶层消费者：

1. **配置**：`config/default.yaml` → `config/local.yaml` → `kb.config_json` 全量快照 → 按库标定；`config/models.yaml` 给后端，`config/instances.yaml` 给站点与板块。`src/knowtune/config.py` / `kbconfig.py` / `settings.py` / `modelcfg.py` / `promptcfg.py` / `roles.py` 逐层解析。
2. **存储**：`src/knowtune/schema.sql` 建表，`db.py` 管连接，`secrets.py` 管密钥，`data/library.db` 是唯一持久化库。
3. **索引**：`ingest/` 把文件变成 chunk，`index/embedder.py` 出向量、`index/sparse.py` 出 `text_seg`，`index/vectors.py` 在内存拼矩阵。
4. **检索**：`retrieve/engine.py` 编排双路召回、融合、精排、拒答；`retrieve/rerank.py` 出精排与 MMR。
5. **生成**：`generate/prompt.py` 装填上下文，`llm.py` 调后端，`cite.py` / `evidence.py` / `support.py` / `think.py` 做校验与后处理。
6. **接口 / 前端**：`api/app.py` 做装配，`api/routes/` 8 个模块暴露后台 HTTP 与 `/v1` 网关；`frontsite/` 起前台线程并挂 `/previsit`。

**为什么嵌入模型只加载一次**：前台是主服务**同进程内的 daemon 线程**（`runner.py` 起 uvicorn），`site.py` 直接复用 `AppState` 里已构造好的 `Embedder` / `VectorStore` / `SparseIndex` / `RetrievalEngine` / `LLMClient`。若改成每个前台实例一个子进程，要么每个站点各加载一次（多占几百 MB、还可能加载失败），要么加一套 IPC 让子进程回主进程检索——后者才是真正的冗杂。这是 2026-10-28 定下的取舍，代价是单实例崩溃会带走后台进程。

## 6. 两条主链路

**导入链路**：`/api/import/path|text|url|upload` 等入口 → `src/knowtune/ingest/pipeline.py` 的 `IngestPipeline` → `loader.py`（抽取，失败给可读原因）→ `cleaner.py`（内存清洗，不写回源文件）→ `chunker.py`（结构优先切块）→ `sparse.py` 预分词 + `embedder.py` 编码 → 落 `chunk` / `embedding` / `chunk_fts` → 原子切换 `document.active_gen`。`document.status` 走 `pending → indexing → ready | failed`；旧代次在新代次就绪前一直是 active。清洗方案存 `clean_json`，重索引沿用。

**问答链路 · 后台 `/api/ask`**：`effective_cfg`（配置 + 库级全量快照 + 按库标定 + 手动判据）→ `retrieve/engine.py` 的 `search`（dense + sparse → 加权融合 → 邻块扩展(关) → 精排 → MMR(关) → 拒答判定）→ 拒答分支 → `generate/prompt.py` 的 `build_context(provider=..., scope=...)` → `llm.stream` → `normalize_cites` → `cite.verify` → `verbatim_ratio` → `support.evaluate` → SSE `done`。降级标记写进 `query_log.degraded`。

**问答链路 · 前台 `/api/ask`**：`lang.py` 翻译提问 → 按板块定 `doc_ids` → `engine.search` → `evidence.py` 证据分档 → `build_context(provider=..., scope=...)` → 两段式生成 / 翻译 → `normalize_cites` → `cite.verify` → `done`。**前台不调用 `support.evaluate`**；支撑度目前只在后台 `/api/ask` 链路上算，不能写成「每次问答都算支撑度」。

**`/v1/chat/completions` 不进上述任何链路**，只转发到选中的 OpenAI 兼容后端。

降级标记（`retrieve/engine.py` + `retrieve/rerank.py`）：`dense_unavailable` / `dense_disabled` / `sparse_failed` / `mmr_failed` / `refusal_uncalibrated` / `llm_error:`；精排六类 `rerank_unavailable`（模型在失败集或未装 sentence-transformers）、`rerank_model_missing`（本地权重找不到）、`rerank_load_failed`、`rerank_timeout`（>8s）、`rerank_failed`、`rerank_bad_output`。`rerank_unavailable` **不是**「模型没下载」的唯一标记，本地权重缺失对应的是 `rerank_model_missing`。

## 7. 启动顺序（`cli.py` → `AppState` → `lifespan` → `start_autostart()`）

1. `src/knowtune/cli.py` 的 `main` → `cmd_serve`（:226）：按实际 `--port` 写 `os.environ["KNOWTUNE_GATEWAY"]`（:234），打印地址，再 `uvicorn.run("knowtune.api.app:app", host, port)`（:244）。
2. uvicorn 导入 `knowtune.api.app`：**模块导入期**由 `api/state.py` 执行 `state = AppState()`。`AppState.__init__` 依次 `load_config()` → `Database` → `Embedder` → `VectorStore` → `SparseIndex` → `Reranker` → `RetrievalEngine` → `IngestPipeline` → `LLMClient` → `db.ensure_schema()` → `sites = InstanceManager(self)`。随后 `api/app.py` 构造 `app = FastAPI(..., lifespan=lifespan)` 并 `include_router` 注册 8 个路由模块。
3. `lifespan`（`api/app.py`）：`secrets.attach()` 恢复密钥 → 清理孤儿导入与过期分片 → `services.warm_cache()` 读回探测结论 → 起 `_warmup` daemon 线程（嵌入预热）与 `_autostart_local_models` daemon 线程（本地 GGUF 自启）→ `for name in state.sites.start_autostart()`（同步做，启动失败要有反馈）。
4. `InstanceManager.start_autostart()`（`frontsite/runner.py:495`）对 `autostart` 的实例逐个 `start()`（:296），在 daemon 线程里跑 uvicorn（:360）监听 8800；`frontsite/site.py` 建前台 app 并 `app.mount("/previsit", _SectionGate(...))`（:333）。
5. `yield` 后服务就绪；关停时 `state.sites.stop_all()`（:358）。前台线程随主进程退出，没有独立子进程要回收。

## 8. 配置优先级

- **参数**：`config/default.yaml`（唯一权威、带注释）→ `config/local.yaml`（用户改过的差异项）→ 库级 `kb.config_json`（**全量快照**，标 `__full__`）→ 按库标定文件覆盖。
- **手动判据**：「用户手动改过」是**与出厂基线值不同**，不是「键存在」（`kbconfig.py`）；否则全量快照会让标定永不生效。
- **后端与密钥**：注册表在 `config/models.yaml`；密钥在 `data/secrets.json`（Windows DPAPI，绑账户 + 本机，仅本次会话可用 `secrets.off`），**不写进 models.yaml**。
- **角色解析链**（`roles.py:81-111`）：**板块级 roles → 站点级 roles → 系统级 roles → 实例 provider → 默认 provider**，每级用已知后端名校验，不合法继续下退。当前 `sections.*.roles` 均未配置，所以 `med` 与 `classic` 的解析结果相同。
- **提示词解析链**（`promptcfg.py:337-344`，以代码为准）：`by_provider`（后端）> `by_scope`（板块）> `default`（角色默认）> 内置（builtin）。仅 `generate` / `translate` 两个 role 支持 `by_provider` 与 `by_scope`；`copy_ug` / `copy_zh` 只读角色级。
- **预问诊参数**：`config/previsit.yaml` + `config/previsit.overrides.yaml`，后台「前台站点 → 预问诊参数」写回；2026-11-07 起只剩界面文案、模型参数和一段 `prompts.chat`（槽位/分诊/病历/追问知识库已删）。现行钉在本机 `provider: local`，走网关的 `X-KnowTune-Provider` 头路由。

## 9. 想改功能，动哪里

| 需求 | 落点文件 |
|---|---|
| 改提示词 | `config/prompts.yaml`（覆盖层）、`config/prompts/qa_v1.md` / `config/prompts/qa_ug.md`（内置版本化）、`src/knowtune/promptcfg.py`（解析链）。 |
| 改检索参数 | `config/default.yaml`（出厂）/ `config/local.yaml`（本机）/ `src/knowtune/settings.py`（加参数）/ `src/knowtune/retrieve/engine.py` · `src/knowtune/retrieve/rerank.py`（改逻辑）。 |
| 加导入格式 | `src/knowtune/ingest/loader.py`（抽取器）+ `pyproject.toml` 的 `extract` 依赖分组；清洗规则看 `src/knowtune/ingest/cleaner.py`。 |
| 改前端 | 后台 `src/knowtune/api/static/index.html` · `src/knowtune/api/static/app.js` · `src/knowtune/api/static/style.css`；前台 `src/knowtune/frontsite/static/index.html` · `src/knowtune/frontsite/static/app.js` · `src/knowtune/frontsite/static/style.css`。两套完全隔离，改完各自同步构建号。 |
| 加板块 | `config/instances.yaml` 的 `sections` + `src/knowtune/frontsite/site.py`（路由 / 挂载 / `_SectionGate`）+ `src/knowtune/roles.py`（板块级 roles）。 |
| 改预问诊 | 参数：`config/previsit.yaml` · `config/previsit.overrides.yaml`；逻辑在 `src/previsit/chat.py`（壳子：提示词 + 模型）；装配在 `src/knowtune/previsit/app.py`。 |
| 换后端 | `config/models.yaml` + `src/knowtune/modelcfg.py`（注册表）+ `src/knowtune/roles.py`（角色绑定）；密钥在界面填，落 `data/secrets.json`，不进配置文件。 |

## 10. 数据目录与备份

`data/library.db` 是主库（WAL 模式，运行期还有 `library.db-wal` / `library.db-shm`）：8 张实体表 `kb` / `document` / `chunk` / `embedding` / `session` / `message` / `query_log` / `feedback`，加 1 张 FTS5 虚表 `chunk_fts`（及 5 张自动影子表）。`data/previsit.db` 是预问诊会话库，`data/previsit/kg.db` 是知识图谱，`data/models/` 放本地嵌入与精排权重，`data/uploads/` 放上传副本。

`data/calibration/` 是按库标定目录，命名 `data/calibration/kb_<id>.json`；**当前只有 `data/calibration/kb_2.json` 存在**，kb3 / kb4 无标定文件、回退出厂阈值。旧格式全局标定文件 `data/refusal_calibration.json` 已不生效（历史，主项目只读 `data/calibration/`）；标定工具已随评测台迁出为独立项目，本仓库不自带。

`data/secrets.json` 是 Windows DPAPI 密文，**绑当前账户 + 本机**：换账户、换机器、把文件拷走都只是一串解不开的字节，迁移后需在模型页重填密钥。整个 `data/` 被 `.gitignore` 排除，不进版本库；备份要连 `data/` 一起带走，但密钥密文换机不可解，别指望靠拷贝 `secrets.json` 免重填。

## 11. 这次重写清掉了什么

旧版 PROJECT_MAP 里已失效的口径，本次按类别清除，不再写成现行状态：

- **精排类**：把「模型未下载 / 精排未开启 / 直接降级 / 没有缓存 / 精排可选」当成现状。现行是三个库 `rerank.enabled=true`、精排实际在跑、有 4096 条进程内 LRU 打分缓存；限制是 CPU 延迟与语料适配，不是缺模型。
- **拒答与标定类**：把早期或上一轮的阈值 / AUC / 留一法组数当成现行值。现行是按库标定、只有 kb2 生效，其余库用出厂值；全局口径与按库口径分开说。
- **测试类**：「测试全绿」这类不带语料与样本数的断言一律不写。
- **支撑度类**：「支撑度随每次问答运行」。现行只有后台 `/api/ask` 算，前台不算。
- **降级标记类**：把 `rerank_unavailable` 当成「模型没下载」的唯一标记。现行精排降级有六类标记，各自语义不同。
- **写法类**：现状与实现期历史混写。现改为历史用过去时并标日期，注释与代码冲突时以代码为准（见 4.12）。

## 与其他文档的分工

- [README](../README.md)：项目入口——是什么、装、启、能力速览、文档地图、已知边界摘要；不写逐文件地图。
- [RUNBOOK.md](RUNBOOK.md)：从零 clone 到三板块可用的操作步骤与排障；不写架构原理。
- [USER_GUIDE.md](USER_GUIDE.md)：日常使用与指标读法；不写安装顺序细节。
- [ARCHITECTURE.md](ARCHITECTURE.md)：现行架构、数据模型、检索/生成链、关键决策与实测数据、需求对照、历史记录归档；不写操作步骤。
- [TECH_STACK.md](TECH_STACK.md)：技术清单与分层、三个关键决策、启动速记；不写需求对照。
- [WBS.html](WBS.html)：交付时的工作包与范围缺口**历史快照**，不冒充现行状态表。
- **本篇**：逐目录逐文件说明、启动顺序、配置优先级、改动落点；不写实测指标与需求对照数字。
