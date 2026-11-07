# KnowTune

本地单机知识库问答系统：文件进来，答案出去，每句结论都带得回原文块。检索链是固定流水线，不是 Agent——没有让大模型决定下一步调什么工具，分支只有若干布尔开关。

多格式导入 → 分块 → 本地嵌入 + FTS5 双路召回 → 加权融合 → CrossEncoder 精排 → 带引用编号的 LLM 生成 → 引用校验 / 支撑度 → 可检索日志。附带一个面向终端用户的前台问答站和一个预问诊引擎。数据全部落在本地 SQLite，生成模型接任意 OpenAI 兼容后端。

## 当前事实卡

| 项 | 现行值 | 条件 |
|---|---|---|
| 版本 | `0.1.0` | `pyproject.toml`，包名 `knowtune`，入口 `knowtune = knowtune.cli:main` |
| Python | ≥ 3.11 | `pyproject.toml` 的 `requires-python` |
| 端口 | 后台管理台 `8013` / 前台问答站 `8800` | 本文与 RUNBOOK 统一口径；`cli.py` 的 `serve` 默认 `--port 8000`，实际部署用 `--port 8013` |
| 数据 | 3 个知识库、32 篇文档全部 `ready`、28020 个 chunk、28020 条 512 维向量 | 实测 `data/library.db`，2026-11-07 采集 |
| 许可 | MIT | `pyproject.toml` |

事实卡里的端口、库数、文档数都是当前跑起来的服务的实测值，不是设计目标。换一台机器、换一批语料，这些数字会变。

## 它解决什么问题

通用大模型不知道你的资料。把文档丢进去让它读，读完就开始编——你分不清哪句来自你的文档、哪句来自它的训练数据。KnowTune 把「猜」改成「查」：

- 你的文件切块后建两路索引：向量 + 关键词。
- 提问先两路召回、融合、精排，只把选中的几块喂给模型。
- 答案里每句结论挂一个 `[n]`，越界编号当场剥离，点开就是原文那一段。
- 证据不够硬就标低置信或拒答（`refusal`，拒答）；后台链路再算一遍支撑度（`support`，支撑度），验证编号指的那段是不是真在说这句话。

把这条链做成固定流水线是明确取舍：检索参数、融合权重都是可测的旋钮，改一个就该能在检索日志里看出候选与耗时的变化。让模型自己决定「要不要再查一次」会把这条因果链打断。

## 核心能力

| 能力 | 现行实现 |
|---|---|
| 三板块 | `section`（板块）分 `med`（维医问答）、`classic`（古籍问答）、`pv`（预问诊）。当前 `config/instances.yaml` 只有一个实例 `uyghur-med`，三个板块开关全开；`med` 绑 kb 2（26 篇 / 322 块），`classic` 绑 kb 4（5 篇 / 1120 块），`pv` 走预问诊引擎、不检索知识库 |
| 双路召回 | dense 用本地 `BAAI/bge-small-zh-v1.5`（512 维），sparse 用 jieba 预分词写进 FTS5。两路各取 top 50，`weighted` 融合（w_dense 0.35 / w_sparse 0.50），前 10 条送精排 |
| 精排 | `rerank`（精排）用 `BAAI/bge-reranker-base` CrossEncoder。三个库的 `retrieval.rerank.enabled` 均为 `true`，`/api/status` 报 `rerank.enabled=true, last_error=null`，**精排实际在跑**；打分有 4096 条进程内 LRU 缓存，键为（模型, 问题摘要, 片段摘要），换问法不命中、重启即空 |
| 引用 | 答案强制挂引用编号，`cite.verify` 剥掉越界编号，给引用覆盖率。引用固定 5 条，不按分数二次裁剪 |
| 支撑度 | `support.evaluate` 逐句与所引证据块比对，外加事实锚点核对。**只在后台 `/api/ask` 链路算**，前台 `/api/ask` 不调用；`/v1/chat/completions` 是纯转发网关，不进这条链 |
| 可配提示词 | `promptcfg` 覆盖层支持按 `provider`（后端）和板块配专属提示词；界面文案（欢迎语、免责声明、示例问题、库名）已改为可配 `copy_ug` / `copy_zh` 角色，不再硬编码 |
| 可查 | 两路召回、融合、精排、耗时、降级标记 `degraded`（降级标记）都落在检索日志里，可按库、按会话回看 |

支撑度不是「每次问答都算」——把这句话写进文档会误导，前台链路和 `/v1` 网关都不算。

### 检索参数现值

下表是 `config/default.yaml` 的现行配置键（2026-11-07），「说明」列给出中文含义；每个值为何这样定的推导与实测条件在 `docs/ARCHITECTURE.md`，本文不展开。

| 参数键 | 值 | 说明 |
|---|---|---|
| `chunking.size / overlap` | 480 / 80 | 标题优先切块，`min_chunk` 80 |
| `retrieval.dense.top_k / sparse.top_k` | 50 / 50 | jieba 预分词 + FTS5 unicode61 |
| `fusion.method / fusion.top_k` | `weighted` / 10 | w_dense 0.35 / w_sparse 0.50 / w_third 0；RRF 的 k=60 保留但不用 |
| `rerank.top_k / max_length / batch_size / timeout_s` | 5 / 384 / 32 / 8.0 | 超时打 `degraded=rerank_timeout` |
| `mmr / expand / query_rewrite` | 均 `false` | MMR（最大边际相关）、邻块扩展、查询改写均未启用 |
| `refusal.min_score / min_margin` | 0.35 / 0.0 | 出厂值；按库标定可覆盖（见「已知限制」） |
| `generation.temperature / max_tokens / context_token_budget` | 0.3 / 3072 / 6000 | `disable_thinking: true`，SSE 流式 |
| `chat.history_turns / history_token_budget` | 5 / 1500 | 会话历史轮数与 token 预算 |

## 装与启

本机快速启动的最小清单（Windows Git Bash；Linux/macOS 把 `.venv/Scripts` 换成 `.venv/bin`）。

**① 装依赖，顺序不能反。**

```bash
python -m venv .venv && . .venv/Scripts/activate

# 先装 CPU 版 torch：必须走官方 index，直接 pip install 会拉 CUDA 版（体积差约 10 倍）
pip install torch --index-url https://download.pytorch.org/whl/cpu

# 再装本体：local extra 里的 sentence-transformers 依赖 torch，顺序反了前面白装
pip install -e ".[extract,local]"
```

**② 两个本地模型目录名不一样，别写错。** 都放在 `data/models/` 下：

| 模型 | 目录名 | 期望体积（2026-11-07 实测） | 规则 |
|---|---|---|---|
| 嵌入 `BAAI/bge-small-zh-v1.5` | `data/models/bge-small-zh-v1.5` | 约 92 MB | **无** `BAAI--` 前缀，与 `embedding.local_path` 字符串一致 |
| 精排 `BAAI/bge-reranker-base` | `data/models/BAAI--bge-reranker-base` | 约 1.1 GB | **有** `BAAI--` 前缀，由 `model` 里的 `/` 替换而来 |

精排是默认链路的一部分，不是可选项：`retrieval.rerank.enabled: true` 且 `allow_download: false`，本地缺权重只会降级、不会联网补下。完整下载命令与校验见 `docs/RUNBOOK.md`。

**③ 起服务，两个地址。**

```bash
knowtune serve --port 8013
# 未激活 venv 时：./.venv/Scripts/knowtune.exe serve --port 8013
```

| 地址 | 是什么 |
|---|---|
| `http://127.0.0.1:8013/` | 后台管理台 Web UI，同一地址也是 Swagger；`http://127.0.0.1:8013/v1` 是 OpenAI 兼容网关 |
| `http://127.0.0.1:8800/` | 前台问答站，`config/instances.yaml` 的 `port: 8800` + `autostart: true`，**随主服务自动起，不用手动开** |

首次启动会建 `data/` 目录、执行 `schema.sql` 建表、生成 `default` 知识库；嵌入模型在后台预热，冷启 43–93 秒（16 核 CPU，2026-11-07 实测热盘 43 s / 冷盘首次约 93 s），期间检索只有 BM25 一路。看 `GET /api/status` 的 `warmup.state` 变 `ready` 再提问。

## 从零部署

clone 后第一次搭、或换机器，从下模型到初始化数据库、绑库 id、逐条验证、排障的完整步骤见 `docs/RUNBOOK.md`；本文不重复。

## 命令速查

以 `src/knowtune/cli.py` 实际注册的子命令为准：

```bash
knowtune init                          # 初始化数据库与默认知识库，打印默认库 id
knowtune status                        # 环境与索引状态，逐库打印标定结论
knowtune kb list                       # 知识库清单
knowtune kb create <name>              # 新建知识库
knowtune ingest <path> [--kb 1]        # 导入文件或目录
knowtune ask "问题" [--kb 1]           # 单次问答，走当前 provider（后端）
knowtune debug "问题"                  # 打印两路召回与融合排序，不调模型
knowtune serve [--port 8013]           # 启动 Web UI
```

`config/models.yaml` 已注册 `siliconflow` / `local` / `deepseek` 三个后端；密钥在界面填，不写进 `models.yaml`。

## 架构速览

一张表说清五个部件各是什么、在哪：

| 组件 | 是什么 | 在哪 |
|---|---|---|
| 后台管理台 | 主服务 FastAPI：建库、导入、调参、看日志、全部 HTTP 接口（`api/app.py`，约 4400 行，唯一大文件） | `http://127.0.0.1:8013/`，`knowtune serve` |
| 前台问答站 | 面向终端用户的问答站，主服务进程内 daemon 线程，不另起进程；`instances.yaml` 的 `port` + `autostart: true` | `http://127.0.0.1:8800/`，`src/knowtune/frontsite/` |
| OpenAI 兼容网关 | `/v1` 纯转发到选中的 OpenAI 兼容后端，**不经过 RAG 链路**；RAG 入口只有后台 `/api/ask` 与前台 `/api/ask` | `http://127.0.0.1:8013/v1` |
| 预问诊 | 挂在前台 `/previsit`，对外全路径 `/previsit/api/previsit/*`，**没有独立站点、没有独立端口**；`section` 开关 `pv` 关闭时按 404 拦截 | 实现真身 `src/previsit/`（v0.3.0，零 knowtune 依赖），`src/knowtune/previsit/` 是桥接再导出 + 装配层 |
| 数据库 | SQLite（WAL），8 张表 + 1 张 FTS5 虚表，另有 FTS5 的 5 张影子表 | `data/library.db` |

两套 `previsit` 不是重复实现：运行时用的是 `src/previsit/`，由 `frontsite/site.py` 挂到 `/previsit` 并注入进程内检索器。

## 界面与配置要点

**三板块独立配模型与提示词。** 后台「站点」弹窗里，`med` / `classic` / `pv` 各自配显示名、开关、`translate` / `generate` 后端、专属提示词（`scope=med|classic`）。板块切换会换会话、换标题、换绑定库，不是只换皮肤。当前 `sections.*.roles` 均未配置，所以 `/api/info` 里 `section_roles.med` 与 `.classic` 解析结果相同——能力已具备，配置为空。当前解析结果：`translate = siliconflow::tencent/Hunyuan-MT-7B`，`generate = siliconflow`（默认模型 `Qwen/Qwen2.5-7B-Instruct`）。

**两条优先级链，别混。**

- 角色（`role`）解析链，`roles.py:81-111`：**板块级 roles → 站点级 roles → 系统级 roles → 实例 `provider`（后端）→ 默认 `provider`**。每级用已知后端名校验，不合法继续下退。
- 提示词解析链，`promptcfg.py:337-344`（以代码为准）：**`by_provider`（后端）> `by_scope`（板块）> `default`（角色默认）> 内置（builtin）**。仅 `generate` / `translate` 两个角色支持 by_provider 与 by_scope；`copy_ug` / `copy_zh` 只读角色级。当前 `config/prompts.yaml` 实配只有 `translate.by_provider.siliconflow.system` 与 `copy_zh.default.samples`，`overridden_scopes` 为空，即没有配板块级提示词覆盖。

源码注释曾与代码口径不一致（`promptcfg.py` 头注释与 `save()` docstring 声称板块优先于后端，`config/prompts.yaml` 头注释漏写 `by_scope`）；**本次重写已把三处注释统一到代码口径**，现行源码不再有旧描述。历史记录见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) §12。

**参数优先级**：`config/default.yaml`（唯一权威，带注释）→ `config/local.yaml`（用户改过的差异项）→ 库级 `kb.config_json`（**全量快照**，标 `__full__`）→ 按库标定文件覆盖。「用户手动改过」的判据是「与出厂基线值不同」，不是「键存在」；否则全量快照会让标定永不生效。

**界面文案已可配**：欢迎语、免责声明、示例问题、库名等改成可配提示词 `copy_ug` / `copy_zh`，不再硬编码。

## 已知限制

这一章是**方法与资产边界**，不是待办清单。下面每条都是取舍或实测出来的能力上限，改文档不会让它们消失。

- **精排约 3 秒 CPU 延迟**（维医库 22 道口语题、`10×384` 候选实测 3.07 s，2026-11-07）。实测精排把 R@5 从 79.2% 提到 83.3%（维医语料），代价 19ms → 7.5s；候选池缩到 12 条时反跌到 75%。短泛问题（「肚子疼」）会答错且系统不自知；古籍带现代注解会吸引无关问题；引用固定 5 条不按分数二次裁剪。半泛问题答错属于方法边界，不是配置没调好。
- **只有 kb 2 有按库标定 `calibration`（标定）；kb 3（临床药理）与 kb 4（古籍）用出厂 `min_score=0.35`。** 现行生效文件只有 `data/calibration/kb_2.json`：`calibrated_at=2026-11-06T09:01:29Z`、`method=real-questions`、52 组样本、拒答侧 `AUC=1.0` / `min_score=0.581` / `min_margin=0.0` / `mode=calibrated`；支撑度侧 `AUC=0.992` / `threshold=0.665` / 两侧误判 0.0435 / 69 样本。生效条件（`config.py:495-510`）：kb_id 非空 + 文件存在 + JSON 可读 + **模型指纹等于 `embedding.model@dim`**。
- **历史标定已失效。** 2026-10 实现期、以及评测台迁出前（2026-11 前）曾用另一套全局/按库标定口径，其阈值、AUC、留一法样本数、误拒/漏拒率均为历史记录，**已失效**；现行判据只认本节给出的 `kb_2.json` 数值。旧 `data/refusal_calibration.json`（全局格式、`reliable:false`）已不生效。标定工具 `model-eval/calibrate.py` 已随评测台迁出为独立项目，本仓库不自带，主项目只读 `data/calibration/` 结果。
- **融合权重与标定都在维医语料上测的**（两套探针 × 两个种子 × n≈300）。换一批文档，BM25 与向量的强弱关系可能反转，须重跑再定 `retrieval.fusion.*`。支撑度对事实篡改几乎无效：同块 `300ml→700ml` 的 AUC 仅 0.514（≈抛硬币），靠事实锚点兜，锚点捕获约 84%。
- **单机单用户**：SQLite + 进程内模型，无并发控制。本地 7B 一次完整回答实测 222 秒（CPU 推理），SSE 流式已有，但检索完到首 token 之间仍静默。嵌入编码约 21 chunk/s（本机 CPU）。
- **后台无入站鉴权，默认只监听 `127.0.0.1`。** 要开局域网须显式 `knowtune serve --host 0.0.0.0`。这是既定取舍，不是漏做：在只监听回环地址的前提下外部暴露面本就不存在；要上公网必须先补鉴权层。
- **仓库不含运行资产**：`data/` 与 `tools/llama/` 被 `.gitignore` 排除，clone 后必须自行下载模型、初始化数据库、绑定库 id。仓库基线提交 `72509d8`，96 个受版本控制文件，是主体代码 + 文档。GitHub git 端点在此网络环境返回 502（2026-11-07 实测），推送走 `api.github.com` 的 Git Data API。

需求覆盖 32 项有三套统计口径，**不得只引用一套**（逐条状态与换算见 `docs/ARCHITECTURE.md`）：

| 口径 | ✅ / ⚠️ / ⬜ | 来源 |
|---|---|---|
| 用户复核口径 | 24 / 4 / 4 | 用户此前确认的复核结论 |
| 曾实现并实测 | 25 / 4 / 3 | 评测台迁出前的实测状态 |
| 当前仓库自带 | 22 / 4 / 6 | 三项能力迁出后按「仓库自带」归类 |

差异全部来自 F5.1 / F5.2 / F5.4 随评测台迁至独立项目 `model-eval` 后如何归类。⚠️ 四项里 F2.4 的限制是「精排 CPU 延迟与语料适配」，不是缺模型；P0 / P1 全部落地。

## 项目结构

```
src/knowtune/            主项目
  cli.py                 命令入口、注入 KNOWTUNE_GATEWAY
  config.py              配置加载、按库标定、模型指纹校验
  kbconfig.py            库级参数全量快照 + 出厂基线比对
  db.py / schema.sql     SQLite（8 表 + 1 FTS5 虚表）
  secrets.py             Windows DPAPI 密钥
  roles.py               后端/角色解析
  promptcfg.py           提示词覆盖层（provider / scope）
  ingest/                loader / cleaner / chunker / pipeline
  index/                 embedder / vectors / sparse
  retrieve/              engine（双路召回、融合、拒答判定）/ rerank（精排 + MMR）
  generate/              prompt / llm / cite / evidence / support / think
  api/app.py             后台全部 HTTP 接口（约 4400 行）
  api/static/            后台前端：index.html / app.js / style.css，构建号 0.9.54
  frontsite/             instcfg / runner / site + static（前台前端）
  previsit/app.py        主项目装配层（注入进程内检索器、LLM 适配器）
src/previsit/            预问诊独立实现（v0.3.0），零 knowtune 依赖
config/                   default / local / models / instances / prompts(.yaml) / prompts/*.md
                          previsit.yaml / previsit.overrides.yaml / previsit_kb.yaml
data/                    不进版本库：library.db、uploads/、models/、calibration/、secrets.json
docs/                    7 份文档（见下表）
scripts/                  fake_llm.py / get_llama.py / shot_models.py
```

## 文档矩阵

7 份文件各司其职，同一事实只在一处写深：

| 文件 | 唯一职责 | 不写什么 |
|---|---|---|
| `README.md`（本文） | 项目入口：是什么、装、启、能力速览、文档地图、已知边界摘要 | 不写完整部署步骤、不写逐文件地图 |
| [docs/RUNBOOK.md](docs/RUNBOOK.md) | 从零 clone 到三板块可用：依赖顺序、模型路径、DB 初始化、绑库、启动、判据、排障、备份 | 不写架构原理 |
| [docs/USER_GUIDE.md](docs/USER_GUIDE.md) | 日常使用：建库导入、提问读引用、参数含义、指标、排障、使用侧限制 | 不写安装顺序细节 |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | 现行架构、数据模型、检索/生成链、关键决策与实测数据、需求对照表、代码已知不一致、历史记录归档 | 不写操作步骤 |
| [docs/PROJECT_MAP.md](docs/PROJECT_MAP.md) | 逐目录逐文件说明、启动顺序、配置优先级、改动落点 | 不写实测指标 |
| [docs/TECH_STACK.md](docs/TECH_STACK.md) | 技术清单与分层、三个关键决策、启动速记 | 不写需求对照 |
| [docs/WBS.html](docs/WBS.html) | 交付时的工作包与范围缺口**快照**，明确标注「历史快照」 | 不冒充现行状态表 |

环境要求见「装与启」，从零部署的完整步骤见 [docs/RUNBOOK.md](docs/RUNBOOK.md)；交付清单见 [docs/WBS.html](docs/WBS.html)。

**改完文档必须跑 `python scripts/verify_docs.py`**：确定性校验相对链接、章节引用、失效口径、现行事实数字、两条优先级链、HTML 标签配平，以及 Python / JS / YAML / SQL 语法。输出 `ALL CHECKS PASSED` 才算写完，避免同一事实在多份文档里各写一遍又写歪。

## 许可

MIT。

## 与其他文档的分工

本文只做入口：给判断、给事实卡、给能力速览、给文档地图，并摘出最容易被写错的已知边界摘要。它不重复任何一份文档的完整内容：

- 完整从零部署、逐条 curl 判据、故障对照、备份，全部在 [docs/RUNBOOK.md](docs/RUNBOOK.md)，本文「从零部署」只给一句指路。
- 现行架构、数据模型、实测数据、需求逐项对照、代码与注释不一致清单、历史记录归档，在 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)；本文只引用结论，不铺开推导。
- 逐文件地图在 [docs/PROJECT_MAP.md](docs/PROJECT_MAP.md)，技术分层与三个关键决策在 [docs/TECH_STACK.md](docs/TECH_STACK.md)，使用侧现象与排障在 [docs/USER_GUIDE.md](docs/USER_GUIDE.md)。
- [docs/WBS.html](docs/WBS.html) 是交付时快照，明确标「历史」，不用来判断现行状态。

这样切分是为了避免同一事实在多处各写一遍又写歪——尤其是状态栏口径、标定现行值、需求三口径这几处，只以 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) 与 [docs/RUNBOOK.md](docs/RUNBOOK.md) 的正文为准。
