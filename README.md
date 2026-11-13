# KnowTune

本地单机知识库问答系统：文件进来，答案出去，每句结论都带得回原文块。检索链是固定流水线，不是 Agent——没有让大模型决定下一步调什么工具，分支只有若干布尔开关。

多格式导入 → 分块 → 本地嵌入 + FTS5 双路召回 → 加权融合 → CrossEncoder 精排 → 带引用编号的 LLM 生成 → 引用校验 / 支撑度 → 可检索日志。附带一个面向终端用户的前台问答站和一个预问诊引擎。数据全部落在本地 SQLite，生成模型接任意 OpenAI 兼容后端。

## 快速开始（clone 之后 3 步）

需要 Python ≥ 3.11。**目录骨架、配置模板、模型下载说明都在仓库里**，clone 下来不用手工建目录、不用猜配置。

```bash
git clone https://github.com/DLskylife/knowtune.git
cd knowtune
python scripts/setup.py --start
```

`setup.py` 一条命令建 `.venv`、按正确顺序装 CPU 版 torch 与本体、初始化空库、检查模型，然后起服务。它是幂等的：已有环境和数据库就跳过，重复跑等价于体检，不会覆盖数据。

**第一次只需补两样**（脚本会打印命令，不会替你下载 1.2 GB）：

| 缺什么 | 怎么办 |
|---|---|
| 两个本地模型（约 92 MB + 1.1 GB） | 按 [`data/models/README.md`](data/models/README.md) 的两条 `hf download` 命令；国内可设 `HF_ENDPOINT=https://hf-mirror.com` |
| 生成后端密钥 | 后台「模型」页填写，或复制 `.env.example` 为 `.env`；**密钥不入库**，界面填的走本机 DPAPI 加密 |

**仓库自带 26 篇公开维医指南**（4.5 MB，新疆卫健委发布），就在 `data/uploads/weiyi/`，按 [`data/uploads/weiyi/README.md`](data/uploads/weiyi/README.md) 三步导入即可有一个能问的库，不用自己找语料。它们按普通上传文件处理，没有单独的语料目录或导入流程。

**路径一律相对项目根**：数据库里的 `source_uri`、`config/models.yaml` 的模型 `path` 都写成 `data/uploads/weiyi/xxx.pdf` 这种相对路径（用 `/` 分隔，基准是仓库根目录，见 [`data/README.md`](data/README.md)）。整个目录改名、换盘符、拷到别的机器，文档都不会突然变成「源文件缺失」；把盘符焊死在库里则会。项目根之外的外部文件无法可移植，仍保留绝对路径。

**本机生成模型也按这套口径**：`config/models.yaml` 的 `local`（7B，8097）与 `local3b`（3B 微调，8098）都指向 `data/models/xxx.gguf`，文件放进该目录就能用。仓库不含任何 `.gguf`——最小的也有 1.83 GB，远超 GitHub 单文件 100 MB 上限；要自己产就跑 `scripts/merge_lora.py` + `scripts/build_gguf.py`（见 [`tools/llama/README.md`](tools/llama/README.md)），或把自己的权重拷进去，放法与校验值见 [`data/models/README.md`](data/models/README.md)。没有这两个文件也能用，走云端后端填密钥即可。

起服务后：后台管理台 `http://127.0.0.1:8013/`，前台问答站 `http://127.0.0.1:8800/` 随主服务自动起。首次预热 43–93 秒，看 `GET /api/status` 的 `warmup.state` 变 `ready` 再提问。导入自己的文档：`knowtune ingest <path> --kb <id>`；想直接体验样例语料见上一段。

完整逐条判据与排障见 [docs/RUNBOOK.md](docs/RUNBOOK.md)；下面的「装与启」是同一流程的手动口径，想看清每一步再读。

## 当前事实卡

| 项 | 现行值 | 条件 |
|---|---|---|
| 版本 | `0.1.0` | `pyproject.toml`，包名 `knowtune`，入口 `knowtune = knowtune.cli:main` |
| Python | ≥ 3.11 | `pyproject.toml` 的 `requires-python` |
| 端口 | 后台管理台 `8013` / 前台问答站 `8800` | 本文与 RUNBOOK 统一口径；`cli.py` 的 `serve` 默认 `--port 8000`，实际部署用 `--port 8013` |
| 数据 | 3 个知识库、32 篇文档全部 `ready`、28020 个 chunk、28020 条 512 维向量 | 实测 `data/library.db`，2026-11-07 采集 |
| 许可 | MIT | `LICENSE` + `pyproject.toml` |

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
| 三板块 | `section`（板块）分 `med`（维医问答）、`classic`（古籍问答）、`pv`（预问诊）。当前 `config/instances.yaml` 只有一个实例 `uyghur-med`，三个板块开关全开；`med` 绑 kb 2（26 篇 / 322 块），`classic` 绑 kb 4（5 篇 / 1120 块），`pv` 是纯对话（提示词 + 模型）、不检索知识库 |
| 双路召回 | dense 用本地 `BAAI/bge-small-zh-v1.5`（512 维），sparse 用 jieba 预分词写进 FTS5。两路各取 top 50，`weighted` 融合（w_dense 0.35 / w_sparse 0.50），前 10 条送精排 |
| 精排 | `rerank`（精排）用 `BAAI/bge-reranker-base` CrossEncoder。三个库的 `retrieval.rerank.enabled` 均为 `true`，`/api/status` 报 `rerank.enabled=true, last_error=null`，**精排实际在跑**；打分有 4096 条进程内 LRU 缓存，键为（模型, 问题摘要, 片段摘要），换问法不命中、重启即空 |
| 引用 | 答案强制挂引用编号，`cite.verify` 剥掉越界编号，给引用覆盖率。默认取前 5 条（`rerank.top_k` 可配），不按精排分数设下限 |
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

本机快速启动的最小清单（Windows Git Bash；Linux/macOS 把 `.venv/Scripts` 换成 `.venv/bin`）。**以上流程已由 `python scripts/setup.py` 自动化**，这一节是脚本内部顺序的人工口径，排障时对照用。

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

`config/models.yaml` 已注册 `siliconflow` / `local`（7B，8097）/ `local3b`（3B 微调，8098）/ `deepseek` 四个后端；两个本机端点各有独立 `base_url`，启动其中一个不会把另一个切走。密钥在界面填，不写进 `models.yaml`。

## 架构速览

一张表说清五个部件各是什么、在哪：

| 组件 | 是什么 | 在哪 |
|---|---|---|
| 后台管理台 | 主服务 FastAPI：建库、导入、调参、看日志、94 个 HTTP 端点。`api/app.py` 是装配入口，端点按资源域拆到 `api/routes/` 8 个模块 | `http://127.0.0.1:8013/`，`knowtune serve` |
| 前台问答站 | 面向终端用户的问答站，主服务进程内 daemon 线程，不另起进程；`instances.yaml` 的 `port` + `autostart: true` | `http://127.0.0.1:8800/`，`src/knowtune/frontsite/` |
| OpenAI 兼容网关 | `/v1` 纯转发到选中的 OpenAI 兼容后端，**不经过 RAG 链路**；RAG 入口只有后台 `/api/ask` 与前台 `/api/ask` | `http://127.0.0.1:8013/v1` |
| 预问诊 | 挂在前台 `/previsit`，对外全路径 `/previsit/api/previsit/*`，**没有独立站点、没有独立端口**；`section` 开关 `pv` 关闭时按 404 拦截；**推理固定走本机模型**（`previsit.overrides.yaml` 钉 `provider: local`），不把患者原话发给云端。**只有一层壳：一段系统提示词 + 模型本身**——没有槽位、分诊规则、病历或结论卡，问什么、问几轮、何时收尾都由模型决定 | 实现真身 `src/previsit/`（v0.4.0，零 knowtune 依赖），`src/knowtune/previsit/` 是桥接再导出 + 装配层 |
| 数据库 | SQLite（WAL），8 张表 + 1 张 FTS5 虚表，另有 FTS5 的 5 张影子表 | `data/library.db` |

两套 `previsit` 不是重复实现：运行时用的是 `src/previsit/`，由 `frontsite/site.py` 挂到 `/previsit` 并注入进程内检索器。

## 界面与配置要点

**站点配置弹窗分三页**（2026-11-07 重排）：`板块与知识库`（开关 / 显示名 / 绑定库）、`模型与提示词`（一张矩阵，行顺序即角色优先级）、`站点设置`（端口 / 语言 / 文案）。模型下拉不再散在板块行里：矩阵四行是站点默认、维医问答、古籍问答、预问诊，两列分别写明「翻译模型 / 推理模型」，留空即跟随上一级。分页只切 `hidden` 属性、不重建 DOM，所以停在任一页保存都会把三页字段一起提交。

**站点卡左右分栏**（2026-11-13 重排）：左栏放身份、状态、端口/运行时长、访问地址与两组操作（改参数 / 管运行）；右栏是四行参数表——「入口 / 知识库 / 译·答 / 语言·产出」，每行整行可点，直接停在站点配置弹窗的对应页（`sections` / `models` / `site`）。纵向行把整栏宽度留给长模型名和维吾尔文 RTL 文本，值不截断并带完整悬浮提示；底部自检/备注折叠横跨整卡，异常默认展开；窄屏（≤900px）左右栏自动堆叠成单列。后台构建号 `0.9.65`。

**三板块独立配模型与提示词。** 后台「站点」弹窗里，`med` / `classic` / `pv` 各自配显示名、开关、`translate` / `generate` 后端、专属提示词（`scope=med|classic`）。板块切换会换会话、换标题、换绑定库，不是只换皮肤。当前 `sections.*.roles` 均未配置，所以 `/api/info` 里 `section_roles.med` 与 `.classic` 解析结果相同——能力已具备，配置为空。当前解析结果：`translate = siliconflow::tencent/Hunyuan-MT-7B`，`generate = siliconflow`（默认模型 `Qwen/Qwen2.5-7B-Instruct`）。

**两条优先级链，别混。**

- 角色（`role`）解析链，`roles.py:81-111`：**板块级 roles → 站点级 roles → 系统级 roles → 实例 `provider`（后端）→ 默认 `provider`**。每级用已知后端名校验，不合法继续下退。
- 提示词解析链，`promptcfg.py:337-344`（以代码为准）：**`by_provider`（后端）> `by_scope`（板块）> `default`（角色默认）> 内置（builtin）**。仅 `generate` / `translate` 两个角色支持 by_provider 与 by_scope；`copy_ug` / `copy_zh` 只读角色级。当前 `config/prompts.yaml` 实配只有 `translate.by_provider.siliconflow.system` 与 `copy_zh.default.samples`，`overridden_scopes` 为空，即没有配板块级提示词覆盖。

源码注释曾与代码口径不一致（`promptcfg.py` 头注释与 `save()` docstring 声称板块优先于后端，`config/prompts.yaml` 头注释漏写 `by_scope`）；**本次重写已把三处注释统一到代码口径**，现行源码不再有旧描述。历史记录见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) §12。

**参数优先级**：`config/default.yaml`（唯一权威，带注释）→ `config/local.yaml`（用户改过的差异项）→ 库级 `kb.config_json`（**全量快照**，标 `__full__`）→ 按库标定文件覆盖。「用户手动改过」的判据是「与出厂基线值不同」，不是「键存在」；否则全量快照会让标定永不生效。

**界面文案已可配**：欢迎语、免责声明、示例问题、库名等改成可配提示词 `copy_ug` / `copy_zh`，不再硬编码。

## 已知限制

这一章是**方法与资产边界**，不是待办清单。下面每条都是取舍或实测出来的能力上限，改文档不会让它们消失。按性质分三组：**A 方法边界**（判据本身分不开，调参无效）、**B 资产与标定边界**（数据、工具、模型指纹的适用条件）、**C 部署与性能取舍**（架构决定，改了就是另一个产品）。每条给「触发条件 / 影响 / 现在能怎么办」。

### A. 方法边界：调参解决不了

1. **短泛问题会答错，且系统不自知。** 触发条件：只给「肚子疼」这类两三字、不带实体名的问法。根因：余弦分数区间重叠，分数阈值在语义上分不开「相似但没有答案」——这是判据的物理上限，不是配置没调好。影响：给出带引用的错答案，外观和答对一样。现在能怎么办：把问法写具体；查询改写与领域词典扩展都未启用，不指望系统兜底。换阈值、多给几条引用这两条思路同样不成立。
2. **古籍里的现代白话注解会吸引无关问题。** 触发条件：语料里原文与注解混在同一批块。影响：字面相似但无关的注解排到前列。现在能怎么办：看证据分档与支撑度栏甄别；语料层面拆分原文与注解是另一项改动，未启用。
3. **引用按名次取，不按精排分数设门槛。** 先把事实说准：`rerank.top_k: 5`（`config/default.yaml:67`）是**可配置**的条数，`engine.py` 排序后取 `candidates[:top_k]`；缺的不是「改条数」，而是**没有分数下限**——前 5 名无论多低分都进上下文，只受 token 预算截断。影响：第 3–5 条常是噪声级也照送，引用覆盖率因此只是形式指标。现在能怎么办：改 `top_k` 能调条数；按分数裁剪需要新判据与阈值标定，当前没有。
4. **支撑度是主题级判据，不是事实级。** 句向量窗口最大余弦对「换一批不相关文档」很准（AUC 0.992、阈值 0.665、两侧误判 0.0435、69 样本），对**同块事实篡改**几乎等于抛硬币：把 `300ml` 改成 `700ml`，AUC 只有 0.514。根因：主题相似不等于数值一致；事实锚点（数字+单位、型号、版本号等纯字符串比对）捕获约 84%，是剩下的兜底手段。影响：数值被改写的答案可能仍显示「有支撑」。现在能怎么办：把锚点栏和证据分档一起看，别只看覆盖率。
5. **拒答是第一道筛子，不是最终判据。** 现行 kb2 标定 52 组样本误拒 / 漏拒均为 0，这个「满分」来自样本构造，有乐观偏差；真实拦截主力是生成后的支撑度与引用校验。

### B. 资产与标定边界：换语料或换模型就失效

1. **只有 kb 2 有按库标定；kb 3（临床药理）、kb 4（古籍）用出厂 `min_score=0.35`。** 现行生效文件只有 `data/calibration/kb_2.json`：`calibrated_at=2026-11-06T09:01:29Z`、`method=real-questions`、52 组样本、拒答侧 `AUC=1.0` / `min_score=0.581` / `min_margin=0.0` / `mode=calibrated`；支撑度侧 `AUC=0.992` / `threshold=0.665` / 两侧误判 0.0435 / 69 样本。**最隐蔽的是生效条件**（`config.py:495-510`）：kb_id 非空 + 文件存在 + JSON 可读 + **模型指纹等于 `embedding.model@dim`**。换嵌入模型或维度，标定文件会静默失效并退回 0.35，界面只说「本库尚未标定」。核对命令见 [docs/USER_GUIDE.md](docs/USER_GUIDE.md) §5。
2. **历史标定已失效。** 2026-10 实现期、以及评测台迁出前（2026-11 前）用过另一套全局/按库口径，其阈值、AUC、留一法样本数、误拒/漏拒率都是历史记录；旧 `data/refusal_calibration.json`（全局格式、`reliable:false`）盘上可能还在，但代码读的是 `data/calibration/kb_<id>.json`，**盘上有文件 ≠ 生效**。现行判据只认上一条的 `kb_2.json` 数值。标定工具 `model-eval/calibrate.py` 已随评测台迁出为独立项目，本仓库不自带，主项目只读 `data/calibration/` 结果。
3. **融合权重与标定只在维医语料上测过**（两套探针 × 两个种子 × n≈300）。换一批文档，BM25 与向量的强弱关系可能反转。影响：`retrieval.fusion.*` 的最优权重随语料变。现在能怎么办：换语料后重跑再定，不沿用维医数字。
4. **仓库带骨架，不带运行资产内容**：`data/` 与 `tools/llama/` 的目录骨架、模型下载说明、一份示例标定结果 `data/calibration/kb_2.json` 在版本库里；数据库、源语料、模型权重、密钥仍被 `.gitignore` 排除——前者含本机问答历史，后几者体积超过 GitHub 单文件 100 MB 上限。clone 后跑 `python scripts/setup.py` 建环境与空库，模型按 [`data/models/README.md`](data/models/README.md) 下，绑库 id 按 [docs/RUNBOOK.md](docs/RUNBOOK.md) 走。截至 2026-11-08，受版本控制的是主体代码、文档、配置模板与目录骨架。

### C. 部署与性能取舍：架构决定

1. **精排约 3 秒 CPU 延迟。** 触发条件：`fusion.top_k=10` + `rerank.max_length=384`，CrossEncoder 在 CPU 上逐对前向；现行实测 3.07 s（维医库 22 道口语题，`model-eval/scan_rerank.py`，2026-11-07），不开精排的同行基线 0.09 s，收益是 R@5 79.2% → 83.3%。减小候选会掉指标（8 条时 top1 0.727 → 0.682），缩短截断会让答案落在块中段时排不出（`10×128` 分数虽高但只看标题首段，未采用）。现在能怎么办：接受；要快就换硬件、量化模型或关精排，关掉会同时丢掉质量收益。**两组看起来打架的延迟数字不是同一组实验**，见下表。
2. **单机单用户**：SQLite + 进程内模型，无并发控制；多人同时提问互相排队。本地 7B 一次完整回答实测 222 秒（CPU 推理），嵌入编码约 21 chunk/s。SSE 流式已有，检索阶段补了 `progress`（进度）事件后首字前不再全静默（见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) §5.5），但生成侧首 token 仍受模型本身速度限制。
3. **后台无入站鉴权，默认只监听 `127.0.0.1`。** 要开局域网须显式 `knowtune serve --host 0.0.0.0`。这是既定取舍，不是漏做：只监听回环时外部暴露面本就不存在；要上公网必须先补鉴权层。
4. **GitHub git 端点在此网络环境返回 502**（2026-11-07 实测），推送走 `api.github.com` 的 Git Data API。与代码无关，换网络即消失。
5. **预问诊固定本机 7B，每轮一次模型调用。** `Qwen2.5-7B-Instruct-Q4_K_M`（4.4GB）常驻约 4.5GB 显存，后台启动后约 20 秒才能回答；这二十秒内模型还没就绪，预问诊会直接报「服务暂时不可用」，不会拿规则拼一句话糊过去。2026-11-07 实测（RTX 3060 6G，全量 offload）：开场 1.8 s、每轮 2.3 s。要更快只能换更小模型（3B 档实测不可用：量化到 Q4 后 77 tok/s，但会把「脐周」写成「全腹」）或换更大的卡。本机系统代理原本会再吃掉 3–4 秒，`knowtune serve` 已把回环并进 `NO_PROXY`。

#### 两组延迟数字为什么不一样

| 数字 | 出处 | 计时口径 | 是否现行配置 |
|---|---|---|---|
| 3.07 s | `model-eval/scan_rerank.py` 参数扫描，维医库 22 道口语题，`10 × 384` | 精排这一步的耗时；同行基线「不精排 0.09 s」 | **是**，现行 `fusion.top_k=10` + `rerank.max_length=384` |
| 19ms → 7.5s | 另一组 R@5 收益对照实验，维医语料 | 端到端检索延迟，开精排前后对比；量级对应更早的 `20 × 512` 档（8.16 s） | 否，历史实验口径 |
| 候选池 12 条反跌到 75% | 同上收益对照里的缩池组 | R@5 | 否，与现行 `top_k=10` 不是同一组 |

评估集、候选配置、计时口径三者都不同，这些数字不能互相当「当前延迟」。现行端到端延迟以问答响应里的 `timings_ms.rerank_ms` 为准，别拿历史实验数字当服务指标。

**为什么改文档消除不了这些**：A 组是判据的物理上限（换参数只会把误差换个方向），B 组是数据与工具的适用范围（文字改不了文件是否存在、模型指纹是否匹配），C 组是单机架构的既定取舍（改写部署形态等于换产品）。列出来是为了让误判可定位，不是为了承诺修复。需求逐项状态与三种统计口径见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) §13，使用侧现象见 [docs/USER_GUIDE.md](docs/USER_GUIDE.md) §9。

### 附：需求覆盖的三套统计口径

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
  api/app.py             FastAPI 装配入口：生命周期、中间件、注册 8 个路由模块
  api/routes/            94 个端点按资源域拆成 8 个模块
  api/common.py / state.py / coverage.py  分片上传 / 全局 AppState / 引用覆盖率
  api/static/            后台前端：index.html / app.js / style.css，构建号 0.9.65
  frontsite/             instcfg / runner / site + static（前台前端）
  previsit/app.py        主项目装配层（LLM 适配器 + 参数面板）
src/previsit/            预问诊独立实现（v0.4.0：提示词 + 模型的纯对话壳），零 knowtune 依赖
config/                   default / local / models / instances / prompts(.yaml) / prompts/*.md
                          previsit.yaml / previsit.overrides.yaml
data/                    目录骨架 + README 在库；library.db、源语料、模型权重（含 *.gguf）、secrets.json 不进版本库
docs/                    6 份文档（+ 根 README 共 7 份，见下表）
scripts/                  setup.py（一键部署）/ merge_lora.py + build_gguf.py（LoRA→GGUF）/ get_llama.py / verify_docs.py / fake_llm.py / shot_*.py
tools/llama/              骨架在库，llama-server 二进制按 README 下载
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

MIT，全文见 [LICENSE](LICENSE)。

## 与其他文档的分工

本文只做入口：给判断、给事实卡、给能力速览、给文档地图，并摘出最容易被写错的已知边界摘要。它不重复任何一份文档的完整内容：

- 完整从零部署、逐条 curl 判据、故障对照、备份，全部在 [docs/RUNBOOK.md](docs/RUNBOOK.md)，本文「从零部署」只给一句指路。
- 现行架构、数据模型、实测数据、需求逐项对照、代码与注释不一致清单、历史记录归档，在 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)；本文只引用结论，不铺开推导。
- 逐文件地图在 [docs/PROJECT_MAP.md](docs/PROJECT_MAP.md)，技术分层与三个关键决策在 [docs/TECH_STACK.md](docs/TECH_STACK.md)，使用侧现象与排障在 [docs/USER_GUIDE.md](docs/USER_GUIDE.md)。
- [docs/WBS.html](docs/WBS.html) 是交付时快照，明确标「历史」，不用来判断现行状态。

这样切分是为了避免同一事实在多处各写一遍又写歪——尤其是状态栏口径、标定现行值、需求三口径这几处，只以 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) 与 [docs/RUNBOOK.md](docs/RUNBOOK.md) 的正文为准。
