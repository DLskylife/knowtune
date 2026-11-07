# KnowTune 完整运行说明

> 从 `git clone` 到三板块（维医问答 / 古籍问答 / 预问诊）全部可用。
> 环境：Python 3.11+；命令以 Windows Git Bash 为例，Linux/macOS 把 `.venv/Scripts` 换成 `.venv/bin`。
> 面向"手上只有仓库、没有原机器"的人。每一步都标出**为什么**，踩坑时知道该往哪查。

---

## 0. 先说清楚：仓库里有什么，没有什么

**代码是完整的，缺的是运行期资产。** 远端 `master` 与本地工作树逐文件比对为 95 : 95，零差异——不存在"漏传文件"。clone 之后看起来空，是因为 `.gitignore` 按设计排除了下面这些：

| 不在仓库里 | 体积 / 性质 | 谁来生成 |
|---|---|---|
| `data/library.db` | SQLite 库、文档、chunk、向量 | 首次启动自动建表；内容靠你导入 |
| `data/models/` | 92 MB 嵌入 + 1.1 GB 精排权重 | 按 §2 下载 |
| `data/secrets.json` | API 密钥密文（Windows DPAPI，绑账户+机器） | 界面填写或环境变量，见 §3 |
| `data/corpus/`、`uploads/`、`vectors/` | 原始文件、上传件、向量矩阵 | 导入时自动写 |
| `data/calibration/` | 拒答/支撑度标定结果 | `model-eval` 产出，非必需 |
| `tools/llama/` | `llama-server` 可执行文件（约 100 MB） | `python scripts/get_llama.py` |
| `config/prompts/_history/` | 每次保存提示词的快照 | 后台自动写 |
| `.workbuddy/`、`_pm/`、`_shot*/` | 本地工作日志、截图、临时产物 | 本地留存，与运行无关 |

这么切是刻意的：**代码与配置可版本化，数据和密钥不该进版本库**。代价就是 clone 下来是空壳，必须按 §1–§7 走一遍。备份只需整个 `data/` 目录。

---

## 1. 装环境与依赖（顺序不能反）

```bash
git clone https://github.com/DLskylife/knowtune.git
cd knowtune
python -m venv .venv && . .venv/Scripts/activate

# ① 先装 CPU 版 torch，必须走官方 index
pip install torch --index-url https://download.pytorch.org/whl/cpu

# ② 再装本体
pip install -e ".[extract,local]"
```

**顺序是关键**：`local` extra 里的 `sentence-transformers` 会依赖 `torch`。若先跑 ②，pip 会拉默认 CUDA 版（体积约 10 倍），再跑 ① 只是覆盖一遍，白下几个 GB。CPU 机器上按上面顺序一次到位。

extras 说明：

| extra | 装什么 | 什么时候要 |
|---|---|---|
| `extract` | pypdf / python-docx / openpyxl / python-pptx / bs4 / trafilatura | 导 PDF、Word、Excel、PPT、网页时；只导 Markdown 可省略 |
| `local` | sentence-transformers | **必需**，本地嵌入与精排都靠它 |
| `onnx` | onnxruntime / fastembed | 可选加速路径，未实测，不必装 |
| `dev` | pytest / ruff / mypy | 开发时才要 |

`pyproject.toml` 写的是"实测可用的最低版本"，没锁 patch。要可复现就在装好后 `pip freeze > requirements.lock` 自己留一份（这个文件同样不进仓库，因为每台机器的 CUDA/CPU 组合不同）。

---

## 2. 下载两个本地模型（**目录名不一样，别写错**）

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

- 嵌入：`config/default.yaml` 里 `embedding.local_path: ./data/models/bge-small-zh-v1.5` 写死了路径，程序按这个字符串找。**若按旧文档下到 `BAAI--bge-small-zh-v1.5`，本地模型永远命中不上，检索只剩 BM25 一路。**
- 精排：`retrieval.rerank.local_path: null`，程序按 `data/models/<model 中 / 换成 --> ` 自动拼，所以是 `BAAI--bge-reranker-base`。

校验：

```bash
du -sh data/models/bge-small-zh-v1.5 data/models/BAAI--bge-reranker-base
# 期望约 92M 与 1.1G，且各自含 model.safetensors / config.json / tokenizer.json
```

`huggingface-cli` 不存在时先 `pip install -U huggingface_hub`（新版 CLI 叫 `hf download`，参数相同）；也可以直接从 HuggingFace / ModelScope 手动下载仓库内容到上述目录。

**精排不是可选项**：`config/default.yaml` 里 `rerank.enabled: true` 且 `allow_download: false`。不下载它，程序不会联网补，而是每次检索直接降级到融合分数排名（`degraded=rerank_model_missing`）——能跑，但召回质量明显下降。实测在维医语料上 R@5 从 79.2% 提到 83.3% 的那一截就没了。单次精排超过 8 秒会触发另一个降级 `rerank_timeout`，属正常保护。

---

## 3. 配生成后端（三选一）

`config/models.yaml` **已预置三个后端**，不需要从零新建：

| 名字 | 类型 | 说明 |
|---|---|---|
| `siliconflow` | 云端 OpenAI 兼容 | 默认生成/翻译走它，需要密钥 |
| `deepseek` | 云端 OpenAI 兼容 | 备用 |
| `local` | 本机 llama.cpp | `base_url: http://127.0.0.1:8097/v1`，其 `path` 是**开发机的绝对路径**（`D:/llamafactory/...gguf`），clone 后必须改成你自己的 `.gguf` 路径或删掉这个后端 |

配密钥两条路：

1. **界面填**（推荐个人机）：后台「模型」页 → 后端卡片 ✎ → 填 API Key → 保存。密文落 `data/secrets.json`，Windows 用 DPAPI 加密、绑当前账户与本机，拷走解不开。换账户/换机器需重填。
2. **环境变量**（服务器 / CI）：在 `models.yaml` 的后端下写 `api_key_env: YOUR_VAR`，程序自动读。优先级：本次填写 > 已保存密文 > 环境变量。

**完全不配密钥也能起服务**，但只能检索和返回引用，生成阶段没有答案——前台第一问答会"只有编号没有正文"。

想完全离线：装本地推理服务后把 `.gguf` 起成 OpenAI 兼容端点。

```bash
python scripts/get_llama.py              # 自动选平台；无独显用 --backend cpu
python scripts/get_llama.py --list       # 只看可下载资产
```

它会把 `llama-server` 装到 `tools/llama/`（该目录不进仓库）。具体启动参数、显存与实测速度见 [USER_GUIDE.md §11](USER_GUIDE.md)。

---

## 4. 初始化数据库

```bash
knowtune init        # 显式初始化，可跳过
```

其实**首次启动会自动完成**：`config.py` 里 `Config._resolve_paths()` 会 `mkdir(parents=True)` 建好 `data/`、`data/vectors/` 等目录；`db.conn()` 首次连接时走 `ensure_schema()` 执行 `src/knowtune/schema.sql` 建表（含 FTS5 虚表）；还会自动建一个名为 `default` 的知识库。

- 数据库文件：`./data/library.db`（WAL 模式，`*.db-wal` / `*.db-shm` 是它的伴生文件，都不要单独删）。
- `knowtune init` 的作用是让你在没起 Web 服务时先确认库可用，并打印默认库 id。

---

## 5. 建库与导入

界面路径：「知识库」页 → 新建（通用 / 代码 / 精确 / 空白四套预设）→「对话」页把文件拖进窗口。

命令行等价物：

```bash
knowtune kb list                       # 看库 id 与文档数
knowtune ingest <文件或目录>            # 导入
knowtune status                        # 库、文档、chunk、向量数
knowtune debug "某个问题"               # 只看两路召回与融合排序，不调模型
```

导入后要等切块与向量写完，文档状态变 `ready` 才可检索。**记下你新建的库 id**——下一步要用。

---

## 6. clone 后必做：把前台站点绑到自己的库

这是"服务起来了但前台一片空白"的头号原因。`config/instances.yaml` 里写的是**开发机上的库 id**：

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

- `kb_id` → 你的主知识库 id（必改）。
- `classic_kb_id` → 你的古籍库 id；**没有单独古籍库就填 `0`**，程序会退回按标题关键词（`classic_keywords`）在主库里分栏目。
- 顺便：`alt.kb_name` 是中文界面的侧栏显示名（留空回落到库表名）；`sections.<key>.name` 是三个板块的显示名，留空用界面语言的内置名；`enabled: false` 会让该板块连入口带接口一起消失。

改完在后台「站点」页点「应用」即可热生效，也可重启。

有个兜底值得知道：`classic_kb_id` 指向的库不存在、或库里一篇 `ready` 文档都没有时，程序按 `0` 处理而不是白屏——先改配置后搬数据（或反过来）的窗口期不会炸。

---

## 7. 启动与端口

```bash
knowtune serve --port 8013
# 未激活 venv 时：./.venv/Scripts/knowtune.exe serve --port 8013
```

| 入口 | 地址 | 说明 |
|---|---|---|
| 后台管理台 | `http://127.0.0.1:8013` | 建库、导入、调参、看日志；同一地址也是 Swagger |
| OpenAI 兼容网关 | `http://127.0.0.1:8013/v1` | 固定入口，`/v1/models`、`/v1/chat/completions`（含流式）、`/v1/completions` |
| 前台问答站 | `http://127.0.0.1:8800` | `instances.yaml` 里 `port: 8800` + `autostart: true`，**随主服务自动起，不用手动开** |

端口都是可配的：后台由 `--port` 决定，前台由 `instances.yaml` 的 `port` 决定。README 与其他文档里出现的 `8000` / `8765` 只是不同时期的示例值，含义相同，不必照抄。

**冷启要等**：嵌入模型加载实测 93 秒（16 核），期间 `embedder.available=false`，检索只有 BM25 一路。看 `GET /api/status` 的 `warmup.state` 变成 `ready` 再提问；前台已用流式光标与计时兜住这段静默，不代表卡死。

**安全边界**：服务默认只监听 `127.0.0.1`，且**后台没有任何入站鉴权**。要开局域网得显式 `knowtune serve --host 0.0.0.0`，别在不可信网络里这么干，更别直接暴露公网。

进程是"前台/会话绑定"的：在终端里跑，会话关掉服务就停。要常驻请自己配系统服务（Windows 计划任务 / systemd / supervisor）。

---

## 8. 三板块：开关、模型与提示词

三个板块都在后台「站点」弹窗里配，不需要改代码：

| 板块 | 引擎 | 可配内容 |
|---|---|---|
| `med` 维医问答 | 主库检索问答 | 显示名、开关、翻译模型、推理模型、专属提示词 |
| `classic` 古籍问答 | 独立库（或按标题分栏）检索问答 | 同上，与 `med` 完全独立 |
| `pv` 预问诊 | 独立预问诊引擎，不检索知识库 | 显示名、开关；点「模型与提示词」进参数面板 |

- 模型按板块配：`sections.<key>.roles.{translate,generate}`，留空跟随站点级 roles，选了只对该板块生效。解析链 `板块 → 站点 → 系统 → 后端默认`。
- 提示词按板块配：抽屉里作用范围显示「板块「维医问答」」，解析顺序 `板块 > 后端 > 角色默认 > 内置`；同后端也能给古籍与维医各配一套话术。
- 预问诊的配置文件都在仓库里：`config/previsit.yaml`、`previsit.overrides.yaml`、`previsit_kb.yaml`，参数面板改完写回这些文件。

---

## 9. 跑通判据（逐条验，别靠"页面能打开"）

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

通过标准：`warmup.state=ready`；库里有 `ready` 文档；`/api/info` 的文档数不为 0；答案带 `[n]` 且能点开原文；检索日志里没有 `degraded=rerank_model_missing`（说明精排模型路径对了）；三个板块切换后标题与内容各自变化。

---

## 10. 常见故障对照

| 现象 | 根因 | 处理 |
|---|---|---|
| 检索只有关键词一路、语义召回为空 | 嵌入模型下到了 `BAAI--bge-small-zh-v1.5`（目录名多了前缀） | 按 §2 改成 `data/models/bge-small-zh-v1.5`，或改 `embedding.local_path` 指过去 |
| 检索日志 `degraded=rerank_model_missing` | 精排权重没下或路径不对 | 下到 `data/models/BAAI--bge-reranker-base`；`allow_download: false` 不会自动补 |
| `import torch` 后磁盘暴涨 / 装了 CUDA 版 | 依赖安装顺序反了 | 按 §1 先装 CPU torch 再装本体 |
| 服务起了、前台侧栏 0 篇文档 | `instances.yaml` 的 `kb_id` / `classic_kb_id` 指向本机不存在的库 | 按 §6 改成自己的库 id，古籍无独立库填 `0` |
| 第一问只返回引用、没有答案 | 生成后端没配密钥或端点不可达 | 按 §3 填密钥 / 改 `models.yaml`；看后台「模型」页红点 |
| `huggingface-cli: command not found` | 没装 hub 工具 | `pip install -U huggingface_hub`，或手动下载 |
| 本地服务明明起着却探测超时 | `httpx` 读了 `HTTP_PROXY`，把 `127.0.0.1` 也送进代理 | 代码里已对 loopback 关掉 `trust_env`；若自己写客户端，同样设 `NO_PROXY=127.0.0.1,localhost` |
| 换机器后已保存的密钥失效 | DPAPI 密文绑账户+本机 | 界面重填，或改走 `api_key_env` 环境变量 |
| 8800 端口被占 | 上一次服务没退干净 | `netstat -ano | findstr :8800` 找 PID；详见 USER_GUIDE §8 |
| 首次提问特别慢 | 冷启 93 秒 + 首次精排载入 1.1 GB | 等 `warmup.ready`；这是本地 CPU 推理的固有开销，换更快后端才是解法 |

---

## 11. 换机器 / 备份

- **只备份 `data/`**：数据库、上传原件、向量、密钥密文、标定结果全在里面。代码从仓库 clone 即可。
- **模型不用备份**：按 §2 重下，或从旧机拷 `data/models/`。
- **密钥不能跨机拷**：`data/secrets.json` 是 DPAPI 密文，换账户/换机解不开，重填一次。
- `config/prompts.yaml`、`config/local.yaml`、`config/instances.yaml` 是**跟着仓库走的覆盖层**，在版本库里；本机改过的值想带到新机就一起带上，想回出厂就删掉覆盖项。

---

## 12. 文档地图

| 文档 | 看什么 |
|---|---|
| 本文 | 从零跑通、配置绑定、验证与排障 |
| [README.md](../README.md) | 项目概览、能力清单、命令速查、已知限制 |
| [USER_GUIDE.md](USER_GUIDE.md) | 怎么用、怎么读引用与支撑度、参数含义、标定、本地 `.gguf` |
| [PROJECT_MAP.md](PROJECT_MAP.md) | 每个文件干什么、五层依赖、两条主链路时序 |
| [ARCHITECTURE.md](ARCHITECTURE.md) | 数据模型、参数实测依据、拒答标定、§14 需求对照表 |
| [TECH_STACK.md](TECH_STACK.md) | 技术清单与三个关键架构决策 |
| [WBS.html](WBS.html) | 交付物 / 工作包 / 范围缺口 |
| `config/default.yaml` | 全部可调参数，每个默认值都写了为什么 |
