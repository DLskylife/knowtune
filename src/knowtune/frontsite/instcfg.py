"""前台问答站点的实例配置（2026-10-28）。

一个"实例"= 一个独立的问答站点：绑定一个知识库、一个模型后端、一个端口，
有自己的标题/欢迎语/示例问题/界面语言。后台负责启停，前台只管问答——两边
各跑各的端口，界面和代码都不交叉。

配置和 models.yaml 分开存：混在一个文件里，改模型会顺手带出站点配置，
排查时还得先分辨哪段是给谁看的。
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

INSTANCES_CONFIG = Path(__file__).resolve().parents[3] / "config" / "instances.yaml"

NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{1,23}$")

# 界面/回答语言。维吾尔语是主场景，中文留作对照与调试。
LANGS = ("ug", "zh", "en")

# 回答怎么产出：
#   direct    —— 直接让模型用维吾尔语答（快，但 7B 级模型质量很差）
#   translate —— 先用资料语言（中文）答，再整段译成维吾尔语（慢一倍，但明显更稳）
# 默认取后者：小模型"看着中文资料写维吾尔语"几乎必然跑偏，而"翻译一段写好的
# 中文"是它更擅长的事。换了大模型再改成 direct。
ANSWER_MODES = ("translate", "direct")

# 端口范围：避开本系统 Web 服务常用的 8000 与 vLLM 的 8100，
# 从 8800 起给前台实例用，冲突概率最低、也最好认。
PORT_MIN = 8800
PORT_MAX = 8899

# 前台站点在 /api/info 里报的身份。后台靠它回答"这个端口上跑的是谁"——
# 少了这个字段，两个后台撞同一个端口时只能说"已被占用"，说不出占的是谁。
SITE_APP_TAG = "knowtune-frontsite"


@dataclass
class InstanceSpec:
    name: str
    kb_id: int
    port: int
    lang: str = "ug"
    kb_lang: str = "zh"         # 知识库内容的语言；与 lang 不同才需要翻译检索
    answer_mode: str = "translate"   # direct | translate
    provider: str = ""          # 留空 = 用后台当前默认后端
    # 角色分工：{"translate": "hunyuan", "generate": "local-qwen"}。
    # 留空 = 跟随系统 roles（再没有就用上面的 provider）。
    roles: dict[str, str] = field(default_factory=dict)
    title: str = ""
    subtitle: str = ""
    welcome: str = ""
    samples: list[str] = field(default_factory=list)
    disclaimer: str = ""
    # 知识库在界面上显示的名字。留空 = 用库自己的名字（通常中文）。
    # 维语站点的侧栏顶着一行"维医知识库"四个汉字，比按钮没翻还显眼，
    # 所以这个名字要能单独配——另一套语言的写在 alt.kb_name 里。
    kb_name: str = ""
    autostart: bool = False     # 后台启动时自动拉起
    public: bool = False        # True 时监听 0.0.0.0（局域网可访问）
    # 另一套语言的界面文案。顶栏那颗语言按钮是**整站切换**：标题、欢迎语、
    # 示例问题、免责声明全部换这套，不只是把答案换个语言。
    # 形如 {"lang": "zh", "title": ..., "subtitle": ..., "welcome": ...,
    #       "samples": [...], "disclaimer": ...}
    # 缺的字段由 default_copy(lang) 补齐，所以只写要改的那几项就行。
    alt: dict[str, Any] = field(default_factory=dict)
    # 「古籍问答」怎么认：文档标题里含这些词的归古籍，其余归维医。
    #
    # 库里同时躺着《黄帝内经》《本草纲目》和维吾尔医诊疗指南，混在一次检索里
    # 的结果就是"肚子疼"召回到《本草纲目》的"腹中虚冷"，再被模型当成维医方案
    # 讲给用户。分板块是最小代价的修法：不改索引、不动打分，只在检索时把
    # doc_ids 限死。
    # 留空 = 没有古籍板块（界面上只剩维医问答）。
    classic_keywords: list[str] = field(default_factory=lambda: ["古籍"])

    def host(self) -> str:
        return "0.0.0.0" if self.public else "127.0.0.1"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


_DEFAULTS_UG = {
    "title": "ئۇيغۇر تېبابىتى بىلىم سوئال-جاۋاب سىستېمىسى",
    "subtitle": "بىلىم ئامبىرىدىن ئىزدەپ، مەنبەسىنى كۆرسىتىپ جاۋاب بېرىدۇ",
    "welcome": "ئەسسالامۇ ئەلەيكۇم! ئۇيغۇر تېبابىتى بويىچە سوئالىڭىزنى يېزىڭ.",
    "samples": [
        "ئۇيغۇر تېبابىتىدىكى تۆت خىل خىلىت قايسىلار؟",
        "مىزاجنىڭ قانداق تۈرلىرى بار؟",
        "قان ئازلىقنى ئۇيغۇر تېبابىتىدە قانداق داۋالايدۇ؟",
    ],
    "disclaimer": "بۇ سىستېما بىلىم ئامبىرىدىكى ماتېرىيالغا ئاساسەن جاۋاب بېرىدۇ، "
                  "داۋالاش ئۈچۈن دوختۇرغا كۆرۈنۈڭ.",
}

_DEFAULTS_ZH = {
    "title": "维医知识问答",
    "subtitle": "基于知识库检索，回答附来源",
    "welcome": "你好！请输入关于维医的问题。",
    "samples": ["维医中的四种体液是什么？", "气质有哪些分类？"],
    "disclaimer": "本系统依据知识库内容作答，不构成医疗建议。",
}


def default_copy(lang: str) -> dict[str, Any]:
    src = _DEFAULTS_UG if lang == "ug" else _DEFAULTS_ZH
    return {k: (list(v) if isinstance(v, list) else v) for k, v in src.items()}


def _load_raw() -> list[dict[str, Any]]:
    if not INSTANCES_CONFIG.exists():
        return []
    try:
        data = yaml.safe_load(INSTANCES_CONFIG.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return []
    items = data.get("instances") if isinstance(data, dict) else data
    return [i for i in (items or []) if isinstance(i, dict)]


def load_all() -> list[InstanceSpec]:
    out: list[InstanceSpec] = []
    for raw in _load_raw():
        try:
            out.append(_coerce(raw))
        except ValueError:
            continue  # 手写坏的条目直接跳过，不能因为一行配置文件让整个后台起不来
    return out


def _coerce(raw: dict[str, Any]) -> InstanceSpec:
    name = str(raw.get("name") or "").strip().lower()
    if not NAME_RE.match(name):
        raise ValueError(f"实例名不合法：{name!r}")
    kb_id = int(raw.get("kb_id") or 0)
    if kb_id <= 0:
        raise ValueError(f"实例 {name} 缺少 kb_id")
    port = int(raw.get("port") or 0)
    if not (PORT_MIN <= port <= PORT_MAX):
        # 端口不在给前台预留的区间里就丢弃：宁可少一个实例，也不要它去抢
        # 后台自己的 8000 或 vLLM 的 8100。
        raise ValueError(f"实例 {name} 端口 {port} 不在 {PORT_MIN}-{PORT_MAX}")
    lang = str(raw.get("lang") or "ug").strip().lower()
    if lang not in LANGS:
        lang = "ug"
    mode = str(raw.get("answer_mode") or "translate").strip().lower()
    if mode not in ANSWER_MODES:
        mode = "translate"
    kb_lang = str(raw.get("kb_lang") or "zh").strip().lower()
    if kb_lang not in LANGS:
        # 语料是中文还是维吾尔语，决定了要不要走翻译检索。猜错方向比不猜更糟，
        # 认不出来就按中文（这一代语料的事实），用户可在界面上改。
        kb_lang = "zh"
    samples = raw.get("samples") or []
    if isinstance(samples, str):
        samples = [s for s in samples.split("\n") if s.strip()]
    return InstanceSpec(
        name=name,
        kb_id=kb_id,
        port=port,
        lang=lang,
        kb_lang=kb_lang,
        answer_mode=mode,
        provider=str(raw.get("provider") or "").strip(),
        roles=coerce_roles(raw.get("roles")),
        title=str(raw.get("title") or "").strip(),
        subtitle=str(raw.get("subtitle") or "").strip(),
        welcome=str(raw.get("welcome") or "").strip(),
        samples=[str(s).strip() for s in samples if str(s).strip()][:8],
        disclaimer=str(raw.get("disclaimer") or "").strip(),
        kb_name=str(raw.get("kb_name") or "").strip(),
        autostart=bool(raw.get("autostart")),
        public=bool(raw.get("public")),
        alt=coerce_alt(raw.get("alt")),
    )


def coerce_alt(raw: Any) -> dict[str, Any]:
    """另一套语言文案。只认 dict，别的写法一律当没配——坏条目不该让实例起不来。"""
    if not isinstance(raw, dict):
        return {}
    out: dict[str, Any] = {}
    lang = str(raw.get("lang") or "").strip().lower()
    if lang in LANGS:
        out["lang"] = lang
    for key in ("title", "subtitle", "welcome", "disclaimer", "kb_name"):
        val = str(raw.get(key) or "").strip()
        if val:
            out[key] = val
    samples = raw.get("samples")
    if isinstance(samples, str):
        samples = [s for s in samples.split("\n") if s.strip()]
    if isinstance(samples, list):
        picked = [str(s).strip() for s in samples if str(s).strip()][:8]
        if picked:
            out["samples"] = picked
    return out


def coerce_roles(raw: Any) -> dict[str, str]:
    """站点级角色分工。名字对不对不在这里判（此刻还不知道后端清单），
       解析时再按"活着没有"逐层退让；这里只保证它是个干净的 {角色: 名字}。"""
    from ..roles import ROLES

    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for role in ROLES:
        name = str(raw.get(role) or "").strip()
        if name:
            out[role] = name
    return out


def get(name: str) -> InstanceSpec | None:
    for spec in load_all():
        if spec.name == name:
            return spec
    return None


def _persist(items: list[InstanceSpec]) -> dict[str, Any]:
    INSTANCES_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    body = {"instances": [i.as_dict() for i in items]}
    INSTANCES_CONFIG.write_text(
        "# 前台问答站点实例。改完在后台「前台站点」页点「应用」即可生效。\n"
        + yaml.safe_dump(body, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return {"path": str(INSTANCES_CONFIG), "count": len(items)}


def upsert(spec: InstanceSpec) -> dict[str, Any]:
    items = [i for i in load_all() if i.name != spec.name]
    items.append(spec)
    items.sort(key=lambda i: i.port)
    return _persist(items)


def remove(name: str) -> dict[str, Any]:
    items = [i for i in load_all() if i.name != name]
    if len(items) == len(load_all()):
        raise ValueError("实例不存在")
    return _persist(items)


def next_free_port(used: set[int] | None = None) -> int:
    """在预留区间里挑一个没被占用、也没被别的实例用掉的端口。"""
    import socket

    used = used or set()
    taken = {i.port for i in load_all()} | used
    for port in range(PORT_MIN, PORT_MAX + 1):
        if port in taken:
            continue
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.15)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                continue  # 有人占着（可能是别的应用）
        return port
    raise ValueError(f"{PORT_MIN}-{PORT_MAX} 之间没有空闲端口了")


def validate(spec: InstanceSpec, *, kb_exists) -> None:
    if not NAME_RE.match(spec.name):
        raise ValueError("实例名只能用小写字母、数字、- 和 _，2-24 个字符")
    if not (PORT_MIN <= spec.port <= PORT_MAX):
        raise ValueError(f"端口必须在 {PORT_MIN}-{PORT_MAX} 之间（避开后台 8000 与 vLLM 8100）")
    if spec.lang not in LANGS:
        raise ValueError(f"语言只能是 {LANGS}")
    if not kb_exists(spec.kb_id):
        raise ValueError(f"知识库 #{spec.kb_id} 不存在")
