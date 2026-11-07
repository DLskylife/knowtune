# KnowTune 运行手册

> 从 `git clone` 到三板块（`med` 维医问答 / `classic` 古籍问答 / `pv` 预问诊）全部可用。
> 环境：Python ≥ 3.11；命令以 Windows Git Bash 为例，Linux/macOS 把 `.venv/Scripts` 换成 `.venv/bin`。
> 面向「手上只有仓库、没有原机器」的人。每一步给出判据，跑不通时知道该往哪查。

---

## 0. 仓库里有什么、没有什么

**代码是完整的，缺的是运行期资产。** 仓库基线提交 `72509d8`，96 个受版本控制文件，是主体代码 + 文档，不含运行资产。clone 之后看起来空，是因为 `.gitignore` 按设计排除了下面这些：

| 不在仓库里 | 体积 / 性质 | 谁来生成 |
|---|---|---|
| `data/library.db` | SQLite 库、文档、chunk、向量 | 首次启动自动建表；内容靠你导入（§4、§5） |
| `data/models/` | 约 92 MB 嵌入 + 约 1.1 GB 精排权重 | 按 §2 下载 |
| `data/secrets.json` | API 密钥密文（Windows DPAPI，绑账户 + 本机） | 界面填写或环境变量（§3） |
| `data/calibration/` | 按库标定结果 `kb_<id>.json` | 评测台产出，非必需（§9） |
| `tools/llama/` | `llama-server` 可执行文件（约 100 MB） | `python scripts/get_llama.py` |

这么切是刻意的：代码与配置可版本化，数据和密钥不该进版本库。代价是 clone 下来是空壳，**必须自己下模型、初始化数据库、绑定库 id**，按 §1–§7 走一遍。备份只需整个 `data/` 目录（§11）。

---

## 1. 装环境与依赖（顺序不能反）

```bash
git clone <你的仓库地址>
cd knowtune
python -m venv .venv && . .venv/Scripts/activate

# ① 先装 CPU 版 torch，必须走官方 index
pip install torch --index-url https://download.pytorch.org/whl/cpu

# ② 再装本体
pip install -e ".[extract,local]"
```

**顺序是关键**：`local` extra 里的 `sentence-transformers` 依赖 `torch`。先跑 ② 会拉默认 CUDA 版（体积约 10 倍），再跑 ① 只是覆盖一遍，白下几个 GB。CPU 机器上按上面顺序一次到位。

国内网络把 pip 换成镜像（装 CPU torch 那步仍须走 PyTorch 官方 index，镜像没有 CPU wheel）：

```bash
pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple
```

extras 说明：

| extra | 装什么 | 什么时候要 |
|---|---|---|
| `extract` | pypdf / python-docx / openpyxl / python-pptx / bs4 / trafilatura | 导 PDF、Word、Excel、PPT、网页时；只导 Markdown 可省略 |
| `local` | sentence-transformers | **必需**，本地嵌入与精排都靠它 |
| `onnx` | onnxruntime / fastembed | 可选加速路径，未启用，不必装 |
| `dev` | pytest / ruff / mypy | 开发时才要 |

`pyproject.toml` 写的是「实测可用的最低版本」，没锁 patch。要可复现就装好后 `pip freeze > requirements.lock` 自己留一份（同样不进仓库，每台机器 CPU/CUDA 组合不同）。

---

## 2. 下载两个本地模型（目录名不一样）

```bash
# 国内访问不便时先设镜像：export HF_ENDPOINT=https://hf-mirror.com
mkdir -p data/models

# ① 嵌入模型 —— 目录名【没有】 BAAI-- 前缀
huggingface-cli download BAAI/bge-small-zh-v1.5 \
  --local-dir data/models/bge-small-zh-v1.5

# ② 精排模型 —— 目录名【有】 BAAI-- 前缀
huggingface-cli download BAAI/bge-reranker-base \
  --local-dir data/models/BAAI--bge-reranker-base
```

两个目录命名规则不同，不是笔误：

- 嵌入：`config/default.yaml` 里 `embedding.local_path: ./data/models/bge-small-zh-v1.5` 写死字符串，程序按它找。下到带 `BAAI--` 的目录名会命中不上，检索只剩 BM25 一路。
- 精排：`retrieval.rerank.local_path: null`，程序按 `data/models/<model 里的 / 换成 -->` 自动拼，所以是 `BAAI--bge-reranker-base`。

校验：

```bash
du -sh data/models/bge-small-zh-v1.5 data/models/BAAI--bge-reranker-base
# 期望约 92M 与 1.1G（2026-11-07 实测），各自含 model.safetensors / config.json / tokenizer.json
```

`huggingface-cli` 不存在时先 `pip install -U huggingface_hub`（新版 CLI 叫 `hf download`，参数相同）；也可从 HuggingFace / ModelScope 手动下载仓库内容到上述目录。

**精排不是可选项**：`rerank.enabled: true` 且 `allow_download: false`，本地缺权重**不会联网自动补**，只会每次检索降级到融合分数排名（打 `degraded=rerank_model_missing`，`degraded` 即降级标记）。实测在维医语料上精排把 R@5 从 79.2% 提到 83.3%（2026-11-07，维医语料），那一截就没了。单次精排超过 8 秒触发另一个降级 `rerank_timeout`，属保护。补下按本节命令，不是调配置。

---

## 3. 配生成后端（三选一）

`config/models.yaml` **已注册三个 `provider`（后端）**，不需要从零新建：

| 名字 | 类型 | 说明 |
|---|---|---|
| `siliconflow` | 云端 OpenAI 兼容 | 当前激活，`Qwen/Qwen2.5-7B-Instruct`，需密钥 |
| `local` | 本机 llama.cpp | `http://127.0.0.1:8097/v1`，其 `path` 是开发机绝对路径（`D:/llamafactory/...gguf`），clone 后改成你自己的 `.gguf` 或删掉这个后端 |
| `deepseek` | 云端 OpenAI 兼容 | `deepseek-flash`，备用 |

**密钥在界面填，不写进 `models.yaml`**：后台「模型」页 → 后端卡片 ✎ → 填 API Key → 保存。密文落 `data/secrets.json`，Windows 用 DPAPI 加密、绑当前账户与本机，拷走解不开；换账户 / 换机器需重填（§10）。服务器场景可在 `models.yaml` 后端下写 `api_key_env: <变量名>` 让程序读环境变量，优先级：本次填写 > 已保存密文 > 环境变量。

`config/local.yaml` 里 `generation.provider: siliconflow` 是当前选中项；要换后端改这里或在界面切换。

**完全不配密钥也能起服务**，但只能检索和返回引用、生成阶段没有答案——前台第一问会「只有编号没有正文」。判据见 §9、§10。

想完全离线：用 `python scripts/get_llama.py` 把 `llama-server` 装到 `tools/llama/`（不进仓库），把 `.gguf` 起成 OpenAI 兼容端点；启动参数见 [USER_GUIDE.md](USER_GUIDE.md)。

---

## 4. 初始化数据库

以 `src/knowtune/cli.py` 实际命令为准：

```bash
knowtune init        # 初始化数据库与默认知识库，打印默认库 id；可跳过
knowtune status      # 环境、库、文档、chunk、向量数，逐库打印标定结论
```

其实**首次启动会自动完成**：`config.py` 建好 `data/`、`data/vectors/` 等目录；`db.conn()` 首次连接走 `ensure_schema()` 执行 `src/knowtune/schema.sql` 建表（含 FTS5 虚表），并自动建一个名为 `default` 的知识库。`knowtune init` 的作用是让你在没起 Web 服务时先确认库可用。

- 数据库文件：`./data/library.db`（WAL 模式，`*.db-wal` / `*.db-shm` 是伴生文件，都不要单独删）。
- `schema.sql` 共 8 张表 + 1 张 FTS5 虚表 + FTS5 的 5 张影子表。

---

## 5. 建库与导入

界面路径：「知识库」页 → 新建（通用 / 代码 / 精确 / 空白四套预设）→「对话」页把文件拖进窗口。

命令行等价物：

```bash
knowtune kb list                       # 看库 id 与文档数
knowtune kb create <name>              # 新建知识库
knowtune ingest <文件或目录> [--kb 1]  # 导入
knowtune status                        # 复核库、文档、chunk、向量数
knowtune debug "某个问题"              # 只看两路召回与融合排序，不调模型
```

导入后等切块与向量写完、文档状态变 `ready` 才可检索。**记下你新建的库 id**，下一步要用。参考口径：当前开发机 `data/library.db` 是 3 库、32 篇文档全部 `ready`、28020 个 chunk、28020 条 512 维向量（2026-11-07 实测）——这是搬过来的资产，不是 clone 后自动就有的。

---

## 6. clone 后必做：把前台站点绑到自己的库

这是「服务起来了但前台一片空白」的头号原因。`config/instances.yaml` 里写的是**开发机上的库 id**：

```yaml
instances:
- name: uyghur-med
  kb_id: 2            # 开发机的维医库
  classic_kb_id: 4     # 开发机的古籍库
  port: 8800
  autostart: true
  sections:
    med:     { enabled: true, name: '' }
    classic: { enabled: true, name: '' }
    pv:      { enabled: true, name: '' }
```

SQLite 的 id 是自增的，新机器上你建的库大概率是 `1`、`2`……**绝不是原机器的 2 和 4**。按 §5 查到的 id 改：

- `kb_id`（知识库 id）→ 你的主知识库 id，**必改**。
- `classic_kb_id`（古籍库 id）→ 你的古籍库 id；**没有独立古籍库就填 `0`**，程序退回按标题关键词（`classic_keywords: [古籍]`）在主库里分栏目。
- `autostart: true`（开机自启）保持，前台才会随主服务拉起；`sections.<key>.enabled: false` 会让该板块连入口带接口一起消失。
- `alt.kb_name` 是中文界面侧栏显示名；`sections.<key>.name` 是三个板块显示名，留空用界面语言内置名。

改完在后台「站点」页点「应用」热生效，也可重启。兜底：`classic_kb_id` 指向的库不存在、或库里一篇 `ready` 文档都没有时，程序按 `0` 处理而不是白屏——先改配置后搬数据的窗口期不会炸。

---

## 7. 启动与端口

```bash
knowtune serve --port 8013
# 未激活 venv 时：./.venv/Scripts/knowtune.exe serve --port 8013
```

| 入口 | 地址 | 说明 |
|---|---|---|
| 后台管理台 | `http://127.0.0.1:8013/` | 建库、导入、调参、看日志；同地址也是 Swagger |
| OpenAI 兼容网关 | `http://127.0.0.1:8013/v1` | `/v1` 纯转发到选中后端，不进 RAG 链路 |
| 前台问答站 | `http://127.0.0.1:8800/` | `instances.yaml` 的 `port: 8800` + `autostart: true`，**随主服务自动起，不另起进程** |

- 后台端口由 `--port` 决定（`cli.py` 默认 `8000`，本文统一用 `8013`）；前台端口由 `instances.yaml` 的 `port` 决定，范围 8800–8899（`instcfg.py` 的 `PORT_MIN/MAX`）。
- **`KNOWTUNE_GATEWAY` 由 `cli.py:234` 按实际 `--port` 写入**（如 `http://127.0.0.1:8013/v1`），供预问诊的 LLM 网关使用，不用手填、不用对齐别的端口。
- 前台是主服务进程内的 daemon 线程（`frontsite/runner.py`），嵌入模型与检索器只加载一次；不是独立程序。
- **冷启要等**：嵌入模型加载实测 43–93 秒（16 核 CPU；2026-11-07 热盘 43 s、冷盘首次约 93 s），期间 `embedder.available=false`，检索只有 BM25 一路。看 `GET /api/status` 的 `warmup.state` 变 `ready` 再提问。
- **安全边界**：服务默认只监听 `127.0.0.1`，且**后台没有任何入站鉴权**。要开局域网须显式 `knowtune serve --host 0.0.0.0`，别在不可信网络这么干。这是单机本地工具的既定取舍，不是漏做。
- 进程与终端会话绑定：终端关掉服务就停。要常驻自己配系统服务（Windows 计划任务 / systemd / supervisor）。

---

## 8. 三板块：开关、模型与提示词

三个板块都在后台「站点」弹窗里配，不改代码：

| `section`（板块） | 引擎 | 可配内容 |
|---|---|---|
| `med` 维医问答 | 主库 RAG | 显示名、开关、`translate` / `generate` 后端、专属提示词（`scope=med`） |
| `classic` 古籍问答 | 独立库 RAG（或按标题分栏） | 同上，与 `med` 完全独立 |
| `pv` 预问诊 | 独立预问诊引擎，不检索知识库 | 显示名、开关；「预问诊参数」面板改 `config/previsit.yaml` 等 |

- **模型按板块配**：`sections.<key>.roles.{translate,generate}`，留空跟随站点级 roles。解析链（`roles.py:81-111`）：**板块级 roles → 站点级 roles → 系统级 roles → 实例 `provider` → 默认 `provider`**，每级用已知后端名校验、不合法继续下退。当前 `sections.*.roles` 均未配置，`med` 与 `classic` 解析结果相同——能力已具备，配置为空。
- **提示词按板块配**：解析链以 `promptcfg.py:337-344` 代码为准：**`by_provider`（后端）> `by_scope`（板块）> `default`（角色默认）> 内置（builtin）**。仅 `generate` / `translate` 支持 by_provider 与 by_scope；`copy_ug` / `copy_zh` 只读角色级。三处注释（`promptcfg.py` 头注释、`save()` docstring、`config/prompts.yaml` 头注释）曾与代码口径不一致，**本次重写已统一**；历史记录见 [ARCHITECTURE.md](ARCHITECTURE.md) §12。
- **预问诊参数**：`config/previsit.yaml`（槽位、危险词、分诊规则、提示词）、`previsit.overrides.yaml`（运行时差异项）、`previsit_kb.yaml`（追问知识库），后台「前台站点 → 预问诊参数」写回。模型一律跟随 `/v1` 固定入口，**不在这些文件里钉后端 / 模型名**，也不写密钥。
- **界面文案已可配**：欢迎语、免责声明、示例问题、库名等走可配提示词 `copy_ug` / `copy_zh`，不再硬编码。

---

## 9. 跑通判据（逐条验，别靠「页面能打开」）

```bash
# ① 嵌入就绪
curl -s http://127.0.0.1:8013/api/status | grep warmup
#   期望 state = ready

# ② 库与文档
knowtune kb list && knowtune status

# ③ 三个板块开关
curl -s http://127.0.0.1:8013/api/instances | grep sections

# ④ 前台信息（库名、文档数、板块解析）
curl -s http://127.0.0.1:8800/api/info

# ⑤ 提问一次，看是否带引用编号
curl -s -N -X POST http://127.0.0.1:8800/api/ask \
  -H "Content-Type: application/json" \
  -d '{"question":"<你库里的问题>","corpus":"med","ui_lang":"zh"}'
```

通过标准：`warmup.state=ready`；库里有 `ready` 文档；`/api/info` 文档数不为 0；答案带 `[n]` 且能点开原文；检索日志里没有 `degraded=rerank_model_missing`（精排模型路径对了）；三个板块切换后标题与内容各自变化。

注意口径：前台 `/api/info` 报 `docs=31 / chunks=1442` 是**站点绑定库（kb2 + kb4）的口径**，与后台全局 `32 / 28020` 不矛盾——kb 3（临床药理）占大头，不属于该站点。别拿这两个数对不上当故障。

### 状态栏四个容易看错的口径（2026-11-07 实测）

- **`GET /api/status` 不带 `kb_id`** 报的是全局出厂配置：拒答 `min_score=0.35`、`mode=advisory`、提示「全局标定样本不足，已回退出厂阈值」。这是**全局口径，不代表按库标定失效**——标定是 `calibration`（标定）按库存的，首页看的是全局。
- **带 `?kb_id=2`** 才是维医库的现行标定：`min_score=0.581`、`auc=1.0`、`mode=calibrated`、`calibrated_at=2026-11-06T09:01:29Z`（`data/calibration/kb_2.json`，`method=real-questions`，52 组样本）。生效条件：kb_id 非空 + 文件存在 + JSON 可读 + 模型指纹等于 `embedding.model@dim`。
- **`?kb_id=3` / `?kb_id=4`** 返回 `0.35, mode=advisory`，提示「本库尚未标定，阈值是出厂值」：临床药理库与古籍库确实没有标定文件。当前只有 `kb_2.json` 存在；需要时用已迁出的 `model-eval/calibrate.py` 重跑再放回 `data/calibration/`。全局 `/api/status` 的 `support` 段是 `threshold=0.55, calibrated=false`；按库标定后改用 `0.665`。
- **精排看 `rerank.enabled=true` 且日志无 `degraded`**：三个库的 `retrieval.rerank.enabled` 均为 `true`，`/api/status` 报 `rerank.enabled=true, last_error=null`；检索日志里没有 `rerank_model_missing` / `rerank_timeout` 等降级标记，才算真的在跑。降级标记共六类（`rerank_unavailable` / `rerank_model_missing` / `rerank_load_failed` / `rerank_timeout` / `rerank_failed` / `rerank_bad_output`），加链路级的 `dense_unavailable` / `dense_disabled` / `sparse_failed` / `mmr_failed` / `refusal_uncalibrated` / `llm_error:`。

### 一个不会踩到的坑，别自己造出来

`config/previsit.yaml:86` 的 `retrieval.url: http://127.0.0.1:8000` **只在预问诊独立部署时生效**。主服务挂载时注入的是 `InProcessRetriever`（进程内查库，不看这个地址），LLM 网关由 `KNOWTUNE_GATEWAY` 按实际 `--port` 注入。所以按 §7 用 `8013` 启动**不会踩 8000 的坑**，不要为了「对齐端口」去改这一行。

---

## 10. 常见故障对照

| 现象 | 根因 | 处理 |
|---|---|---|
| 检索只有关键词一路、语义召回为空 | 嵌入模型下到 `data/models/BAAI--bge-small-zh-v1.5`（目录名多了前缀） | 按 §2 改成 `data/models/bge-small-zh-v1.5`，或改 `embedding.local_path` 指过去 |
| 检索日志 `degraded=rerank_model_missing` | 精排权重没下或目录名不对（少了 `BAAI--` 前缀） | 下到 `data/models/BAAI--bge-reranker-base`；`allow_download: false` 不会自动补 |
| `import torch` 后磁盘暴涨 / 装了 CUDA 版 | 依赖安装顺序反了 | 按 §1 先装 CPU torch 再装本体 |
| 服务起了、前台侧栏 0 篇文档 | `instances.yaml` 的 `kb_id` / `classic_kb_id` 指向本机不存在的库 | 按 §6 改成自己的库 id；古籍无独立库填 `0` |
| 第一问只返回引用、没有答案 | 生成后端没配密钥或端点不可达 | 按 §3 在界面填密钥 / 改 `models.yaml`；看后台「模型」页红点。完全不配密钥就是只有编号没有正文 |
| `huggingface-cli: command not found` | 没装 hub 工具 | `pip install -U huggingface_hub`（新版 CLI `hf download`），或手动下载到对应目录 |
| 本地服务明明起着却探测超时 | `httpx` 读了 `HTTP_PROXY`，把 `127.0.0.1` 也送进代理 | 对 loopback 关掉 `trust_env`；自己写客户端同样设 `NO_PROXY=127.0.0.1,localhost` |
| 换机器后已保存的密钥失效 | DPAPI 密文绑账户 + 本机 | 界面重填（§3），或改走 `api_key_env` 环境变量 |
| 8800 端口被占 | 上次服务没退干净 | `netstat -ano | findstr :8800` 找 PID 结束它；或改 `instances.yaml` 的 `port`（8800–8899） |
| 首次提问特别慢 | 冷启 43–93 秒（16 核 CPU）+ 首次精排载入 1.1 GB | 等 `warmup.state=ready`；本地 CPU 推理固有开销，换更快后端才是解法 |
| 状态栏显示「全局标定样本不足」/ 阈值 0.35 | 不带 `kb_id` 调 `/api/status` 只报全局出厂值 | 带 `?kb_id=2` 看该库生效值 0.581 / AUC 1.0；标定按库存，见 §9 四个口径 |
| 提示词改了不生效 / 板块与后端谁优先分不清 | 按已修正前的旧注释理解优先级 | 以 `promptcfg.py:337-344` 为准：`by_provider` > `by_scope` > `default` > 内置，见 §8；同名字段两层都配时后端级盖过板块级 |

---

## 11. 换机器 / 备份

- **只备份 `data/`**：数据库、上传原件、向量、密钥密文、标定结果全在里面。代码从仓库 clone 即可。
- **模型不用备份**：按 §2 重下，或从旧机拷 `data/models/`。
- **密钥不能跨机拷**：`data/secrets.json` 是 DPAPI 密文，换账户 / 换机解不开，重填一次（§3、§10）。
- `config/prompts.yaml`、`config/local.yaml`、`config/instances.yaml` 是跟着仓库走的覆盖层，在版本库里；本机改过的值想带到新机就一起带上，想回出厂就删掉覆盖项。**`instances.yaml` 的库 id 到新机器必须重绑（§6）**，不能照搬。
- 换嵌入模型必须重新标定：余弦分布不可比，模型指纹 `embedding.model@dim` 对不上时标定文件自动失效、退回粗筛。

---

## 12. 文档地图

| 文档 | 看什么 |
|---|---|
| 本文 | 从零跑通、配置绑定、验证判据与排障 |
| [README.md](../README.md) | 项目概览、能力清单、命令速查、已知边界摘要 |
| [USER_GUIDE.md](USER_GUIDE.md) | 怎么用、怎么读引用与支撑度、参数含义、标定、本地 `.gguf` |
| [PROJECT_MAP.md](PROJECT_MAP.md) | 每个文件干什么、五层依赖、两条主链路时序、配置优先级 |
| [ARCHITECTURE.md](ARCHITECTURE.md) | 数据模型、参数实测依据、拒答标定、需求对照三口径、代码已知不一致 |
| [TECH_STACK.md](TECH_STACK.md) | 技术清单与三个关键架构决策 |
| [WBS.html](WBS.html) | 交付时工作包 / 范围缺口快照（明确标「历史」，不冒充现行状态） |

`config/default.yaml` 是全部可调参数、每个默认值都写了为什么；`config/models.yaml` 是后端注册表；`config/previsit.yaml` 是预问诊参数主文件。改完任一文档跑 `python scripts/verify_docs.py`（链接、章节引用、失效口径、现行数字、优先级链、HTML 配平、源码语法一起验），输出 `ALL CHECKS PASSED` 再提交。

---

## 与其他文档的分工

本文只管「从零 clone 到三板块可用」这一条线，不写架构原理；README 的「从零部署」只给一句指路，不重复本文步骤。项目定位、能力速览、已知边界摘要在 [README.md](../README.md)；现行架构、实测数据、需求三口径、代码已知不一致、历史归档在 [ARCHITECTURE.md](ARCHITECTURE.md)；逐文件地图在 [PROJECT_MAP.md](PROJECT_MAP.md)，技术分层在 [TECH_STACK.md](TECH_STACK.md)，使用侧现象与本地 `.gguf` 在 [USER_GUIDE.md](USER_GUIDE.md)；[WBS.html](WBS.html) 是标「历史」的交付快照，不用来判断现行状态。状态栏口径、标定现行值、精排状态这几处最易写歪的事实只在一处写深：判据以本文 §9 为准，推导与历史以 [ARCHITECTURE.md](ARCHITECTURE.md) 为准。
