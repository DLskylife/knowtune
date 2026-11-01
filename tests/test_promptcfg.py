"""提示词配置层的行为锁。

这一层的失败模式是**静默**的：读不到覆盖就退回内置，问答链照常出字，只是
你刚改的那条约束没生效。所以每条不变式都要有断言，不能靠"跑一次看着还行"。
"""

from __future__ import annotations

import pytest
import yaml

from ragkb import promptcfg as pc


@pytest.fixture()
def iso(tmp_path, monkeypatch):
    """把覆盖文件与历史目录挪到临时目录，并清掉 mtime 缓存。

    不清缓存的话，前一个用例写的文件会被后一个用例读到——症状是"单独跑都过，
    一起跑就挂"，而这类失败最容易被当成偶然。
    """
    overlay = tmp_path / "prompts.yaml"
    hist = tmp_path / "_history"
    monkeypatch.setattr(pc, "OVERLAY_PATH", overlay)
    monkeypatch.setattr(pc, "HISTORY_DIR", hist)
    monkeypatch.setattr(pc, "_cache", {"key": None, "data": {}})
    return tmp_path


class TestLayering:
    def test_builtin_when_nothing_configured(self, iso):
        text = pc.system_prompt("qa_v1", None, "mock")
        assert text == pc.builtin_fields("generate")["system"]
        assert pc.effective_fields("generate", "mock")["system"]["source"] == "builtin"

    def test_provider_beats_role_default(self, iso):
        pc.save("generate", "", {"system": "角色默认：只能依据资料，标 [1]。"})
        pc.save("generate", "mock", {"system": "后端专属：只能依据资料，标 [1]，简洁。"})

        assert pc.effective_fields("generate", "mock")["system"]["source"] == "provider"
        assert "后端专属" in pc.system_prompt("qa_v1", None, "mock")
        # 没单独配的后端应该拿到角色默认，而不是被别的后端连坐
        assert pc.effective_fields("generate", "other")["system"]["source"] == "role"

    def test_provider_override_does_not_leak(self, iso):
        pc.save("generate", "mock", {"system": "只给 mock：依据 [1]。"})
        assert pc.system_prompt("qa_v1", None, "bailian") == \
            pc.builtin_fields("generate")["system"]

    def test_empty_string_clears_layer(self, iso):
        pc.save("generate", "mock", {"system": "临时：依据 [1]。"})
        assert pc.effective_fields("generate", "mock")["system"]["source"] == "provider"
        pc.save("generate", "mock", {"system": ""})
        assert pc.effective_fields("generate", "mock")["system"]["source"] == "builtin"

    def test_identical_to_builtin_is_not_an_override(self, iso):
        """改回原样要真的等于"没改过"，否则界面永远显示"已自定义"，
        「恢复默认」就没人敢按——按下去会发生什么说不清。"""
        builtin = pc.builtin_fields("generate")["system"]
        pc.save("generate", "mock", {"system": builtin})
        assert not pc.OVERLAY_PATH.exists()
        assert pc.effective_fields("generate", "mock")["system"]["source"] == "builtin"

    def test_file_shape_is_prompts_rooted(self, iso):
        pc.save("generate", "mock", {"system": "依据 [1] 作答。"})
        raw = yaml.safe_load(pc.OVERLAY_PATH.read_text(encoding="utf-8"))
        assert list(raw) == ["prompts"]
        assert raw["prompts"]["generate"]["by_provider"]["mock"]["system"]


class TestTranslate:
    def test_fill_replaces_all_placeholders(self, iso):
        out = pc.fill(pc.BUILTIN_TRANSLATE_USER, src="维吾尔语", dst="中文",
                      question="مىزاج نېمە؟")
        assert "维吾尔语" in out and "中文" in out and "مىزاج نېمە؟" in out
        assert "{question}" not in out

    def test_fill_tolerates_literal_braces(self, iso):
        """模板里写 JSON 例子是常事，用 str.format 会当场 KeyError。"""
        out = pc.fill('输出格式 {"src": "{src}"}，原句：{question}', src="维", question="问")
        assert out == '输出格式 {"src": "维"}，原句：问'

    def test_missing_question_is_rejected(self, iso):
        r = pc.save("translate", "mock", {"user": "把{src}翻成{dst}"})
        assert r["ok"] is False
        assert r["errors"][0]["level"] == "error"
        assert not pc.OVERLAY_PATH.exists()

    def test_missing_lang_placeholder_warns_only(self, iso):
        r = pc.save("translate", "mock", {"user": "翻译：{question}"})
        assert r["ok"] is True
        assert any(w["level"] == "warn" for w in r["warnings"])


class TestCheck:
    def test_dropping_citation_rule_warns(self, iso):
        r = pc.save("generate", "mock", {"system": "你是一个热心助手，随便回答。"})
        assert r["ok"] is True, "缺引用规则是警告不是拒绝：有人确实想关掉引用"
        texts = " ".join(w["text"] for w in r["warnings"])
        assert "引用" in texts or "编号" in texts

    def test_over_length_is_rejected(self, iso):
        r = pc.save("generate", "mock", {"system": "标 [1]。" + "啊" * (pc.MAX_CHARS + 1)})
        assert r["ok"] is False
        assert "上限" in r["errors"][0]["text"]

    def test_unknown_provider_is_warning_not_error(self, iso):
        """后端可能临时下线或改名，这时覆盖该留着，等它回来还能用。"""
        r = pc.save("generate", "ghost-backend", {"system": "依据 [1] 作答。"})
        assert r["ok"] is True


class TestHistory:
    def test_snapshot_taken_before_overwrite(self, iso):
        pc.save("generate", "mock", {"system": "第一版：依据 [1]。"})
        first = pc.OVERLAY_PATH.read_text(encoding="utf-8")
        pc.save("generate", "mock", {"system": "第二版：依据 [1]，更简洁。"})
        items = pc.history()
        assert len(items) == 1
        assert (pc.HISTORY_DIR / items[0]["name"]).read_text(encoding="utf-8") == first

    def test_rollback_restores_previous_state(self, iso):
        pc.save("generate", "mock", {"system": "第一版：依据 [1]。"})
        pc.save("generate", "mock", {"system": "第二版：依据 [1]，更简洁。"})
        assert "第二版" in pc.system_prompt("qa_v1", None, "mock")

        pc.rollback(pc.history()[0]["name"])
        assert "第一版" in pc.system_prompt("qa_v1", None, "mock")

    def test_rollback_rejects_path_traversal(self, iso):
        with pytest.raises(ValueError):
            pc.rollback("../../local.yaml")

    def test_reset_removes_layer(self, iso):
        pc.save("generate", "mock", {"system": "临时：依据 [1]。"})
        pc.reset("generate", "mock")
        assert not pc.OVERLAY_PATH.exists()
        assert pc.effective_fields("generate", "mock")["system"]["source"] == "builtin"


class TestResilience:
    def test_broken_yaml_falls_back_to_builtin(self, iso):
        """手改 YAML 改坏是必然会发生的。坏文件不能让问答链路 500，
        也不能被静默删除——用户的编辑还在里面，下次保存会先备份走。"""
        pc.OVERLAY_PATH.write_text("prompts: [unclosed\n", encoding="utf-8")
        pc._cache["key"] = None
        assert pc.system_prompt("qa_v1", None, "mock") == \
            pc.builtin_fields("generate")["system"]
        assert pc.OVERLAY_PATH.exists()

    def test_unknown_role_rejected(self, iso):
        with pytest.raises(ValueError):
            pc.save("nonexistent", "", {"system": "x"})
