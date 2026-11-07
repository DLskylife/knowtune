"""预问诊的作答语言行为闸门。

2026-11-07 之前这里是"预问诊自己的一套语言系统"：会话记一个 lang、切语言就
重开一场问诊、翻译失败直接 503。那套东西和问答板块（`answer_lang` + 主文/原稿
两份文本 + 逐条语言标记）对不上，表现就是"界面切一下就串语言、病史还丢了"。

现在统一成和 `/api/ask` 一样的口径，这个文件守的就是这几条：

1. 作答语言 = **本次请求**的语言（不钉在会话上），每条 turn 自带 `lang`；
2. 每条答复都存两份：`content`（目标语言）+ `content_int`（中文原稿），
   界面据此在两种语言间切换，不用重开；
3. 翻译失败**不 503**：落原文 + `translated=False`，界面挂本地化提示
   （与问答板块 `answer_translated=false` 同一语义）；
4. 翻译客户端懒装配，宿主注入 / reload 之后缺了能自愈；
5. 语言码要收敛（UG / ug-UY 都算 ug），否则整段翻译会被跳过。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from previsit.chat import ChatEngine  # noqa: E402
from previsit.store import Store  # noqa: E402

ZH_ANSWER = "了解了，疼痛是持续的还是间歇性的呢？"
UG_ANSWER = "چۈشەندىم، ئاغرىق ئۈزلۈكسىز بولىدۇ ياكى ئارىلاپ بولىدۇ؟"


class FakeLLM:
    """只会说中文的"本地 7B"。"""

    def __init__(self, text: str = ZH_ANSWER) -> None:
        self.text = text
        self.calls = 0

    def complete(self, msgs, *, temperature=0.2, max_tokens=320, **_):
        self.calls += 1
        return self.text


class FakeTranslator:
    """按 direction 决定交什么货，并记下每一次被要求翻译的原文。"""

    def __init__(self, zh2ug: str = UG_ANSWER, ug2zh: str = "肚子疼",
                 boom: bool = False) -> None:
        self.zh2ug, self.ug2zh, self.boom = zh2ug, ug2zh, boom
        self.seen: list[str] = []

    def complete(self, msgs, *, temperature=0.1, max_tokens=800, **_):
        text = msgs[-1]["content"]
        self.seen.append(text)
        if self.boom:
            raise RuntimeError("网关 502")
        # 系统提示是中文还是"维→中"，靠提示词里的方向词区分
        sys_p = msgs[0]["content"]
        return self.zh2ug if "翻译成维吾尔语" in sys_p else self.ug2zh


def make(tmp_path, *, translator=None, factory=None, llm=None):
    store = Store(tmp_path / "pv.db")
    eng = ChatEngine({"prompts": {"chat": "你是预问诊助手。"}}, store,
                     llm or FakeLLM(), translator=translator,
                     translator_factory=factory)
    return eng, store


def open_ug(store) -> str:
    """直接开一条会话，跳过开场那句模型调用。

    只考 `reply` 这条路径时，从"会话已存在"起跑最干净。"""
    return store.create(chief="", empty_slots={}, lang="ug")


# --------------------------------------------------------------- 开场白

def test_start_without_complaint_never_calls_model(tmp_path):
    """患者刚点进来：只建会话，不调模型。

    开场问候是界面文案（前端 t.pvHello），让模型现说第一句 = 患者对着空白页
    多等一次生成 + 一次翻译，而那句话本来就是写死的。
    """
    llm = FakeLLM()
    store = Store(tmp_path / "pv.db")
    eng = ChatEngine({"prompts": {"chat": "你是预问诊助手。"}}, store, llm,
                     translator=FakeTranslator())
    out = eng.start(lang="ug")
    assert llm.calls == 0
    assert out["turns"] == []                    # 流水里没有开场白这一条
    assert out["session_id"]


def test_start_with_complaint_answers_directly(tmp_path):
    """第一句就是主诉：建会话 + 记主诉 + 直接给出第一问，一次到位。"""
    eng, _ = make(tmp_path, translator=FakeTranslator())
    out = eng.start("肚子疼", lang="ug")
    assert [t["role"] for t in out["turns"]] == ["user", "assistant"]
    assert out["turns"][-1]["content"] == UG_ANSWER


# --------------------------------------------------------------- 双语与逐条语言

def test_ug_reply_keeps_both_languages_and_label(tmp_path):
    eng, store = make(tmp_path, translator=FakeTranslator())
    sid = eng.start(lang="ug")["session_id"]
    turn = eng.reply(sid, "肚子疼", lang="ug")["turns"][-1]
    assert turn["content"] == UG_ANSWER          # 患者看到的
    assert turn["content_int"] == ZH_ANSWER      # 中文原稿（切中文界面时用它）
    assert turn["lang"] == "ug"
    assert turn["translated"] is True


def test_zh_reply_has_no_translation_and_label_zh(tmp_path):
    tr = FakeTranslator()
    eng, _ = make(tmp_path, translator=tr)
    sid = eng.start(lang="zh")["session_id"]
    turn = eng.reply(sid, "肚子疼", lang="zh")["turns"][-1]
    assert turn["content"] == ZH_ANSWER
    assert turn["lang"] == "zh"
    assert turn["translated"] is True
    assert UG_ANSWER not in tr.seen              # 中文会话不碰翻译模型


def test_request_language_wins_per_turn(tmp_path):
    """语言按请求走：同一个会话里两种语言混着问，各条各自带语言。

    这正是问答板块的口径（`answer_lang` 每次请求一算）。以前"会话语言优先"，
    界面切了语言会被会话按回去，只能靠重开问诊兜——那是第二套语言系统。
    """
    eng, store = make(tmp_path, translator=FakeTranslator())
    sid = eng.start(lang="ug")["session_id"]
    first = eng.reply(sid, "肚子疼", lang="ug")["turns"][-1]
    second = eng.reply(sid, "疼两天了", lang="zh")["turns"][-1]
    assert first["lang"] == "ug" and first["content"] == UG_ANSWER
    assert second["lang"] == "zh" and second["content"] == ZH_ANSWER


def test_missing_lang_falls_back_to_last_used(tmp_path):
    """前端漏发 ui_lang（缓存了旧 JS）时，沿用上一次用过的语言。"""
    eng, _ = make(tmp_path, translator=FakeTranslator())
    sid = eng.start(lang="ug")["session_id"]
    turn = eng.reply(sid, "肚子疼")["turns"][-1]
    assert turn["content"] == UG_ANSWER
    assert turn["lang"] == "ug"


# --------------------------------------------------- 翻译失败：降级，但不报废

def test_translation_failure_falls_back_and_marks_untranslated(tmp_path):
    """译回中文 = 译了等于没译。落原文并打标记，不抛错、不丢这一轮。"""
    eng, store = make(tmp_path, translator=FakeTranslator(zh2ug=ZH_ANSWER))
    sid = open_ug(store)
    turn = eng.reply(sid, "肚子疼", lang="ug")["turns"][-1]
    assert turn["content"] == ZH_ANSWER           # 原文，界面会挂"翻译未完成"
    assert turn["lang"] == "zh"
    assert turn["translated"] is False
    # 上下文仍然连续：下一轮 7B 还能接着问
    assert turn["content_int"] == ZH_ANSWER


def test_half_translated_also_falls_back(tmp_path):
    """半翻（维吾尔语里夹一整句中文）同样判失败。"""
    eng, store = make(tmp_path, translator=FakeTranslator(zh2ug="چۈشەندىم، 疼痛是持续的吗？"))
    sid = open_ug(store)
    turn = eng.reply(sid, "肚子疼", lang="ug")["turns"][-1]
    assert turn["translated"] is False
    assert turn["content"] == ZH_ANSWER


def test_missing_translator_falls_back(tmp_path):
    eng, store = make(tmp_path)                   # 没有 translator、没有工厂
    sid = open_ug(store)
    turn = eng.reply(sid, "肚子疼", lang="ug")["turns"][-1]
    assert turn["translated"] is False
    assert turn["lang"] == "zh"


# --------------------------------------------------- 切语言：补翻译，不重答

def test_localize_fills_ug_for_chinese_answers(tmp_path):
    """中文问出来的回答，切到维语时被**翻译**成维语，而不是重开重答。

    用户原话：「这两个切换每次内容不统一」。旧做法是换语言就重开一场，
    模型每次生成的内容都不一样；现在补的是同一句话的译文，只翻一次。
    """
    tr = FakeTranslator()
    eng, _ = make(tmp_path, translator=tr)
    sid = eng.start("肚子疼", lang="zh")["session_id"]
    turns = eng.localize(sid, lang="ug")["turns"]
    bot = [t for t in turns if t["role"] == "assistant"][-1]
    assert bot["content_ug"] == UG_ANSWER
    assert bot["content"] == ZH_ANSWER          # 原文不动，两份并存
    assert bot["content_int"] == ZH_ANSWER

    n = len(tr.seen)
    eng.localize(sid, lang="ug")                # 再切一次：不再翻
    assert len(tr.seen) == n


def test_localize_keeps_patient_words_untouched(tmp_path):
    """只补机器人那侧；患者自己说的话照原样显示。"""
    eng, _ = make(tmp_path, translator=FakeTranslator())
    sid = eng.start("肚子疼", lang="zh")["session_id"]
    turns = eng.localize(sid, lang="ug")["turns"]
    assert turns[0]["role"] == "user" and turns[0]["content_ug"] == ""


def test_localize_without_translator_keeps_original(tmp_path):
    """翻不出来不算错：界面继续显示原文并挂语言标记。"""
    eng, _ = make(tmp_path)                     # 没有 translator、没有工厂
    sid = eng.start("肚子疼", lang="zh")["session_id"]
    turns = eng.localize(sid, lang="ug")["turns"]
    assert [t for t in turns if t["role"] == "assistant"][-1]["content_ug"] == ""


# --------------------------------------------------------------- 懒装配

def test_lazy_factory_recovers_missing_translator(tmp_path):
    """宿主注入的裸引擎（translator=None）在第一个 ug 请求前自愈。"""
    tr = FakeTranslator()
    eng, _ = make(tmp_path, factory=lambda pv, llm: tr)
    sid = eng.start(lang="ug")["session_id"]
    assert eng.translator is tr                   # start 时已补上
    assert eng.reply(sid, "肚子疼", lang="ug")["turns"][-1]["content"] == UG_ANSWER


def test_factory_rebuilt_after_reload_clears_client(tmp_path):
    """reload 把客户端置空后，工厂还能再补一个（不是一次性开关）。"""
    calls = []

    def factory(pv, llm):
        calls.append(1)
        return FakeTranslator()

    eng, _ = make(tmp_path, factory=factory)
    sid = eng.start(lang="ug")["session_id"]
    eng.set_translator(None)                      # 模拟面板保存后重建失败
    assert eng.reply(sid, "肚子疼", lang="ug")["turns"][-1]["content"] == UG_ANSWER
    assert len(calls) >= 2


# --------------------------------------------------------------- 输入侧

def test_chinese_input_skips_translation(tmp_path):
    """维语界面上患者用中文打字：直接进上下文，不必来回翻一遍。"""
    tr = FakeTranslator()
    eng, store = make(tmp_path, translator=tr)
    sid = eng.start(lang="ug")["session_id"]
    tr.seen.clear()
    eng.reply(sid, "肚子疼", lang="ug")
    assert "肚子疼" not in tr.seen
    users = [t for t in store.get(sid)["turns"] if t["role"] == "user"]
    assert users[-1]["content_int"] == "肚子疼"
    assert users[-1]["lang"] == "zh"


def test_uyghur_input_is_translated_before_model(tmp_path):
    tr = FakeTranslator(ug2zh="我肚子疼")
    eng, store = make(tmp_path, translator=tr)
    sid = eng.start(lang="ug")["session_id"]
    eng.reply(sid, "قورسىقىم ئاغرىيدۇ", lang="ug")
    assert "قورسىقىم ئاغرىيدۇ" in tr.seen
    users = [t for t in store.get(sid)["turns"] if t["role"] == "user"]
    assert users[-1]["content_int"] == "我肚子疼"
    assert users[-1]["lang"] == "ug"


@pytest.mark.parametrize("raw,expect", [
    ("ug", "ug"), ("UG", "ug"), ("ug-UY", "ug"), ("ug_CN", "ug"),
    ("en", "en"), ("EN-us", "en"), ("zh", "zh"), ("zh-Hans", "zh"),
    ("", None), (None, None),
])
def test_lang_aliases(raw, expect):
    assert ChatEngine.normalize_lang(raw) == expect
