"""预问诊桥接（knowtune → previsit 独立包）。

预问诊的实现已经**整体迁移到顶层独立包 `previsit`**（src/previsit）——
它不 import 主项目的任何东西，能单独部署、只依赖后端知识库
（previsit.yaml / previsit_kb.yaml / kg.db）。本模块只做两件事：

1. **向后兼容的再导出**：老代码里 `from knowtune.previsit import ...`
   照常用，实现全在 `previsit` 包；
2. **LLM 适配器**：主项目的 LLMClient（带 provider 选择、密钥体系、
   mock 降级）包一层，对齐独立包引擎要的
   `complete(msgs, *, temperature, max_tokens) -> str` 接口。

新代码请直接 import `previsit`。
"""

from __future__ import annotations

from typing import Any

# 再导出（实现见 src/previsit）
from previsit import (                       # noqa: F401
    PrevisitEngine,
    Store,
    kgdb,
)
from previsit.config import load_previsit    # noqa: F401
from previsit.llm import LLMError            # noqa: F401


class KnowtuneLLMAdapter:
    """把主项目的 LLMClient 适配成独立引擎的 complete() 接口。

    mock 后端回吐的是"链路回显"，不是答案也不是追问；拿它当话术发给患者
    等于骗人。明确当作"没有模型"返回空串，引擎自然走规则兜底。
    """

    def __init__(self, client: Any, provider: str | None = None,
                 model: str | None = None) -> None:
        self._client = client
        self._provider = provider
        self._model = model

    def retune(self, provider: str | None = None, model: str | None = None) -> None:
        """改「预问诊用哪个后端/模型」，不动主项目的全局设置。

        参数面板保存时调它：provider 留空 = 跟随后台当前默认，不写死。
        """
        self._provider = provider or None
        self._model = model or None

    def complete(self, msgs: list[dict[str, str]], *, temperature: float = 0.2,
                 max_tokens: int = 320, **_: Any) -> str:
        from previsit.llm import LLMError as _LLMError

        try:
            res = self._client.complete(
                msgs,
                provider=self._provider,
                model=self._model,
                temperature=temperature,
                max_tokens=max_tokens,
                reasoning="none",
            )
        except Exception as exc:             # 主项目侧任何报错 → 引擎熔断降级
            raise _LLMError(str(exc)) from exc
        if res.provider == "mock" or "mock" in (res.degraded or []):
            return ""
        return (res.text or "").strip()
