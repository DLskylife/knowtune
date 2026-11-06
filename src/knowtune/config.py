"""配置加载。YAML 是唯一真相来源，代码里不出现魔法数字。

相对路径一律相对**项目根**解析，而不是相对进程 CWD——否则从不同目录启动会
写到不同地方，这是个人项目最常见的"我的数据去哪了"事故。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping

import yaml
from pydantic import BaseModel, Field, PrivateAttr

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = ROOT / "config" / "default.yaml"
# 用户调参的落点。与 default.yaml 分开：那份文件里的注释（"为什么是 480
# 不是 512"）是这个项目最贵的资产，程序只能读它，绝不能回写。
LOCAL_CONFIG = ROOT / "config" / "local.yaml"


class StorageCfg(BaseModel):
    data_dir: Path = Path("./data")
    db_path: Path = Path("./data/library.db")
    vector_dir: Path = Path("./data/vectors")
    busy_timeout_ms: int = 5000
    keep_generations: int = 1


class EmbeddingCfg(BaseModel):
    model: str = "BAAI/bge-small-zh-v1.5"
    # 本地目录存在时优先用它，避免每次启动都去碰网络（国内网络下 HF 直连必挂）
    local_path: str | None = None
    dim: int = 512
    device: str = "cpu"
    batch_size: int = 16
    max_seq_length: int = 512
    normalize: bool = True
    threads: int = 0  # 0 = 交给 torch 决定；实测 16 核上 8 与 16 差异 <10%
    # 查询侧指令前缀。bge-zh-v1.5 是"对称"模型，但官方对短查询检索长段落
    # （s2p）明确要求给查询加指令句；实测能抬召回（见 eval_recall）。
    # 只加在查询上，**索引语料绝不能加**——两边格式必须一致才可比。
    query_instruction: str = ""


class ChunkingCfg(BaseModel):
    size: int = 480
    overlap: int = 80
    respect_headings: bool = True
    min_chunk: int = 80
    # 分块策略。三种针对不同文体：
    #   size      按长度打包，长文/论文默认，块大小最均匀
    #   heading   不跨标题合并，手册/规格书用，保证一块只讲一件事
    #   paragraph 每段一块，问答/条目/代码片段用，块边界跟原文一致
    # 之前 UI 上有个 respect_headings 开关，但它从来没被 chunker 读过——
    # 调了没反应比没有这个选项更糟，所以这里做成真正参与切分的策略。
    strategy: str = "size"


class DenseCfg(BaseModel):
    enabled: bool = True
    top_k: int = 50


class SparseCfg(BaseModel):
    enabled: bool = True
    top_k: int = 50
    segmenter: str = "jieba"
    userdict: str | None = None


class FusionCfg(BaseModel):
    method: str = "rrf"
    k: int = 60
    w_dense: float = 0.50
    w_sparse: float = 0.35
    w_third: float = 0.0
    top_k: int = 20
    # 每路前 N 名保底进候选池（见 engine._apply_quota）。0 = 关。
    quota: int = 0


class RerankCfg(BaseModel):
    enabled: bool = True
    model: str = "BAAI/bge-reranker-base"
    # 与 embedding 同理：本地目录优先，找不到就不碰网络（见 allow_download）
    local_path: str | None = None
    allow_download: bool = False
    top_k: int = 5
    batch_size: int = 8
    max_length: int = 512
    timeout_s: float = 10.0
    allow_override: bool = True


class MmrCfg(BaseModel):
    enabled: bool = False
    lambda_: float = Field(default=0.65, alias="lambda")

    model_config = {"populate_by_name": True}


class RewriteCfg(BaseModel):
    enabled: bool = False
    max_variants: int = 3


class ExpandCfg(BaseModel):
    """邻块扩展：把命中的块前后各 N 块一起拉进来。

    切分必然会把一个完整论点劈开，尤其是跨块边界的那句。命中块相邻的那块
    常常就是"答案的另一半"，而它自己因为缺开头/缺结尾，向量分不够进不了
    Top-K。扩展是为了补这个结构性的漏检。
    """

    enabled: bool = False
    neighbors: int = 1        # 前后各取几块
    discount: float = 0.65    # 邻块分数折扣，确保不会挤掉真正的命中块
    seed_top: int = 8         # 只扩展前 N 名，避免噪声块带进一串邻居
    # 扩展后的候选池上限。原来写死 60：宽 seed_top + 宽 neighbors 能把列表
    # 撑到几百条，精排会白算一堆注定进不了 top_k 的块；但砍太狠等于把扩展
    # 的意义抹掉。60 是"够精排挑、又不至于把 CPU 拖死"的经验值。
    max_candidates: int = 60


class RetrievalCfg(BaseModel):
    dense: DenseCfg = DenseCfg()
    sparse: SparseCfg = SparseCfg()
    fusion: FusionCfg = FusionCfg()
    rerank: RerankCfg = RerankCfg()
    mmr: MmrCfg = MmrCfg()
    expand: ExpandCfg = ExpandCfg()
    query_rewrite: RewriteCfg = RewriteCfg()


class RefusalCfg(BaseModel):
    enabled: bool = True
    min_score: float = 0.35
    min_margin: float = 0.02
    # ---- 以下三项原先是 engine.py 里的模块常量，写死不可调 ----
    # margin 取前几条算。这个窗口决定了"几条候选算一批"，也是标定的口径：
    # 改了窗口，历史标定出来的 min_margin 就不再可比（标定脚本读的也是它）。
    margin_window: int = 5
    # 低置信提醒：top1 低于 min_score + soft_margin 时给一句"相关度偏低"，
    # 不拦截、只提示。0.10 来自实测分布（可答最低 0.560 / 硬阈值 0.35）。
    soft_margin: float = 0.10
    # margin 低于此值也提示"前几条几乎持平"，与 min_margin(拒答) 是两件事：
    # 一个用来拒，一个用来提醒，阈值自然不该共用。
    soft_floor: float = 0.015
    calibrated_at: str | None = None
    calibrated_on_model: str | None = None
    # 标定时的正/负样本是否够用。样本太少依然会套用（总比随手写的 0.35 强），
    # 但 UI 要如实标出来，不能让人以为这个数字有多可靠
    calibrated_samples_ok: bool = False
    # ---- leave-one-doc-out 标定的产物 ----
    # AUC = 随机取一条"库里有答案"和一条"库里没有"，前者分数更高的概率。
    # 它比阈值本身重要：阈值可以调，信号有没有区分力不能调。
    auc: float | None = None
    # usable(AUC>=0.8) / weak(0.7~0.8) / invalid(<0.7)。invalid 时**不采用**
    # 扫出来的阈值——宁可退回"明显不沾边才拒"，也不要拿一个和抛硬币差不多的
    # 信号去拦用户。
    verdict: str | None = None
    n_samples: int = 0
    # 标定结果单独存一个 JSON，而不是回写 YAML：回写会丢掉 YAML 里的注释，
    # 而注释是这个项目最贵的东西之一
    calibration_file: str = "./data/refusal_calibration.json"


class SupportCfg(BaseModel):
    """答案支撑度：生成之后的校验，不是检索之前的猜测。

    与 RefusalCfg 分开，是因为它们回答的问题根本不同：拒答问「库里有没有」，
    支撑度问「答案有没有被库里的资料接住」。后者能算准，前者算不准——所以
    真正该做拦截判断的是后者，前者只保留一个「明显不沾边」的粗筛。

    threshold 是经验初值（0.55，bge-small 上「同主题但不同事实」的句对通常
    落在 0.35~0.5，「同一事实换个说法」在 0.65 以上）。**未标定**，所以 UI 上
    必须标出这一点，别让用户以为它是扫出来的。
    """

    enabled: bool = True
    threshold: float = 0.55
    # 每份答案最多判几句。长答案全算会让一次问答多花几百毫秒，收益却集中在
    # 前几句结论上；超出的部分交给 grounded 的整体比例兜底。
    max_sentences: int = 24
    min_answer_chars: int = 40
    # 低于这个整体的 grounded 视为「没落地」，前端给醒目提示。
    weak_ratio: float = 0.3
    # ---- 标定产物，语义同 RefusalCfg 的同名字段 ----
    # 这里有两个 AUC，别混：
    #   auc        句 vs「别的文档的块」  —— 测的是"有没有依据"
    #   auc_fact   句 vs「同一块但改掉事实」—— 测的是"事实对不对"
    # 后者天然接近 0.5：余弦对「300ml → 700ml」这种改动几乎无感。所以
    # auc_fact 低**不是**停用信号的理由，它的意思是"这一层得靠锚点补"。
    auc: float | None = None
    auc_fact: float | None = None
    verdict: str | None = None
    n_samples: int = 0
    # 锚点对篡改事实的捕获率。低就说明锚点表还缺单位/型号。
    anchor_catch: float | None = None


class ModelEntry(BaseModel):
    """一个模型。它现在是配置里的一等实体，不再是后端下面的一个字符串。

    为什么要变成实体：以前「模型」只是 `model: "xxx"` 一个字段，加上
    `model_list` 里的一串 id。于是「这个模型的上下文多大」「它的 gguf 在哪」
    「它该监听哪个端口」这三件事没有地方放，只能散落在代码常量和 UI 提示里。
    本地实例一多，端口冲突和上下文超限就都变成运行时才发现的坑。

    现在这三件事跟着模型走：
      - `id`   发给 OpenAI 接口的 model 字段
      - `path` 本地 .gguf 的绝对路径；有它才说明"这个模型本机能起"
      - `port` 本机起服务时占的端口；空 = 启动时自动挑
    """

    id: str
    # 界面上显示的名字。留空用 id——文件名常常带着量化后缀，展示时不该照搬。
    alias: str = ""
    context: int | None = None      # 上下文长度，0/None = 未填
    max_output: int | None = None   # 单次最大输出，0/None = 未填
    path: str | None = None         # 本地 gguf；非空 = 本机可起
    port: int | None = None         # 本机监听端口

    @property
    def label(self) -> str:
        return self.alias or self.id

    @property
    def local(self) -> bool:
        return bool(self.path)


class ProviderCfg(BaseModel):
    """一个 OpenAI 兼容端点。只有两种：本机能起的（local）和别的（openai）。

    以前这里有八种 kind（vllm / lmstudio / ollama / deepseek / bailian /
    openai / custom / mock），每种都带一段"怎么启动它"的命令模板。问题在于
    那八种说的是**同一个协议**，区别只在"谁在跑"和"怎么起"——而"怎么起"
    在本地模型这件事上早就有更好的答案（见 localmodels.py：直接读 gguf 起
    llama.cpp），云端则根本没有启动命令可给。八种类型带来的全部收益，就是
    八套会过期的提示文案。

    现在只留两种语义：
      local  —— 本机能起。模型带 path，界面给「启动 / 停止」。
      openai —— 任何兼容 /v1 的地址。云端、别人搭的网关、容器里的服务都算。
    """

    base_url: str | None = None
    model: str = ""
    kind: str = "openai"            # local | openai，只用于界面分组
    # 该端点"我打算用"的模型清单。现在是实体列表，带路径和端口。
    # YAML 里写成 `models:`——`model_list` 是旧名字，保留字段名兼容已有代码，
    # 但配置一律用新写法（populate_by_name 让两种都能解析）。
    model_list: list[ModelEntry] = Field(default_factory=list, alias="models")
    # 环境变量名（不是密钥本身）。配了就读它，用于"密钥完全不进这个应用"的部署。
    api_key_env: str | None = None
    # 探测时用：按"刚敲进去的地址+密钥"试一次，不经过注册表
    _inline_key: str = PrivateAttr(default="")

    model_config = {"populate_by_name": True}

    def set_inline_key(self, key: str | None) -> None:
        self._inline_key = (key or "").strip()

    def entry(self, model_id: str = "") -> ModelEntry | None:
        """按 id 找清单里的那条。找不到返回 None（调用方自己决定回退）。"""
        want = (model_id or self.model or "").strip()
        for m in self.model_list:
            if m.id == want:
                return m
        return None

    def api_key(self, name: str | None = None) -> str | None:
        """取密钥。优先级：本次探测临时填的 > 内存（含落盘恢复）的 > 环境变量。

        第三级是给服务器 / CI 用的：把密钥放进 `api_key_env` 指向的环境变量，
        这个应用就永远不需要人手填，也不会有密钥落在 data 目录里。
        `api_key_env` 为空就跳过，不去猜 `DEEPSEEK_API_KEY` 那种通用变量名。
        """
        if self._inline_key:
            return self._inline_key
        if name:
            from . import secrets as secrets_mod

            got = secrets_mod.get(name)
            if got:
                return got
        env_name = (self.api_key_env or "").strip()
        if env_name:
            return (os.environ.get(env_name) or "").strip() or None
        return None


class TimeoutCfg(BaseModel):
    connect: float = 5.0
    read: float = 60.0
    write: float = 10.0
    pool: float = 5.0


class RetryCfg(BaseModel):
    max_attempts: int = 3
    initial_backoff_s: float = 1.0
    max_backoff_s: float = 10.0


class GenerationCfg(BaseModel):
    # 空字符串 = 还没配任何端点。这是合法状态：全新安装时配置里一条模型都没有，
    # 界面上引导用户「扫描本地模型」或「加一个 OpenAI 兼容地址」，而不是先塞一个
    # 指向某个端口的占位后端——那种占位在用户机器上永远是红的，只会让人以为坏了。
    provider: str = ""
    providers: dict[str, ProviderCfg] = {}
    # 角色分工：{"translate": "local::qwen2.5-3b", "generate": "local::qwen2.5-3b"}。
    # 留空 = 所有活儿都由 provider 干，配了才分工。
    roles: dict[str, str] = {}
    # 0.1 会让 7B 这一档的输出高度模板化（三条短句 + 一句空话收尾），
    # 事实约束靠下方【资料】与引用校验，不靠把温度压到接近贪心。
    temperature: float = 0.3
    max_tokens: int = 3072
    context_token_budget: int = 6000
    # 装填时的地板：预算被历史吃光后仍至少留这么多字符给资料，否则模型拿到
    # 的是一个空【资料】段。原先 prompt.py 和 chatctx.py 各自写死 200，
    # 两处必须同源，否则前端提示的"还剩多少"和真实装填结果对不上。
    min_context_chars: int = 200
    stream: bool = True
    pii_policy: str = "warn"
    # 关掉思考模式。推理过程本来也不给用户看，留着只会：① 漏进答案变成自言自语；
    # ② 小模型把草稿当思考再复述一遍，答案重复两遍。想要推理就置 False。
    disable_thinking: bool = True
    timeout: TimeoutCfg = TimeoutCfg()
    retry: RetryCfg = RetryCfg()

    def active(self) -> tuple[str, ProviderCfg]:
        if self.provider not in self.providers:
            raise KeyError(
                f"generation.provider={self.provider!r} 不在 providers 中；"
                f"可选：{sorted(self.providers)}"
            )
        return self.provider, self.providers[self.provider]


class ChatCfg(BaseModel):
    history_turns: int = 5
    history_token_budget: int = 1500


class EvidenceCfg(BaseModel):
    """检索证据分档的阈值比例。

    分档本身复用 refusal.min_score（"够不够硬"的已知分界线），这里只调
    "中档从哪开始"：top1 >= min_score 为 high，>= min_score * mid_ratio 为 mid，
    其余 low。原先 0.6 是 evidence.py 里的模块常量。
    """

    mid_ratio: float = 0.6


class PdfCfg(BaseModel):
    min_text_chars_per_page: int = 20


class UrlCfg(BaseModel):
    allowed_domains: list[str] = []
    max_depth: int = 2
    max_pages: int = 200


class IngestCfg(BaseModel):
    max_file_mb: int = 512
    exclude_patterns: list[str] = []
    pdf: PdfCfg = PdfCfg()
    url: UrlCfg = UrlCfg()


class ObservabilityCfg(BaseModel):
    log_level: str = "INFO"
    log_json: bool = True
    sample_rate: float = 1.0
    log_question_text: bool = False


class Config(BaseModel):
    version: int = 1
    prompt_version: str = "qa_v1"
    storage: StorageCfg = StorageCfg()
    embedding: EmbeddingCfg = EmbeddingCfg()
    chunking: ChunkingCfg = ChunkingCfg()
    retrieval: RetrievalCfg = RetrievalCfg()
    refusal: RefusalCfg = RefusalCfg()
    support: SupportCfg = SupportCfg()
    evidence: EvidenceCfg = EvidenceCfg()
    generation: GenerationCfg = GenerationCfg()
    chat: ChatCfg = ChatCfg()
    ingest: IngestCfg = IngestCfg()
    observability: ObservabilityCfg = ObservabilityCfg()

    def resolve_paths(self, base: Path = ROOT) -> "Config":
        """把 YAML 里的相对路径钉到绝对路径，避免 CWD 漂移。"""
        self.storage.data_dir = base / self.storage.data_dir
        self.storage.db_path = base / self.storage.db_path
        self.storage.vector_dir = base / self.storage.vector_dir
        if self.embedding.local_path:
            self.embedding.local_path = str(base / self.embedding.local_path)
        if self.retrieval.rerank.local_path:
            self.retrieval.rerank.local_path = str(base / self.retrieval.rerank.local_path)
        if self.retrieval.sparse.userdict:
            self.retrieval.sparse.userdict = str(base / self.retrieval.sparse.userdict)
        self.refusal.calibration_file = str(base / self.refusal.calibration_file)
        return self

    def ensure_dirs(self) -> None:
        self.storage.data_dir.mkdir(parents=True, exist_ok=True)
        self.storage.vector_dir.mkdir(parents=True, exist_ok=True)
        self.storage.db_path.parent.mkdir(parents=True, exist_ok=True)


_CACHE: dict[str, Config] = {}


def _read_yaml(p: Path) -> dict[str, Any]:
    if not p.exists():
        return {}
    try:
        return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        # 配置写坏了也要能启动：报一行、退回出厂值，总好过整个服务打不开
        print(f"[config] {p.name} 解析失败，已忽略：{exc}")
        return {}


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, val in overlay.items():
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], val)
        else:
            out[key] = val
    return out


def calibration_of(cfg: "Config") -> dict[str, Any] | None:
    """读标定文件；模型指纹不匹配返回 None（旧模型的阈值不可用）。"""
    import json

    path = Path(cfg.refusal.calibration_file)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if data.get("model") != f"{cfg.embedding.model}@{cfg.embedding.dim}":
        return None
    return data


def refusal_mode(cfg: "Config") -> str:
    """拒答现在到底处于哪种状态。

    四种，界面上必须区分开——把 advisory 显示成"阈值 0.35"是过去最误导人的
    一处：数字看着像个决定，实际上 0.35 低于本语料全部实测可答分（最低 0.541），
    除了检索完全空，它一次都不会触发。

    - off        用户主动关了
    - calibrated 阈值由 leave-one-doc-out 扫出来且 AUC 达标，可以当判据
    - advisory   没标定 / 样本不足，只在"明显不沾边"时拦，其余交给人看引用
    - invalid    标定过了，但 AUC < 0.7：这个信号分不开"有答案"和"没答案"，
                 阈值再调也是抛硬币。此时**不套用**扫出来的数字，退回粗筛，
                 并在界面上直说"这个判据不成立"，而不是换个数继续装。
    """
    if not cfg.refusal.enabled:
        return "off"
    if cfg.refusal.verdict == "invalid":
        return "invalid"
    if cfg.refusal.calibrated_at and cfg.refusal.calibrated_samples_ok:
        return "calibrated"
    return "advisory"


# ------------------------------------------------------------------ 按库标定

def calibration_path(cfg: "Config", kb_id: int) -> Path:
    """某个知识库的标定文件。

    按库分开而不是全库一份：阈值是「这个嵌入模型 + 这批语料」的属性。维医库
    31 篇同质文档，药理库 2.6 万块单文档，两者的分数分布根本不可比，共用一个
    阈值必然一边松一边紧。
    """
    base = Path(cfg.refusal.calibration_file).parent
    return base / "calibration" / f"kb_{int(kb_id)}.json"


def calibration_of_kb(cfg: "Config", kb_id: int | None) -> dict[str, Any] | None:
    """读该库的标定结果；没有、坏了、或模型指纹不匹配都返回 None。"""
    import json

    if not kb_id:
        return None
    path = calibration_path(cfg, kb_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if data.get("model") != f"{cfg.embedding.model}@{cfg.embedding.dim}":
        return None
    return data


def _verdict_of(auc: float | None) -> str | None:
    """AUC → 三档。界线不是拍的：0.7 以下两类分布重叠过半，任何阈值都会
    在"该拒的照答"和"该答的被拒"之间二选一；0.8 以上才谈得上"判据"。"""
    if auc is None:
        return None
    if auc >= 0.8:
        return "usable"
    if auc >= 0.7:
        return "weak"
    return "invalid"


def apply_kb_calibration(cfg: "Config", data: dict[str, Any] | None,
                         skip: "set[str] | frozenset[str]" = frozenset()) -> None:
    """把某个库的标定结果套进这份配置副本。

    skip 语义同全局标定：参数面板手调过的字段不被覆盖。
    """
    if not data:
        return
    ref = data.get("refusal") or {}
    if ref:
        auc = ref.get("auc")
        verdict = ref.get("verdict") or _verdict_of(auc)
        cfg.refusal.auc = auc
        cfg.refusal.verdict = verdict
        cfg.refusal.n_samples = int(ref.get("n_samples") or 0)
        cfg.refusal.calibrated_on_model = data.get("model")
        if verdict == "invalid":
            # 信号无效：不写阈值。写进去等于用一个和抛硬币同水平的数拦人。
            cfg.refusal.calibrated_at = None
            cfg.refusal.calibrated_samples_ok = False
        else:
            if "refusal.min_score" not in skip and ref.get("min_score") is not None:
                cfg.refusal.min_score = float(ref["min_score"])
            if "refusal.min_margin" not in skip and ref.get("min_margin") is not None:
                cfg.refusal.min_margin = float(ref["min_margin"])
            cfg.refusal.calibrated_at = data.get("calibrated_at")
            cfg.refusal.calibrated_samples_ok = True

    sup = data.get("support") or {}
    if sup:
        cfg.support.auc = sup.get("auc")
        cfg.support.auc_fact = sup.get("auc_fact")
        cfg.support.anchor_catch = sup.get("anchor_catch")
        cfg.support.n_samples = int(sup.get("n_samples") or 0)
        cfg.support.verdict = sup.get("verdict") or _verdict_of(cfg.support.auc)
        # support 的 verdict 说的是"语义层能不能判"；语义层废了才不写阈值，
        # 事实层（auc_fact）低是意料之中的，由锚点补，不影响阈值。
        if cfg.support.verdict != "invalid":
            if "support.threshold" not in skip and sup.get("threshold") is not None:
                cfg.support.threshold = float(sup["threshold"])



def _apply_calibration(cfg: "Config", skip: "set[str] | frozenset[str]" = frozenset()) -> None:
    """把标定结果套进配置。

    skip：local.yaml 里手写过的字段。手调优先级高于标定结果——否则用户
    在参数面板改完阈值，重启一次就被 JSON 覆盖回去，这种「改了没用」最伤信任。
    """
    data = calibration_of(cfg)
    if data is None:
        return
    reliable = bool(data.get("reliable", False))
    if not reliable:
        # 样本不足的标定结果**不能当权威**。之前的实现不看 reliable，把
        # "2 条负样本扫出来的 0.54" 直接钉进运行配置——本语料 20 条可答问题
        # 的 top 分布是 0.560~0.763，0.54 看似安全，但用户换一种问法就掉到
        # 0.53，于是"问什么都拒"。宁可退回出厂值，也不要用一个统计上站不住
        # 的数字替用户做决定。
        # 仍然把文件信息记进 calibrated_samples_ok=False，界面据此提示"标定不可靠"。
        cfg.refusal.calibrated_at = None
        cfg.refusal.calibrated_on_model = data.get("model")
        cfg.refusal.calibrated_samples_ok = False
        return
    if "min_score" not in skip:
        cfg.refusal.min_score = float(data.get("min_score", cfg.refusal.min_score))
    if "min_margin" not in skip:
        cfg.refusal.min_margin = float(data.get("min_margin", cfg.refusal.min_margin))
    cfg.refusal.calibrated_at = data.get("calibrated_at")
    cfg.refusal.calibrated_on_model = data.get("model")
    cfg.refusal.calibrated_samples_ok = True



def _apply_models_file(tree: dict[str, Any]) -> dict[str, Any]:
    """把 config/models.yaml 里的后端注册表叠上去。

    与 local.yaml 分开是刻意的：local.yaml 是"参数调优结果"，由参数面板按差异
    写入；models.yaml 是"后端清单"，由模型管理页整体维护。混在一份文件里，
    两边的写入逻辑会互相覆盖——参数面板保存一次就可能把用户加的本地端口抹掉。

    替换语义而不是深合并：删除一个内置后端必须真的消失，深合并会让它复活。
    """
    from .modelcfg import MODELS_CONFIG  # 延迟导入：modelcfg 依赖 defaults_config

    data = _read_yaml(MODELS_CONFIG)
    if not data:
        return tree

    gen = dict(tree.get("generation") or {})
    providers: dict[str, Any] = {
        name: dict(spec) for name, spec in (gen.get("providers") or {}).items()
        if isinstance(spec, dict)
    }
    for name, spec in (data.get("providers") or {}).items():
        if isinstance(spec, dict):
            providers[name] = {**providers.get(name, {}), **spec}
    for name in data.get("deleted") or []:
        providers.pop(str(name), None)

    if not providers:
        # 一条都没配不是错误，是全新安装的默认状态。以前这里塞一个 mock 兜底，
        # 于是界面上永远挂着一个"能选但没用"的后端，用户分不清那是占位还是故障。
        providers = {}
    if gen.get("provider") not in providers:
        gen["provider"] = next(iter(providers), "")
    gen["providers"] = providers
    # 角色分工和后端清单同住 models.yaml：它们说的是同一件事（哪个后端干什么），
    # 分两份文件的话，删一个后端就可能留下一份指向空气的 roles。
    roles = _clean_roles(data.get("roles"), providers)
    if roles:
        gen["roles"] = roles
    tree = dict(tree)
    tree["generation"] = gen
    return tree


def _clean_roles(raw: Any, providers: Mapping[str, Any]) -> dict[str, str]:
    """只留"还活着"的后端名。配置指向一个已经删掉的后端时，静默当没配。

    这里 import 放在函数里：roles 模块不依赖 config，但两者互相都只是工具层，
    顶层互引会在将来某次重构时变成环。
    """
    from .roles import normalize

    return normalize(raw, known=providers)


def load_config(
    path: str | Path | None = None, *, reload: bool = False, overlay: bool = True
) -> Config:
    """default.yaml 打底，local.yaml 覆盖。

    overlay=False 只给参数面板取"出厂值"用，正常路径永远是 True。
    """
    explicit = path is not None
    p = Path(path) if explicit else DEFAULT_CONFIG
    key = str(p.resolve())
    if not reload and key in _CACHE:
        return _CACHE[key]

    raw = _read_yaml(p)
    over = _read_yaml(LOCAL_CONFIG) if (overlay and not explicit) else {}
    merged = _deep_merge(raw, over)
    if overlay and not explicit:
        merged = _apply_models_file(merged)
    cfg = Config(**merged).resolve_paths()
    manual = {k for k in ("min_score", "min_margin") if k in (over.get("refusal") or {})}
    _apply_calibration(cfg, skip=manual)
    cfg.ensure_dirs()
    _CACHE[key] = cfg
    return cfg


_DEFAULTS: Config | None = None


def defaults_config() -> Config:
    """出厂配置（不含 local.yaml、不含标定结果），供参数面板比对与「恢复默认」。"""
    global _DEFAULTS
    if _DEFAULTS is None:
        _DEFAULTS = Config(**_read_yaml(DEFAULT_CONFIG)).resolve_paths()
    return _DEFAULTS

