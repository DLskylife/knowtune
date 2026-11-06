"""维吾尔语翻译链路端到端实测（无需真实 LLM 密钥）。

翻译是维医前台问答的第一道关：维语问题必须先译成中文再去检索，否则
bge-small-zh 这个中文嵌入模型根本召回不到东西。这条链路以前只在真模型
配好时才跑得通，没配就完全测不到——于是"翻译被静默丢弃"这类 bug 能藏
三轮排查都发现不了（见 lang.py 注释）。

这里用确定性假模型把整条链路跑通：detect(ug) → llm.complete →
_clean_translation → 返回译文。既验证"识别维语 + 调模型 + 清洗"串得起来，
也把评测集 evals/ug_translate.jsonl 当成翻译质量的基线（真模型配好后
同样这一份就能用来量翻译准不准）。
"""

from __future__ import annotations

import json
from pathlib import Path

from knowtune.lang import detect, translate_query

_EVAL = Path(__file__).resolve().parent.parent / "evals" / "ug_translate.jsonl"


class _Out:
    def __init__(self, text: str = "", model: str = "fake-mt") -> None:
        self.text = text
        self.model = model


class _TranslateLLM:
    """确定性假翻译模型：按 ug → zh 映射回译文，模拟一个"翻译对了"的模型。"""

    def __init__(self, mapping: dict[str, str]) -> None:
        self._map = mapping

    def complete(self, msgs, provider=None, **kw):
        # 用户消息是「指令…\n\n原问题」——原问题总在最后一个空行之后。
        # 拿原问题去映射里查预期译文。
        user_text = ""
        for m in msgs:
            if m.get("role") == "user":
                user_text = m.get("content", "")
                break
        question = user_text.split("\n\n")[-1].strip()
        return _Out(self._map.get(question, ""))


def _load_cases() -> list[dict]:
    cases: list[dict] = []
    for line in _EVAL.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        cases.append(json.loads(line))
    return cases


def test_ug_cases_loaded() -> None:
    cases = _load_cases()
    assert len(cases) >= 3, "维语评测集至少应有 3 条"
    for c in cases:
        assert c.get("ug") and c.get("zh"), f"缺字段：{c}"


def test_detect_classifies_uyghur() -> None:
    cases = _load_cases()
    for c in cases:
        assert detect(c["ug"]) == "ug", f"没识别成维语：{c['ug']}"
    # 对照组：中文 / 英文不应误判成维语
    assert detect("高血压不能吃什么") == "zh"
    assert detect("What is mizaj?") == "en"


def test_translate_query_e2e() -> None:
    """整条翻译链路：识别维语 → 调模型 → 清洗 → 返回译文。"""
    cases = _load_cases()
    mapping = {c["ug"]: c["zh"] for c in cases}
    llm = _TranslateLLM(mapping)
    for c in cases:
        out, err = translate_query(llm, c["ug"], src="ug", dst="zh")
        assert err is None, f"翻译报错：{c['ug']} → {err}"
        assert out == c["zh"], f"译文不符：{c['ug']} → 得到「{out}」期望「{c['zh']}」"


def test_translate_falls_back_on_empty() -> None:
    """模型返回空时退回原文 + 带错误信息，而不是拿空串去查库。"""

    class _EmptyLLM:
        def complete(self, msgs, provider=None, **kw):
            return _Out("")

    out, err = translate_query(_EmptyLLM(), "باش ئاغرىقى", src="ug", dst="zh")
    assert out == "باش ئاغرىقى", "空译文应退回原文"
    assert err is not None and "模型返回空" in err


def test_translate_falls_back_on_echo() -> None:
    """模型把问题原样抄回（短问题常见）时判废并退回原文。"""

    class _EchoLLM:
        def complete(self, msgs, provider=None, **kw):
            # 直接把用户问题回显
            return _Out("باش ئاغرىقى")

    out, err = translate_query(_EchoLLM(), "باش ئاغرىقى", src="ug", dst="zh")
    assert out == "باش ئاغرىقى"
    assert err is not None and "原样抄回" in err
