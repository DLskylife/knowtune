# 项目地图 · KnowTune 是怎么搭起来的

这份文件回答两件事：**每个文件是干什么的**，以及**它们按什么顺序攒成一个能跑的系统**。
想读设计取舍和实测数据去 [ARCHITECTURE.md](ARCHITECTURE.md)；想直接跑起来看 [README](../README.md)。
这里只讲结构。

---

## 1. 一句话

一个**纯本地**的 RAG 知识库：文件进来 → 切成块 → 建两路索引（向量 + 关键词）→ 提问时两路召回融合 → 交给任意 OpenAI 兼容的模型写答案 → 每条结论都挂一个可点回原文的引用编号。

三样东西不碰外部服务：数据（SQLite）、嵌入模型（本地 bge）、界面（自己写的静态页）。
唯一的外部依赖是"你配的那个 LLM 后端"，它可以是本机的 vLLM / LM Studio，也可以是云端。

---

## 2. 运行时全景

系统里有**三类进程 / 入口**，共用同一份代码和同一个数据库：

| 入口 | 默认地址 | 谁在用 | 代码入口 |
|---|---|---|---|
| 后台管理台 | `127.0.0.1:8000` | 你自己：建库、导入、调参、看日志 | `knowtune.cli serve` → `api/app.py` |
| 前台问答站 | `127.0.0.1:8800` 起 | 终端用户：只能提问，看不到任何管理功能 | 由后台的 `InstanceManager` 在**同进程**内起 uvicorn 线程 |
| 命令行 | — | 批量导入、脚本化评估 | `cli.py` |

关键设计：**前台不是一个独立程序**。它是后台进程里的一条线程（`frontsite/runner.py`），
这样检索器和嵌入模型只需要加载一次——模型加载是 40 秒级的开销，每个站点复制一份
就等于把内存和冷启动都乘上站点数。

```
api/app.py  ← 后台 :8000
├─ AppState         SQLite 连接 / 嵌入模型 / 向量与关键词索引 / 检索器 / 导入流水线 / LLM 客户端
└─ InstanceManager  同进程起线程 → frontsite/site.py  ← 前台 :8800–8899
                    复用上面那套检索器与 LLM 客户端（不再加载一遍模型）

cli.py  ← 命令行
└─ 批量导入 / 检索调试 / 评估 / 单次问答，调的是同一批组件

                       ↓ 全部落到
              data/library.db（SQLite）
              ├─ 正文：kb / document / chunk
              ├─ 索引：embedding / chunk_fts
              └─ 会话：session / message / query_log / feedback
```

---

## 3. 顶层目录：每个文件夹为什么存在

| 路径 | 是什么 | 能不能删 |
|---|---|---|
| `src/knowtune/` | 全部 Python 源码（含两套前端静态文件） | 不能 |
| `config/` | 全部配置：默认值、本机覆盖、模型注册表、站点实例、提示词 | 不能 |
| `data/` | 运行期数据：数据库、上传原件、模型权重、密钥 | 不能（删了等于重建库） |
| `docs/` | 架构文档、WBS、本文件 | 不能 |
| `.venv/` | 虚拟环境（1.3 GB，含 torch） | 可删，重装见 README |
| `.workbuddy/memory/` | 开发过程的工作日志 | 可删，属于过程记录 |
| `.idea/` | PyCharm 工程配置 | 可删，IDE 会重建 |

---

## 4. 逐文件说明

### 4.1 根目录

| 文件 | 职责 |
|---|---|
| `pyproject.toml` | 打包与依赖声明。依赖分三档：核心（fastapi/numpy/jieba…）、`[extract]`（PDF/Word/Excel 抽取）、`[local]`（sentence-transformers）。**torch 不在这里**——CPU 版必须从 PyTorch 官方 index 装，写进依赖会被解析成 CUDA 版，体积差 10 倍。 |
| `README.md` | 使用手册：装环境、跑起来、命令清单。 |
| `docs/ARCHITECTURE.md` | 设计说明书。数据模型、检索参数为什么这么定、拒答阈值怎么标定、以及每一轮踩坑的完整记录（§12 是开发日志，出问题时先在这里搜）。 |
| `docs/PROJECT_MAP.md` | 本文件。 |

### 4.2 `config/`

| 文件 | 职责 |
|---|---|
| `default.yaml` | **唯一真相来源**。所有可调参数与默认值都在这里，每个值都写了"为什么是这个数"。代码里不出现魔法数字。 |
| `local.yaml` | 本机覆盖层，覆盖 `default.yaml` 的任意项。改端口、换后端地址这类"只对我这台机器成立"的东西放这儿。 |
| `models.yaml` | 模型后端注册表（增删、切换、探测结果）。由「模型」页写入，也可手改。 |
| `instances.yaml` | 前台问答站点的实例定义：哪个库、哪个端口、什么语言、用哪个模型角色。由「站点」页写入。 |
| `prompts/qa_v1.md` | 默认系统提示（中文）。`prompt_version` 指向它。 |
| `prompts/qa_ug.md` | 维吾尔语版本的系统提示。回答语言不是中文时自动改用它。 |

### 4.3 `src/knowtune/` 地基（谁都要用的那层）

| 文件 | 职责 |
|---|---|
| `__init__.py` | 包声明，只有版本号。 |
| `config.py` | 配置加载：YAML → pydantic 模型。**相对路径一律相对项目根解析**，不跟着 CWD 跑。合并顺序见 §8。还负责把 `refusal_calibration.json` 里的标定结果并进配置。 |
| `settings.py` | 可调参数的**声明式注册表**（30 个参数 / 7 组）。一个参数只在这里声明一次：类型、范围、默认值、中文名、说明。参数面板、校验、落盘、恢复默认全部由这张表驱动——UI 上不再手写控件，就不会出现"前端写 0.5、后端按 0.3 算"。 |
| `kbconfig.py` | **知识库级**参数覆盖。每个库一套自己的检索口径，存在 `kb.config_json` 里；建库时按预设复制一份，改完立刻生效。 |
| `db.py` | SQLite 连接管理。核心是 `check_same_thread=False` + 每线程一条连接：FastAPI 的同步路由跑在线程池里，共用一条连接会撞锁。 |
| `schema.sql` | 建表语句与索引。整个数据模型在这里看最快。 |
| `models.py` | 跨模块传递的数据结构（`Chunk`、`Hit`、`DocMeta`…）。刻意保持无逻辑。 |
| `paths.py` | 两个纯函数：判断源文件是否还在、判断路径是否越界。单独成模块是为了**可测**——放进 `app.py` 就等于让测试去背整个应用的启动成本。 |
| `logbus.py` | 内存环形日志。接管 root logger 一份，首页「系统日志」读它。个人单机不需要 500MB 的 access.log，需要的是"最近 200 条里有没有错"。 |
| `services.py` | 后端可用性探测："现在到底能不能用"。两段式（先 TCP 探活、再打一次 `/v1/models`），结果缓存 15 秒。本机地址自动绕代理。 |
| `secrets.py` | 密钥存放。默认**落盘但只落密文**（Windows DPAPI，账户 + 机器级；无系统级加密的平台拒绝落盘并报错）。原来默认只在内存、勾了才存，实测"没人会去勾那个框"，于是每次重启都在重填，2026-11-01 反转了默认值。另有 `api_key_env` 第三来源，给服务器 / CI 用。 |
| `modelcfg.py` | `models.yaml` 的读写：后端增删改、只存与默认值的差异、软删除。另出两份视图：`providers`（卡片用）与 `catalog`（扁平的后端×模型表，对话页下拉用——只列当前后端的话，用户加的模型就"选不到"）。 |
| `roles.py` | **模型角色**：一次问答里其实有三种不同的模型调用（翻译 / 生成 / 嵌入），可以分别指定后端。绑死在一个后端上是偷懒。 |
| `lang.py` | 语言识别、查询翻译、译文判废、答案翻译。跨语言问答的全部逻辑。含"剥掉模型原样抄回问题"这类防御。 |
| `chatctx.py` | 上下文占用核算：这一轮还剩多少字符给历史。不依赖 torch，所以能单测。 |
| `dashboard.py` | 首页数据聚合（资产、近 24h、存储评估、健康度）。只读，不探测。 |
| `evaluate.py` | 评估：Recall@K / MRR / 拒答混淆矩阵 / 阈值标定。**没有基线的 RAG 无法判断改动好坏**，这个模块是唯一的裁判。 |
| `cli.py` | 命令行入口：`init / status / kb / ingest / ask / debug / eval / serve`。 |

### 4.4 `src/knowtune/ingest/` 导入链路

| 文件 | 职责 |
|---|---|
| `loader.py` | 多格式抽取：PDF / Word / Excel / PPT / Markdown / 网页 / 源码 → (纯文本 + 结构块)。原则是**失败必须给人话**，绝不静默返回空文档。 |
| `cleaner.py` | 抽取之后、切分之前的清洗：去页眉页脚、去水印、去重复行。第一条原则是**源文件一个字节都不改**。 |
| `chunker.py` | 分块：结构优先（标题层级 → 段落 → 句子），字符兜底。中文技术文档一个论点常横跨 3–5 段，按固定窗口切必然切断论证。 |
| `pipeline.py` | 流水线：抽取 → 清洗 → 切分 → 分词 → 嵌入 → 落库 → **原子切换**。带状态机与进度，供后台轮询。重新索引时保留一代旧数据，用于回滚。 |

### 4.5 `src/knowtune/index/` 两种索引

| 文件 | 职责 |
|---|---|
| `embedder.py` | 本地嵌入模型（bge-small-zh-v1.5，512 维）。含预热线程与就绪状态——冷启动 40 秒里前端必须能解释"为什么还不能问"。 |
| `sparse.py` | 关键词检索：jieba 预分词 + SQLite FTS5。**必须预分词**：实测 FTS5 默认分词器对中文完全失效（2 字查询命中 0 条），trigram 同样失效。 |
| `vectors.py` | 向量检索：内存矩阵 + 暴力点积。10 万 chunk ≈ 196MB，这个量级上暴力算比建 ANN 索引更快也更准。 |

### 4.6 `src/knowtune/retrieve/` 检索编排

| 文件 | 职责 |
|---|---|
| `engine.py` | 主链路：双路召回 → 加权 RRF 融合 → 精排 → MMR → 拒答判定。**每一段都可以失败，失败后必须降级并打标**，不能静默返回空结果。 |
| `rerank.py` | 精排（CrossEncoder）与 MMR 去冗余。模型没下载时自动降级，并把 `rerank_unavailable` 写进响应。 |

### 4.7 `src/knowtune/generate/` 生成链路

| 文件 | 职责 |
|---|---|
| `prompt.py` | 上下文装填与提示词组装：按相关度排序、按 token 预算裁剪、拼系统提示。两条硬规则：资料不足必须拒答、答案必须带引用编号。 |
| `llm.py` | LLM 客户端：一个协议（OpenAI 兼容）、三档后端（vLLM / LM Studio / 云端）。整条链路唯一不可控的一段，超时、重试、字段退让、`reasoning_effort` 都在这里。 |
| `think.py` | 剥掉思考型模型吐出的 `<think>…</think>` 块，避免推理过程被当成答案正文。 |
| `evidence.py` | 证据分档：把"这批资料有多可信"量化成高/中/低，既不直接扔给模型、也不让界面假装很有把握。 |
| `cite.py` | 引用校验：把"模型说它引用了"变成"确认它引用了"。算覆盖率、抓越界编号、给 feedback 关联 `message_id`。**它只判格式**——编号在不在、超没超范围。 |
| `support.py` | 答案支撑度：把"引用了"变成"引到了"。答案逐句与所引证据块的 **150 字窗口**算余弦取最大（整块口径会把单句语义稀释掉，实测漏断率差 16 倍），外加事实锚点**句级否决**——半数以上锚点落空即不算有据。回答"这句话在资料里找不找得到"。不依赖 LLM，本地嵌入模型即可，所以能进每次问答的运行时链路。 |

### 4.8 `src/knowtune/api/` 后台

| 文件 | 职责 |
|---|---|
| `app.py` | **全部 HTTP 接口**（约 2900 行，是项目里唯一的大文件）。启动时构造 `AppState`（连接、模型、检索器、流水线、LLM、站点管理器）并预热。路由按域分组：状态/看板、模型、设置、知识库、导入、检索调试、问答(SSE)、会话、日志、评估、实例。 |
| `static/index.html` | 后台外壳：五视图导航（首页 / 对话 / 知识库 / 模型 / 站点）+ 弹窗容器 + 构建号自检。 |
| `static/app.js` | 后台全部逻辑（约 5000 行）：路由、各视图渲染、SSE 对话、参数面板、站点卡片。无框架、无打包，直接 ES2020。 |
| `static/style.css` | 后台样式。深色为基底。 |

### 4.9 `src/knowtune/frontsite/` 前台问答站

| 文件 | 职责 |
|---|---|
| `site.py` | 前台应用的工厂：`build_site(spec, state)` 生成一个只含「问 + 答 + 来源」的 FastAPI 实例。**每次请求现读配置**（`_cur()`），所以改标题、改欢迎语不用重启站点。页面语言按**文案内容**判定（`_content_lang`），配置里的 `lang` 只在判不出时兜底；答案语言取前端随请求下发的 `ui_lang`（= 右上角高亮那颗）。 |
| `instcfg.py` | 实例配置的读写（`instances.yaml`）：一个实例 = 一个知识库 + 一个端口 + 一套语言 + 一组模型角色。`alt` 存另一套语言的文案（含 `kb_name`），缺的字段由 `default_copy` 补齐。 |
| `runner.py` | 实例启停：每条线程一个 uvicorn、一个端口。选线程而不是子进程，是为了共用已加载的模型。含端口占用检测（被别的程序占了要明说，不能默默失败）。 |
| `static/index.html` | 前台外壳：左栏历史 + 中间问答 + 底部输入框。骨架**永不镜像**——切维语只换字形方向，不让侧栏跑到右边。 |
| `static/app.js` | 前台逻辑：流式问答、会话历史（存在 localStorage）、引用牌断行绑定、三语文案。 |
| `static/style.css` | 前台样式。和后台完全独立：配色、字体、圆角三套都不共用，改后台不会动到这里。维吾尔语字体必须走 `Microsoft Uighur` 一族，字体选错会让阿拉伯字母连写断开。 |

### 4.10 `data/` 运行期数据

| 路径 | 内容 |
|---|---|
| `library.db` | 全部数据：知识库、文档、chunk、向量、FTS 索引、会话、消息、请求日志、反馈。**这是唯一需要备份的东西。** |
| `uploads/` | 导入文件的原始副本，用于「回查原文」与重建索引。 |
| `models/` | 本地模型权重（嵌入 92M，精排可选）。 |
| `vectors/` | 预留的向量落盘目录，当前向量直接存在 SQLite 里。 |
| `secrets.json` | 会话密钥（服务停止即失效）。 |
| `refusal_calibration.json` | **旧格式**的全局标定结果（仅 22 条样本、`reliable:false`，已不生效）。新标定按库分文件，见下一条。 |
| `calibration/kb_<id>.json` | 该知识库的标定结果：拒答 AUC / 阈值 / 误拒漏拒率 / 每组探针的原始分数，支撑度 AUC（语义层与事实层各一个）/ 阈值 / 分布。由 `model-eval/calibrate.py` 产出，主项目只读。模型指纹对不上时自动失效。 |
| `probe_cache.json` | 后端探测结果缓存，15 秒过期。 |

---

## 5. 项目是怎么"构建"起来的

不是靠打包脚本，而是靠**依赖方向**。五层，箭头只允许从上往下指：

```
L4  表皮层   api/static（后台界面）      frontsite/static（访客界面）
L3  编排层   api/app.py 的 AppState —— 把下面几层组装成一条流水线
L2  能力层   ingest · index · retrieve · generate
L1  配置层   config · settings · kbconfig · modelcfg · roles
L0  契约层   models（数据结构）· schema.sql（存储）· db（连接）
旁挂 工具    lang（语言识别与翻译）· paths · logbus
             谁都能调，但它们自己不 import 任何业务模块，不参与分层

     依赖只会向下：上面的 import 下面的，下面的从不 import 上面的
```

规则很硬：

- **L0 不认识任何人**。`models.py` 里没有业务逻辑，`schema.sql` 里没有 Python。
- **L1 只读配置，不碰业务**。这一层回答"参数是多少"，不回答"参数怎么用"。
- **L2 之间互不调用**。`retrieve` 不知道 `generate` 存在（`engine.py` 只 import `index` 和 `models`），
  `ingest` 不知道自己以后会被检索。唯一的一处横向依赖是 `generate/prompt.py` 用了
  `lang.answer_instruction`——`lang` 是旁挂工具，不反向依赖任何业务模块，所以不会成环。
  这一层的每个模块都只靠 `models.py` 里的数据结构对话，这就是 259 条单测能跑在 13 秒内的原因：
  每一层都能单独喂数据进去，不需要起服务、不连模型。
- **L3 是唯一知道全局的地方**。`AppState` 把七个组件实例化并串起来，它不实现任何算法。
- **L4 只做 I/O**。`app.py` 收参数、调 L2/L3、拼 JSON，不在路由里写业务分支。
  两套前端（后台/前台）共用同一批 L2 组件，但**样式与文案完全独立**。

反过来做就是灾难：如果 `retrieve` 直接 `import` 了 `generate`，想换个生成后端就得动检索；
如果前台复用后台的 CSS，改一个后台按钮就会把访客界面弄乱。

---

## 6. 两条主链路

### 6.1 导入一篇文档

```
POST /api/kb/{id}/import   (或 CLI: knowtune ingest)
   └─ ingest/loader.py      抽取：任意格式 → 纯文本 + 结构块
   └─ ingest/cleaner.py     清洗：去页眉页脚/水印/重复行（不改源文件）
   └─ ingest/chunker.py     切分：标题层级优先，句子兜底
   └─ index/sparse.py       jieba 分词 → FTS5
   └─ index/embedder.py     批量编码 → 512 维向量
   └─ db.py + schema.sql    写 document / chunk / embedding / chunk_fts
   └─ ingest/pipeline.py    原子切换：新代全部写成功才切，失败保留旧代
```

### 6.2 一次问答（前台，维吾尔语提问）

```
浏览器 ──POST /api/ask (SSE)──► frontsite/site.py
   1. lang.py        识别语种 → 需要时把问题译成中文（reasoning_effort=none）
   2. AppState.engine 双路召回（向量 + BM25）→ 加权 RRF → 精排 → 拒答判定
   3. generate/evidence.py  给这批资料定档（高/中/低）
   4. generate/prompt.py    装填上下文（按 token 预算裁剪）
   5. generate/llm.py       流式生成 → 边生成边推 delta 事件
   6. generate/think.py     剥掉思考块
   7. generate/cite.py      校验引用编号是否越界
   8. lang.py               把答案译回维吾尔语
   ──SSE events──► meta / stage / query / delta / done / error
```

每一步失败都有明确去向：翻译失败 → 退原文并在界面挂提示条；精排不可用 → 降级并打标；
证据不足 → 拒答模板而不是硬编。**没有"静默降级"这条路**。

---

## 7. 启动顺序

```
knowtune serve --port 8000
  └─ uvicorn 加载 knowtune.api.app:app
       └─ lifespan 启动 → AppState.__init__
            ├─ load_config()          default.yaml + local.yaml + models.yaml + 标定结果
            ├─ Database(cfg)          开 SQLite，PRAGMA foreign_keys=ON
            ├─ Embedder(cfg)          加载 bge，**后台线程预热**（不阻塞启动）
            ├─ VectorStore / SparseIndex / Reranker
            ├─ RetrievalEngine        把上面四个串起来
            ├─ IngestPipeline
            ├─ LLMClient
            └─ InstanceManager        读 instances.yaml，把 autostart 的站点拉起来
```

冷启动约 40 秒（主要是加载嵌入模型）。这段时间 `/api/status` 会持续返回
`embedder.warmup.state=loading`，前端据此显示"索引预热中"，而不是让用户对着转圈猜。

---

## 8. 配置优先级

从低到高，后者覆盖前者：

```
config/default.yaml          ← 出厂值，代码里不再有第二份默认值
  ↑ config/local.yaml        ← 本机覆盖
  ↑ config/models.yaml       ← 模型后端注册表（整段替换 generation.providers）
  ↑ data/calibration/kb_<id>.json ← 该库的标定结果（拒答阈值 + 支撑度阈值；手改过的参数不覆盖）
  ↑ kb.config_json           ← 知识库级覆盖（存数据库，每库一份）
  ↑ 请求体里的临时参数        ← 单次请求有效
```

标定与手调的优先级：**手调过的参数 > 标定结果 > 出厂值**。判"手调过"的依据是与出厂值不同，而不是键存在——库里存的是全量快照，按"键存在"判断会让标定永远不生效（这个坑踩过一次：界面显示 calibrated，实际跑的还是快照里的 0.35）。

**热更新边界**：绝大多数参数改完立刻生效（请求期现读配置）。只有两个是冷字段——
站点端口、是否公开访问，改完必须重启那个站点，因为监听套接字已经建好了。
这条边界写在「站点」页的提示里，不靠用户猜。

---

## 9. 想改功能，动哪里

| 想做的事 | 改这些文件 |
|---|---|
| 调检索参数（top-k、融合权重、阈值） | `config/default.yaml` → 参数面板；代码在 `retrieve/engine.py` |
| 改分块策略 | `ingest/chunker.py`（+ `config` 的 `chunk` 段），改完要重新索引 |
| 支持一种新文件格式 | `ingest/loader.py` 的抽取器表 + `pyproject.toml` 的可选依赖 |
| 换生成后端 / 加一个模型 | 「模型」页（写 `config/models.yaml`），协议层在 `generate/llm.py` |
| 改提示词 | `config/prompts/qa_v1.md`，版本号在 `config/default.yaml` 的 `prompt_version` |
| 改后台界面 | `api/static/{index.html,app.js,style.css}`，改完升 `ADMIN_BUILD` |
| 改前台界面 | `frontsite/static/{index.html,app.js,style.css}`，改完升 `SITE_BUILD` |
| 改站点文案 / 加一种界面语言 | `config/instances.yaml`（`title`/`alt`）+「站点」页；判定逻辑在 `frontsite/site.py` 的 `_content_lang` / `_fill_copy`，内置文案在 `instcfg._DEFAULTS_UG/_DEFAULTS_ZH` |
| 加一个 HTTP 接口 | `api/app.py`（按域加在对应分组里） |
| 加一条单测 | 单测与前端冒烟工装已从精简版仓库移除；需要时自建 `tests/` 复用 `src` 的纯函数层（`src` 各层都可单独喂数据，无需起服务、不连模型） |

**两个构建号**（`ADMIN_BUILD` / `SITE_BUILD`）别忘了一起改：HTML 是 no-store 的，
但 WebView 可能把旧 JS 留在缓存里，编号对不上时页面会强制换一次，否则会出现
"新按钮在、点了没反应"。三处要一致：`index.html` 的 `?v=`、`index.html` 的自检常量、`app.js` 的常量。

---

## 10. 这次清理掉了什么

项目此前不是 git 仓库，删之前已整体打包到 **`D:\PythonProject\sky_agent_backup_20261029.tar.gz`**（16 MB，排除 `.venv/` 与 `data/`）。

| 删掉的 | 为什么 |
|---|---|
| `_shot/`（约 30 张 PNG + 1 个 demo HTML） | 开发期截图，每轮改 UI 的临时产物 |
| `.workbuddy/` 下 134 张 PNG、18 个 CDP 脚本、4 个诊断脚本（13 MB → 124 KB） | 一次性排查工具；保留 `memory/` 工作日志 |
| `.pytest_cache/`、全部 `__pycache__/` | 缓存，可再生成 |
| `_patch.py` | 一次性补丁脚本（改动早已落到 `tests/test_core.py`） |
| `_serve.log` | 一次启动的临时输出 |
| `data/knowtune.db` | 0 字节空壳，真实库是 `data/library.db` |
| `docs/env_report.json` | `scripts/check_env.py` 的输出（脚本已在精简版移除，此为历史产物） |
| `.idea/workspace.xml` | IDE 窗口状态。删了会被 PyCharm 立刻重建，已写进 `.gitignore` |

### 10.1 第二轮清理：数据库瘦身（2026-11-01）

上一轮清的是文件，这轮清的是**数据库里的死数据**——`library.db` 从
**217 MB 降到 22 MB**，检索指标一字未变（26 题 R@5 79.2% / MRR 0.570）。

| 清理项 | 数量 | 为什么是死数据 |
|---|---|---|
| 非活跃 generation 的 chunk | 2059 行 | 重新索引会写新一代（`generation+1`），旧代从此无人读——全项目所有查询都带 `generation = active_gen`，没有一处例外，也没有任何清理逻辑 |
| 孤儿向量（`embedding` 表） | 13485 / 14605 条 | `embedding` 按 `content_hash` 存、**没有指向 chunk 的外键**，所以 chunk 换了代它也赖着不走。92% 是孤儿 |

两个关键前提，动手前必须验证，否则就是毁库：
1. **`embedding` 是向量的唯一存储**（`chunk` 表里没有 vec 列），删错等于毁索引；
2. 活跃块的向量覆盖率必须是 100%，不是就拒绝执行。

这两条当时写进了 `scripts/prune_db.py` 做成硬闸门、默认 dry-run（该脚本已在精简版移除）。

**曾误判、已排除**：一度怀疑 FTS 缺索引（`chunk_fts` 1120 行 vs `chunk` 3179 行），
查下来是虚惊——活跃块恰好 1120 个且全部已索引，差的 2059 个正是上述废弃旧代块，
它们不进 FTS 是**正确行为**。别再照着"FTS 缺 2/3"这个错误结论去改代码。

顺带移出项目（在 `D:\_trash_skyagent_20261101\`，确认无误后可整个删）：
`data/models/BAAI--bge-reranker-base`（1.1 GB，当时库级关着精排；后已放回并开启，见 ARCHITECTURE §4.5）、
`data/library.db.bak_prefold`（208 MB 旧备份）、`_shot/`（开发截图）。

清理后：非 `.venv` 文件数 334 → 97（其中 2 个是这次新增的：本文件与 `.gitignore`），
非依赖部分体积 163 MB（全部是 `data/` 里的模型权重与数据库）。后台与前台服务照常响应，
`pytest` 175 条全绿、前台冒烟 48 项全过。

顺带补了一个 `.gitignore`：缓存、数据、模型、截图都写进去了，以后接版本库不会再把
69 MB 的数据库和 92 MB 的模型提交上去。

**刻意没删的**：`docs/WBS.html`（交付文档；`scripts/gen_wbs.py` 已在精简版移除，WBS 作为静态快照保留）、
`data/models/`（92 MB 模型权重，删了要重新下载）、`data/uploads/`（原文副本，删了引用回查会断）。
