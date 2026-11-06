"""提示词配置层（2026-10-30）。

原先两处硬编码，各自都有毛病：

  generate  —— 提示词在 `config/prompts/qa_*.md`，按 `prompt_version` 选文件，
               还要按回答语言切到 `qa_ug.md`。想调一句话就得改文件，
               改完没有留痕，改坏了只能靠回忆。
  translate —— 提示词直接写在 `lang.py` 的字符串里。调它要改代码。

但也不能干脆把提示词搬进数据库：`qa_v1` / `qa_ug` 是**版本化资产**，按语言选
文件这件事本身是有价值的（见 prompt.py 里那段注释），搬走就等于把版本切换弄丢。

所以这里只做「叠加」，不做「搬家」：

    内置默认（md 文件 / 代码常量）
      ↑ 被覆盖
    角色级覆盖（prompts.yaml: prompts.<role>.default.<field>）
      ↑ 被覆盖
    后端级覆盖（prompts.yaml: prompts.<role>.by_provider.<name>.<field>）

后端级在最上层，是因为同一个提示词喂给 7B 本地模型和云端强模型，效果差得很远：
小模型需要把约束拆成编号条款、把例子写死，强模型一句话就懂。两套提示词共存
才是正常状态，逼它们共用一份等于永远在为一个模型调参、让另一个模型跑偏。

三条不变式：
  1. 内置默认永远可恢复。`config/prompts/*.md` 和本文件的常量只读，后台只写
     `config/prompts.yaml`；「恢复默认」= 删掉覆盖项，不依赖记得原文。
  2. 解析永不抛异常。任何一层坏掉（YAML 写坏、字段类型不对、后端名不存在）
     都往下退一层，最差退到内置兜底——问答链路不能因为一个配置字写错就 500。
  3. 每次保存前留快照。提示词改坏是**静默**的：模型照样出字，只是不再标引用、
     不再拒答。没有快照，出问题时连"改之前长什么样"都说不出来。
"""

from __future__ import annotations

import copy
import os
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

import yaml

from .config import ROOT

PROMPT_DIR = ROOT / "config" / "prompts"
OVERLAY_PATH = ROOT / "config" / "prompts.yaml"
HISTORY_DIR = PROMPT_DIR / "_history"

# 一份提示词 2 万字还没写完，通常是贴错了东西（整篇文档、整个日志）。
MAX_CHARS = 20000
KEEP_HISTORY = 30


# --------------------------------------------------------------------- 角色与字段

ROLE_FIELDS: dict[str, tuple[str, ...]] = {
    "generate": ("system",),
    "translate": ("system", "user"),
    # 界面文案（2026-11-06 从站点配置里搬过来）。以前这五句话写在
    # config/instances.yaml 的 title/subtitle/welcome/samples/disclaimer 里，
    # 代码里还各自带一份默认值——改一句话要开站点弹窗，改完没有留痕，
    # 改坏了连"上一版写的什么"都说不出来。搬到这里之后，它们和提示词共用
    # 同一套能力：内置默认、覆盖、保存前快照、整体回滚。
    #
    # 按语言拆成两个角色而不是一个角色十个字段：界面一次只显示一套语言的
    # 五句话，两组挤在一起必然有一个是当前页面的"另一半"，编辑时容易串行。
    # 站点按自己的语言取对应的那个角色。
    "copy_ug": ("title", "subtitle", "welcome", "samples", "disclaimer"),
    "copy_zh": ("title", "subtitle", "welcome", "samples", "disclaimer"),
}

ROLE_LABELS: dict[str, str] = {
    "generate": "生成 · 读检索资料组织答案",
    "translate": "翻译 · 跨语言检索与成稿回译",
    "copy_ug": "界面文案 · 维吾尔语",
    "copy_zh": "界面文案 · 中文",
}

# 只对 generate / translate 有意义的"按后端分"。界面文案跟模型无关：
# 同一个站点换后端，标题不会跟着变。这类角色读写都强制走角色默认层，
# 免得从某张模型卡片进来保存时被写进 by_provider，站点按默认层读不到，
# 表现成"保存成功、页面不变"。
PER_PROVIDER_ROLES = frozenset({"generate", "translate"})

_COPY_ROLE_PREFIX = "copy_"

_COPY_FIELD_META: dict[str, dict[str, Any]] = {
    "title": {"label": "标题", "hint": "浏览器标签页与页面顶部的大标题。"},
    "subtitle": {"label": "副标题", "hint": "标题下面那行说明，一句话讲清这个站点做什么。"},
    "welcome": {"label": "欢迎语", "hint": "对话区空白时的第一句话。"},
    "samples": {"label": "示例问题", "hint": "一行一个，最多 8 条；点一下就会提这个问题。"},
    "disclaimer": {"label": "免责声明", "hint": "页面底部的固定说明，如医疗建议边界。"},
}

FIELD_META: dict[tuple[str, str], dict[str, Any]] = {
    ("generate", "system"): {
        "label": "系统提示（system）",
        "hint": "决定模型怎么使用资料、怎么标注引用编号、资料不足时怎么拒答。"
                "改这里等于改系统的行为契约——下面三条是引用校验与拒答机制赖以生效的前提。",
        "placeholders": (),
    },
    ("translate", "system"): {
        "label": "系统提示（system）",
        "hint": "翻译模型的角色设定。这里的约束越硬，模型越少自作主张加解释。",
        "placeholders": (),
    },
    ("translate", "user"): {
        "label": "用户消息模板（user）",
        "hint": "每次翻译实际发出去的内容。必须保留 {question}，否则模型收不到要翻的句子。",
        "placeholders": ("src", "dst", "question"),
    },
}

PLACEHOLDER_LABELS: dict[str, str] = {
    "src": "源语言名（如「维吾尔语」）",
    "dst": "目标语言名（如「中文」）",
    "question": "要翻译的原句",
}

# 两个文案角色的字段说明完全一样（都是标题/副标题/欢迎语/示例/免责），
# 逐条复制十遍只会让以后加字段时漏掉一半。
for _copy_role in ("copy_ug", "copy_zh"):
    for _copy_field, _copy_meta in _COPY_FIELD_META.items():
        FIELD_META[(_copy_role, _copy_field)] = _copy_meta

# generate 的兜底。与 config/prompts/*.md 失败时的最后一层，和 REST 的默认值同源。
SYSTEM_PROMPT_FALLBACK = """你是一个严格基于给定资料回答问题的助手。

硬性要求：
1. 只能使用下方【资料】中的内容作答，禁止使用任何外部知识或常识补充。
2. 每一个事实性陈述后面必须紧跟来源编号，格式为 [1]、[2]，可多标如 [1][3]。
3. 如果资料不足以回答，直接说明"资料中未找到相关依据"，禁止猜测或编造。
4. 不要复述资料原文的长段落，用简洁的语言组织答案。
5. 引用编号只能使用资料中出现过的编号，不得使用不存在的编号。

输出使用中文，结构化但不冗长。"""

# translate 的内置默认，逐字搬自 lang.py，行为与改造前一致。
BUILTIN_TRANSLATE_SYSTEM = "你是翻译器。用户输入一句话，你只输出它的译文，不输出任何其他内容。"

# 给两个例子再让它续写：本地小模型照着"把 A 翻成 B"这种抽象指令做，十次有八次
# 会自己发挥；给成对的样例它才知道要输出什么形态。结尾停在目标语言冒号上，
# 模型会接着写译文而不是先复述一遍要求。
BUILTIN_TRANSLATE_USER = (
    "把{src}翻译成{dst}，只输出译文。\n\n"
    "{src}：مىزاج دېگەن نېمە؟\n{dst}：气质是什么？\n\n"
    "{src}：قان ئازلىقنى قانداق داۋالايدۇ؟\n{dst}：贫血怎么治疗？\n\n"
    "{src}：{question}\n{dst}："
)


# --------------------------------------------------------------------- 文件读写


_cache: dict[str, Any] = {"key": None, "data": {}}


def _empty() -> dict[str, Any]:
    return {"prompts": {}}


def _fingerprint() -> tuple:
    try:
        st = OVERLAY_PATH.stat()
    except OSError:
        # 「文件不存在」必须是一个**能区分于初始值**的指纹。早先这里返回 None，
        # 而初始 _cache["key"] 也是 None，于是删掉覆盖文件后 read() 判定"命中缓存"，
        # 把删除前的旧内容又读了出来——表现是「恢复默认成功，但提示词没变」。
        return ("missing",)
    return (st.st_mtime_ns, st.st_size)


def read() -> dict[str, Any]:
    """读覆盖文件。带 mtime+size 缓存：问答链路每次请求都会走这里，
    而 YAML 解析比读 md 文件贵得多。改了文件 mtime 就变，缓存自动失效。"""
    fp = _fingerprint()
    if _cache["key"] == fp:
        return _cache["data"]
    data = _empty()
    if fp != ("missing",):
        try:
            raw = yaml.safe_load(OVERLAY_PATH.read_text(encoding="utf-8")) or {}
            if isinstance(raw, dict) and isinstance(raw.get("prompts"), dict):
                data = {"prompts": raw["prompts"]}
        except (OSError, yaml.YAMLError):
            # 文件被手改坏了：当成没有覆盖，让系统跑内置默认。坏文件留着不动，
            # 用户下次保存时会被备份走，不至于把他的编辑直接抹掉。
            data = _empty()
    _cache["key"] = fp
    _cache["data"] = data
    return data


def _write(data: dict[str, Any]) -> None:
    """原子写。写到一半被读走，读到的是空文件——问答链路正好在跑，就吃一次兜底。"""
    body = data.get("prompts") or {}
    if not body:
        if OVERLAY_PATH.exists():
            OVERLAY_PATH.unlink()
        _cache["key"] = None
        return
    header = (
        "# 提示词覆盖层。由后台「模型 → 提示词」维护，也可手改。\n"
        "# 只记录与内置默认不同的字段：删掉某一行 = 该项回到内置默认。\n"
        "# 内置默认在 config/prompts/*.md（generate）和 knowtune/promptcfg.py（translate），\n"
        "# 本文件永远不覆盖它们，因此「恢复默认」是安全的。\n"
        "# 解析顺序：by_provider.<后端> → default → 内置 → 兜底。\n\n"
    )
    OVERLAY_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = OVERLAY_PATH.with_suffix(".yaml.tmp")
    # 必须整份 dump：read() 认的是顶层 prompts 键，只 dump 内层会让文件写完
    # 立刻读不回来——表现为「保存成功，但取出来还是默认值」。
    tmp.write_text(
        header + yaml.safe_dump({"prompts": body}, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    os.replace(tmp, OVERLAY_PATH)
    # 这里必须置空而不是写成当前指纹：_cache["data"] 还是写之前那份，
    # 指纹一对上就会命中陈旧缓存，表现为「保存成功但读出来还是旧的」。
    _cache["key"] = None


def _snapshot(reason: str) -> str | None:
    """保存前把整份覆盖文件存一份。返回快照文件名，没有旧文件就不存。"""
    if not OVERLAY_PATH.exists():
        return None
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    safe = re.sub(r"[^0-9A-Za-z_\-]", "_", reason)[:40] or "save"
    name = f"{stamp}__{safe}.yaml"
    target = HISTORY_DIR / name
    # 同一秒内连按两次保存会撞名，加个序号，别让后一次把前一次的快照盖掉
    n = 1
    while target.exists():
        target = HISTORY_DIR / f"{stamp}-{n}__{safe}.yaml"
        n += 1
    shutil.copy2(OVERLAY_PATH, target)
    _trim_history()
    return target.name


def _trim_history() -> None:
    files = sorted(HISTORY_DIR.glob("*.yaml"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in files[KEEP_HISTORY:]:
        try:
            old.unlink()
        except OSError:
            pass


# --------------------------------------------------------------------- 内置默认


def _generate_default(version: str, answer_lang: str | None) -> str:
    """generate 的内置默认：按回答语言优先，再按版本号，最后兜底。

    语言优先是 prompt.py 原本就有的规则——提示文件是版本化资产，而"这个站点回答
    什么语言"是运行期配置，两者不能耦合在一起，否则改个站点语言就得动提示版本，
    回滚时也会被一起卷进去。
    """
    if answer_lang and version == "qa_v1":
        alt = PROMPT_DIR / f"qa_{answer_lang}.md"
        if alt.exists():
            version = f"qa_{answer_lang}"
    path = PROMPT_DIR / f"{version}.md"
    if path.exists():
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError:
            text = ""
        if text:
            return text
    return SYSTEM_PROMPT_FALLBACK


def builtin_fields(role: str, *, version: str = "qa_v1",
                   answer_lang: str | None = None) -> dict[str, str]:
    if role == "generate":
        return {"system": _generate_default(version, answer_lang)}
    if role == "translate":
        return {"system": BUILTIN_TRANSLATE_SYSTEM, "user": BUILTIN_TRANSLATE_USER}
    if role.startswith(_COPY_ROLE_PREFIX):
        # 语言取自角色名（copy_ug / copy_zh）。默认文案仍放在 instcfg——那是
        # 站点侧的出厂值，挪进这里只会多一份要同步的副本；此处只是**引用**它，
        # 写成文本是为了跟其它角色的字段形态一致（samples 一行一个）。
        lang = role[len(_COPY_ROLE_PREFIX):]
        try:
            from .frontsite.instcfg import default_copy

            base = default_copy(lang)
        except Exception:
            base = {}
        out: dict[str, str] = {}
        for field in ROLE_FIELDS[role]:
            val = base.get(field, "")
            if isinstance(val, list):
                val = "\n".join(str(x) for x in val)
            out[field] = str(val or "")
        return out
    return {}


# --------------------------------------------------------------------- 解析


def effective_fields(role: str, provider: str = "", *, version: str = "qa_v1",
                     answer_lang: str | None = None) -> dict[str, dict[str, Any]]:
    """每个字段最终用哪份、从哪来。UI 要能一眼看出「这行是改过的还是内置的」。"""
    builtin = builtin_fields(role, version=version, answer_lang=answer_lang)
    data = read()
    node = (data.get("prompts") or {}).get(role) if isinstance(data.get("prompts"), dict) else None
    node = node if isinstance(node, dict) else {}
    by = node.get("by_provider") if isinstance(node.get("by_provider"), dict) else {}
    if role not in PER_PROVIDER_ROLES:
        # 文案与模型无关：即使历史上有人从某张模型卡片保存过，也不该让
        # "换个后端文案就变"发生。这一层直接不读。
        by = {}
    prov_map = by.get(provider) if isinstance(by.get(provider), dict) else {}
    flat = node.get("default") if isinstance(node.get("default"), dict) else {}

    out: dict[str, dict[str, Any]] = {}
    for field in ROLE_FIELDS.get(role, ()):
        b = builtin.get(field, "")
        pv = prov_map.get(field)
        dl = flat.get(field)
        if isinstance(pv, str) and pv.strip():
            text, source = pv, "provider"
        elif isinstance(dl, str) and dl.strip():
            text, source = dl, "role"
        else:
            text, source = b, "builtin"
        out[field] = {
            "text": text,
            "source": source,
            "overridden": source != "builtin",
            "builtin": b,
            "provider_value": pv if isinstance(pv, str) else "",
            "role_value": dl if isinstance(dl, str) else "",
        }
    return out


def system_prompt(version: str = "qa_v1", answer_lang: str | None = None,
                  provider: str = "") -> str:
    """generate 的 system 提示（不含语言后缀与证据覆盖，那两段由 prompt.py 追加）。"""
    got = effective_fields("generate", provider, version=version,
                           answer_lang=answer_lang).get("system")
    return (got or {}).get("text") or SYSTEM_PROMPT_FALLBACK


def translate_messages(provider: str = "") -> tuple[str, str]:
    """translate 的 (system, user 模板)。user 模板里的占位符由 lang.py 填。"""
    got = effective_fields("translate", provider)
    return (got["system"]["text"], got["user"]["text"])


def copy_texts(lang: str) -> dict[str, Any]:
    """界面文案：该语言下**配过**的字段，samples 已切成列表。

    只返回非空项。站点拿到之后按"提示词 > 站点实例 > 内置默认"的顺序合并：
    没配的字段不该用空串把实例里已有的值盖掉，否则搬完配置的第一天，
    站点上就会有一半文字突然变成出厂默认。
    """
    role = f"{_COPY_ROLE_PREFIX}{lang}"
    if role not in ROLE_FIELDS:
        return {}
    out: dict[str, Any] = {}
    for key, info in effective_fields(role).items():
        # 只取真正被覆盖过的字段。内置默认不在这里返回：站点自己的实例配置
        # 可能写过一句话，用出厂默认把它顶掉等于"搬配置搬丢了自定义内容"。
        if info.get("source") == "builtin":
            continue
        text = str(info.get("text") or "").strip()
        if not text:
            continue
        if key == "samples":
            picked = [line.strip() for line in text.splitlines() if line.strip()][:8]
            if picked:
                out[key] = picked
        else:
            out[key] = text
    return out


def fill(template: str, **values: str) -> str:
    """填占位符。用 replace 而不是 str.format：提示词里常常带 {、}（JSON 例子、
    模板语法），format 会当场 KeyError，而用户并不知道自己写错的是花括号。"""
    out = template
    for key, val in values.items():
        out = out.replace("{" + key + "}", str(val))
    return out


# --------------------------------------------------------------------- 体检


_REF_RULE = re.compile(r"\[\s*\d\s*\]|编号|引用")
_NO_OUTSIDE = re.compile(r"禁止|不得|不要|只能|仅|不允许")


def check(role: str, fields: Mapping[str, str], *,
          provider: str = "") -> list[dict[str, str]]:
    """保存前体检。返回 error + warn 列表，error 会拒绝保存。

    为什么必须体检：提示词改坏是**静默**的。删掉"引用编号"那条，模型照样流畅
    作答，只是全篇没有 [1]——`cite.verify` 的覆盖率掉到 0，界面上却看不出是
    提示词的问题，只会以为"今天模型状态不好"。这种故障最难查，所以在写入时拦。
    """
    issues: list[dict[str, str]] = []

    for field, text in fields.items():
        if not isinstance(text, str):
            continue
        if len(text) > MAX_CHARS:
            issues.append({
                "level": "error", "field": field,
                "text": f"超过 {MAX_CHARS} 字符上限（当前 {len(text)}）。"
                        f"这通常意味着贴错了内容——整篇文档或日志不该出现在这里。",
            })

    if role == "translate":
        user = fields.get("user", "")
        if user.strip() and "{question}" not in user:
            issues.append({
                "level": "error", "field": "user",
                "text": "用户消息模板里没有 {question}。模型收不到要翻译的原句，"
                        "翻译会直接退化成「照抄模板」——这条必须补上。",
            })
        if user.strip() and not any(p in user for p in ("{src}", "{dst}")):
            issues.append({
                "level": "warn", "field": "user",
                "text": "模板里没有 {src} / {dst}。不写语言名也能跑，但模型不知道"
                        "要翻成什么语言，跨语言检索会时好时坏。",
            })

    if role == "generate":
        sys_text = fields.get("system", "")
        if sys_text.strip():
            if not _REF_RULE.search(sys_text):
                issues.append({
                    "level": "warn", "field": "system",
                    "text": "没提到来源编号（[1] 这类）。删掉这条后模型不会再标引用，"
                            "引用覆盖率会归零，而回答看上去一切正常——这是最难查的一种退化。",
                })
            if not _NO_OUTSIDE.search(sys_text):
                issues.append({
                    "level": "warn", "field": "system",
                    "text": "没提到「禁止外部知识」之类的约束。拒答机制依赖这条："
                            "没有它，资料不足时模型会用自己的知识补完，拒答形同虚设。",
                })

    if provider:
        known = _known_providers()
        if known and provider not in known:
            issues.append({
                "level": "warn", "field": "",
                "text": f"后端「{provider}」当前不在注册表里。覆盖会存下来，"
                        f"但它要等这个后端重新出现才生效。",
            })
    return issues


def _known_providers() -> set[str]:
    """从 models.yaml + default.yaml 里读后端名，仅用于体检提示，不用于鉴权。"""
    names: set[str] = set()
    try:
        from .config import defaults_config

        names |= set(defaults_config().generation.providers)
    except Exception:
        pass
    try:
        raw = yaml.safe_load((ROOT / "config" / "models.yaml").read_text(encoding="utf-8")) or {}
        if isinstance(raw.get("providers"), dict):
            names |= set(raw["providers"])
    except Exception:
        pass
    return names


# --------------------------------------------------------------------- 增删改


def save(role: str, provider: str, fields: Mapping[str, str], *,
         version: str = "qa_v1", answer_lang: str | None = None) -> dict[str, Any]:
    """保存覆盖。字段为空字符串 = 删掉该字段的覆盖，回到上一层。

    故意不提供「只改一个字段」的接口：UI 一次提交整组字段，服务端整体替换这一层，
    不会出现"改了 system 但 user 还是三天前的"这种半新半旧状态。
    """
    if role not in ROLE_FIELDS:
        raise ValueError(f"未知角色：{role}")
    provider = (provider or "").strip()
    if role not in PER_PROVIDER_ROLES:
        # 文案只有"角色默认"这一层。不在这里清掉的话，从模型卡片进来保存
        # 会写成 by_provider.<name>，而站点永远按默认层读——保存成功、页面不变。
        provider = ""
    if provider and not re.match(r"^[a-z0-9][a-z0-9_-]{0,23}$", provider):
        raise ValueError("后端名不合法")

    clean: dict[str, str] = {}
    for key, val in (fields or {}).items():
        if key not in ROLE_FIELDS[role]:
            continue
        text = (val or "").replace("\r\n", "\n").strip("\n")
        if text.strip():
            clean[key] = text

    # 内容跟内置一模一样就等于没改。留着一条内容相同的「覆盖」，界面上会一直
    # 显示"已自定义"，而用户明明已经改回去了——这种脏状态攒多了，
    # 「恢复默认」就没人敢按，因为不知道按下去会发生什么。
    builtin = builtin_fields(role, version=version, answer_lang=answer_lang)
    clean = {k: v for k, v in clean.items()
             if v.strip() != (builtin.get(k) or "").strip()}

    issues = check(role, clean, provider=provider)
    errors = [i for i in issues if i["level"] == "error"]
    if errors:
        return {"ok": False, "errors": errors, "warnings": [], "saved": False}

    # read() 返回的是缓存里的同一个 dict，直接改等于把缓存改成"改了一半"的状态，
    # 后续读取会拿到脏数据。深拷一份再改。
    data = copy.deepcopy(read())
    prompts = data.setdefault("prompts", {})
    node = prompts.setdefault(role, {})
    if not isinstance(node, dict):
        node = prompts[role] = {}

    if provider:
        by = node.get("by_provider")
        if not isinstance(by, dict):
            by = node["by_provider"] = {}
        if clean:
            by[provider] = clean
        else:
            by.pop(provider, None)
        if not by:
            node.pop("by_provider", None)
    else:
        if clean:
            node["default"] = clean
        else:
            node.pop("default", None)

    if not node:
        prompts.pop(role, None)
    if not prompts:
        data["prompts"] = {}

    snap = _snapshot(f"{role}-{provider or 'default'}")
    _write(data)
    return {
        "ok": True,
        "saved": bool(clean),
        "cleared": not clean,
        "errors": [],
        "warnings": [i for i in issues if i["level"] == "warn"],
        "snapshot": snap,
        "path": str(OVERLAY_PATH),
        "fields": effective_fields(role, provider, version=version, answer_lang=answer_lang),
    }


def reset(role: str, provider: str = "", *, version: str = "qa_v1",
          answer_lang: str | None = None) -> dict[str, Any]:
    """恢复默认 = 删覆盖项。内置文件一个字都不动，所以这一步永远安全。"""
    return save(role, provider, {}, version=version, answer_lang=answer_lang)


def history() -> list[dict[str, Any]]:
    if not HISTORY_DIR.exists():
        return []
    out = []
    for p in sorted(HISTORY_DIR.glob("*.yaml"), key=lambda x: x.stat().st_mtime, reverse=True):
        st = p.stat()
        stamp, _, reason = p.stem.partition("__")
        out.append({
            "name": p.name,
            "reason": reason.replace("_", " "),
            "ts": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            "size": st.st_size,
        })
    return out


def rollback(name: str) -> dict[str, Any]:
    """整体回滚到某个快照。快照是同名文件的完整副本，直接换回去最不容易出错——
    做「按字段回滚」需要三方合并，而三方合并出错时用户看到的是半份提示词。"""
    if not re.match(r"^[0-9A-Za-z_\-]+\.yaml$", name or ""):
        raise ValueError("快照名不合法")
    src = HISTORY_DIR / name
    if not src.exists():
        raise ValueError("快照不存在")
    _snapshot("rollback")
    shutil.copy2(src, OVERLAY_PATH)
    _cache["key"] = None
    return {"ok": True, "restored": name, "path": str(OVERLAY_PATH)}


# --------------------------------------------------------------------- 给 UI 的概览


def overview(*, version: str = "qa_v1", answer_lang: str | None = None,
             providers: list[str] | None = None,
             active: str = "") -> dict[str, Any]:
    data = read()
    roles = []
    for role, fields in ROLE_FIELDS.items():
        node = (data.get("prompts") or {}).get(role)
        node = node if isinstance(node, dict) else {}
        by = node.get("by_provider") if isinstance(node.get("by_provider"), dict) else {}
        roles.append({
            "role": role,
            "label": ROLE_LABELS.get(role, role),
            "fields": [
                {
                    "key": f,
                    "label": FIELD_META.get((role, f), {}).get("label", f),
                    "hint": FIELD_META.get((role, f), {}).get("hint", ""),
                    "placeholders": [
                        {"key": p, "label": PLACEHOLDER_LABELS.get(p, p)}
                        for p in FIELD_META.get((role, f), {}).get("placeholders", ())
                    ],
                }
                for f in fields
            ],
            "has_default_override": bool(node.get("default")),
            "overridden_providers": sorted(by.keys()),
            "per_provider": role in PER_PROVIDER_ROLES,
        })

    provs = []
    for name in (providers or []):
        provs.append({
            "name": name,
            "active": name == active,
            "overrides": {
                role: sorted((((data.get("prompts") or {}).get(role) or {})
                              .get("by_provider") or {}).get(name, {}).keys())
                for role in ROLE_FIELDS
            },
        })

    return {
        "roles": roles,
        "providers": provs,
        "active": active,
        "version": version,
        "answer_lang": answer_lang or "",
        "path": str(OVERLAY_PATH),
        "exists": OVERLAY_PATH.exists(),
        "history_count": len(history()),
        "max_chars": MAX_CHARS,
    }
