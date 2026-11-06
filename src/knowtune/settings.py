"""可调参数的声明式注册表 —— 声明、校验、运行时覆盖、落盘。

为什么参数必须集中成一张表：UI 上每多一个手写控件，就多一处"前端写 0.5、
后端读 0.35"的机会。这里把每个参数的边界、步长、说明、生效方式都写成**数据**，
`/api/settings` 直接把它吐给前端渲染，前端不需要知道任何业务含义——以后加参数
只改这个文件，页面自动多一行控件。

三层取值优先级（高 → 低）：
    运行时覆盖（本文件 apply）  >  config/local.yaml  >  config/default.yaml

local.yaml 是"用户的调参结果"，default.yaml 是"出厂值"，两者分开的好处是
default.yaml 里那些解释"为什么是 480 不是 512"的注释永远不会被程序写坏。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .config import (
    LOCAL_CONFIG,
    Config,
    calibration_of,
    defaults_config,
    load_config,
)


@dataclass(frozen=True)
class Param:
    path: str                      # 点号路径，最后一段是 pydantic 字段名
    label: str
    group: str
    kind: str                      # int | float | bool | enum
    desc: str = ""
    lo: float | None = None
    hi: float | None = None
    step: float | None = None
    unit: str = ""
    options: tuple[str, ...] = ()
    options_from: str = ""         # "providers" = 从 generation.providers 取
    effect: str = "runtime"        # runtime | reindex
    yaml_key: str = ""             # YAML 里的别名（如 mmr.lambda_ 写成 lambda）

    @property
    def key(self) -> str:
        return self.path


GROUPS: tuple[tuple[str, str, str], ...] = (
    ("recall", "召回", "两路各捞多少候选。越大越不容易漏，但融合与精排成本线性上涨。"),
    ("fusion", "融合", "加权 RRF。k 是平滑常数，权重只表达两路相对比例，之和不要求为 1。"),
    ("rerank", "精排与去重", "精排决定最终顺序，MMR 在 Top-K 内部压掉重复片段。"),
    ("refusal", "拒答与分档", "分数阈值 + 分布区分度双条件，任一不满足即拒答；"
                              "证据分档决定模型「贴着资料答」还是「允许自由作答」。"),
    ("support", "答案支撑度", "生成之后校验答案有没有被资料接住。与拒答不同："
                             "拒答猜的是「库里有没有」，支撑度看的是「答出来的话有没有依据」。"),
    ("generation", "生成", "只影响 LLM，不影响检索命中。"),
    ("chunking", "分块", "只影响之后导入或重建索引的文档，已入库的 chunk 不会被动。"),
    ("chat", "会话", "多轮对话带几轮历史进 prompt。"),
    ("ingest", "导入", "只影响之后新导入的文档；已入库内容不会因为改这里而变化。"),
)

PARAMS: tuple[Param, ...] = (
    # ---------------------------------------------------------------- 召回
    Param("retrieval.dense.enabled", "向量召回", "recall", "bool",
          "关掉就只靠 BM25：同义改写、语义近邻的提问会直接查不到。"),
    Param("retrieval.dense.top_k", "向量候选数", "recall", "int",
          "向量路交付给融合的条数。10 万 chunk 下 50→200 的延迟增量约 20-40ms。",
          lo=10, hi=500, step=10, unit="条"),
    Param("retrieval.sparse.enabled", "BM25 召回", "recall", "bool",
          "关掉后专有名词、错误码、函数名这类精确匹配会明显变差。"),
    Param("retrieval.sparse.top_k", "BM25 候选数", "recall", "int",
          "分词后走 FTS5 bm25()，单次检索 0-2ms，放宽到 200 几乎免费。",
          lo=10, hi=500, step=10, unit="条"),

    # ---------------------------------------------------------------- 融合
    Param("retrieval.fusion.method", "融合方式", "fusion", "enum",
          "rrf 只看名次；weighted 把两路分数各自归一到 0~1 再加权相加。"
          "本语料实测 weighted 的 R@5 是 0.96、rrf 只有 0.75——差距来自"
          "rrf 丢掉了『这一路有多确定』这个信息。",
          options=("weighted", "rrf")),
    Param("retrieval.fusion.k", "RRF k", "fusion", "int",
          "平滑常数。k 小则名次靠前的那条权重更突出，k 大则两路更平均。60 是常用起点。",
          lo=1, hi=200, step=1),
    Param("retrieval.fusion.w_dense", "向量权重", "fusion", "float",
          "与 BM25 权重只比相对大小。语义型问题调高，术语型问题调低。",
          lo=0.0, hi=3.0, step=0.05),
    Param("retrieval.fusion.w_sparse", "BM25 权重", "fusion", "float",
          "精确匹配更重要的知识库（代码、错误码）可提到 0.6 以上。",
          lo=0.0, hi=3.0, step=0.05),
    Param("retrieval.fusion.top_k", "融合候选数", "fusion", "int",
          "送进精排的条数，CPU 延迟的主要开关：20 条约 1s，40 条约 2s。",
          lo=5, hi=200, step=5, unit="条"),
    Param("retrieval.fusion.quota", "每路保底名额", "fusion", "int",
          "每一路的前 N 名无条件进候选池。融合前两路各自归一化，而 min-max "
          "只保留『这一路内部谁最高』，抹掉了『这一路可不可信』——稀疏路一条"
          "无关结果也能拿满分，把稠密路的正确答案挤出候选池。设成和 top_k 相当"
          "（如 top_k=40 配 quota=20）可以让两路都被精排看见。0 = 关。",
          lo=0, hi=100, step=5, unit="条"),

    Param("retrieval.expand.enabled", "邻块扩展", "fusion", "bool",
          "把命中块前后各 N 块一起拉进来，补『答案被切分劈开』的漏检。"
          "注意：在当前语料实测是负收益（折价邻块会把真命中挤出候选池），默认关。"),
    Param("retrieval.expand.neighbors", "扩展半径", "fusion", "int",
          "命中块前后各取几块。1 补边界断句，2 以上会把整段上下文都拉进来。",
          lo=1, hi=8, step=1, unit="块"),
    Param("retrieval.expand.discount", "邻块折扣", "fusion", "float",
          "邻块分数 = 种子块分数 × 此折扣。调高（接近 1）邻块可能反超真命中。",
          lo=0.1, hi=1.0, step=0.05),
    Param("retrieval.expand.seed_top", "扩展种子数", "fusion", "int",
          "只扩展前 N 名。放宽会带进一串噪声邻居，且不能超过融合候选数。",
          lo=1, hi=50, step=1, unit="条"),
    Param("retrieval.expand.max_candidates", "扩展后上限", "fusion", "int",
          "扩展后的候选池封顶，超出按分数砍掉。太小会把扩展白做，太大拖慢精排。",
          lo=20, hi=200, step=10, unit="条"),
    # ---------------------------------------------------------- 精排与去重
    Param("retrieval.rerank.enabled", "启用精排", "rerank", "bool",
          "关掉就直接用 RRF 顺序，快很多但 Top-1 准确率会掉。"),
    Param("retrieval.rerank.model", "精排模型", "rerank", "enum",
          "base 在 CPU 上约为 v2-m3 的三分之一耗时，质量损失通常 <2%。",
          options=("BAAI/bge-reranker-base", "BAAI/bge-reranker-v2-m3")),
    Param("retrieval.rerank.top_k", "最终条数", "rerank", "int",
          "装进 prompt 的片段数。太少会缺证据，太多会稀释注意力并推高首字延迟。",
          lo=1, hi=20, step=1, unit="条"),
    Param("retrieval.rerank.timeout_s", "精排超时", "rerank", "float",
          "超时即退回 RRF 顺序并打 degraded=rerank_timeout，不会卡住整个请求。",
          lo=1.0, hi=60.0, step=1.0, unit="s"),
    Param("retrieval.rerank.batch_size", "精排 batch", "rerank", "int",
          "CPU 上 8 比 32 更稳，调大容易在内存峰值处被系统换页拖慢。",
          lo=1, hi=64, step=1),
    Param("retrieval.rerank.max_length", "精排截断长度", "rerank", "int",
          "送进 CrossEncoder 的单条最大 token。chunk 远短于此值时可调小提速。",
          lo=128, hi=1024, step=64, unit="tok"),
    Param("retrieval.mmr.enabled", "MMR 去重", "rerank", "bool",
          "Top-K 里同文档高度重复时开启；否则会挤掉其他来源的证据。"),
    Param("retrieval.mmr.lambda_", "MMR λ", "rerank", "float",
          "越大越偏相关性，越小越偏多样性。0.65 是折中值。",
          lo=0.0, hi=1.0, step=0.05, yaml_key="lambda"),

    # ---------------------------------------------------------------- 拒答
    Param("refusal.enabled", "启用拒答", "refusal", "bool",
          "关掉后无论检索多差都会硬答，容易出现「看起来有理但全靠编」。"),
    Param("refusal.min_score", "分数阈值", "refusal", "float",
          "top1 余弦低于此值即拒答。注意它量的是「话题像不像」，不是「有没有答案」——"
          "主题相邻的问题（库里有 K8s、问的是 GPU 推理）经常拿高分。"
          "真正该用来判定的是「答案支撑度」。",
          lo=0.0, hi=1.0, step=0.005),
    Param("refusal.min_margin", "区分度阈值", "refusal", "float",
          "top1 减 mean(top2..5)。分数全挤在一起说明模型对哪条都没把握，此时就该拒答。",
          lo=0.0, hi=0.5, step=0.005),
    Param("refusal.margin_window", "margin 窗口", "refusal", "int",
          "取前几条算 margin。它同时是标定口径——改了窗口，历史标定结果不再可比，"
          "需要在 model-eval 项目里重跑标定。",
          lo=2, hi=20, step=1, unit="条"),
    Param("refusal.soft_margin", "低置信提醒余量", "refusal", "float",
          "top1 低于 min_score + 此值时不拦截、只在答案旁提示『相关度偏低』。"
          "调大 = 提醒更频繁；设为 0 等于关掉这条提醒。",
          lo=0.0, hi=0.5, step=0.01),
    Param("refusal.soft_floor", "低置信 margin 下界", "refusal", "float",
          "前几条分数几乎持平时也提示。与拒答用的 min_margin 是两件事，不要混。",
          lo=0.0, hi=0.1, step=0.005),
    Param("evidence.mid_ratio", "中档分界比例", "refusal", "float",
          "证据分档：top1 >= min_score 为充足，>= min_score × 此值 为一般，其余不足。"
          "调高更容易判成『不足』（模型更敢用自身知识），调低更要求贴着资料答。",
          lo=0.1, hi=1.0, step=0.05),

    # ------------------------------------------------------------ 答案支撑度
    Param("support.enabled", "启用支撑度校验", "support", "bool",
          "生成后把答案逐句和证据块比对。关掉后只剩「编号有没有越界」这种格式检查。"),
    Param("support.threshold", "句级支撑阈值", "support", "float",
          "句子与证据块的余弦高于此值才算「被接住」。0.55 是经验初值，**未经标定**；"
          "本语料里「同主题不同事实」约 0.35~0.50，「同一事实换个说法」多在 0.65 以上。",
          lo=0.2, hi=0.9, step=0.01),
    Param("support.max_sentences", "最多判定句数", "support", "int",
          "长答案只判前 N 句。调大更全面，代价是每次问答多花一点编码时间。",
          lo=4, hi=80, step=2, unit="句"),
    Param("support.weak_ratio", "未落地判定线", "support", "float",
          "整份答案里被支撑的句子低于此比例，就标为「资料未支撑」。"
          "调高 = 更严格（更多答案被标黄）。",
          lo=0.0, hi=1.0, step=0.05),

    # ---------------------------------------------------------------- 生成
    Param("generation.provider", "后端", "generation", "enum",
          "三档后端统一走 OpenAI 兼容协议，切换不影响已入库数据。",
          options_from="providers"),
    Param("generation.temperature", "温度", "generation", "float",
          "问答场景 0.1 足够。调高会让引用编号更不稳定。",
          lo=0.0, hi=2.0, step=0.05),
    Param("generation.max_tokens", "最大输出", "generation", "int",
          "只限制回答长度，与上下文预算无关。",
          lo=128, hi=8192, step=64, unit="tok"),
    Param("generation.context_token_budget", "上下文预算", "generation", "int",
          "装填进 prompt 的检索片段总预算，超出会从最低分那条开始丢。",
          lo=512, hi=32768, step=512, unit="tok"),
    Param("generation.min_context_chars", "资料保底长度", "generation", "int",
          "多轮对话把预算吃光后，仍至少给资料留这么多字符。设太小模型会看到空的"
          "【资料】段，然后开始编。必须小于上下文预算。",
          lo=50, hi=2000, step=50, unit="字"),
    Param("generation.disable_thinking", "关闭思考链", "generation", "bool",
          "推理型后端（deepseek-flash 等）关掉思考：问答/翻译这类确定性任务不需要，"
          "实测一次翻译的 reasoning token 从 1020 降到 0，答案也不再被截断。"),
    Param("generation.stream", "流式输出", "generation", "bool",
          "开：边生成边吐字（SSE），首字快。关：等服务端生成完一次性返回 JSON，"
          "适合不支持 SSE 或会在网关处被缓冲截断的客户端。两种模式的检索、拒答、"
          "引用校验是同一条链路，只有传输方式不同。"),

    # ---------------------------------------------------------------- 分块
    Param("chunking.strategy", "分块策略", "chunking", "enum",
          "按长度：块大小最均匀；按章节：一块只讲一件事；按段落：块边界与原文一致。",
          options=("size", "heading", "paragraph"), effect="reindex"),
    Param("chunking.size", "分块长度", "chunking", "int",
          "中文 400-480 较稳。改了要对文档重索引才生效。"
          "不能超过嵌入模型窗口（当前 512 token），否则块的后半段会被截掉、"
          "检索不到且不报错。",
          lo=100, hi=2000, step=20, unit="字", effect="reindex"),
    Param("chunking.overlap", "重叠长度", "chunking", "int",
          "保留跨块语义连续性，约 size 的 1/6。必须小于分块长度。",
          lo=0, hi=1000, step=20, unit="字", effect="reindex"),
    Param("chunking.min_chunk", "最小块", "chunking", "int",
          "短于此值并入前一块，避免产生「孤立标题」这类垃圾 chunk。",
          lo=0, hi=1000, step=10, unit="字", effect="reindex"),
    Param("chunking.respect_headings", "按标题对齐", "chunking", "bool",
          "仅「按长度」策略下生效：装到六成满且遇到新章节时提前换块。",
          effect="reindex"),

    # ---------------------------------------------------------------- 会话
    Param("chat.history_turns", "历史轮数", "chat", "int",
          "带几轮问答进 prompt。设 0 即单轮检索，追问会失去指代对象。",
          lo=0, hi=20, step=1, unit="轮"),
    Param("chat.history_token_budget", "历史占用上限", "chat", "int",
          "历史消息最多吃掉多少字符，超出从最早一轮开始整轮丢弃。"
          "设 0 等于不带历史；设太大则会挤掉本轮的资料。",
          lo=0, hi=8000, step=100, unit="字"),

    # ---------------------------------------------------------------- 导入
    Param("ingest.max_file_mb", "单文件上限", "ingest", "int",
          "超过即拒收。上传走分片，传输本身不再是瓶颈，这个值现在实际约束的是"
          "解析：整本 PDF 会载入内存，低配机器别往 GB 级调。",
          lo=1, hi=4096, step=50, unit="MB"),
    Param("ingest.pdf.min_text_chars_per_page", "PDF 文本层下限", "ingest", "int",
          "单页文字少于此时判定『没有文本层，需要 OCR』并明确报错。"
          "扫描件多的库调低能收进来，但入库的会是空白页。",
          lo=0, hi=200, step=5, unit="字"),
)

PARAM_MAP: dict[str, Param] = {p.path: p for p in PARAMS}


# --------------------------------------------------------------------- 读写


def _get(cfg: Config, path: str) -> Any:
    node: Any = cfg
    for part in path.split("."):
        node = getattr(node, part)
    return node


def _set(cfg: Config, path: str, value: Any) -> None:
    parts = path.split(".")
    node: Any = cfg
    for part in parts[:-1]:
        node = getattr(node, part)
    setattr(node, parts[-1], value)


def read_value(cfg: Config, path: str) -> Any:
    """按点号路径读。给 kbconfig 用，避免那边到处 import 私有函数。"""
    return _get(cfg, path)


def write_value(cfg: Config, path: str, value: Any) -> None:
    _set(cfg, path, value)


def options_of(p: Param, cfg: Config) -> list[str]:
    if p.options_from == "providers":
        return list(cfg.generation.providers)
    return list(p.options)


def coerce(p: Param, cfg: Config, value: Any) -> Any:
    """把前端传来的字符串/数字洗成合法值，越界直接拒。

    宁可在写入口报错，也不要把 0.95 的阈值静默截成 0.5 —— 用户以为自己调了，
    行为却没变，这种"沉默的失败"比报错难查十倍。
    """
    if p.kind == "bool":
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    if p.kind == "enum":
        opts = options_of(p, cfg)
        if value not in opts:
            # 还没配任何端点时，`generation.provider` 只能是空串。这是全新安装
            # 的合法状态，不该逼用户"先去配个端点"才能改别的参数。
            if value == "" and not opts:
                return ""
            raise ValueError(f"{p.label} 只能是 {' / '.join(opts)}，收到 {value!r}")
        return value

    if isinstance(value, bool) or value is None:
        raise ValueError(f"{p.label} 需要一个数字，收到 {value!r}")
    try:
        num = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{p.label} 需要一个数字，收到 {value!r}") from exc
    if num != num:  # NaN
        raise ValueError(f"{p.label} 不接受 NaN")
    if p.lo is not None and num < p.lo:
        raise ValueError(f"{p.label} 不能小于 {p.lo:g}")
    if p.hi is not None and num > p.hi:
        raise ValueError(f"{p.label} 不能大于 {p.hi:g}")
    if p.kind == "int":
        return int(round(num))
    return round(num, 4)


def _cross_check(cfg: "Config", p: Param, value: Any, current: dict[str, Any]) -> None:
    """单参数合法不代表组合合法。overlap >= size 会让分块窗口原地打转。"""
    merged = {**current, p.path: value}
    size, overlap = merged.get("chunking.size"), merged.get("chunking.overlap")
    if p.path.startswith("chunking.") and size is not None and overlap is not None:
        if int(overlap) >= int(size):
            raise ValueError(f"重叠长度（{overlap}）必须小于分块长度（{size}）")

    # 块长可以超过嵌入窗口（超窗部分会切窗编码再平均，不会丢），但不能离谱地大：
    # 一块讲三四件事时，向量是这几件事的中点，跟哪个问题都不太像。实测同一份
    # 语料上 size=1000 比 480 召回高 17 个点——所以这里拦的是"语义稀释"，
    # 不是"截断"（截断已在嵌入器里解决），阈值取窗口的两倍。
    win = int(getattr(cfg.embedding, "max_seq_length", 512) or 512)
    if p.path.startswith("chunking.") and size is not None and int(size) > win * 2:
        raise ValueError(
            f"分块长度（{size}）超过嵌入窗口（{win} token）的两倍："
            f"一块里塞太多主题，向量会变成它们的中点，跟哪个问题都不像。请调到 {win * 2} 以下。"
        )

    # 扩展的种子从融合候选池里取，超过池子大小等于"全都扩"，噪声邻居会翻倍。
    seed, pool = merged.get("retrieval.expand.seed_top"), merged.get("retrieval.fusion.top_k")
    if p.path.startswith("retrieval.expand.") and seed is not None and pool is not None:
        if int(seed) > int(pool):
            raise ValueError(f"扩展种子数（{seed}）不能超过融合候选数（{pool}）")

    # 地板高于总预算，等于把【资料】段按地板塞进超预算的 prompt。
    floor, budget = (
        merged.get("generation.min_context_chars"),
        merged.get("generation.context_token_budget"),
    )
    if p.path.startswith("generation.") and floor is not None and budget is not None:
        if int(floor) >= int(budget):
            raise ValueError(f"资料保底长度（{floor}）必须小于上下文预算（{budget}）")


def apply(cfg: Config, values: dict[str, Any]) -> dict[str, Any]:
    """校验全部、再统一写入。半途报错留下的是"改了一半的配置"。"""
    if not values:
        return {}
    unknown = [k for k in values if k not in PARAM_MAP]
    if unknown:
        raise ValueError(f"未知参数：{', '.join(sorted(unknown))}")

    current = {p.path: _get(cfg, p.path) for p in PARAMS}
    staged: dict[str, Any] = {}
    for key, raw in values.items():
        p = PARAM_MAP[key]
        val = coerce(p, cfg, raw)
        _cross_check(cfg, p, val, {**current, **staged})
        staged[key] = val

    for key, val in staged.items():
        _set(cfg, key, val)
    return staged


# ---------------------------------------------------------------- 落盘/重置


def _local_values() -> dict[str, Any]:
    """local.yaml 里显式写过的参数（只用于标记"已持久化"）。"""
    return _flatten(_read_local())


def _read_local() -> dict[str, Any]:
    if not LOCAL_CONFIG.exists():
        return {}
    try:
        return yaml.safe_load(LOCAL_CONFIG.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return {}


def _flatten(tree: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, val in tree.items():
        path = f"{prefix}{key}"
        if isinstance(val, dict):
            out.update(_flatten(val, path + "."))
        else:
            out[path] = val
    return out


def _canonical(path: str) -> str:
    """YAML 里的 lambda 对应模型字段 lambda_，两个名字归一到同一把钥匙。"""
    return "retrieval.mmr.lambda_" if path == "retrieval.mmr.lambda" else path


def save_local(cfg: Config) -> dict[str, Any]:
    """把"与出厂值不同"的参数写进 local.yaml。

    只写差异：整份拷一份过去，就等于永久冻结了所有当前值——以后我改了
    default.yaml 的推荐值，用户机器上却还跑着旧的，而且看不出为什么。
    """
    base = defaults_config()
    cal = calibration_of(cfg) or {}
    tree: dict[str, Any] = {}
    written: list[str] = []
    for p in PARAMS:
        cur = _get(cfg, p.path)
        if cur == _get(base, p.path):
            continue
        # 标定值不算「用户改过」。把它抄进 local.yaml 会变成一个死锁：
        # 以后重新标定写出新阈值，这里却被旧值压着，而且没人看得见原因。
        if p.path.startswith("refusal.min_") and p.path.endswith(("score", "margin")):
            ref = cal.get(p.path.split(".")[-1])
            if ref is not None and abs(float(cur) - float(ref)) < 1e-9:
                continue
        segs = p.path.split(".")
        if p.yaml_key:
            segs[-1] = p.yaml_key
        node = tree
        for seg in segs[:-1]:
            node = node.setdefault(seg, {})
        node[segs[-1]] = cur
        written.append(p.path)

    if not written:
        if LOCAL_CONFIG.exists():
            LOCAL_CONFIG.unlink()
        return {"written": [], "path": str(LOCAL_CONFIG), "removed": True}

    header = (
        "# 用户在参数面板里改过的值。只记录与 default.yaml 不同的项，\n"
        "# default.yaml 是唯一权威文档，不要把这个文件当成它的副本。\n"
        "# 想回出厂值：删掉本文件，或点参数面板的「恢复默认」再保存。\n\n"
    )
    LOCAL_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    LOCAL_CONFIG.write_text(
        header + yaml.safe_dump(tree, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return {"written": written, "path": str(LOCAL_CONFIG), "removed": False}


def reset(cfg: Config) -> dict[str, Any]:
    """恢复出厂值（内存里），不删 local.yaml —— 那是「保存」按钮的事。

    例外：拒答阈值退回**标定值**而不是 YAML 里的占位值。0.35 是"还没标定时
    拿来兜底的数"，把它当成"默认"会让一次误点把系统从 0.54 掉到 0.35，
    表现是拒答突然变得几乎不设防——用户完全看不出发生了什么。
    """
    base = defaults_config()
    cal = calibration_of(cfg) or {}
    changed = []
    for p in PARAMS:
        target = _get(base, p.path)
        if p.path in ("refusal.min_score", "refusal.min_margin") and cal.get(
            p.path.split(".")[-1]
        ) is not None:
            target = cal[p.path.split(".")[-1]]
        if _get(cfg, p.path) != target:
            _set(cfg, p.path, target)
            changed.append(p.path)
    return {"restored": changed}


def reload_from_disk(cfg: Config) -> dict[str, Any]:
    """丢弃运行时改动，从 default.yaml + local.yaml 重新读一遍。

    注意是**拷贝进**原对象而不是替换引用：embedder / reranker / engine 都握着
    同一个 cfg 实例，换对象等于让它们继续用旧配置跑。
    """
    fresh = load_config(reload=True)
    changed = []
    for p in PARAMS:
        value = _get(fresh, p.path)
        if _get(cfg, p.path) != value:
            _set(cfg, p.path, value)
            changed.append(p.path)
    return {"reloaded": changed}


# ------------------------------------------------------------------- 快照


def snapshot(cfg: Config) -> dict[str, Any]:
    base = defaults_config()
    saved = {_canonical(k) for k in _local_values()}
    cal = calibration_of(cfg) or {}
    cal_score = cal.get("min_score")
    cal_margin = cal.get("min_margin")
    # 标定值是「已持久化」的：它躺在 refusal_calibration.json 里，
    # 不该在面板上永远挂着「未保存」三个字把人往「保存」上逼。
    persisted_by_calibration = {
        "refusal.min_score": cal_score,
        "refusal.min_margin": cal_margin,
    }

    groups = []
    for gid, label, note in GROUPS:
        items = []
        for p in PARAMS:
            if p.group != gid:
                continue
            cur = _get(cfg, p.path)
            default = _get(base, p.path)
            is_saved = p.path in saved or _canonical(p.path) in saved
            if not is_saved and p.path in persisted_by_calibration:
                ref = persisted_by_calibration[p.path]
                is_saved = ref is not None and abs(float(cur) - float(ref)) < 1e-9
            items.append({
                "key": p.path,
                "label": p.label,
                "kind": p.kind,
                "desc": p.desc,
                "value": cur,
                "default": default,
                "lo": p.lo,
                "hi": p.hi,
                "step": p.step,
                "unit": p.unit,
                "options": options_of(p, cfg),
                "effect": p.effect,
                "modified": cur != default,
                "saved": is_saved,
                "from_calibration": p.path in persisted_by_calibration
                and is_saved and cal_score is not None,
            })
        if items:
            groups.append({"id": gid, "label": label, "note": note, "items": items})

    flat = [i for g in groups for i in g["items"]]
    return {
        "groups": groups,
        "calibration": {
            "at": cal.get("calibrated_at"),
            "model": cal.get("model"),
            "samples_ok": bool(cal.get("reliable", False)),
            "min_score": cal_score,
            "min_margin": cal_margin,
        },
        "local_file": {"path": str(LOCAL_CONFIG), "exists": LOCAL_CONFIG.exists(),
                       "count": len(saved)},
        "modified": [i["key"] for i in flat if i["modified"]],
        "unsaved": [i["key"] for i in flat if i["modified"] and not i["saved"]],
    }


def local_path() -> Path:
    return LOCAL_CONFIG
