"""预问诊状态机。

一条会话的流转：

    intake（填基本信息）
        │
        ▼
    start ──▶ 安检门 ──命中危险词──▶ emergency（转急诊，停止采集）
                │
                ▼
             抽取槽位 → 更新档案 → 槽位齐了？
                │                       │
                │ 否                     │ 是（或轮次用尽）
                ▼                       ▼
        本地检索追问要点 → 生成一句追问   triage（本地规则算科室）→ 生成病历 → done

三条铁律写死在这里，配置改不动：

1. **分诊科室由本地规则计算**，大模型只负责把已结构化的档案写成一句人话。
   把"去哪个科"交给大模型，等于把一件可审计、可复现的事变成抽奖。
2. **每一段对外话术都过合规扫描**，命中违禁词（确诊 / 开药 / 保证）就退化成
   安全模板，绝不把违规内容发给患者。
3. **任意一步失败都降级继续**：LLM 挂了就用规则抽取 + 模板追问，绝不因为
   模型报错把患者卡在半路——"问得不够漂亮"远好过"问不下去"。

第四条同样不开放：**病历正文由结构化字段拼装**，不靠大模型自由写。
模型写的那段摘要只作为「就诊摘要」一栏附在病历后面，改不了主诉、现病史、
分诊这些可审计的部分。
"""

from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Any

from . import kb as kb_mod
from . import kgdb
from .llm import LLMError
from .retriever import InProcessRetriever, SemanticRetriever
from .store import Store


def _fmt(tpl: str, **kw: Any) -> str:
    out = tpl or ""
    for k, v in kw.items():
        out = out.replace("{" + k + "}", str(v))
    return out


# 规则抽取器：按**槽位 key** 挂载，配置里加一个没有抽取器的新槽位不影响，
# 只是那个槽位交给大模型抽（或直接用模板问）。
_SITE_MAP: list[tuple[str, str]] = [
    ("右下腹", "右下腹"), ("右下", "右下腹"),
    ("右上腹", "右上腹"), ("右上", "右上腹"),
    ("左下腹", "左下腹"), ("左下", "左下腹"),
    ("左上腹", "左上腹"), ("左上", "左上腹"),
    ("脐周", "脐周"), ("肚脐", "脐周"),
    ("全腹", "全腹"), ("上腹", "上腹"), ("下腹", "下腹"), ("小腹", "下腹"),
    ("胃部", "上腹"), ("胃", "上腹"),
    # 胸部（胸痛域用）：定位到"胸骨后/前胸/某侧胸"就够分诊了，
    # 再细的皮节定位是查体的事，问诊问不出来也不必问。
    ("胸骨后", "胸骨后"), ("前胸", "前胸"), ("胸口", "前胸"),
    ("左胸", "左侧胸"), ("右胸", "右侧胸"),
    # 头部
    ("后脑", "枕部"), ("枕部", "枕部"), ("太阳穴", "颞部"),
    ("前额", "额部"), ("整个头", "全头"),
]
_NATURE_WORDS = ["绞痛", "胀痛", "刺痛", "隐痛", "烧灼", "烧心", "刀割", "钝痛", "酸痛",
                 "坠痛", "闷痛", "压痛", "搏动", "跳痛", "紧箍", "压榨", "压着", "撕裂",
                 "针刺", "电击", "钻顶", "干咳", "有痰", "咳痰", "湿咳", "风团", "水疱",
                 "红点", "斑丘疹", "紫癜", "水样", "糊状", "黏液", "脓血", "喷射",
                 "天旋地转", "昏沉", "头重脚轻"]
# 诱因/加重缓解因素。跨症状域通用：腹痛问"跟吃饭有没有关系"，咳嗽问
# "是不是晚上躺下厉害"，头晕问"起床翻身会不会诱发"——都是在这张表里
# 认出关键词，抽到的原词直接进档案。
_TRIGGER_WORDS = ["晚上", "夜间", "躺下", "平躺", "早上", "清晨", "冷空气", "受凉",
                  "活动", "走路", "爬楼", "上楼", "深呼吸", "进食", "饭后", "餐后",
                  "空腹", "饥饿", "翻身", "起床", "转头", "运动", "情绪", "紧张",
                  "劳累", "熬夜", "喝酒", "吃海鲜"]
_COMPANION_WORDS = ["恶心", "呕吐", "想吐", "吐了", "吐过", "反胃", "想呕", "干呕",
                    "发热", "发烧",
                    "腹泻", "拉肚子", "便秘", "黑便", "呕血", "吐血",
                    "头晕", "乏力", "食欲不振", "反酸", "腹胀",
                    "气短", "喘", "胸痛", "心慌", "出汗", "盗汗",
                    "皮疹", "疹子", "瘙痒", "血尿", "尿频", "尿痛",
                    "黄疸", "体重下降", "消瘦", "晕厥", "耳鸣", "视力模糊",
                    "腿肿", "麻木", "无力", "阴道出血"]
_DURATION_RE = re.compile(r"(\d+|[一二两三四五六七八九十半几])\s*(?:个)?\s*(小时|钟头|天|日|周|星期|个月|月)")
_MIGRATION_RE = re.compile(r"转移|先.{0,8}后|从.{0,10}到|脐周.{0,20}右下|一开始.{0,24}后来|后来.{0,12}固定")
_TEMP_RE = re.compile(r"(\d{2}(?:\.\d)?)\s*(?:度|℃)")

# 过敏史。优先抓「对XX过敏」这个明确句式；认不出过敏原时退回常见过敏原词表。
_ALLERGEN_WORDS = ["青霉素", "头孢", "磺胺", "阿司匹林", "布洛芬", "花粉", "海鲜",
                   "虾", "蟹", "鸡蛋", "牛奶", "酒精", "尘螨", "芒果", "药物"]
_ALLERGY_PAT = re.compile(r"对(.{1,12}?)过敏")
_PAST_WORDS = ["高血压", "糖尿病", "冠心病", "心脏病", "哮喘", "慢阻肺", "肝炎",
               "乙肝", "肝硬化", "肾炎", "肾病", "胃溃疡", "胃炎", "甲亢", "甲减",
               "贫血", "癫痫", "脑梗", "中风", "肿瘤", "癌症", "结核", "白内障",
               "结石", "手术", "住院"]

# 明确否定/明确不知道的短答。「回答也不会结束」的主要根源就在这：患者对
# "还有没有其他不舒服"答"没有"，抽取器抽不出任何值，状态机只能靠
# "问两次就放弃"兜底——多耗一轮、病历上还留不下"无"这个明确结论。
_PARTICLES = "的了啊呢吧呀哦哟哈嗯～~!?！？。 "
_NO_WORDS = ("没有", "没了", "无", "木有", "不曾", "从未", "从来没有",
             "从来不", "都没有", "不是", "非")
_UNKNOWN_WORDS = ("不清楚", "不知道", "不确定", "没注意", "记不清", "忘了",
                  "没感觉", "说不上")


def _strip_particles(t: str) -> str:
    return t.strip().strip(_PARTICLES + "，,;；")


def _explicit_no(text: str) -> bool:
    """这句话是不是对刚才问题的**干脆否定**（"没有""没了""都没有"）。

    刻意保守：只认短答。长句里藏着内容（"不过有点恶心"）必须走正常抽取，
    不能因为开头有个"不"字就把伴随症状吞成"无"。
    """
    t = _strip_particles(str(text or ""))
    if not t or len(t) > 12:
        return False
    if any(w in t for w in _UNKNOWN_WORDS):
        return False
    if t in _NO_WORDS or t in ("没有其他", "没有别的", "没什么", "没什么不舒服",
                               "没有其他不舒服", "没有别的症状", "没有不舒服",
                               "没有其它的", "没有别的了", "没有其他毛病",
                               "没什么其他的", "没有了其他", "没有过敏", "不过敏"):
        return True
    # 短答里"没"字头的都算否认（"没有过""没有啦"），"不"字头不敢算——
    # "不太疼""不舒服"都可能是内容。
    return len(t) <= 6 and re.match(r"^(没|无|不曾|从未|木有)", t) is not None


def _explicit_unknown(text: str) -> bool:
    """这句话是不是"不知道"类回答：不是否定，但也没信息，值得跳过别再问。"""
    t = _strip_particles(str(text or ""))
    if not t or len(t) > 12:
        return False
    return t in _UNKNOWN_WORDS or any(t == w or t == w + "了" for w in _UNKNOWN_WORDS)

# 否定识别。裸「无」要排除"无力/无尿/无痛"这类本身是症状的构词，
# 否则患者说"手脚无力"会被记成"没有无力"，把阳性主诉吃掉。
_NEG_STRONG = re.compile(
    r"(没|未|否认|不是|并无|无明显|无特殊|无(?!(力|尿|痛|味|法|奈|数|需)))"
    r"[^，。；、,;]{0,3}$"
)
_NEG_WEAK = re.compile(r"不$")
_CLAUSE_SEP = re.compile(r"[，。；、,;！？!?]")


def _negated(text: str, pos: int) -> bool:
    """`pos` 处的词是不是被前面的否定词否掉了。

    只看当前分句、且只往前看 8 个字：距离一远就不可靠——"没有恶心，
    但今天吐了两次"里的"没有"不该否定"吐了"。
    """
    seg = _CLAUSE_SEP.split(text[:pos])[-1]
    tail = seg[-8:]
    return bool(_NEG_STRONG.search(tail) or _NEG_WEAK.search(tail))


def _affirmed(text: str, word: str) -> bool:
    """word 是否**以肯定方式**出现过至少一次。

    患者说"没有耳鸣，也不头晕"时，把耳鸣、头晕记进伴随症状是严重失真：
    病历上写着阳性、分诊按阳性算，医生看到的就是一份假病史。
    """
    idx = 0
    while True:
        i = text.find(word, idx)
        if i < 0:
            return False
        if not _negated(text, i):
            return True
        idx = i + len(word)


def _first_in(text: str, words: list[str], *, affirmed: bool = False) -> str | None:
    for w in words:
        if w not in text:
            continue
        if affirmed and not _affirmed(text, w):
            continue
        return w
    return None


def _rule_site(text: str) -> str | None:
    for needle, label in _SITE_MAP:
        if needle in text and _affirmed(text, needle):
            return label
    return None


def _rule_duration(text: str) -> str | None:
    m = _DURATION_RE.search(text)
    if m:
        return f"{m.group(1)}{m.group(2)}"
    if "半天" in text:
        return "半天"
    return None


def _rule_migration(text: str) -> str | None:
    return "是" if _MIGRATION_RE.search(text) else None


def _rule_temperature(text: str) -> str | None:
    m = _TEMP_RE.search(text or "")
    if not m:
        return None
    try:
        val = float(m.group(1))
    except ValueError:
        return None
    # 34-43 之外的数字基本是"39 床""37 号"之类，不是体温
    return f"{m.group(1)}℃" if 34 <= val <= 43 else None


def _rule_allergy(text: str) -> str | None:
    """过敏史：抓「对XX过敏」句式，认不出过敏原就用常见词表兜。

    被否定的不算（"不过敏""没有过敏"由否定快答记"无"，不在这里硬抽）。
    """
    if not _affirmed(text, "过敏"):
        return None
    for m in _ALLERGY_PAT.finditer(text):
        seg_start = m.start()
        if not _negated(text, seg_start):
            return f"对{m.group(1).strip()}过敏"
    for w in _ALLERGEN_WORDS:
        if f"{w}过敏" in text and _affirmed(text, f"{w}过敏"):
            return f"{w}过敏"
    return None


def _rule_past(text: str) -> str | None:
    """既往病史：只认明确说出的病名/手术住院，多值拼成一句。

    "没有高血压"这种否定由 _affirmed 挡住——把否认的病记成阳性病史，
    是病历上最不能接受的失真。
    """
    found = [w for w in _PAST_WORDS if _affirmed(text, w)]
    if not found:
        return None
    return "、".join(dict.fromkeys(found))

_RULE_EXTRACTORS = {
    "site": _rule_site,
    "nature": lambda t: _first_in(t, _NATURE_WORDS, affirmed=True),
    "companions": lambda t: [w for w in _COMPANION_WORDS if _affirmed(t, w)],
    "duration": _rule_duration,
    "migration": _rule_migration,
    "temperature": _rule_temperature,
    "trigger": lambda t: _first_in(t, _TRIGGER_WORDS, affirmed=True),
    "allergy": _rule_allergy,
    "past_history": _rule_past,
}




class PrevisitEngine:
    def __init__(self, pv: dict[str, Any], store: Store,
                 llm: Any = None) -> None:
        """llm 是可选的：任何带 `complete(msgs, *, temperature, max_tokens) -> str`
        的对象都行（自带 OpenAICompatLLM、主项目的适配器、测试里的假模型）。
        None = 没有模型，问诊全程走规则 + 知识库，照样收网出病历。
        """
        self.store = store
        self.llm = llm
        self._last_error: str | None = None
        # 分诊来源：rule = 本地规则/域/图谱；llm = 完全没线索时的推理兜底。
        # 病历里如实标注，医生能分清"系统判的"还是"模型猜的"。
        self._triage_source: str = "rule"
        # 熔断：模型连不上时不要在**每一轮**都重试一遍。
        # 实测本地 lmstudio 没启动时，一次 start 要 7 秒（httpx 连接超时 +
        # tenacity 退避），而患者每答一句都要再等 7 秒——体感就是"卡死了"。
        # 失败一次就安静地降级到规则/模板，过一会儿再允许试探。
        self._down_until: float = 0.0
        self._pool = ThreadPoolExecutor(max_workers=4,
                                        thread_name_prefix="previsit-llm")
        self.last_semantic: dict[str, Any] = {}
        # 路 B 的产物：这次召回命中的 MedRAG 病种（含"该确认什么"的素材）
        self.last_diseases: list[dict[str, Any]] = []
        # 每个会话的槽位排序（语义召回命中顺序）：进程内缓存即可，丢了就
        # 退回配置顺序，不影响正确性。
        self._slot_order: dict[str, list[str]] = {}
        # 语义召回按会话**只试一次**：域一直判不出来时（患者说"我不舒服"）
        # 不能每轮都去撞一次后端，那等于把患者的等待时间翻倍。
        self._sem_tried: set[str] = set()
        # start() 里 _extract_chief 已对主诉抽过一次，_advance 会对同一句话
        # 再抽一次——同一句话两次抽取结果必然相同，纯属浪费一次模型调用。
        # 这里记下「会话 → 主诉抽取是否用上模型」，_advance 见到标记就跳过。
        # 危险闸门提前返回的路径仍依赖 _extract_chief 的落库结果，所以是
        # 跳过重复调用，不是删掉那次抽取。
        self._chief_done: dict[str, bool] = {}
        self.kg: kgdb.KG | None = None
        self.retriever: Any = None
        self.apply_config(pv)

    def apply_config(self, pv: dict[str, Any]) -> None:
        """把一份新配置灌进来，**不重启进程**。

        后台的「站点 → 预问诊」页保存后走这里：改一个温度、换一个模型，不该
        让正在问诊的患者掉线。会话状态存在 SQLite 里、按 session_id 取，所以
        换配置只影响"下一句话怎么说"，不影响已经问过的内容。

        只重建配置派生的东西（模型熔断参数、图谱连接、检索器），线程池、会话
        缓存、熔断计时器都保留——重建它们等于把正在等模型的那一轮凭空掐掉。
        """
        self.pv = pv
        self.slots: list[dict[str, Any]] = pv.get("slots") or []
        self._slot_defs: dict[str, dict[str, Any]] = {s["key"]: s for s in self.slots}
        # 症状域：域决定「问哪些槽位、每个槽位怎么问」，见 config/previsit_kb.yaml。
        # 配置里没有 domains（旧格式）时退化成单域，用全局 slots 问到底。
        self.domains: list[dict[str, Any]] = [d for d in (pv.get("domains") or [])
                                              if isinstance(d, dict) and d.get("key")]
        self._domains: dict[str, dict[str, Any]] = {d["key"]: d for d in self.domains}
        self.default_domain = str(pv.get("default_domain") or "general")
        if self._domains and self.default_domain not in self._domains:
            self.default_domain = self.domains[0]["key"]
        self.fields: list[dict[str, Any]] = pv.get("profile_fields") or []
        self.provider = pv.get("provider") or None
        self.model = pv.get("model") or None
        # 预问诊是**交互式**的，等 60 秒（全局 read 超时）没有任何意义：
        # 患者宁可要一句模板追问，也不要盯着转圈一分钟。所以单独用一个更短的
        # 上限，超了就熔断、这轮走规则。线程跑完也照样丢弃结果——它已经过期了。
        # 显式写 0 是"这一轮别等模型"（测试用这个把整条链路钉在规则上），
        # 不能被 `or 20` 吃掉——0 是 falsy，写成 or 的写法会把"立即超时"
        # 变成"等 20 秒"，测试于是变成一场看环境的赌博：模型 401 就秒过，
        # 模型连不上就每轮干等 20 秒，追问顺序还跟着变。
        raw_timeout = pv.get("timeout_s")
        self.timeout_s = 20.0 if raw_timeout is None else float(raw_timeout)
        # 知识图谱（追问增强）。库不在/坏了 self.kg.ok 就是 False，所有
        # 图谱路径自动退化——它是增强项，不是依赖。
        kg_cfg = pv.get("kg") or {}
        self.kg_cfg: dict[str, Any] = kg_cfg if isinstance(kg_cfg, dict) else {}
        self.ros_max = int(self.kg_cfg.get("ros_max") or 0)
        old_kg, self.kg = self.kg, kgdb.KG(
            self.kg_cfg.get("db") or kgdb.DEFAULT_DB,
            enabled=bool(self.kg_cfg.get("enabled", True)))
        if old_kg is not None:
            old_kg.close()               # 换配置时把旧连接收掉，别攒着文件句柄
        self._apply_retrieval(
            pv.get("retrieval") if isinstance(pv.get("retrieval"), dict) else {})

    def _apply_retrieval(self, cfg: dict[str, Any]) -> None:
        """重建检索器。

        主项目挂载时注入的是 `InProcessRetriever`（进程内查库，省一次 HTTP）。
        它是**注入进来的**，不能因为改了个 top_k 就把它换成 HTTP 版——那样
        改完参数反而变慢，还多一个连不上的失败点。所以原对象在就只改参数，
        连"关掉"也是置 disabled，而不是把注入的那条线整个扔掉（关掉再打开
        还得有人重新注入，面板做不到）。
        """
        old = self.retriever
        enabled = bool(cfg.get("enabled"))
        if isinstance(old, InProcessRetriever):
            old.top_k = int(cfg.get("top_k") or 8)
            old.max_per_doc = int(cfg.get("max_per_doc") or 2)
            old.disabled = not enabled
            old.last_error = "" if enabled else "已在参数面板里关闭"
            self.retriever = old
            return
        if not enabled:
            self.retriever = None
            return
        self.retriever = SemanticRetriever.from_config(cfg)


    LLM_COOLDOWN_S = 30.0

    # -------------------------------------------------------- 语义召回（选病症）
    def _domain_by_semantics(self, text: str) -> tuple[str, list[str]]:
        """用语义召回判域，返回 (域 key, 该域被命中的槽位 key 顺序)。

        判不出来返回 ("", [])：关键词那条老路继续兜底。语义只在**选域**这一
        步发力——问法原文仍取自配置，避免"召回一段话就当话术发给患者"这种
        不可控行为。
        """
        self.last_semantic = {}
        self.last_diseases = []
        if not (self.retriever and self.retriever.ok and text.strip()):
            return "", []
        hits = self.retriever.search(text)
        label, weight, slots = SemanticRetriever.best_domain(hits)
        key = self._domain_key_by_label(label)
        # 路 B：同一批命中里的 MedRAG 病种留着，给"还该确认什么"用
        self.last_diseases = SemanticRetriever.best_diseases(hits)
        self.last_semantic = {
            "domain": key, "label": label, "weight": round(weight, 4),
            "slots": slots, "hits": len(hits),
            "diseases": [d["disease"] for d in self.last_diseases],
            "degraded": not (self.retriever.ok and bool(hits)),
        }
        return key, slots

    def _domain_key_by_label(self, label: str) -> str:
        """召回结果的域标签 → 配置里的域 key。

        标签可能带前后缀（"儿童发热" vs "发热"），先精确匹配、再退到包含
        匹配，包含时取最长的那个——避免"发热"抢走"儿童发热"的命中。
        """
        label = str(label or "").strip()
        if not label:
            return ""
        for d in self.domains:
            if str(d.get("label") or "").strip() == label:
                return str(d["key"])
        best, best_len = "", 0
        for d in self.domains:
            lab = str(d.get("label") or "").strip()
            if lab and (lab in label or label in lab) and len(lab) > best_len:
                best, best_len = str(d["key"]), len(lab)
        return best

    @property
    def last_error(self) -> str | None:
        return self._last_error

    # ---------------------------------------------------------------- 症状域
    def _domain_of(self, sess: dict[str, Any]) -> str:
        """当前会话属于哪个症状域。

        **一旦判到具体域就锁死**，不跟着每一句话漂移：患者先说"胸口疼"、
        再补一句"有点咳嗽"，如果每轮重判，槽位集合会在两个域之间跳来跳去，
        前面答过的"部位：前胸"在咳嗽域里无处安放。锁定的代价是"说错了域"，
        那个由 max_turns 兜底，比起反复换槽位要好受得多。
        """
        if not self._domains:
            return ""
        cur = str(sess.get("domain") or "")
        if cur and cur in self._domains and cur != self.default_domain:
            return cur
        sid = str(sess.get("id") or "")
        text = " ".join([str(sess.get("chief") or ""), self._user_text(sess)])
        got = kb_mod.detect_domain(self.domains, text)
        if not got and sid and sid not in self._sem_tried:
            # 关键词全没命中，最后一招是语义召回：这时患者往往已经说了两三句，
            # 上下文比主诉那一句更完整，召回命中率也更高。只试一次。
            self._sem_tried.add(sid)
            got, prefer = self._domain_by_semantics(text)
            if got and prefer:
                self._slot_order[sid] = prefer
        return got or cur or self.default_domain

    def _slots_for(self, domain_key: str,
                   prefer: list[str] | None = None) -> list[dict[str, Any]]:
        """该域要问哪些槽位，顺序即追问优先级。

        域只声明槽位 key，具体 label/hint 从全局槽位池取，域可以用
        `slot_hints` 覆盖成更贴合本症状的说法（腹痛问"部位"，头晕问"哪种晕"）。
        标了 `always: true` 的槽位（过敏史、既往病史）不受域管辖——不管哪里
        不舒服都必须问，排在域槽位后面。

        `prefer` 是语义召回命中的槽位：患者这句话最像的那一节排到最前面，
        剩下的按配置原序。召回只改顺序、不改集合——问什么仍是配置说了算。
        """
        if not self._domains:
            return [dict(s) for s in self.slots]
        d = self._domains.get(domain_key) or self._domains.get(self.default_domain) or {}
        keys = [str(k) for k in (d.get("slots") or [])]
        if prefer:
            keys = [k for k in prefer if k in keys] + [k for k in keys if k not in prefer]
        keys += [s["key"] for s in self.slots
                 if s.get("always") and s["key"] not in keys]
        hints = d.get("slot_hints") or {}
        out: list[dict[str, Any]] = []
        for k in keys:
            base = self._slot_defs.get(k)
            if not base:
                continue
            item = dict(base)
            if hints.get(k):
                item["hint"] = str(hints[k])
            out.append(item)
        return out or [dict(s) for s in self.slots]

    def _domain_cfg(self, domain_key: str) -> dict[str, Any]:
        return self._domains.get(domain_key) or {}

    # ------------------------------------------------------------ 知识图谱追问
    # 槽位问完之后，用图谱补一轮"系统回顾"：在含主症的疾病集合里查还该
    # 问哪些伴随症状。这是**增强**——库不在就直接跳过，问诊照样收网。
    @staticmethod
    def _ros_state(sess: dict[str, Any]) -> dict[str, Any]:
        raw = sess.get("ros")
        return raw if isinstance(raw, dict) else {}

    def _kg_seeds(self, chief: str, merged: dict[str, Any]) -> list[str]:
        """主诉 + 已采集值 → 图谱词表里的标准症状名。

        两段式归一，缺一不可：

        - `normalize()` 走 sameas 别名表，把**口语**换成标准词（"拉稀"→
          "腹泻""肚子疼"→"腹痛"）。这是主要来源，一万八千条别名比任何
          手写词表都全。
        - `probes()` 走词表包含匹配，兜住 normalize 漏掉的（患者直接说
          了标准词，但 sameas 里恰好没有这条别名）。

        患者说"咳得厉害"、库里只有"咳嗽"，不归一就是一条都命中不了。
        """
        if not self.kg.ok:
            return []
        texts = [str(chief or ""), *self._values(merged)]
        out: list[str] = []
        for t in texts:
            out.extend(self._normalize(str(t)))
            out.extend(self.kg.probes(str(t), limit=3))
        return list(dict.fromkeys(out))

    # ------------------------------------------------------ 诊断线索（不是诊断）
    @staticmethod
    def _dx_state(sess: dict[str, Any]) -> dict[str, Any]:
        dx = sess.get("dx")
        return dx if isinstance(dx, dict) else {}

    def _normalize(self, text: str) -> list[str]:
        """一句话 → 标准症状词列表。

        两段：图谱的 `sameas` 别名表（一万八千条，主力），加上配置里
        `kg.synonyms` 的人工补充（词表本身的缺口，比如"出汗"这个词根本
        不在词表里，只能靠人补）。
        """
        t = str(text or "")
        if not t.strip() or not self.kg.ok:
            return []
        out = [str(n["std"]) for n in self.kg.normalize(t)]
        for raw, std in (self.kg_cfg.get("synonyms") or {}).items():
            if str(raw) in t and str(std) not in out:
                out.append(str(std))
        return list(dict.fromkeys(out))

    def _extract_chief(self, sid: str, complaint: str, slots: list[dict[str, Any]],
                       profile: dict[str, Any]) -> None:
        """把主诉也当成一次"回答"来抽取，合并进槽位。

        此时槽位还全是空值，合并不存在覆盖问题；它的意义是让患者**第一句话
        里说过的信息**别浪费——"胸口压着疼还出汗"里的"出汗"直接决定分诊
        结论（ACS 红旗征）。
        """
        if not str(complaint or "").strip():
            return
        try:
            got, used_llm = self._extract(complaint, slots, profile, complaint)
        except Exception:
            return
        self._chief_done[sid] = bool(used_llm)
        if not got:
            return
        sess = self.store.get(sid)
        if not sess:
            return
        merged = self._merge(dict(sess.get("slots") or {}), got, slots)
        self.store.update(sid, slots=merged)

    def _dx_update(self, sid: str, sess: dict[str, Any], text: str,
                   merged: dict[str, Any] | None = None) -> dict[str, Any]:
        """把这一轮的话归一到标准症状，累积进会话的诊断线索。

        两个来源都要读：

        - **患者原话**：直接说的症状（"拉稀"→"腹泻"）。
        - **已抽出的槽位值**：患者说"白色的痰"时抽取器能填 nature，但归一
          未必认得出——把槽位值再归一一次，把"伴随症状：恶心"这类补进线索。
          不这么做，症状池永远停在主诉那一两个词上，候选病永远是空的。

        产品红线写死在这里：**这些线索永远不写进给患者看的正文**。它们只用于
        (a) 决定还该确认什么 (b) 科室兜底 (c) 病历里给医生的一栏参考。
        "你可能得了 X"这句话，本模块一句都不会说。
        """
        dx = dict(self._dx_state(sess))
        if not self.kg.ok:
            return dx
        texts = [str(text or "")]
        if merged:
            texts.extend(self._values(merged))
        got: list[str] = []
        for t in texts:
            got.extend(self._normalize(str(t)))
        if not got:
            return dx
        # 症状 vs 既往病：`腹痛` 是 1001 个病的症状、9 个病的名字；`高血压`
        # 是 72 个病的症状、7 个病的名字。后者该进病史栏，不该参与"你像
        # 什么病"的打分。判据在 kgdb.classify_terms 里，按量级分。
        syms, conds = self.kg.classify_terms(got)
        changed = False
        cur = [str(x) for x in (dx.get("symptoms") or [])]
        add = [x for x in syms if x not in cur]
        if add:
            dx["symptoms"] = cur + add
            changed = True
        if conds:
            old_c = [str(x) for x in (dx.get("conditions") or [])]
            addc = [x for x in conds if x not in old_c]
            if addc:
                dx["conditions"] = old_c + addc
                changed = True
        if changed:
            self.store.update(sid, dx=dx)
        return dx

    def _dx_lock(self, sess: dict[str, Any], dx: dict[str, Any]) -> dict[str, Any]:
        """路 A：已确认症状 → 候选病 + 科室投票（本地 SQLite，几毫秒）。

        候选病**必须有 2 个以上症状命中**才留下：一个"头晕"能对上几十个病，
        这时候报一个具体病名是纯噪声。宁可空着，也不给医生一条编出来的线索。
        """
        if not self.kg.ok:
            return dx
        syms = [str(x) for x in (dx.get("symptoms") or [])]
        if not syms:
            return dx
        dx = dict(dx)
        cands = [c for c in self.kg.candidate_diseases(syms, limit=5)
                 if int(c.get("hits") or 0) >= 2]
        dx["candidates"] = cands
        dx["dept"] = self.kg.dept_votes(syms, limit=3)
        return dx

    def _dx_merge_medrag(self, dx: dict[str, Any]) -> dict[str, Any]:
        """路 B：把这次语义召回命中的 MedRAG 病种并进线索。"""
        if not self.last_diseases:
            return dx
        dx = dict(dx)
        old = {str(d.get("disease")): d for d in (dx.get("medrag") or [])}
        for d in self.last_diseases:
            name = str(d.get("disease") or "")
            if not name:
                continue
            prev = old.get(name)
            if not prev or float(d.get("score") or 0) > float(prev.get("score") or 0):
                old[name] = {"disease": name, "score": float(d.get("score") or 0),
                             "symptoms": list(d.get("sections", {}).get("symptoms") or [])}
        merged = sorted(old.values(), key=lambda x: -float(x.get("score") or 0))[:3]
        dx["medrag"] = merged
        return dx

    def _dx_view(self, sess: dict[str, Any]) -> dict[str, Any]:
        """给 view 用的精简线索：症状、科室倾向、候选病名（不带素材原文）。"""
        dx = self._dx_state(sess)
        if not dx:
            return {}
        return {
            "symptoms": [str(x) for x in (dx.get("symptoms") or [])],
            "conditions": [str(x) for x in (dx.get("conditions") or [])],
            "dept": [{"dept": d.get("dept"), "ratio": d.get("ratio")}
                     for d in (dx.get("dept") or [])],
            "candidates": [{"disease": c.get("disease"), "score": c.get("score"),
                            "dept": c.get("dept"), "hits": c.get("hits")}
                           for c in (dx.get("candidates") or [])],
            "medrag": [str(m.get("disease")) for m in (dx.get("medrag") or [])],
        }

    def _medrag_probes(self, sess: dict[str, Any], merged: dict[str, Any]) -> list[str]:
        """MedRAG 里"这个病典型、但患者还没确认"的症状，按可问性排序。

        这是路 B 的落地点：命中"慢阻肺加重"，它写着"呼吸困难加重、脓痰"，
        患者只说了"咳嗽有痰"，那"呼吸困难"就该问。比统计共现更准——那是
        人工精编的病种描述，不是从 8685 个病里数出来的频率。

        三道过滤，少一道都会问出废话：必须在症状词表里（否则问不明白）、
        必须有足够多的病携带（太罕见问了没意义）、必须还没问过也没确认过。
        """
        if not self.kg.ok or not self.ros_max:
            return []
        dx = self._dx_state(sess)
        vocab = self.kg.symptom_vocab()
        st = self._ros_state(sess)
        skip = set(str(x) for x in (st.get("asked") or [])) | self._kg_known(merged)
        skip |= set(str(x) for x in (dx.get("symptoms") or []))
        out: list[str] = []
        for item in dx.get("medrag") or []:
            for line in item.get("symptoms") or []:
                for sym in self._normalize(str(line)):
                    if sym in skip or sym in out:
                        continue
                    if not self.kg.askable(sym):
                        continue          # "昏迷/休克"不能问患者"您有没有"
                    if sym not in vocab or int(vocab.get(sym) or 0) < 30:
                        continue
                    out.append(sym)
        return out

    def _kg_known(self, merged: dict[str, Any]) -> set[str]:
        """已经采集到的症状（避免把患者说过的话再问一遍）。"""
        known: set[str] = set()
        for v in self._values(merged):
            known.update(self.kg.probes(str(v), limit=3) if self.kg.ok else [])
        return known

    def _ros_take(self, sess: dict[str, Any], merged: dict[str, Any]) -> dict[str, Any] | None:
        """挑下一个该问的伴随症状；没有就返回 None（说明该收网了）。

        顺序是刻意的：**先 MedRAG 后统计共现**。MedRAG 那 48 个病种是人工
        精编的，"慢阻肺加重会呼吸困难加重、脓痰"这句话是确定的；统计共现
        给的是"含咳嗽的病里有 36% 也有呼吸困难"，是概率。确定的东西优先问，
        概率的补位。
        """
        if not self.kg.ok or self.ros_max <= 0:
            return None
        st = self._ros_state(sess)
        asked = [str(x) for x in (st.get("asked") or [])]
        if len(asked) >= self.ros_max:
            return None
        # 路 B：MedRAG 病种里"典型但还没确认"的症状
        for sym in self._medrag_probes(sess, merged):
            if sym not in asked:
                return {"symptom": sym, "ratio": 0.0, "co": 0, "base": 0,
                        "source": "medrag"}
        # 路 A 之外的老路：OpenCMKG 症状共现（覆盖率排序）
        seeds = self._kg_seeds(str(sess.get("chief") or ""), merged)
        if not seeds:
            return None
        cands = self.kg.ros(
            seeds,
            exclude=set(asked) | self._kg_known(merged),
            limit=6,
            min_ratio=float(self.kg_cfg.get("min_ratio") or 0.12),
        )
        # 统计共现最容易冒出"昏迷""休克"这类词——它们是某个病的关键表现，
        # 但不该拿去问一个还能打字的人。跳过继续找下一个，别整批放弃。
        for c in cands:
            sym = str(c.get("symptom") or "")
            if sym in asked or not self.kg.askable(sym):
                continue
            c["source"] = "ros"
            return c
        return None

    def _ros_question(self, symptom: str) -> str:
        """图谱追问的话术。只问"有没有"，绝不暗示可能是什么病。"""
        return f"再确认一下：除了刚才说的，您还有没有{symptom}？"

    @staticmethod
    def _ros_verdict(text: str, symptom: str) -> str | None:
        """患者这句话是在肯定/否定这个症状吗？答非所问返回 None（不记账）。"""
        t = str(text or "").strip()
        if not t:
            return None
        if symptom and _affirmed(t, symptom):
            return "yes"
        if symptom and symptom in t and _negated(t, t.find(symptom)):
            return "no"
        if _NEG_STRONG.search(t) or re.search(r"(没有|不|无|未|不曾)", t):
            return "no"
        if re.search(r"^(有|是|对|会|嗯|对的有)", t):
            return "yes"
        return None

    def _ros_record(self, sid: str, sess: dict[str, Any], text: str) -> bool:
        """有挂着的问题就记账，返回是否消费了这个回答。"""
        st = self._ros_state(sess)
        pending = str(st.get("pending") or "")
        if not pending:
            return False
        verdict = self._ros_verdict(text, pending)
        answers = dict(st.get("answers") or {})
        if verdict:
            answers[pending] = verdict
        st["answers"] = answers
        st.pop("pending", None)
        st["asked"] = [str(x) for x in (st.get("asked") or [])] + [pending]
        self.store.update(sid, ros=st)
        return True

    @staticmethod
    def _is_askable(slot: dict[str, Any], merged: dict[str, Any],
                    attempts: dict[str, Any] | None) -> bool:
        """这个槽位还值不值得问。

        问过两次还问不出来就放弃：患者要么不知道、要么不想说，再问第三遍
        只是消耗耐心，而且会占满 max_turns，把真正该问的槽位挤掉。
        放弃的槽位在病历里显示为「未采集」——这比硬凑一个猜测诚实得多。
        """
        if merged.get(slot["key"]):
            return False
        return int((attempts or {}).get(slot["key"]) or 0) < 2

    # ---------------------------------------------------------------- public
    def start(self, complaint: str, profile: dict[str, Any] | None = None) -> dict[str, Any]:
        prof = self.clean_profile(profile or {})
        complaint = str(complaint or "").strip()
        if not complaint:
            return self._start_blank(prof)
        # 先用主诉判一次域：第一句追问就该是这个症状的问法。判不出来
        # （"我不舒服"）就落到兜底域，后面每轮还会再试，一旦说清楚就锁定。
        #
        # 语义召回优先：患者不会照着词表说话，"胃里翻江倒海"一个关键词都
        # 命中不了，但语义上它就是腹痛域。语义判不出来再退回关键词。
        domain_key, prefer = self._domain_by_semantics(complaint)
        if not domain_key:
            domain_key = kb_mod.detect_domain(self.domains, complaint) or ""
        if self._domains and not domain_key:
            domain_key = self.default_domain
        slots = self._slots_for(domain_key, prefer)
        empty = {s["key"]: ([] if s.get("multi") else None) for s in slots}
        sid = self.store.create(complaint, empty, prof, domain=domain_key)
        if prefer:                     # 记住这次的槽位排序，后面每轮沿用
            self._slot_order[sid] = prefer
        # 主诉也要过一遍槽位抽取。以前只抽"每轮回复"，主诉里的话就白说了：
        # 患者说"胸口压着疼，还出汗"，"出汗"是 ACS 的红旗征，却没进
        # companions 槽位，分诊规则匹配不到，本该"尽快就诊"的判成了"门诊"。
        self._extract_chief(sid, complaint, slots, prof)
        # 诊断线索：主诉先归一到标准症状（"拉稀"→"腹泻"），再并进这次语义
        # 召回命中的 MedRAG 病种。只落库，不给患者看。
        dx: dict[str, Any] = {}
        if self.kg.ok:
            got_all = self._normalize(complaint)
            syms, conds = self.kg.classify_terms(got_all)
            if domain_key:
                # 主诉归一不出症状时（"胸口压着疼"这种说法词表里没有），
                # 用**域标签**兜一条：语义召回已经判定这是胸痛域，那"胸痛"
                # 本身就是可靠线索。宁可要一条粗的，也不要空着让候选病、
                # 科室投票全哑火。
                lab = str(self._domain_cfg(domain_key).get("label") or "")
                lab_syms, _ = self.kg.classify_terms(self._normalize(lab))
                syms = list(dict.fromkeys([*syms, *lab_syms]))
            if syms:
                dx["symptoms"] = syms
            if conds:
                dx["conditions"] = conds
        dx = self._dx_merge_medrag(dx)
        if dx:
            self.store.update(sid, dx=dx)
        # 首轮已经判到一个具体域 → 后面不再重复召回；只落到兜底域的话留着
        # 机会，等患者多说几句再试语义。
        if domain_key and domain_key != self.default_domain:
            self._sem_tried.add(sid)
        self.store.add_turn(sid, "user", complaint)
        return self._advance(sid, complaint)

    def _start_blank(self, prof: dict[str, Any]) -> dict[str, Any]:
        """空主诉开场：建一个空会话，等患者自己开口。

        表单上那个"这次哪里不舒服"曾经是必填——它其实是整个门禁里最难的一格：
        要求患者在还不知道系统要问什么的时候，先用一句话概括自己的病。写不出来
        就进不去，而本该问这句话的恰恰是第一句追问。

        所以空着也能开始：这里只建会话、落默认域的槽位集合，**不生成问题**。
        前端进对话页显示欢迎语（"先说说您哪里不舒服"），患者说第一句时由
        `reply` 把这句话落成主诉，`_advance` 随即判域、换槽位、正常追问。
        """
        domain_key = self.default_domain if self._domains else ""
        slots = self._slots_for(domain_key)
        empty = {s["key"]: ([] if s.get("multi") else None) for s in slots}
        sid = self.store.create("", empty, prof, domain=domain_key)
        sess = self.store.get(sid)
        if not sess:
            raise KeyError(sid)
        return self.view(sess, note="waiting_chief")

    def clean_profile(self, raw: dict[str, Any]) -> dict[str, Any]:
        """只收配置里声明过的字段，值统一转成字符串。

        前端可以绕过、API 也能直接打，所以这里必须白名单：否则随便一个
        `{"__proto__": ...}` 或者一坨嵌套 dict 都会进病历和提示词。
        """
        out: dict[str, Any] = {}
        for f in self.fields:
            k = f["key"]
            if k not in raw:
                continue
            # show_if：表单上根本没显示的字段不该被收进来。前端已经藏了，
            # 但 API 是公开的——不校验就会出现"男性患者怀孕中"这种病历，
            # 而且它会真的参与分诊（孕期那条规则），后果比难看严重。
            cond = f.get("show_if") or {}
            if cond.get("field"):
                dep = str(raw.get(cond["field"]) or "").strip()
                if dep != str(cond.get("value") or ""):
                    continue
            v = raw[k]
            if isinstance(v, list):
                v = "、".join(str(x).strip() for x in v if str(x).strip())
            v = str(v or "").strip()
            if not v:
                continue
            if f.get("type") == "number":
                m = re.search(r"\d+(?:\.\d+)?", v)
                if not m:                 # 填了"四十岁"这种，转不出来就原样留着
                    out[k] = v
                    continue
                v = m.group(0)
                lo, hi = f.get("min"), f.get("max")
                try:
                    if lo is not None and float(v) < float(lo):
                        continue
                    if hi is not None and float(v) > float(hi):
                        continue
                except (TypeError, ValueError):
                    pass
            if f.get("options") and v not in f["options"]:
                # 选项外的东西不认：病历抬头出现一个界面上选不出来的值，
                # 后面没有任何一处能解释它是哪来的。
                continue
            out[k] = v
        return out

    def reply(self, sid: str, message: str) -> dict[str, Any]:
        sess = self.store.get(sid)
        if not sess:
            raise KeyError(sid)
        if sess["state"] in ("done", "emergency"):
            return self.view(sess, note="本次问诊已结束，可点「重新开始」。")
        if not str(sess.get("chief") or "").strip() and not sess.get("turns"):
            # 空主诉开场（见 `_start_blank`）：患者说的第一句话就是主诉。
            # 不落库的话病历抬头会一直是"未填写"，而现病史、分诊用的却是
            # 同一段话——同一份病历里两处说法对不上，医生只会信抬头。
            self.store.update(sid, chief=message)
            sess = self.store.get(sid) or sess
        self.store.add_turn(sid, "user", message)
        return self._advance(sid, message)

    @staticmethod
    def _coverage(slots: list[dict[str, Any]], merged: dict[str, Any]) -> dict[str, Any]:
        """档案完整度：进度线、侧栏、结果页共用同一个口径，别各算一套。"""
        total = len(slots)
        filled = sum(1 for s in slots if merged.get(s["key"]))
        return {"filled": filled, "total": total,
                "percent": round(filled * 100 / total) if total else 0}

    def _profile_missing(self, sess: dict[str, Any]) -> list[dict[str, str]]:
        """还缺哪几项必填基本信息。跳过首屏表单的患者靠它在页面上看到提示。"""
        prof = sess.get("profile") or {}
        return [{"key": str(f["key"]), "label": str(f["label"])}
                for f in self.fields
                if f.get("required") and not prof.get(f["key"])]

    def _absorb_profile(self, sid: str, sess: dict[str, Any],
                        text: str) -> dict[str, Any]:
        """跳过表单后，从患者自己的话里补回性别 / 年龄。

        只认明确说法（"42岁"、"男，…"），宁可留空也不猜：病历抬头写错
        年龄比空着更糟。已填过的字段一律不动。
        """
        prof = dict(sess.get("profile") or {})
        missing = {str(f["key"]) for f in self.fields
                   if f.get("required") and not prof.get(f["key"])}
        if not missing:
            return sess
        got: dict[str, str] = {}
        if "age" in missing:
            m = re.search(r"(?:年龄\s*)?(\d{1,3})\s*岁", str(text or ""))
            if m:
                got["age"] = m.group(1)
        if "gender" in missing:
            m = re.search(r"(?:我是|性别是|性别)?\s*([男女])(?:性|士)?(?=[，,。、；;\s]|$)",
                          str(text or ""))
            if m:
                got["gender"] = m.group(1)
        if not got:
            return sess
        self.store.update(sid, profile=self.clean_profile({**prof, **got}))
        return self.store.get(sid) or sess

    def set_profile(self, sid: str, raw: dict[str, Any]) -> dict[str, Any]:
        """问诊中途补填 / 修改基本信息，不新建会话。"""
        sess = self.store.get(sid)
        if not sess:
            raise KeyError(sid)
        allowed = {str(f["key"]) for f in self.fields}
        prof = dict(sess.get("profile") or {})
        for k, v in (raw or {}).items():
            if k not in allowed:
                continue
            val = str(v or "").strip()
            if val:
                prof[k] = val
            else:
                prof.pop(k, None)     # 表单清空 = 真的删掉，别留着旧值
        self.store.update(sid, profile=self.clean_profile(prof))
        return self.view(self.store.get(sid) or sess)

    def supplement(self, sid: str, text: str) -> dict[str, Any]:
        """结束后继续补充信息：重新抽取并**重新生成**病历。

        不清空旧病历——万一中途出错，医生端还能看到上一版。轮次预算已经
        用满，_advance 会直接收网，所以这一句补充只被抽取，不会再多问一轮。
        """
        sess = self.store.get(sid)
        if not sess:
            raise KeyError(sid)
        if sess["state"] == "emergency":
            raise PermissionError("已触发急症提示，请立即就医，不能继续补充")
        if sess["state"] != "done":
            return self.reply(sid, text)
        text = str(text or "").strip()
        if not text:
            return self.view(sess)
        self.store.update(sid, state="asking")
        self.store.add_turn(sid, "user", text)
        return self._advance(sid, text)

    def view(self, sess: dict[str, Any], *, question: str = "",
             degraded: list[str] | None = None, note: str = "") -> dict[str, Any]:
        domain_key = str(sess.get("domain") or "") or self._domain_of(sess)
        slots = self._slots_for(domain_key,
                                self._slot_order.get(str(sess.get("id") or "")))
        merged = sess.get("slots") or {}
        attempts = sess.get("attempts") or {}
        slot_status = []
        for s in slots:
            v = merged.get(s["key"])
            if s.get("multi"):
                display = "、".join(v) if v else "—"
            else:
                display = str(v) if v not in (None, "") else "—"
            slot_status.append({
                "key": s["key"], "label": s["label"],
                "filled": bool(v), "display": display,
            })
        # 只把"还问得出来"的槽位算作待补：问过两次仍空着的已经从追问队列里
        # 摘掉，再算进 missing 会让界面一直显示"还差 2 项"却永远问不到。
        missing = [s["label"] for s in slots
                   if self._is_askable(s, merged, attempts)]
        prof = sess.get("profile") or {}
        profile_status = []
        for f in self.fields:
            v = prof.get(f["key"])
            if f.get("type") == "number" and v:
                v = f"{v}{f.get('unit') or ''}"
            profile_status.append({
                "key": f["key"], "label": f["label"],
                "value": v or "", "filled": bool(v),
                "display": v or "—",
            })
        return {
            "session_id": sess["id"],
            "state": sess["state"],
            "chief": sess["chief"],
            "profile": prof,
            "profile_status": profile_status,
            "slots": sess.get("slots") or {},
            "slot_status": slot_status,
            "missing": missing,
            "domain": domain_key,
            "domain_label": str(self._domain_cfg(domain_key).get("label") or ""),
            "danger": bool(sess.get("danger")),
            "danger_message": sess.get("danger_msg") or "",
            "question": question,
            "report": sess.get("report") or "",
            "record": sess.get("record") or None,
            "dept": sess.get("dept") or "",
            "urgency": sess.get("urgency") or "",
            "turns": sess.get("turns") or [],
            "degraded": degraded or [],
            "note": note,
            # 语义召回这一轮到底干了什么：判到哪个域、权重多少、命中几个
            # 分块、后端是不是没连上。调试"为什么问了这句"全靠它。
            "semantic": dict(self.last_semantic),
            # 诊断线索。**前端默认不渲染**——这是给医生端和调试看的，患者
            # 界面出现"你可能得了 X"是越界。字段放在这里是因为病历生成和
            # 分诊要用同一份数据，藏起来不如摆明了注明用途。
            "dx": self._dx_view(sess),
            # 完整度 / 待补档案 / 是否允许继续补充：结果页与侧栏共用。
            "coverage": self._coverage(slots, merged),
            "profile_missing": self._profile_missing(sess),
            "can_supplement": sess["state"] == "done",
            "meta": {
                "title": self.pv.get("title"),
                "subtitle": self.pv.get("subtitle"),
                "disclaimer": self.pv.get("disclaimer"),
                "placeholder": self.pv.get("placeholder"),
            },
        }

    # ---------------------------------------------------------------- 主流程
    def _advance(self, sid: str, text: str) -> dict[str, Any]:
        sess = self.store.get(sid)
        if not sess:
            raise KeyError(sid)
        degraded: list[str] = []

        # 0) 定域：首轮按主诉判断，一旦命中具体域就锁进会话，之后不再漂移。
        #    槽位集合、追问话术、甚至分诊默认科室都从这里派生。
        domain_key = self._domain_of(sess)
        if domain_key and domain_key != (sess.get("domain") or ""):
            self.store.update(sid, domain=domain_key)
            sess = self.store.get(sid)
        slots = self._slots_for(domain_key, self._slot_order.get(sid))

        # 1) 安检门：扫用户说过的所有话，不只看这一句
        danger = self._gate(self._user_text(sess))
        if danger:
            dept, urgency = "急诊科", "立即就医"
            record = self._record(
                sess, sess.get("slots") or {}, dept, urgency,
                str(danger.get("message") or ""),
                extra_note="触发安全闸门：" + str(danger.get("name") or ""),
                slots=slots,
            )
            self.store.update(sid, state="emergency", danger=True,
                              danger_msg=danger.get("message", ""),
                              dept=dept, urgency=urgency, record=record)
            self.store.add_turn(sid, "assistant", danger.get("message", ""))
            sess = self.store.get(sid)
            return self.view(sess, question=danger.get("message", ""), degraded=["danger_gate"])

        # 2) 抽取 + 更新档案。start() 的 _extract_chief 刚对同一句主诉抽过
        #    （_chief_done 里有标记）：结果必然相同，不重复调模型，患者少等
        #    两三秒；degraded 标记沿用那次抽取的真实情况。
        if sid in self._chief_done:
            used_llm = self._chief_done.pop(sid)
            got: dict[str, Any] = {}
        else:
            got, used_llm = self._extract(text, slots, sess.get("profile") or {},
                                          str(sess.get("chief") or ""))
        if not used_llm:
            degraded.append("extract_rules")
        merged = self._merge(sess.get("slots") or {}, got, slots)
        self.store.update(sid, slots=merged)
        sess = self.store.get(sid)

        # 2.4) 跳过首屏表单的患者，性别 / 年龄在对话里顺手补齐。放在抽取
        #      之后、图谱记账之前：补完的档案马上能参与本轮分诊。
        sess = self._absorb_profile(sid, sess, text)

        # 2.5) 上一句图谱追问的答案先记账（有/没有/没答上来），再决定下一句。
        #     放在这里而不是最后：不记账的话，患者刚回答的"有咯血"永远进不
        #     了病历，图谱追问就白问了。
        ros_consumed = self._ros_record(sid, sess, text)
        sess = self.store.get(sid)

        # 2.6) 明确否定/明确不知道的快答。患者答"没有""没了"就是在回答刚才
        #      那个问题，状态机必须给个说法：记"无"（病历上留明确阴性），
        #      或者跳过（"不清楚"不是否定，硬问第二遍也问不出来）。
        #      只在图谱没消费这句话时才做——挂着"有没有咯血"时患者答"没有"，
        #      记的是 ROS 的阴性，不能再顺手把伴随症状槽位也写成"无"。
        if not ros_consumed:
            merged, sess = self._fast_answer(sid, sess, merged, slots, text, got)

        # 2.7) 诊断线索：把这句话归一到标准症状累积起来（"拉稀"→"腹泻"）。
        #      只做线索，不下诊断——候选病与科室倾向留给追问和给医生的参考栏。
        dx = self._dx_update(sid, sess, text, merged)
        dx = self._dx_merge_medrag(dx)
        dx = self._dx_lock(sess, dx)
        self.store.update(sid, dx=dx)
        sess = self.store.get(sid)

        # 3) 槽位齐 / 轮次用尽 → 分诊 + 病历
        attempts = sess.get("attempts") or {}
        missing = [s["key"] for s in slots if self._is_askable(s, merged, attempts)]
        asked = sum(1 for t in sess["turns"] if t["role"] == "user")
        max_turns = int(self.pv.get("max_turns") or 10)
        if asked >= max_turns:
            profile = sess.get("profile") or {}
            dept, urgency = self._triage(merged, profile, domain_key)
            report, ok = self._report(sess, merged, dept, urgency, slots)
            if not ok:
                degraded.append("report_template")
            record = self._record(sess, merged, dept, urgency, report, slots=slots,
                                  triage_source=self._triage_source)
            self.store.update(sid, state="done", dept=dept, urgency=urgency,
                              report=report, record=record)
            self.store.add_turn(sid, "assistant", report)
            sess = self.store.get(sid)
            return self.view(sess, question=report, degraded=degraded)

        # 3.5) 槽位问齐、还有预算 → 图谱追问（系统回顾）。
        #      位置很关键：放在槽位之后、收网之前。提前会打乱"先问清主诉"
        #      的顺序，患者会觉得系统在东拉西扯；放到最后就等于没问。
        if not missing:
            cand = self._ros_take(sess, merged)
            if cand:
                st = self._ros_state(sess)
                st["pending"] = str(cand.get("symptom") or "")
                self.store.update(sid, ros=st)
                question = self._ros_question(st["pending"])
                self.store.add_turn(sid, "assistant", question)
                sess = self.store.get(sid)
                return self.view(sess, question=question, degraded=degraded)

        if not missing:
            profile = sess.get("profile") or {}
            dept, urgency = self._triage(merged, profile, domain_key)
            report, ok = self._report(sess, merged, dept, urgency, slots)
            if not ok:
                degraded.append("report_template")
            record = self._record(sess, merged, dept, urgency, report, slots=slots,
                                  triage_source=self._triage_source)
            self.store.update(sid, state="done", dept=dept, urgency=urgency,
                              report=report, record=record)
            self.store.add_turn(sid, "assistant", report)
            sess = self.store.get(sid)
            return self.view(sess, question=report, degraded=degraded)

        # 4) 还有缺失 → 从知识库取「这个域这个槽位该怎么问」 → 生成下一句
        focus = next(s for s in slots if s["key"] == missing[0])
        tried = int(attempts.get(focus["key"]) or 0)
        tips, ask_hint = self._tips(sess, merged, focus, domain_key)
        question, ok = self._ask(sess, merged, focus, tips, ask_hint, slots, tried)
        if not ok:
            degraded.append("ask_template")
        self._bump(sess, focus["key"])
        # 记下这句问的是谁：下一轮患者答"没有"时，否定快答靠它找到目标，
        # 不然重问中的槽位已被排除、答案会被安到别的槽位头上。
        self.store.update(sid, last_ask=focus["key"])
        self.store.add_turn(sid, "assistant", question)
        sess = self.store.get(sid)
        return self.view(sess, question=question, degraded=degraded)

    def _bump(self, sess: dict[str, Any], key: str) -> None:
        """记一次"这个槽位问过了"。用来实现"问两次就不问了"。"""
        att = dict(sess.get("attempts") or {})
        att[key] = int(att.get(key) or 0) + 1
        self.store.update(sess["id"], attempts=att)

    def _fast_answer(self, sid: str, sess: dict[str, Any], merged: dict[str, Any],
                     slots: list[dict[str, Any]], text: str,
                     got: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        """明确否定/明确不知道的短答落账。

        目标槽位 = **上一句实际问的那个**（last_ask 记录在案）。不能拿"追问
        队列第一个"来猜：重问中的槽位 attempts 已到顶、被排除在队列外，猜的
        结果就是把"没有"安到下一个无辜槽位头上（实测过敏史因此被记成"无"，
        而患者否定的其实是相关病史）。last_ask 也空了才退回队列第一个。
        只在这一轮**什么都没抽到**时才生效：患者说"没有，就是有点恶心"，
        伴随症状已经被正常抽到了，绝不能让这句"没有"再写"无"。
        """
        if got:
            return merged, sess
        last = str(sess.get("last_ask") or "")
        target = None
        if last:
            target = next((s for s in slots if s["key"] == last
                           and not merged.get(s["key"])), None)
        if target is None:
            target = next((s for s in slots
                           if self._is_askable(s, merged, sess.get("attempts") or {})), None)
        if target is None:
            return merged, sess
        att = dict(sess.get("attempts") or {})
        if _explicit_no(text):
            # "无"是**明确阴性**，比空着强得多：病历上写"伴随症状：无"，
            # 医生才知道这是问过且否认的，而不是没问到。
            val = ["无"] if target.get("multi") else "无"
            merged = self._merge(merged, {target["key"]: val}, slots)
            att[target["key"]] = 2          # 顺带把追问额度关掉，不再问第二遍
            self.store.update(sid, slots=merged, attempts=att)
        elif _explicit_unknown(text):
            att[target["key"]] = 2          # 不知道≠没有：不记账，但也不再问
            self.store.update(sid, attempts=att)
        else:
            return merged, sess
        return merged, self.store.get(sid) or sess

    # ---------------------------------------------------------------- 安检门
    def _gate(self, text: str) -> dict[str, Any] | None:
        for rule in self.pv.get("danger_rules") or []:
            for trigger in rule.get("triggers") or []:
                trigger = str(trigger)
                if trigger and trigger in text:
                    return rule
        return None

    # ---------------------------------------------------------------- 抽取
    def _extract(self, text: str, slots: list[dict[str, Any]],
                 profile: dict[str, Any] | None = None,
                 chief: str = "") -> tuple[dict[str, Any], bool]:
        # 规则先抽：对"右下腹 / 绞痛 / 6小时"这类明确表达，规则比模型稳得多，
        # 也不受有没有密钥影响。
        rule_got: dict[str, Any] = {}
        for s in slots:
            fn = _RULE_EXTRACTORS.get(s["key"])
            if not fn:
                continue
            try:
                val = fn(text)
            except Exception:
                val = None
            if s["key"] == "companions" and val and chief:
                # 只排除**主诉那一句**里的症状：主诉"宝宝发烧39度"再把发烧
                # 记进伴随症状，病历上就是同一件事写两遍。
                #
                # 但绝不能按"这个词在不在整段主诉里"来排除——患者说"胸口
                # 压着疼，还出汗"，"出汗"当然出现在主诉里，可它正是 ACS 的
                # 红旗征，被排掉后分诊规则匹配不到，本该"尽快就诊"的判成了
                # "门诊就诊"。按第一个分句切，只删主症本身。
                head = str(chief).replace(",", "，").split("，")[0]
                val = [w for w in val if w not in head]
            if val:
                rule_got[s["key"]] = val

        # 再让模型补规则抽不到的（多值槽位取并集，单值以规则为准）
        llm_got: dict[str, Any] = {}
        used_llm = False
        prompt = self._extract_prompt(slots, profile or {})
        raw = self._complete("你是医疗预问诊的信息抽取器，只输出 JSON。", prompt + "\n\n患者这句话：" + text)
        if raw:
            parsed = self._parse_json(raw)
            if isinstance(parsed, dict):
                used_llm = True
                for s in slots:
                    k = s["key"]
                    if k in parsed and parsed[k] not in (None, "", []):
                        llm_got[k] = parsed[k]

        got: dict[str, Any] = {}
        for s in slots:
            k = s["key"]
            if s.get("multi"):
                cur: list[str] = []
                for src in (rule_got.get(k), llm_got.get(k)):
                    if isinstance(src, list):
                        cur.extend(str(x).strip() for x in src if str(x).strip())
                    elif isinstance(src, str) and src.strip():
                        cur.append(src.strip())
                if cur:
                    got[k] = list(dict.fromkeys(cur))
            else:
                got[k] = rule_got.get(k) or llm_got.get(k)
                if not got[k]:
                    got.pop(k, None)
        return got, used_llm

    def _extract_prompt(self, slots: list[dict[str, Any]],
                        profile: dict[str, Any]) -> str:
        fields = "、".join(f'{s["label"]}（{s["key"]}，{s.get("hint","")}）' for s in slots)
        multi = "、".join(s["key"] for s in slots if s.get("multi")) or "无"
        tpl = (self.pv.get("prompts") or {}).get("extract", "")
        return _fmt(tpl, fields=fields, multi_fields=multi,
                    profile=self._profile_text(profile) or "（未填）")

    @staticmethod
    def _parse_json(raw: str) -> Any:
        s = raw.strip()
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
        m = re.search(r"\{.*\}", s, re.S)
        if not m:
            return None
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return None

    # ---------------------------------------------------------------- 档案
    @staticmethod
    def _merge(old: dict[str, Any], got: dict[str, Any],
               slots: list[dict[str, Any]]) -> dict[str, Any]:
        out = dict(old or {})
        for s in slots:
            k = s["key"]
            if k not in got:
                continue
            v = got[k]
            if s.get("multi"):
                cur = [x for x in (out.get(k) or []) if x != "无"]
                add = v if isinstance(v, list) else ([v] if v else [])
                cur = [*cur, *[str(x).strip() for x in add
                               if str(x).strip() and str(x).strip() != "无"]]
                # 全是"无"又没有新内容时保留"无"——明确阴性不能被冲掉
                out[k] = list(dict.fromkeys(cur)) or ["无"]
            elif v not in (None, "", []):
                out[k] = v
        return out

    @staticmethod
    def _missing(merged: dict[str, Any], slots: list[dict[str, Any]]) -> list[str]:
        return [s["key"] for s in slots if not merged.get(s["key"])]

    # ---------------------------------------------------------------- 基本信息
    def _profile_text(self, profile: dict[str, Any]) -> str:
        """把基本信息拼成一句给大模型看的上下文。

        刻意不带姓名：姓名对"问什么问题、去哪个科"没有任何信息量，却在提示词
        和日志里多留一处隐私。病历抬头才用它。
        """
        parts = []
        for f in self.fields:
            k = f["key"]
            v = profile.get(k)
            if not v or k == "name":
                continue
            unit = (f.get("unit") or "") if f.get("type") == "number" else ""
            parts.append(f'{f["label"]}：{v}{unit}')
        return "；".join(parts)

    def _profile_headline(self, profile: dict[str, Any]) -> str:
        """病历抬头：姓名，42岁男，怀孕中。"""
        bits: list[str] = []
        name = str(profile.get("name") or "").strip()
        if name:
            bits.append(name)
        age = str(profile.get("age") or "").strip()
        gender = str(profile.get("gender") or "").strip()
        if age and gender:
            bits.append(f"{age}岁{gender}")
        elif age:
            bits.append(f"{age}岁")
        elif gender:
            bits.append(gender)
        preg = str(profile.get("pregnant") or "").strip()
        if preg and preg != "不适用":
            bits.append(preg)
        return "，".join(bits) or "未填写"

    @staticmethod
    def _describe(merged: dict[str, Any], slots: list[dict[str, Any]]) -> str:
        parts = []
        for s in slots:
            v = merged.get(s["key"])
            if not v:
                continue
            if s.get("multi"):
                parts.append(f'{s["label"]}：{"、".join(v)}')
            else:
                parts.append(f'{s["label"]}：{v}')
        return "；".join(parts)

    @staticmethod
    def _values(merged: dict[str, Any]) -> list[str]:
        out: list[str] = []
        for v in merged.values():
            if isinstance(v, list):
                out.extend(str(x) for x in v)
            elif v:
                out.append(str(v))
        return out

    @staticmethod
    def _user_text(sess: dict[str, Any]) -> str:
        return "\n".join(t["content"] for t in sess.get("turns") or [] if t["role"] == "user")

    # ---------------------------------------------------------------- 检索
    def _tips(self, sess: dict[str, Any], merged: dict[str, Any],
              focus: dict[str, Any], domain_key: str) -> tuple[str, str]:
        """从知识库取这一步的「知识点」和「现成问法」。

        返回 (小抄, 现成问法)。小抄喂给大模型当参考，问法是模型不可用时的
        兜底——所以知识库里的 ask 必须是一句能直接发给患者的话，不是提示词。
        """
        query = " ".join([
            str(sess.get("chief") or ""),
            *self._values(merged),
            focus["label"], focus.get("hint", ""),
        ])
        got = kb_mod.for_slot(self._domain_cfg(domain_key), focus["key"], query)
        if got:
            text = str(got[0].get("text") or "").strip()
            ask = str(got[0].get("ask") or "").strip()
            return (f"· {text}" if text else "（无）"), ask
        # 兜底：配置里还是旧格式的平铺 kb（没有 domains），按老办法检索
        items = kb_mod.retrieve(self.pv.get("kb") or [], query, top_k=2)
        texts = [str(e.get("text") or "").strip() for e in items if e.get("text")]
        return ("\n".join("· " + t for t in texts) or "（无）"), ""

    # ---------------------------------------------------------------- 生成
    def _ask(self, sess: dict[str, Any], merged: dict[str, Any],
             focus: dict[str, Any], tips: str, ask_hint: str,
             slots: list[dict[str, Any]], tried: int = 0) -> tuple[str, bool]:
        known = self._describe(merged, slots)
        tpl = (self.pv.get("prompts") or {}).get("ask", "")
        hint = str(focus.get("hint") or "").strip()
        # kb_first：知识库里为这个症状这个槽位手写的现成问法直接发，不调模型。
        # 一轮 reply 本来是「抽取 + 追问」两次串行模型调用，省掉追问那次，
        # 患者每轮少等两三秒；现成问法可审计，也比模型即兴的更稳。
        # 重问（tried>0）仍走模型——那正是需要"换个角度"即兴能力的场景。
        if (tried == 0 and ask_hint
                and str(self.pv.get("ask_llm") or "auto").strip().lower() == "kb_first"
                and self._safe(ask_hint)):
            return self._first_question(ask_hint), True
        # 已经问过一次还没答上来：不能再把同一句话发一遍。患者会觉得系统
        # 卡住了，而且第二遍的回答通常和第一遍一样——真正需要的是换个角度。
        if tried > 0:
            retry_note = "（这个问题已经问过一次，患者没答到点上：请换一个角度或给更具体的选项，不要重复上一次的问法）"
            ask_ref = "（换一种问法）"
        else:
            retry_note = ""
            ask_ref = ask_hint or "（无）"
        prompt = _fmt(tpl, known=known or "（暂无）", label=focus["label"],
                      hint=hint, tips=tips, ask=ask_ref, retry=retry_note,
                      profile=self._profile_text(sess.get("profile") or {}) or "（未填）")
        if tried > 0:
            if hint:
                # hint 是选项式（"压榨样 / 闷痛 / 刺痛"）就问"哪种更接近"，
                # 是描述式（"烧几天了、退烧药能不能降"）就请对方再说具体点。
                joiner = "，您觉得哪一种更接近？" if ("/" in hint or "、" in hint) \
                    else "，能再具体说说吗？"
                fallback = hint + joiner
            else:
                fallback = f'能再说说{focus["label"]}的情况吗？'
        else:
            # 兜底优先级：知识库现成问法 > 通用模板。知识库那句是为这个症状
            # 这个槽位写好的，比"关于X能具体说说吗"专业得多。
            fallback = ask_hint or (f'关于{focus["label"]}，{hint}，能具体说说吗？'
                                    if hint else f'关于{focus["label"]}，能具体说说吗？')
        raw = self._complete("你是预问诊助手，只能追问，不能诊断或推荐药物。", prompt)
        if not raw:
            return self._fallback(fallback, focus), False
        text = raw.strip().strip('"').strip("“”").strip()
        if not text or not self._safe(text):
            return self._fallback(fallback, focus), False
        return self._first_question(text), True

    @staticmethod
    def _first_question(text: str) -> str:
        """物理截断：只保留第一个问号（含）之前的内容。

        模型天生爱连珠炮——"那您肚子疼是偏上腹还是偏下腹？有没有发烧？有没有
        吐过？"——一次抛三个问题，患者只会答最后一个，另外两个白问，而状态机
        还等着三个答案。在 prompt 里写"只问一个问题"是**建议**，模型经常不
        听；代码强切是**事实**。这是流程图里那步"物理截断"的落地。
        """
        s = str(text or "")
        for i, ch in enumerate(s):
            if ch in "？?":
                return s[:i + 1].strip()
        return s.strip()

    def _fallback(self, text: str, focus: dict[str, Any]) -> str:
        """兜底话术也要过合规扫描：知识库是人写的，同样可能写进不该说的词。"""
        return text if self._safe(text) else f'能再说说{focus["label"]}的情况吗？'

    def _report(self, sess: dict[str, Any], merged: dict[str, Any],
                dept: str, urgency: str,
                slots: list[dict[str, Any]] | None = None) -> tuple[str, bool]:
        slots = slots or self.slots
        known = self._describe(merged, slots)
        chief = str(sess.get("chief") or "")
        tpl = (self.pv.get("prompts") or {}).get("report", "")
        prompt = _fmt(tpl, known=(f"主诉：{chief}；{known}" if known else f"主诉：{chief}"),
                      dept=dept, urgency=urgency,
                      profile=self._profile_text(sess.get("profile") or {}) or "（未填）")
        tail = "以上信息由 AI 辅助整理，请以医生面诊为准。"
        fallback = (f"患者主诉{chief}。" + (f"{known}。" if known else "")
                    + f"建议就诊科室：{dept}，{urgency}。{tail}")
        warning = str(self.pv.get("disclaimer") or "").strip()
        try:
            raw = self._complete("你是预问诊助手，只做信息整理，不能诊断。", prompt)
            if not raw:
                return fallback, False
            text = raw.strip()
            if tail[:8] not in text:
                text = text.rstrip("。") + "。" + tail
            if warning and warning not in text:
                # 免责声明放在最后，保证每份报告都有
                text = text.rstrip() + "\n\n" + warning
            if not self._safe(text):
                return fallback, False
            return text, True
        except Exception:
            return fallback, False

    # ---------------------------------------------------------------- 病历
    def _record(self, sess: dict[str, Any], merged: dict[str, Any],
                dept: str, urgency: str, summary: str,
                extra_note: str = "",
                slots: list[dict[str, Any]] | None = None,
                triage_source: str = "rule") -> dict[str, Any]:
        """把档案拼成结构化病历。

        为什么不用大模型整篇写：主诉、现病史、既往史、分诊结论都是**已经结构化**
        的事实，让模型重述一遍只会引入改写误差（把"绞痛"写成"隐痛"、把"6 小时"
        写成"数小时"）。模型只负责 summary 那一栏的润色，其余字段是拼出来的，
        逐条可追溯到原始输入。
        """
        profile = sess.get("profile") or {}
        chief = str(sess.get("chief") or "").strip()
        use_slots = slots or self.slots

        # 主诉：原话 + 持续时间（原话里已经带了就不重复）
        duration = str(merged.get("duration") or "").strip()
        chief_line = chief
        if duration and duration not in chief:
            chief_line = f"{chief}，持续{duration}".strip("，")

        # 现病史：每个槽位一行，没采集到的显式写"未采集"——病历里留白会被
        # 当成"问过且正常"，那是错的；写明未采集，医生才知道要补问。
        # 但一个都没采到时（急诊直接中断）不列五行"未采集"，那只是噪声，
        # 整段省略，让病历停留在"主诉 + 分诊"。
        present_lines = []
        # 既往史/过敏史属于"既往史"栏的内容，不重复塞进现病史——否则病历上
        # 会出现"现病史：既往病史高血压"和"既往史：未提供"并列，医生一眼就
        # 看出自相矛盾。
        always_keys = {str(s["key"]) for s in self.slots if s.get("always")}
        for s in use_slots:
            if str(s["key"]) in always_keys:
                continue
            v = merged.get(s["key"])
            if s.get("multi"):
                val = "、".join(str(x) for x in v) if v else "未采集"
            else:
                val = str(v) if v not in (None, "") else "未采集"
            present_lines.append(f'{s["label"]}：{val}')
        present = "\n".join(present_lines)
        if all("：未采集" in ln for ln in present_lines):
            present = ""

        # 系统回顾（图谱追问）的结果单列一行：它是"患者明确回答了有没有"的
        # 事实，跟槽位里自由描述的内容不是一个来源，混在一起医生分不清哪些
        # 是患者主动说的、哪些是系统问出来的。
        st = self._ros_state(sess)
        answers = st.get("answers") or {}
        if isinstance(answers, dict) and answers:
            pos = [k for k, v in answers.items() if v == "yes"]
            neg = [k for k, v in answers.items() if v == "no"]
            lines = ["系统回顾（图谱追问，患者逐项确认）"]
            lines.append(f'  有：{"、".join(pos) if pos else "无"}')
            lines.append(f'  无：{"、".join(neg) if neg else "无"}')
            present = (present + "\n" if present else "") + "\n".join(lines)

        # 既往史：表单里除抬头字段外的都归到这一栏。**表单没填就用问诊
        # 里问到的值回填**——患者在第 5 轮答了"有高血压"，既往史栏还写
        # "未提供"就是打自己的脸。
        head_keys = {"name", "gender", "age", "pregnant"}
        slot_fallback = {"allergy": "allergy", "history": "past_history"}
        past_lines = []
        for f in self.fields:
            if f["key"] in head_keys:
                continue
            val = profile.get(f["key"])
            if not val:
                src = slot_fallback.get(str(f["key"]))
                sv = merged.get(src) if src else None
                val = "、".join(str(x) for x in sv) if isinstance(sv, list) else sv
            past_lines.append(f'{f["label"]}：{val or "未提供"}')

        # 用「·」而不是括号拼接：urgency 本身就带括号（"尽快就诊（警惕阑尾炎）"），
        # 再套一层就成了"普外科（尽快就诊（警惕阑尾炎））"，括号嵌套读着别扭。
        triage_line = f"{dept} · {urgency}" if (dept and urgency) else (dept or urgency or "未给出")
        if extra_note:
            triage_line += f"\n{extra_note}"

        body = {
            "basic": self._profile_headline(profile),
            "chief": chief_line or "未填写",
            "present": present,
            "past": "\n".join(past_lines) or "未提供",
            "triage": triage_line,
            "summary": (summary or "").strip(),
        }

        cfg_rec = self.pv.get("record") or {}
        picked = [s for s in (cfg_rec.get("sections") or []) if s.get("key") in body]
        if not picked:      # 配置被写空时不能让病历变成空白页
            picked = [{"key": k, "label": v} for k, v in (
                ("basic", "基本信息"), ("chief", "主诉"), ("present", "现病史"),
                ("past", "既往史 / 过敏 / 用药"), ("triage", "分诊建议"),
                ("summary", "就诊摘要"),
            )]
        sections = [{"key": s["key"], "label": str(s.get("label") or s["key"]),
                     "text": body[s["key"]]} for s in picked if body.get(s["key"])]

        title = str(cfg_rec.get("title") or "预问诊病历")
        note = str(cfg_rec.get("note") or "")
        text = title + "\n" + "\n\n".join(
            f"【{s['label']}】\n{s['text']}" for s in sections)
        if note:
            text += "\n\n" + note
        return {
            "title": title,
            "created_at": time.strftime("%Y-%m-%d %H:%M"),
            "chief": chief_line,
            "dept": dept,
            "urgency": urgency,
            # 分诊来源：rule = 本地规则/域/图谱；llm = 完全没线索时的推理兜底。
            # 医生端/接口可据此区分"系统判的"与"模型猜的"，不进患者可见的 text。
            "triage_source": triage_source,
            "sections": sections,
            "text": text,
            "note": note,
            # 给医生的参考线索，**故意不进 sections、不进 text**：病历正文
            # 患者也会看到，"你可能存在 X"这种字样出现在患者界面上就是越界。
            # 医生端/接口需要时单独取这个字段。
            "clues": self._clues_text(sess),
            # 患者原话全程留档：槽位是抽出来的结论，医生有时要看原话判断
            # 抽得对不对。只放额外字段，不进 sections / text（患者界面不展示）。
            "patient_turns": [
                {"index": i + 1, "text": str(t.get("content") or "")}
                for i, t in enumerate(sess.get("turns") or [])
                if t.get("role") == "user"
            ],
            "coverage": self._coverage(use_slots, merged),
            "missing": [str(s["label"]) for s in use_slots if not merged.get(s["key"])],
            "edited": False,
        }

    def _clues_text(self, sess: dict[str, Any]) -> str:
        """把诊断线索拼成一段给医生看的话。空线索返回空串。"""
        dx = self._dx_state(sess)
        if not dx:
            return ""
        lines: list[str] = []
        syms = [str(x) for x in (dx.get("symptoms") or [])]
        if syms:
            lines.append("标准症状：" + "、".join(syms))
        conds = [str(x) for x in (dx.get("conditions") or [])]
        if conds:
            lines.append("提到的既往病：" + "、".join(conds))
        cands = dx.get("candidates") or []
        if cands:
            lines.append("症状组合提示：" + "；".join(
                f'{c.get("disease")}（命中 {c.get("hits")} 项）' for c in cands[:3]))
        med = [str(m.get("disease")) for m in (dx.get("medrag") or []) if m.get("disease")]
        if med:
            lines.append("MedRAG 相关病种：" + "、".join(med))
        dept = dx.get("dept") or []
        if dept:
            # 明确写成"图谱投票"：它跟上面的分诊建议可能不一致（投票是全科
            # 室分布，分诊是规则优先），不写清楚医生会以为系统自相矛盾。
            lines.append("图谱科室投票（对照用）：" + "、".join(
                f'{d.get("dept")} {int(round(float(d.get("ratio") or 0) * 100))}%'
                for d in dept[:3]))
        if not lines:
            return ""
        lines.append("※ 以上是知识库匹配结果，用于协助医生定位，不构成诊断。")
        return "\n".join(lines)

    # ---------------------------------------------------------------- 分诊
    def _triage(self, merged: dict[str, Any],
                profile: dict[str, Any] | None = None,
                domain_key: str = "") -> tuple[str, str]:
        profile = profile or {}
        for rule in self.pv.get("triage_rules") or []:
            # 规则可以限定只对某些症状域生效（胸痛的红旗征跟皮疹没关系），
            # 不写 domain 就是全局规则。
            need = [str(x) for x in (rule.get("domain") or [])]
            if need and domain_key not in need:
                continue
            conds = rule.get("when") or []
            if conds and all(self._cond(c, merged, profile) for c in conds):
                return str(rule.get("dept") or ""), str(rule.get("urgency") or "")
        d = self.pv.get("default") or {}
        # 兜底顺序：该症状域的默认科室 > 图谱投票 > 全局 default。
        # 咳嗽去呼吸内科总比去"全科医学科"有用，域里那个 dept 就是为这件事
        # 写的；域没写时，用图谱里"含这些症状的疾病都挂哪个科"投票——比
        # 给一个万能的"全科医学科"更贴合，而且票数可追溯。
        dom_dept = str(self._domain_cfg(domain_key).get("dept") or "")
        if not dom_dept and self.kg_cfg.get("dept_fallback") and self.kg.ok:
            votes = self.kg.dept_votes(self._kg_seeds("", merged), limit=1)
            if votes and str(votes[0].get("ratio") or 0) not in ("", "0"):
                return str(votes[0].get("dept") or ""), str(d.get("urgency") or "门诊就诊")
        dept = dom_dept or str(d.get("dept") or "") or "全科医学科"
        # 规则/域/图谱都没给具体科室（落到「全科医学科」裸兜底）时，再用 LLM
        # 做一次**轻量分诊兜底**——只建议科室 + 紧急程度，不做诊断。只要前面
        # 有任何一条具体依据（规则命中 / 域默认 / 图谱投票），就不碰 LLM：
        # 可审计的本地结论优先，模型只是"完全没线索"时的最后手段。无模型
        # 时 _triage_llm 直接返回 None，这里退回原规则，行为不变。
        if dept == "全科医学科":
            llm_tri = self._triage_llm(merged, profile)
            if llm_tri:
                self._triage_source = "llm"
                return llm_tri[0], llm_tri[1]
        self._triage_source = "rule"
        return dept, str(d.get("urgency") or "门诊就诊")

    def _triage_llm(self, merged: dict[str, Any],
                    profile: dict[str, Any] | None = None) -> tuple[str, str] | None:
        """分诊的轻量推理兜底（**非诊断**）。

        仅在 `_triage` 落到「全科医学科」裸兜底时调用。模型只回答两件事：
        最可能的就诊科室、紧急程度（门诊就诊 / 尽快就诊 / 急诊），不诊断、
        不给治疗建议。任何解析失败或没配模型都返回 None，调用方退回规则。
        """
        if self.llm is None:
            return None
        # 只挑症状相关字段，别把年龄/性别/过敏史这些无关信息喂进去干扰判断。
        skip = {"name", "gender", "age", "pregnant", "allergy", "past_history"}
        syms = [f"{k}={v}" for k, v in merged.items()
                if k not in skip and v not in (None, "", [], {})]
        chief = (profile or {}).get("chief") or ""
        if not syms and not chief:
            return None
        user = "患者自述症状：\n" + "\n".join(syms[:20])
        if chief:
            user += f"\n主诉：{chief}"
        system = (
            "你是分诊助手。只根据患者自述的症状，建议最可能的就诊科室和紧急程度。"
            "不要做诊断，不要给任何治疗建议。严格只输出一行，格式：科室｜紧急程度。"
            "紧急程度从[门诊就诊, 尽快就诊, 急诊]三选一。"
            "示例：消化内科｜尽快就诊"
        )
        out = self._complete(system, user)
        if not out:
            return None
        # 取最后一行：模型偶尔会先复述一遍指令，最后那行才是答案。
        line = [ln for ln in out.splitlines() if ln.strip()][-1]
        if "｜" in line:
            parts = line.split("｜", 1)
        elif "|" in line:
            parts = line.split("|", 1)
        elif "·" in line:
            parts = line.split("·", 1)
        else:
            return None
        dept = parts[0].strip().strip("【】[]（）()")
        urg = parts[1].strip() if len(parts) > 1 else ""
        if not dept:
            return None
        if urg not in ("门诊就诊", "尽快就诊", "急诊"):
            urg = "门诊就诊"
        return dept, urg

    def _cond(self, c: dict[str, Any], merged: dict[str, Any],
              profile: dict[str, Any] | None = None) -> bool:
        slot = str(c.get("slot") or "")
        if slot.startswith("profile."):
            val = (profile or {}).get(slot.split(".", 1)[1])
        else:
            val = merged.get(slot)
        op = str(c.get("op") or "eq")
        want = c.get("value")
        if op == "truthy":
            return self._truthy(val)
        if op == "any":
            return bool(val)
        if op == "eq":
            return str(val or "") == str(want or "")
        if op == "in":
            return str(val or "") in [str(x) for x in (want or [])]
        if op == "contains":
            if isinstance(val, list):
                return str(want) in [str(x) for x in val]
            return str(want) in str(val or "")
        if op == "gte":
            try:
                num = float(re.sub(r"[^\d.]", "", str(val) or "0") or 0)
                return num >= float(want or 0)
            except (TypeError, ValueError):
                return False
        return False

    @staticmethod
    def _truthy(v: Any) -> bool:
        if isinstance(v, list):
            return bool(v)
        if isinstance(v, str):
            return v.strip() not in ("", "否", "没有", "无", "no", "false", "False", "0")
        return bool(v)

    # ---------------------------------------------------------------- 合规/LLM
    def _safe(self, text: str) -> bool:
        for pat in self.pv.get("forbidden") or []:
            pat = str(pat)
            try:
                if re.search(pat, text):
                    return False
            except re.error:
                if pat in text:
                    return False
        return True

    def _complete(self, system: str, user: str) -> str:
        if time.time() < self._down_until:
            return ""
        if self.llm is None:          # 没配模型：不试探、不报错，直接走规则
            return ""
        msgs = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        try:
            fut = self._pool.submit(self._call, msgs)
            return fut.result(timeout=self.timeout_s)
        except FutureTimeout:
            # 注意：工作线程还在跑，它跑完结果会被丢弃（已经过期）。这正是
            # 想要的——不能让一个超时的回答在下一轮莫名其妙地冒出来。
            self._last_error = f"LLM 超时（>{self.timeout_s:.0f}s）"
            self._down_until = time.time() + self.LLM_COOLDOWN_S
            return ""

    def _call(self, msgs: list[dict[str, str]]) -> str:
        try:
            text = self.llm.complete(
                msgs,
                temperature=float(self.pv.get("temperature") or 0.2),
                max_tokens=int(self.pv.get("max_tokens") or 320),
            )
            return (text or "").strip()
        except LLMError as exc:
            self._last_error = str(exc)
            self._down_until = time.time() + self.LLM_COOLDOWN_S
            return ""
        except Exception as exc:      # 任何异常都不该把问诊卡死
            self._last_error = f"{type(exc).__name__}: {exc}"
            self._down_until = time.time() + self.LLM_COOLDOWN_S
            return ""
