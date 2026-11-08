# 按库标定结果

`kb_<id>.json` 由评测台产出，主项目只读不写。仓库带一份 `kb_2.json` 作示例：维医语料、52 组真实问法、模型指纹 `BAAI/bge-small-zh-v1.5@512`。

**生效条件有三条，缺一即静默退回出厂阈值 0.35**（`src/knowtune/config.py`）：

1. 查询时 `kb_id` 非空且能匹配文件名；
2. JSON 可读；
3. 文件里的 `model` 等于当前 `embedding.model@dim`——换嵌入模型或维度会让标定失效。

所以换机器 clone 后，只有在使用相同嵌入模型、且知识库 id 恰为 2 时这份示例才生效。用自己的语料需要重新标定（标定工具在独立项目 `model-eval`，本仓库不自带）。细节见 [`docs/ARCHITECTURE.md`](../../docs/ARCHITECTURE.md) 与 [`docs/USER_GUIDE.md`](../../docs/USER_GUIDE.md) §5。
