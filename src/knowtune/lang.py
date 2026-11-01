"""语言识别与跨语言问答（2026-10-28）。

为什么需要这一层：知识库里的维医文献是**中文**的，而前台是维吾尔语问答。
两件事因此变得不一样：

1. **检索**：嵌入模型是 `bge-small-zh-v1.5`——中文专用。维吾尔语的问题直接
   拿去编码，得到的向量和中文 chunk 不在同一个语义空间里，召回基本靠运气；
   BM25 那一路更直接，中文分词器切不开阿拉伯字母。所以维语提问必须先把问题
   翻译成知识库的语言再去检索，这一步省不掉，省了就是"检索不到还不知道为什么"。

2. **生成**：资料是中文、要求用维吾尔语答。系统提示必须换成维语版本，否则
   模型会照着中文资料直接吐中文——它对"最后一句用维语"的服从度远不如
   "整段指令都用维语写"。

翻译失败不阻断：降级成用原文检索，宁可召回差也别报错卡住用户。
"""

from __future__ import annotations

import re
from typing import Any

from . import promptcfg

# ---------------------------------------------------------------- 字符集

# 维吾尔语特有字母：这些字符阿拉伯语/波斯语基本不用，出现即高置信判为维语。
# ې ۆ ۈ ۋ ڭ ۇ 是乌兹别克/哈萨克也用的几个，但本系统里足够区分"维语 vs 中文"。
_UG_SPECIAL = set("ېۆۈۋڭۇغچپژئ")
_ARABIC = re.compile(r"[\u0600-\u06ff\u0750-\u077f\ufb50-\ufdff\ufe70-\ufeef]")
_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_LATIN = re.compile(r"[A-Za-z]")


def detect(text: str) -> str:
    """判断一段文字的主导语言，返回 `ug` / `zh` / `en` / `other`。

    不看"有没有"，看"占比多少"：一句中文里夹一个阿拉伯字母不该判成维语。
    """
    s = text or ""
    if not s.strip():
        return "other"
    letters = [c for c in s if not c.isspace() and not c.isdigit()]
    if not letters:
        return "other"
    n = len(letters)
    ug = sum(1 for c in letters if c in _UG_SPECIAL)
    arabic = sum(1 for c in letters if _ARABIC.match(c))
    cjk = sum(1 for c in letters if _CJK.match(c))
    latin = sum(1 for c in letters if _LATIN.match(c))

    # 维语判定要同时满足两条：有维语特有字母，且整句是阿拉伯字母为主。
    # 只看特有字母的话，一句中文里引用一个字母也会误判成维语。
    if ug and ug / n >= 0.06 and arabic / n >= 0.3:
        return "ug"
    if cjk / n >= 0.15:
        return "zh"
    if arabic / n >= 0.3:
        return "ug"  # 纯阿拉伯字母但缺特有字符（用户可能漏打），按维语处理
    if latin / n >= 0.5:
        return "en"
    return "other"


# ---------------------------------------------------------------- 提示与文案

# 生成阶段的"回答语言"指令。放在系统提示之后单独追加，而不是改提示文件本身：
# 提示文件是版本化的（qa_v1 / qa_ug），语言指令是运行期的实例配置，两者不该耦合。
_ANSWER_LANG_INSTRUCTION = {
    "ug": (
        "\n\n【تىل تەلىپى】\n"
        "ماتېرىيال خەنزۇ تىلىدا بولۇشى مۇمكىن، لېكىن جاۋابنى چوقۇم "
        "ئۇيغۇر تىلىدا (ئەرەب يېزىقىدا) يېزىڭ. ئۇيغۇرچە ئاتالغۇلارنى "
        "ئىمكانقەدەر ساقلاڭ؛ مەنبە نومۇرى [1]، [2] شەكلىدە ئۆزگەرمەيدۇ."
    ),
    "zh": "\n\n【语言要求】\n资料可能是维吾尔语或其他语言，但回答必须用中文。引用编号保持 [1]、[2] 形式。",
    "en": "\n\n【Language】\nAnswer in English. Keep citation ids as [1], [2].",
}

REFUSE_TEMPLATES = {
    "ug": (
        "نۆۋەتتىكى بىلىم ئامبىرىدا بۇ سوئالغا جاۋاب بېرىدىغان يېتەرلىك ئاساس تېپىلمىدى.\n\n"
        "ئېھتىمالىق سەۋەبلەر:\n"
        "1. مۇناسىۋەتلىك بىلىم تېخى بۇ بىلىم ئامبىرىغا كىرگۈزۈلمىگەن؛\n"
        "2. سوئالنىڭ ئىپادىلىنىشى بىلەن ھۆججەتتىكى سۆزلەرنىڭ پەرقى چوڭ "
        "(باشقىچە ئىپادىلەپ سىناپ بېقىڭ)؛\n"
        "3. تېپىلغان مەزمۇنلارنىڭ مۇناسىۋەتلىكلىكى يېتەرلىك ئەمەس."
    ),
}

# 拒答也要能定位：前台把 refusal 原因展示给用户，否则"没找到"和"系统坏了"
# 在界面上长得一模一样。
REFUSE_REASON_TEXT = {
    "ug": {
        "no_hit": "بىلىم ئامبىرىدا مۇناسىۋەتلىك مەزمۇن تېپىلمىدى",
        "low_score": "تېپىلغان مەزمۇنلارنىڭ مۇناسىۋەتلىكلىكى يېتەرلىك ئەمەس",
    },
}


def answer_instruction(lang: str) -> str:
    return _ANSWER_LANG_INSTRUCTION.get(lang, "")


def repeating(text: str) -> bool:
    """文本是否滑进了复读：结尾是同一段话连着重复三遍以上。

    本地小模型输出弱语言（维吾尔语）时最常见的失败模式就是这个——它会一直
    复述自己刚写的一半，直到撞上 max_tokens。检测它比检测"语法对错"容易得多，
    而且足够用来决定要不要截断。

    旧实现是在尾部任意位置数某个片段出现几次（`tail.count(seg) >= 3`）。
    维语的连接词与短语在正常行文里本来就反复出现，于是"资料中是……；资料中
    是……；资料中是……"这种正常排比被判成复读，**从中间砍掉**，用户看到一句
    没说完的话。现在只认"紧挨着的三段完全一样"，那是复读，不是修辞。
    """
    tail = text[-600:]
    n = len(tail)
    for w in range(20, min(240, n // 3) + 1):
        seg = tail[-w:]
        if tail.endswith(seg * 3):
            return True
    return False


def refuse_template(lang: str, default: str) -> str:
    return REFUSE_TEMPLATES.get(lang, default)


# ---------------------------------------------------------------- 跨语言检索

_SRC_NAME = {"ug": "维吾尔语", "en": "英语", "zh": "中文", "other": ""}
_DST_NAME = {"zh": "中文", "en": "English", "ug": "维吾尔语"}

# 本地 7B 模型时不时会把整个提示连同"问题："一起吐回来。这种回显一旦被当成
# 检索词，等于拿一段指令去查库——召回全是噪声，而界面上看不出任何异常。
# 宁可判它失败、退回原文检索。
_ECHO_MARKERS = ("源语言", "问题：", "译文", "翻译", "维吾尔语", "源语言代码",
                 "Translate", "Question:", "翻译结果", "以下是")


def _clean_translation(raw: str, question: str) -> tuple[str, str]:
    """从模型输出里抠出真正的译文。返回 `(译文, 丢弃原因)`，二者必有其一为空。

    这里每一种丢弃都是"宁可退回原文检索也不要拿指令去查库"，但它们以前的
    返回值都是空串，调用方一律当成 `error=None`——于是"翻译被丢了"和"本来就
    不需要翻译"在界面上长得一模一样。2026-10-29 排查"问题为什么没翻译"时，
    整整三轮都卡在这一点上：模型明明正常返回了译文，代码却静默丢弃，
    而 query 事件里 `translated:false`、`error:null`，看起来像压根没调模型。
    """
    text = (raw or "").strip()
    if not text:
        return "", "模型返回空"
    # 只取第一行：翻译请求永远只要一行，多行基本就是模型在发挥
    for line in text.splitlines():
        line = line.strip().strip("\"'“”‘’ ")
        if line:
            text = line
            break
    text = re.sub(r"^(译文|中文译文|翻译结果|翻译|中文|维吾尔语|英语|English)\s*[:：]\s*",
                  "", text)
    text = text.strip().strip("\"'“”‘’ ")
    if not text:
        return "", "去掉语言前缀后为空"
    hit = [m for m in _ECHO_MARKERS if m in text]
    if hit:
        return "", f"疑似回显提示词（含「{hit[0]}」）"
    if text == question.strip():
        return "", "模型原样抄回问题"
    if len(text) > max(120, len(question) * 3):
        return "", f"译文过长（{len(text)} 字，原文 {len(question)} 字）"
    return text, ""


def translate_query(
    llm: Any,
    question: str,
    *,
    src: str,
    dst: str = "zh",
    provider: str | None = None,
    attempts: int = 2,
) -> tuple[str, str | None]:
    """把问题翻译成知识库的语言，用于检索。返回 `(译文, 错误信息)`。

    失败时返回原文而不是抛异常：召回变差还能用，抛异常就整个问答都废了。
    调用方只拿译文去检索，生成阶段仍然用**原始问题**，这样模型看到的是用户
    真正的提问，而不是翻译腔。

    `attempts=2`：模型偶尔会把问题原样抄回来（短问题、夹着专名时更容易），
    此时 `_clean_translation` 会判废。只试一次的话，这一次问答就退回维语原文去
    查中文库——召回等于随机，而用户看到的只是"答案不相关"，根本想不到是翻译
    掉了。第二次换成更硬的约束（禁止输出任何维吾尔字母）通常就能过。
    第二次仍失败才退回原文，并把原因带出去。
    """
    if src == dst or not _SRC_NAME.get(src):
        return question, None
    src_name = _SRC_NAME.get(src, src)
    dst_name = _DST_NAME.get(dst, dst)
    # 提示词本身可配（后台「模型 → 提示词 → 翻译」），这里只负责把语言名和原句
    # 填进模板。用 replace 而不是 str.format：用户模板里可能带 { } 字面量
    # （JSON 例子、模板语法），format 会当场 KeyError 而用户看不出错在哪。
    sys_text, user_tpl = promptcfg.translate_messages(provider)
    prompt = promptcfg.fill(user_tpl, src=src_name, dst=dst_name, question=question)
    # 第二次不再给例子：抄回的模型往往是被 few-shot 的最后一行"带跑"的，
    # 换成"只准出现目标语言字符"的硬约束更有效。
    #
    # 这一条**故意不开放配置**：它是提示词失效后的降级路径，用户改不到才不会被
    # 一起改坏。可配的只有上面那份正常路径。
    retry_prompt = (
        f"把下面这句{src_name}翻译成{dst_name}。只输出{dst_name}译文，"
        f"一个字都不要保留原文，也不要解释。\n\n{question}"
    )
    sys_msg = {"role": "system", "content": sys_text}
    sys_retry = {
        "role": "system",
        "content": f"你是翻译器。只输出{dst_name}译文，不要重复原文，"
                   f"不要解释，不要加引号。",
    }

    last_why = ""
    # 预算要按"思考 + 译文"给，不能按"译文"给。deepseek-flash 这类推理模型
    # 会先花一大截 token 内部推理（实测一次短问题 784 reasoning tokens，168 字
    # 的长段落甚至 2197），原来 200 的上限在思考阶段就烧完了，content 直接是
    # 空串——而且不报错、不抛异常。表现就是"翻译静默失败、拿原文去查中文库"。
    budget = min(8192, max(2048, len(question) * 12))
    for i in range(max(1, attempts)):
        use_prompt = prompt if i == 0 else retry_prompt
        use_sys = sys_msg if i == 0 else sys_retry
        try:
            out = llm.complete(
                [use_sys, {"role": "user", "content": use_prompt}],
                provider=provider,
                temperature=0.0,
                max_tokens=budget,
                # 翻译是确定性任务，不需要推理。不关的话推理模型会先烧掉
                # 几百到两千个 token 才动笔，慢且容易把预算吃光。
                reasoning="none",
            )
        except Exception as exc:  # 翻译是加分项，不是必经环节
            return question, f"{type(exc).__name__}: {exc}"

        text, why = _clean_translation(getattr(out, "text", None) or "", question)
        if text:
            return text, None
        last_why = why

    # 把丢弃原因原样带出去：调用方会塞进 SSE 的 query 事件，前端和日志
    # 都能直接看到"翻译被判废了、因为什么"，不用再靠猜。
    return question, f"translation_dropped: {last_why}（已试 {attempts} 次）"
