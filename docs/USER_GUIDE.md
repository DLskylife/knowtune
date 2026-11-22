# KnowTune 使用说明书

本地单机知识库问答。文档进来，答案出去，每个结论挂着能点回原文的引用编号 `[n]`。

这份文档只讲**怎么用、怎么读结果、出了问题怎么办**。完整从零部署（依赖顺序、模型下载、数据库初始化、绑库、逐条判据、备份）见 [RUNBOOK.md](RUNBOOK.md)；设计取舍与实测推导见 [ARCHITECTURE.md](ARCHITECTURE.md)；逐文件地图见 [PROJECT_MAP.md](PROJECT_MAP.md)；技术清单见 [TECH_STACK.md](TECH_STACK.md)；项目入口与边界摘要见 [README.md](../README.md)。

> 本文所有带数字的现状都采集自 2026-11-07 跑起来的服务（`data/library.db` 与运行态实测）。换机器、换语料、换嵌入模型，这些数字会变；条件一并写在数字旁边。

---

## 1. 五分钟跑起来

最短路径只有一条命令。后台主服务（后台管理台，`8013`）起来后，前台问答站（`8800`）由 `config/instances.yaml` 的 `autostart: true` 随主服务在**同一进程内的 daemon 线程**自动拉起，不用另开程序。

```bash
knowtune serve --port 8013
# 未激活 venv：./.venv/Scripts/knowtune.exe serve --port 8013
# cli.py 的 serve 默认端口是 8000，实际部署统一用 8013
```

打开两个地址：

| 地址 | 是什么 |
|---|---|
| `http://127.0.0.1:8013/` | 后台管理台 Web UI，同地址也是 Swagger；`http://127.0.0.1:8013/v1` 是 OpenAI 兼容网关，纯转发，不走检索链 |
| `http://127.0.0.1:8800/` | 前台问答站，`instances.yaml` 的 `port: 8800` + `autostart: true`，随主服务自动起 |

**预热 43–93 秒期间只有 BM25 一路。** 嵌入模型 `BAAI/bge-small-zh-v1.5` 在后台线程预热，实测 43–93 秒（16 核 CPU；2026-11-07 热盘 43 s、冷盘首次约 93 s）。这段时间 `embedder.available=false`，稠密（`dense`，稠密召回）路没就绪，检索只剩稀疏（`sparse`，稀疏召回）的 jieba + FTS5 BM25 一路。看 `GET /api/status` 的 `warmup.state` 变成 `ready` 才算双路齐全，再开始提问。

**状态栏逐项变绿**，首次提问前看左下角这三行：

```text
● 索引就绪  28,020 chunk
● 精排     BAAI/bge-reranker-base   enabled=true, last_error=null
● 生成     siliconflow（Qwen/Qwen2.5-7B-Instruct）
```

第一行是 2026-11-07 采集的全局口径：3 个知识库、32 篇文档全部 `ready`、28020 个 `chunk`（块）、28020 条 512 维向量。另外三条判断：

- 第三行红点 = 生成后端没配好，提问只返回引用、不返回答案。去「模型服务」页加端点、填密钥。
- 精排这行是实跑状态：三个知识库的 `rerank.enabled`（精排开关）均为 `true`，`/api/status` 报 `rerank.enabled=true, last_error=null`，**精排实际在跑**；打分有 4096 条进程内 LRU 缓存，键为（模型, 问题摘要, 片段摘要），换问法不命中、重启即空。
- 精排权重 `data/models/BAAI--bge-reranker-base`（约 1.1 GB）缺失时只降级、不联网补（`allow_download: false`），并打 `degraded`（降级标记）= `rerank_model_missing`。

完整从零部署（CPU 版 torch 安装顺序、两个模型目录名、数据库初始化、绑库 id、逐条 curl 判据、排障、备份）见 [RUNBOOK.md](RUNBOOK.md)，本文不重复。

---

## 2. 建库与导入

左侧「+」新建知识库。建库预设只改分块、召回、融合、精排参数，**不碰拒答阈值**——那个数必须标定，不能预置。

**导入入口**：文件拖进对话窗口即可，或 `knowtune ingest <路径> --kb <id>`。支持 PDF / Word / Excel / PPT / Markdown / 源码 / 单条与批量 URL / 粘贴文本（`POST /api/import/path|text|url`）。目录递归已通（`cli.py` 的 `rglob`）；sitemap 整站抓取未做，`.gitignore` 解析未做，目录递归已通——这两项是当前的确切边界。超过 8 MB 自动分片上传、断了可续传（`/api/upload/*`、`/api/import/task|tasks`）。

**文档状态机**：导入走「抽取 → 清洗 → 标题优先切块 → jieba 分词 → 本地嵌入 → 双写 `document`/`chunk`/`embedding`/`chunk_fts` → 原子切换」。全部写成功才把文档置为 `ready`（可检索）；失败保留旧代（`generation`，代次）可回滚，不会半截生效。命令行 `ingest` 也以 `status == "ready"` 判 OK。导入后等状态变 `ready` 再提问。

**chunk 预览 / 编辑 / 拆合**：知识库详情 → 文档 → 分块，可看原文（`GET /api/doc/{id}/preview`）、看块（`GET /api/chunks`）、改块（`PATCH /api/chunk/{id}`）、删块（`DELETE`）、拆块（`split`）、合块（`merge`）。

**重索引**：`POST /api/doc/{id}/reindex`。分块参数或块内容的改动只对之后新导入或重建索引的文档生效——改完界面会提示「需重建索引」，点了才生效；不点，提问拿到的还是旧块。

**文档搬库只改 `document.kb_id`**（文档所属知识库外键）：`POST /api/docs/move`（或 `POST /api/doc/{id}/move`）。`chunk`、向量、引用编号全部原样保留，不用重新导入。搬完要刷一次向量缓存：`VectorStore` 里 chunk → kb_id 的内存掩码是加载时的快照，不刷会让稠密路继续按旧库过滤，表现成「文档明明在库里却搜不到」。同一源文件在目标库已有一份时会被拒绝，不会静默覆盖。

**当前三个库的规模**（2026-11-21 采集）：

| 库 | id | 文档 | 块 | 绑定 |
|---|---|---|---|---|
| 维医 | #2 | 46 | 507 | `med` 维医问答板块 |
| 古籍 | #4 | 5 | 1120 | `classic` 古籍问答板块 |
| 临床药理 | #3 | 1 | 26578 | 不进前台，OCR 链路验证用 |

合计 52 篇 / 28205 块。前台 `GET /api/info` 报的 `docs / chunks` 是**站点绑定库（kb2 + kb4）的口径**，与后台全局数不矛盾：kb 3 占大头，不属于该站点。别拿这两个数对不上当故障。

**维医库 #2 是两套体系共存**：26 篇维吾尔医诊疗指南与疗法规范（新疆卫健委发布）+ 20 篇现代医学常见病参考（《默沙东诊疗手册》大众版，见 [`data/corpus/现代医学_来源清单.md`](../data/corpus/现代医学_来源清单.md)）。后者标题带 **（现代医学参考）**，覆盖高血压、糖尿病、感冒流感、胃肠炎、反流、贫血、失眠、腰痛、偏头痛、痛风、皮炎、焦虑、哮喘、儿童发热、鼻炎、便秘、血脂异常、消化不良。两批内容同库检索、由模型在答案里分别标注来源，不互相顶替：问「膝关节肿怎么治」命中的是维医指南，问「膝关节疼该看什么科」命中的是现代医学参考。

**补语料时先看它在不在库里**：拒答多半不是检索坏了，是语料里没有。查法：`knowtune debug "<问题>" --kb 2`，看 `final` 前五条是不是真的不相关。加新病种用 `scripts/fetch_msd.py`（改 `TARGETS` 后重跑，或 `python scripts/fetch_msd.py 21` 单篇重抓），再把目录重新导入一次；重跑会覆盖同名文件，来源头与免责声明由脚本统一写入文末。

前台站点可给古籍板块单独绑一个库（`instances.yaml` 的 `classic_kb_id`）；没绑、库被删、或库里没有 `ready` 文档时，退回在主库里按标题关键词（`classic_keywords: [古籍]`）分栏。两种模式下两边检索都只在自己的 `kb_id` 里找，古籍不会串进维医答案。板块切换会换会话、换标题、换绑定库，不是只换皮肤，详见 §3。

---

## 3. 提问，然后读结果

一次回答底下有四个信号，各回答一个不同的问题：

| 信号 | 回答的问题 | 怎么读 |
|---|---|---|
| 引用编号 `[n]` | 这句话的依据在哪条资料 | 点编号跳到原文、悬停看预览；越界编号由 `cite.verify` 当场剥离 |
| 引用覆盖率 | 有多少事实句挂了编号 | **形式指标**：只判编号在不在、超没超范围，不判编号指的那段是不是真在讲这句话 |
| 支撑度 `support` | 挂的号到底支不支持这句话 | 逐句与所引证据块开窗口算余弦取最大，再对型号 / 剂量 / 数字等事实锚点做句级否决 |
| 拒答 `refusal` / 低置信 | 库里到底有没有这个答案 | 拒答 = 不答；低置信 = 答但标黄 |

**覆盖率会骗人，支撑度补后半句。** 「GPU 推理需要 A100[1]」只要上下文里存在 [1] 就算被覆盖，哪怕 [1] 讲的是节点调度。支撑度逐句窗口最大余弦（整块口径会把单句语义稀释掉）加事实锚点否决（半数以上锚点落空即不算有据）才是内容级判据；弱支撑的回答标黄 `weak`，不删掉——删掉等于替你做决定。

**支撑度只在后台 `/api/ask` 链路计算，前台 `/api/ask` 不调用 `support.evaluate`。** 后台链路是 `… → cite.verify → verbatim_ratio → support.evaluate → SSE done`；前台链路到 `cite.verify → done` 为止，用证据分档 `high/mid/low`（`generate/evidence.py` 把「这批资料有多可信」量化成三档）代替。`http://127.0.0.1:8013/v1/chat/completions` 是纯转发网关，不进任何一条链路。前台链路与 `/v1` 网关都不算支撑度。

**证据分档 high / mid / low** 在前台生成前给出，模型据此决定措辞强度；它不直接拒答。拒答由 `engine.search` 里的 `refusal.min_score`（拒答最低分）+ `min_margin`（最低分差）判定，不达标走拒答模板而不是硬编。kb 2 用标定值 0.581，kb 3 / kb 4 用出厂 0.35，见 §5。

**SSE 流式**：`generation` 走 SSE（Server-Sent Events，服务端推送式流式输出），边生成边推 `delta`。本地 7B 一次完整回答实测 222 秒（CPU 推理，2026-11-07），SSE 已有，但检索完到首 token 之间仍静默——等待时没有进度可看，这是当前事实。

**三板块切换会换会话、换标题、换绑定库。** `config/instances.yaml` 只有一个实例 `uyghur-med`，`med` / `classic` / `pv` 三个板块开关全开：

| 板块 | 引擎 | 绑定 | 状态 |
|---|---|---|---|
| `med` 维医问答 | 主库 RAG | kb 2 | 开 |
| `classic` 古籍问答 | 独立库 RAG | kb 4 | 开 |
| `pv` 预问诊 | 纯对话：系统提示词 + 模型，不检索知识库，不出病历/结论 | — | 开 |

在「维医问答」问完一段，点「古籍问答」看到的是古籍自己的对话，切回来还是原来那段维医记录；顶栏标题、副标题、示例问题、侧栏库名与篇数一起换。预问诊只是一场对话（同一个聊天区、同一套气泡），切走再回来接着上一句；要重开就点侧栏那颗「+」（标题写着「重新开始」）。三个板块可在后台「站点」页的内联配置区里分别配显示名、开关、`translate` / `generate` 后端、专属提示词（`scope=med|classic`）；当前 `sections.*.roles` 均未配置，所以 `/api/info` 里 `section_roles.med` 与 `.classic` 解析结果相同——能力已具备，配置为空。界面文案（标题、欢迎语、免责声明、示例问题、库名）是前台固定内置值（`instcfg.default_copy`），不在提示词配置里改；**中维两套默认文案一一对应**（同句数、同含义），改一边就要改另一边，否则两个语言版本截图摆在一起对不上。

**站点语言与产出方式改配置文件**（2026-11-20）：后台「站点」页只留运行三项（端口 / 监听 / 开机）。`lang`（前台默认语言）、`kb_lang`（语料语言：`zh` 时维语提问先译成中文再检索）、`answer_mode`（`translate` 先中后译 / `direct` 直接维语作答）在 `config/instances.yaml` 里改；`alt.lang` 是右上那颗语言键能切到的另一种。这三项此前在「站点」页还摆过一份，保存时整份 PUT 写回配置，于是页面和文件各有一个值——现在只留文件一个落点。`instcfg` 每次请求现读 `instances.yaml`，改完刷新前台即生效，不用重启。

### 怎么输入维吾尔语

三条路，按"要不要先装东西"排：

| 方式 | 怎么做 | 适合 |
|---|---|---|
| 系统键盘 | Windows：设置 → 时间和语言 → 语言和区域 → 添加语言 → 搜 `ئۇيغۇرچە`（Uyghur, China）→ 装完用 `Win + 空格` 切；安卓 / iOS 在键盘设置里加「维吾尔语」 | 天天要打维语的人 |
| 页面里的「维语输入」 | 输入框左下角那颗小键盘键：上半是**拉丁转写**，照 ULY 打 `yaxshimusiz` → `ياخشىمۇسىز`，词首元音自动补 `ئ`（`ana` → `ئانا`）；下半是 8×4 **字母表**，点一下插到光标处 | 没装键盘、只有触屏、临时打一句 |
| 手机第三方输入法 | 讯飞、搜狗大多不带维吾尔语 | 不建议，兜底用上面那条 |

面板不遮输入框、不跳布局，按 `Esc` 收起；它只往输入框里写字符，写完照常回车发送。字母表按从右往左排（`ا` 在右上角），跟维语阅读顺序一致。

---

## 4. 参数：哪些能动，动了的后果

下表是 `config/default.yaml` 的现行值（2026-11-07），逐项给「改了会怎样」。每个知识库一套独立值，绝大多数改完立刻生效，分块类是例外。

| 参数键 | 现值 | 改了会怎样 |
|---|---|---|
| `chunking.size / overlap`（切块大小 / 重叠） | 480 / 80 | 标题优先、`min_chunk` 80。改大块更完整但更易混入噪声、召回变粗；只对之后新导入或重建索引的文档生效，不点重索引旧块不变 |
| `retrieval.dense.top_k / sparse.top_k`（两路召回条数） | 50 / 50 | 加大召回更全，但融合与精排更慢；减小可能漏掉真命中。两路要一起调，别只动一路 |
| `fusion.method`（融合方式） | `weighted` | w_dense 0.35 / w_sparse 0.50 / w_third 0；RRF 的 k=60 参数保留但不用。**结论**：加权融合实测 R@5 0.96~0.97 / MRR 0.89~0.94，RRF 只有 0.74~0.76 / 0.64（两套探针 × 两个种子 × n≈300，维医语料，2026-11-07；工具与数据已迁出 `model-eval`）。改回 RRF 会显著掉分。逐行对比与推导见 [ARCHITECTURE.md](ARCHITECTURE.md) §4.3 |
| `fusion.top_k`（送精排候选数） | 10 | 降到 8 会掉分；候选池缩到 12 条时 R@5 反跌到 75%（维医语料，2026-11-07）；升到 20 只慢不更好 |
| `rerank.top_k / max_length / batch_size / timeout_s`（精排条数 / 截断长度 / 批大小 / 超时秒） | 5 / 384 / 32 / 8.0 | `max_length` 改 512 更慢且更差（8.16 s / top1 0.636 / MRR 0.750，对比现值 3.07 s / 0.727 / 0.807，维医库 22 道口语题，2026-11-07）；超时 8 s 即回退融合顺序并打 `degraded=rerank_timeout` |
| `mmr.enabled` / `expand.enabled` / `query_rewrite.enabled`（MMR 去冗余 / 邻块扩展 / 查询改写） | false / false / false | 三项均未启用。打开会各自引入一条新分支与新的延迟、新的错误面，当前没有实测支撑，别当优化开关用 |
| `refusal.min_score / min_margin`（拒答最低分 / 最低分差） | 0.35 / 0.0 | 出厂值；kb 2 由按库标定覆盖为 0.581。**必须标定，不要手填** |
| `generation.temperature / max_tokens / context_token_budget`（生成温度 / 最大 token / 上下文预算） | 0.3 / 3072 / 6000 | `disable_thinking: true`，SSE 流式。温度调高答案更发散、更可能带通用知识；上下文预算决定能带多少证据进 prompt，砍太小会把真命中截掉 |
| `chat.history_turns / history_token_budget`（前台会话的上下文轮数 / 历史预算） | 5 / 1500 | 调大更连贯但更慢、更费预算；调小多轮追问会丢上下文 |

**库级参数是独立的一份全量快照**（`kb.config_json`，标 `__full__`），建库时复制一整套，之后全局改它不跟。判断「这个库手调过没有」是拿它和**出厂基线值**比，不是看键存不存在——全量快照里键永远在。参数优先级：`config/default.yaml`（唯一权威）→ `config/local.yaml`（用户改过的差异项）→ 库级 `kb.config_json` → 按库标定文件覆盖。

**界面入口按作用域分成两处**（2026-11-22）：知识库页「参数」栏目只管分块、召回、融合、精排这四组"资料怎么切、怎么检索"；拒答开关与三个阈值、生成温度、历史轮数、历史预算这七项在**「站点 → 回答行为」**。2026-11-22 起这一段**按模块各一份**：页签切「维医问答 / 古籍问答 / 预问诊」，维医写站点主库（`kb_id`）、古籍写 `classic_kb_id` 那个库、预问诊写 `previsit.yaml`（它不走检索，只有温度和单次上限两项）。改哪个模块只写哪个库，另外两个不动。面板顶上一行灰字会写明这一份存在哪个库；古籍没配独立库时它会直说「跟维医是同一份」。后台对话页是调试台，不再摆摘要条和「回答与会话」入口——不是删参数，是把旋钮放回真正消费它们的主体上。侧栏底部那颗「全局检索参数」也已删掉。要改出厂初值请直接改 `config/default.yaml`，或 `PATCH /api/settings`（接口保留，界面不再给入口）；老书签 `#settings` 会落到当前库的参数页。

**两条优先级链别混**（以代码为准）：

- 角色 `role` 解析链（`roles.py`）：**板块级 roles → 站点级 roles → 站点 `provider`（后端）**。每级用已知后端名校验，不合法继续下退；没有系统级 roles / 默认 provider 这一层，谁都不配就是空、由调用方明确报错。当前站点已配：`translate = siliconflow::tencent/Hunyuan-MT-7B`，`generate = siliconflow::Qwen/Qwen2.5-7B-Instruct`。站点这一列的下拉**只列「模型服务」页里已经加入的模型**（`config/models.yaml` 的 `models` 清单），探测回来的服务商全量目录（siliconflow 60 多个、aliyun 140 多个）不进候选——要用别的模型，先去模型页在端点卡的下拉里选一下，选过就会进清单，回来这里才看得见；配置里已有的值即使已被移出清单也仍然显示（后缀标「未加入清单」），不会被静默改掉。下拉里不再有「默认」项：要么留空跟随上级/后端下拉，要么选带名字的具体模型；只写了后端名的旧配置会自动落到该后端的 `model` 作为具体模型选中，保存一次写成显式 `后端::模型`。显示名为了好读做了压缩（去厂商前缀、去 `-Instruct`、全组共有的量化尾巴只留一份在悬停里），鼠标停上去始终是完整 id，保存写的也是完整值——界面短名不会改掉配置。**后台「站点」页的状态只在卡片里出现一次**（2026-11-22）：页顶那张独立的「当前运行」表已删——同一站点在页面上出现两次，还只读站点默认 `resolved`，漏掉板块自己的 `section_resolved`。现在卡头写名称 / 状态灯 / 时长 / 端口 / URL / 操作，卡头下面一条「当前生效」逐入口（维医 / 古籍 / 预问诊）显示真正解析后的模型：跟站点默认相同的标「继承」，独立配置的写实际后端名（`local` 与 `siliconflow` 都可能叫 7B，只写「独立」等于没说）。模型名沿用短名规则，悬停看完整 id；保存后即时同步。
- 提示词解析链（`promptcfg.py`）：**`by_provider`（后端）> `by_scope`（板块）> `default`（角色默认）> 内置（builtin）**。只有 `generate` / `translate` 两个角色参与；界面文案走前台固定内置值。当前 `config/prompts.yaml` 实配只有 `translate.by_provider.siliconflow.system`，`overridden_scopes` 为空，即没配板块级提示词覆盖。

源码注释曾与代码口径不一致（`promptcfg.py` 头注释与 `save()` docstring 声称板块优先于后端，`config/prompts.yaml` 头注释漏写 `by_scope`）；**本次重写已统一到代码口径**。历史记录与逐条对照见 [ARCHITECTURE.md](ARCHITECTURE.md) §12。

---

## 5. 标定：现行状态与核对命令

`refusal.min_score`（拒答）和 `support.threshold`（支撑度）是**这个嵌入模型 + 这批语料**的属性。换语料、换嵌入模型、换问法，旧数字作废，必须重标。

**现行只有 kb 2 有按库标定文件 `data/calibration/kb_2.json`**（2026-11-07 核对）：

- `calibrated_at=2026-11-06T09:01:29Z`、`method=real-questions`、`model=BAAI/bge-small-zh-v1.5@512`、`n_docs=31`、`reliable=true`。
- 拒答侧：`min_score=0.581`、`AUC=1.0`、`verdict=usable`、`min_margin=0.0`、`false_refusal_rate=0.0`、`missed_refusal_rate=0.0`、`n_samples=52`。
- 支撑度侧：`AUC=0.992`、`auc_fact=0.514`、`threshold=0.665`、两侧误判 0.0435、`n_samples=69`。

生效条件（`config.py:495-510`）：kb_id 非空 + 文件存在 + JSON 可读 + **模型指纹等于 `embedding.model@dim`**（即 `BAAI/bge-small-zh-v1.5@512`）。换嵌入模型会让指纹对不上，标定自动失效、退回粗筛。

- kb 3、kb 4 没有标定文件，用出厂 `0.35`，`mode=advisory`，提示「本库尚未标定，阈值是出厂值」。
- **不带 `kb_id` 的 `GET /api/status` 报的是全局口径**：`0.35, mode=advisory`，提示「全局标定样本不足，已回退出厂阈值」。这是全局口径，**不代表按库标定失效**——标定是按库存的，首页看的是全局。
- 全局 `/api/status` 的 `support` 段是 `threshold=0.55, weak_ratio=0.3, calibrated=false`；按库标定后改用 `0.665`。

**标定工具 `model-eval/calibrate.py` 已随评测台迁出为独立项目，本仓库不自带**；主项目只读 `data/calibration/` 里的结果。要重标得去 `model-eval` 项目跑，再把 `kb_<id>.json` 放回本目录。

**历史数字（已失效，仅作对照）**：更早的 `0.54`；评测台迁出前（2026-11 前）上一轮的 `0.692` / `AUC 0.959` / 61 组留一法 / 误拒 4.9% / 漏拒 23%；以及旧格式全局标定 `data/refusal_calibration.json`（`reliable:false`）。这些在 2026-10 实现期与 2026-11 前曾使用，**现均已失效**。现行判据只认 `kb_2.json` 的 `0.581 / AUC 1.0 / 52 组 / real-questions / 2026-11-06`，以及 kb 3、kb 4 与全局口径的出厂 `0.35`。

**可复制的核对命令**（服务按 8013 启动后）：

```bash
# 逐库看生效阈值：kb 2 应为 0.581 / calibrated / auc=1.0
curl -s "http://127.0.0.1:8013/api/status?kb_id=2" | python -m json.tool
# kb 3 / kb 4 应为 0.35 / advisory「本库尚未标定」
curl -s "http://127.0.0.1:8013/api/status?kb_id=3" | python -m json.tool
curl -s "http://127.0.0.1:8013/api/status?kb_id=4" | python -m json.tool
# 不带 kb_id = 全局口径 0.35 / advisory，别据此判定标定失效
curl -s "http://127.0.0.1:8013/api/status" | python -m json.tool

# 命令行逐库打印标定结论（含 AUC、样本数、事实层 AUC）
knowtune status

# 直接看现行标定文件
cat data/calibration/kb_2.json
```

---

## 6. 检索链路

一条固定流水线，不是 Agent：没有让大模型决定下一步调什么工具，分支只有若干布尔开关（精排、MMR、拒答、邻块扩展）。

**后台 `/api/ask`**：`effective_cfg` → `engine.search`（dense + sparse → 加权融合 → 邻块扩展(关) → 精排 → MMR(关) → 拒答判定） → 拒答分支 → `build_context` → `llm.stream` → `normalize_cites` → `cite.verify` → `verbatim_ratio` → `support.evaluate` → SSE `done`。

**前台 `/api/ask`**：翻译提问 → 按板块定 `doc_ids` → `engine.search` → 证据分档 → `build_context(provider=…, scope=…)` → 两段式生成 / 翻译 → `normalize_cites` → `cite.verify` → `done`。**前台不调用 `support.evaluate`**；支撑度目前只在后台 `/api/ask` 链路算。

```text
问题
  │
  ├─ dense 稠密召回   bge-small-zh 向量 / numpy 暴力点积 / top 50
  ├─ sparse 稀疏召回  jieba 预分词 → FTS5 BM25 / top 50
  │         └─ 双路各自失败各自降级：dense_unavailable / dense_disabled / sparse_failed
  ├─ 加权融合 weighted(0.35 / 0.50) → fusion.top_k = 10
  ├─ 邻块扩展 expand(关)
  ├─ 精排 rerank  bge-reranker-base CrossEncoder / 10×384 / top 5   ← CPU ~3 s，延迟大头
  │         └─ 六类精排降级：rerank_unavailable / rerank_model_missing /
  │                          rerank_load_failed / rerank_timeout / rerank_failed / rerank_bad_output
  ├─ MMR(关)        失败打 mmr_failed
  ├─ 拒答判定 refusal.min_score + min_margin  ── 不达标 ──► 拒答模板
  │                                            未标定 ──► refusal_uncalibrated
  ├─ build_context → LLM 流式生成（失败打 llm_error: 前缀）
  ├─ normalize_cites → cite.verify（引用覆盖率，形式指标）
  └─ 后台才做：verbatim_ratio → support.evaluate（逐句窗口最大余弦 + 事实锚点否决）
                                    └─► SSE done
```

**降级标记 `degraded` 分两组**：精排六类（`rerank_unavailable` 模型在失败集或未装 sentence-transformers、`rerank_model_missing` 本地权重找不到、`rerank_load_failed`、`rerank_timeout` >8 s、`rerank_failed`、`rerank_bad_output`）；引擎五类（`dense_unavailable`、`dense_disabled`、`sparse_failed`、`mmr_failed`、`refusal_uncalibrated`）。另 `llm_error:` 是生成侧失败前缀，不属于引擎级。每一段失败都降级并打标，不存在「静默返回空」这种看着正常、实际召回腰斩的状态。

---

## 7. 指标现状

全部为 2026-11-07 实测，条件随行给出。这是**现行**口径，不是设计目标。

| 数据 | 条件 |
|---|---|
| 冷启嵌入预热 43–93 秒 | 16 核 CPU；本次实测 43 s（热盘），冷盘首次约 93 s；期间只有 BM25 一路 |
| 精排 `10×384` = 3.07 s / top1 0.727 / MRR 0.807 | 维医库 22 道口语题，`model-eval/scan_rerank.py`（工具已迁出） |
| 不精排 0.09 s / top1 0.591 / MRR 0.691 | 同上 |
| `20×512` = 8.16 s / 0.636 / 0.750 | 同上，出厂旧值，最慢且最差 |
| `10×128` = 1.08 s / 0.773 / 0.822 | 同上，分数最高但只看标题首段，未采用 |
| 精排把 R@5 从 79.2% 提到 83.3% | 维医语料；代价 19 ms → 7.5 s；候选池缩到 12 条时反跌到 75% |
| 融合 `weighted(0.35/0.50)` R@5 0.96~0.97 / MRR 0.89~0.94 | 两套探针 × 两个种子 × n≈300；RRF 只有 0.74~0.76 / 0.64 |
| 支撑度 AUC 0.992、阈值 0.665、两侧误判 4.35% | 句 vs 异文档块，留一法 |
| 支撑度对事实篡改 AUC 0.514（≈抛硬币） | 同块 `300ml→700ml`；靠事实锚点兜，锚点捕获 84% |
| 本地 7B 一次完整回答 222 秒 | CPU 推理；SSE 已有，但检索完到首 token 仍静默 |
| 嵌入编码约 21 chunk/s | 本机 CPU |

**已随评测台迁出 `model-eval`、本仓库不自带的工具与数据**：`model-eval/scan_rerank.py`（精排参数扫描）、`model-eval/rerank_cases_wy.jsonl`（22 道口语题与真值）、`model-eval/calibrate.py`（标定）、评估集与两套探针数据。上表精排与融合两行的数字出自这些已迁出的工具，主项目不自带重跑能力；主项目只读 `data/calibration/` 结果。需求项 F5.1 / F5.2 / F5.4 标记为「曾实测，工具与数据已迁出」。

**需求覆盖 32 项有三套统计口径，不得只引用一套**（逐条状态与换算见 [ARCHITECTURE.md](ARCHITECTURE.md) §13）：

| 口径 | ✅ / ⚠️ / ⬜ | 来源 |
|---|---|---|
| 用户复核口径 | 24 / 4 / 4 | 用户此前确认的复核结论 |
| 曾实现并实测 | 25 / 4 / 3 | 评测台迁出前的实测状态 |
| 当前仓库自带 | 22 / 4 / 6 | 三项能力迁出后按「仓库自带」归类 |

差异全部来自 F5.1 / F5.2 / F5.4 随评测台迁至 `model-eval` 后如何归类。⚠️ 四项里 F2.4 的限制是「精排 CPU 延迟与语料适配」，不是缺模型；P0 / P1 全部落地。

---

## 8. 排障表（使用侧）

| 现象 | 原因 | 处理 |
|---|---|---|
| 短泛问题答错（「肚子疼」「头晕」） | 稠密余弦 0.60–0.62，与正常问题的分数区间重叠，阈值分不开；分数最高的块常来自不相关段落，系统不自知 | 把问法写具体：「用热沙子埋身子治什么病」能答对，「肚子疼」不能。别把高分当答对 |
| 古籍注解钓鱼 | 语料是带现代注释的版本，「与当代精准医学理念不谋而合」「思考：你认为……」对现代问题字面吸引力极强 | 不是检索算法的问题，是语料没区分原文与注解；看支撑度栏，弱支撑引用会标出来 |
| 引用按名次取，第 3–5 条常是噪声 | `retrieval.rerank.top_k: 5` 是**可配置**的条数，这 5 条全进 prompt（只受 token 预算截断），不按精排分数设下限 | 当前设计，不是漏做；改 `top_k` 能调条数，按分数裁剪是另一项独立改动、需要阈值标定，未启用 |
| 第一次提问慢 | 冷启 43–93 秒预热 + 首次精排载入 1.1 GB 权重 | 等 `GET /api/status` 的 `warmup.state=ready`；不是精排没预热（4096 条打分缓存已有）。本地 CPU 推理固有开销，换更快后端才是解法 |
| 翻译不走 | `translate` 角色当前解析为 `siliconflow::tencent/Hunyuan-MT-7B`；后端不可用、密钥失效或被判为无需翻译时会退原文并挂提示条 | 先看 `GET /api/info` 的角色解析，再去「模型服务」页确认该后端可用；别只重发问题 |
| 板块切换无内容 | 切换会换会话 / 标题 / 绑定库；目标库没绑、库不存在、或库里没有 `ready` 文档时会空白 | 查 `GET /api/info` 文档数与 `instances.yaml` 的 `kb_id` / `classic_kb_id` 是否指向本机存在的库（SQLite id 自增，换机器后不是原值）；改完在后台「站点」页应用或重启 |
| `GET /api/status` 误判「标定失效」 | 不带 `kb_id` 只报全局出厂 `0.35, mode=advisory` | 带 `?kb_id=2` 看该库生效值 0.581 / AUC 1.0 / calibrated；标定按库存，见 §5 |
| 只返回引用、没有答案 | 生成后端没配密钥或端点不可达 | 「模型服务」页加端点、填密钥；那一节的服务状态会显示端点是红的 |
| 精排显示「已关闭」 | 这个库的参数里关了（出厂默认开着） | 知识库页 → 参数 → 「精排与去重」→ 启用精排；库参数是独立全量快照，看的是本库的值 |

诊断入口：右侧「检索测试」面板会把两路召回的原始排名、融合分、精排分全列出来，是回答「为什么这段没被召回」的唯一工具。API 版：

```bash
curl -s -X POST http://127.0.0.1:8013/api/search/debug \
  -H 'Content-Type: application/json' \
  -d '{"question":"沙疗的适应证","kb_id":2,"top_k":5}'
```

---

## 9. 已知限制（使用侧）

本章讲**提问与切换时会撞上的现象**，不是工程待办清单。三份清单分工不同：工程与资产边界在 [README.md](../README.md)「已知限制」，需求逐项状态与三种统计口径在 [ARCHITECTURE.md](ARCHITECTURE.md) §13，从零部署与跑通判据在 [RUNBOOK.md](RUNBOOK.md)。以下每条给「为什么 / 影响 / 现在能怎么办」，都是确实还没解决的取舍。

1. **精排约 3 秒 CPU 延迟。** 为什么：CrossEncoder 对 10 条 × 384 token 逐对在 CPU 上跑一次前向，维医库 22 道口语题实测 3.07 s（2026-11-07）。影响：每次提问多等约 3 秒，本地 7B 更慢。现在能怎么办：接受；要快就关 `rerank.enabled`，代价是文档级 top1 从 0.727 掉到 0.591（同 22 题，2026-11-07）。检索阶段已推 `progress` 事件（[ARCHITECTURE.md](ARCHITECTURE.md) §5.5），界面会显示「正在检索」，这 3 秒不再全程空白；生成侧首 token 仍受模型速度限制。

2. **短而泛的问题答错，且系统不自知。** 为什么：余弦分数区间重叠，阈值分不开。影响：给出带引用的错答案，看起来和答对一样。现在能怎么办：把问法写具体；查询改写与领域词典扩展都未做，不指望系统兜底。

3. **古籍里的现代白话解读会「钓鱼」。** 为什么：语料没区分原文与注解，注解对现代问题字面吸引力强。影响：无关古籍被排到前列。现在能怎么办：看支撑度栏甄别弱支撑引用；语料层面拆分原文与注解是另一项改动，未启用。

4. **引用按名次取，不按精排分数设门槛。** 为什么：`rerank.top_k: 5`（`config/default.yaml:67`）是**可配置**的条数，`engine.py` 排序后取 `candidates[:top_k]`；缺的是**分数下限**，不是改条数的能力——前 5 名无论多低分都进上下文，只受 token 预算截断。影响：第 3–5 条常是噪声级也照送，引用覆盖率只是形式指标。现在能怎么办：改 `top_k` 能调条数；按分数裁剪需要新判据与阈值标定，当前没有。F5.3 支撑度判的是「这句话有没有依据」，不删低分证据。

5. **只有 kb 2 有按库标定，kb 3 / kb 4 用出厂 0.35。** 为什么：标定是「这个嵌入模型 + 这批语料」的属性，融合权重与标定都在维医语料上测的。影响：换语料后 BM25 与向量的强弱关系可能反转，拒答与融合判断变粗。现在能怎么办：换语料或换嵌入模型必须用已迁出的 `model-eval/calibrate.py` 重标再放回 `data/calibration/`，本仓库不自带重跑能力。

6. **单机单用户。** 为什么：SQLite + 进程内模型，无并发控制。影响：多人同时提问互相排队（精排单线程）。现在能怎么办：按单用户用；要多人用得上服务化，那是另一个架构。

7. **后台无入站鉴权，默认只监听 `127.0.0.1`。** 为什么：这是单机本地工具的既定取舍，不是漏做——只监听回环时外部暴露面本就不存在。影响：要开局域网须显式 `knowtune serve --host 0.0.0.0`，一旦开了任何人可调。现在能怎么办：别在不可信网络开 `0.0.0.0`；要上公网先补鉴权层。

8. **仓库带样例语料，不含运行资产。** 为什么：`data/` 与 `tools/llama/` 的大件被 `.gitignore` 排除（数据库、模型权重、密钥、本机上传的第三方文档），只保留目录骨架与两批可直接导入的公开语料——`data/uploads/weiyi/` 26 篇维医指南、`data/corpus/现代医学/` 20 篇常见病参考摘要。影响：clone 后必须自行下载两个模型、初始化数据库、绑定库 id；两批样例外语料可按 [RUNBOOK.md](RUNBOOK.md) 直接导入，不导也能跑。现在能怎么办：按 [RUNBOOK.md](RUNBOOK.md) 走一遍；备份只需整个 `data/`。

9. **GitHub git 端点在此网络环境返回 502**（2026-11-07 实测）。为什么：网络环境限制，与代码无关。影响：`git push` 走不通。现在能怎么办：推送走 `api.github.com` 的 Git Data API。

---

## 10. 命令速查

以 `src/knowtune/cli.py` 实际注册的子命令为准（下面是全部，没有 `eval`、没有 `previsit` 子命令）：

```bash
knowtune init                              # 初始化数据库与默认知识库，打印默认库 id
knowtune status                            # 环境 + 索引状态，逐库打印标定结论（含 AUC / 样本数）
knowtune kb list                           # 知识库清单
knowtune kb create <name>                  # 新建知识库
knowtune ingest <path> [--kb N]            # 导入文件或目录（目录递归）
knowtune ask "问题" [--kb N] [--provider NAME]   # 单次问答，走当前 provider（后端）
knowtune debug "问题" [--kb N] [--top-k N]       # 打印两路召回与融合排序，不调模型
knowtune serve [--port 8013] [--host 127.0.0.1]  # 启动 Web UI（默认 8000，实际用 8013）
```

`config/models.yaml` 已注册 `siliconflow` / `local`（7B，8097）/ `deepseek` 三个后端；密钥在界面填，不写进 `models.yaml`。填一次就记住：存 `data/secrets.json`（Windows 账户加密 DPAPI，换账户/换机器解不开），重启自动恢复。模型页顶部那行就是在说这件事——如果显示「密钥只存在本次会话」，点旁边的「记住到本机」把它打开，否则每次重启都要重填。HTTP 侧的 `curl` 核对命令见 §5 与 §8。

---

## 11. 本地模型 `.gguf`（可选路径）

云端 API 要密钥、要外网。本机有 `.gguf` 就能完全离线跑，代价是慢：本地 7B 一次完整回答实测 222 秒（CPU 推理，2026-11-07），SSE 已有但检索完到首 token 仍静默。**本地路径的价值是离线与隐私，不是速度。**

```bash
# ① 只做一次：把 llama-server 装到 tools/llama/（不进版本库）
python scripts/get_llama.py
# 没独显 / 想最小体积：python scripts/get_llama.py --backend cpu

# ② 无真模型时验证「生成 → 引用 → 支撑度」整条链路（假 OpenAI 端点）
python scripts/fake_llm.py --port 18080 --mode good   # 抄资料，应判 grounded
python scripts/fake_llm.py --port 18080 --mode bad    # 改数字，应被锚点抓住
```

**模型服务**页分成两节：上面「本地模型」（跑在这台机器上的 `.gguf`），下面「外部模型」（云服务或别的机器上的 OpenAI 兼容地址）。本地那一节是左右两栏：左边是**模型清单**（一行一个 .gguf，第二行写 `:端口 · 上下文 · 文件大小`，右边一颗启动键；当前用哪个不用专门设——启动哪个，哪个就是当前；这一节没有"新增端点"——本地模型只能从扫描结果里挑），右边「模型文件」区（点标题最左边的 ▾ 可整块收起，只留一行箭头 + 标题 + 环境结论；窗口窄到 1100px 以下自动上下堆叠）——点「浏览…」弹系统的「选择文件夹」窗口，选一个放 `.gguf` 的目录（也可以在输入框直接粘路径，批量用逗号隔开，粘完点「添加」）→ 目录进 chips 并自动扫一次 → 每个 `.gguf` 点「＋ 加入」（已在清单的那行给「移出」，只改清单、不删磁盘文件）。目录属于这台机器，存在浏览器里，换机器各填各的；扫描是只读的，不会自动往清单里塞东西。`local` 后端固定为 `http://127.0.0.1:8097/v1`（标准 OpenAI 兼容地址），一个模型一个进程一个端口。切到本地：对话页顶部后端下拉选 `local`，或在 `config/models.yaml` 里换。密钥在界面填、不入库。

起不来时按这三处查：二进制不在 `tools/llama` / PATH / `KNOWTUNE_LLAMA_SERVER`（重跑 `get_llama.py`）；显存不够（换更小量化或把 `context` 调小）；端口被占（配置写死的端口不会自动换，停占用者或改端口）。

**本地模型不自启**：重启后台服务不会拉起 llama.cpp 进程，得去「模型服务」页在清单里点「启动」。现行配置里 `local` 的默认模型是 `Qwen2.5-7B-Instruct-Q4_K_M`（:8097），加载 4.4GB 权重约 20 秒，期间那行显示「启动中」。没启就点预问诊会报「预问诊服务暂时不可用」——这是设计，不会偷偷改走云端。启动失败（显存不够、端口被占、文件被删）在「日志」里有一条 ERROR，写明原因。前台问答站的自启是另一回事，仍由 `config/instances.yaml` 控制。

**预问诊推理固定走本地**：`config/previsit.overrides.yaml` 里 `provider: local` + `model: Qwen2.5-7B-Instruct-Q4_K_M`，问诊的判断**不会**把患者原话发给云端。它在 `/v1` 网关上按 `X-KnowTune-Provider` 头路由到本机，模型没起来时直接报「预问诊服务暂时不可用」，**不会**偷偷改走云端，也没有规则兜底。**预问诊只有一段系统提示词**（`config/previsit.yaml` 的 `prompts.chat`）：每轮一次模型调用，把完整对话历史交过去，问什么、问几轮、何时收尾都由模型决定。2026-11-07 实测（RTX 3060 6G）开场 1.8 s、每轮 2.3 s。

**唯一的出机路径：维吾尔语会话的翻译**。本地 7B 写不出通顺维语，所以维语会话走「7B 中文推理 + 边界翻译」：患者那句维语先译成中文进上下文，模型的中文答复再译回维语给患者看。翻译模型由 `config/previsit.yaml` 的 `translator_provider` / `translator_model` 决定，默认 `deepseek-ai/DeepSeek-V4.1-Flash`（云端）——也就是说**维语会话里每个句子都会经过云端一次**。中英文会话完全不碰它。不想要这条出机路径：在后台「站点 → 入口与模型 → 预问诊」把翻译模型改成本地模型——但本地 7B 翻不出维语，此时维语会话会**直接报错**（前端用维语文案提示"服务暂时不可用"），不会把中文答案推给患者。判断依据：维语界面上出现中文就是坏掉了，宁可报错。

---

## 与其他文档的分工

本文只管**日常使用**：建库导入、提问读引用、参数含义、标定核对、指标现状、使用侧排障与限制、本地 `.gguf`。同一事实只在一处写深，避免各写一遍又写歪：

- [README.md](../README.md)：项目入口、事实卡、能力速览、文档地图、**工程 / 资产 / 方法边界**摘要。本文 §9 讲的是使用时会撞上的现象，与它不重复。
- [RUNBOOK.md](RUNBOOK.md)：从零 clone 到三板块可用的完整步骤、逐条 curl 判据、故障对照、备份。本文 §1 只给最短启动路径。
- [ARCHITECTURE.md](ARCHITECTURE.md)：现行架构、数据模型、检索 / 生成链、关键决策与实测推导（融合加权 vs RRF 在 §4.3）、需求对照三口径（§13）、代码与注释已知不一致、历史记录归档。本文 §4、§7 只给结论并指路。
- [PROJECT_MAP.md](PROJECT_MAP.md)：逐目录逐文件说明、启动顺序、配置优先级、改动落点。
- [TECH_STACK.md](TECH_STACK.md)：技术清单与分层、三个关键架构决策、启动速记、构建号与缓存失效。
- [WBS.html](WBS.html)：交付时的工作包与范围缺口**快照**，明确标「历史」，不用来判断现行状态。

最易写歪的三处——状态栏口径、标定现行值、需求三口径——以 [ARCHITECTURE.md](ARCHITECTURE.md) 与 [RUNBOOK.md](RUNBOOK.md) 的正文为准；本文的核对命令（§5）只用来快速确认，不替代推导。
