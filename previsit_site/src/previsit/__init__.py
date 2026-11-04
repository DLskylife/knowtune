"""previsit —— 独立的预问诊模块。

**刻意与 knowtune 主项目零依赖**：这个包不 import 主项目的任何东西
（config / secrets / LLMClient / 检索链都不碰），它的数据面只有三样：

1. `config/previsit.yaml` —— 槽位、危险词、分诊规则、提示词；
2. `config/previsit_kb.yaml` —— 分域追问知识库；
3. `data/previsit/kg.db` —— 清洗后的医疗知识图谱（可选，缺了自动降级）。

大模型是**可选项**：设了 `PREVISIT_LLM_BASE_URL`（可选配
`PREVISIT_LLM_API_KEY` / `PREVISIT_LLM_MODEL`）就走一个 OpenAI 兼容的
最小客户端；不设就纯规则 + 知识库跑完全程。会话数据落自己的 SQLite
（`data/previsit.db`），不碰主库。

主项目（knowtune）挂载本包的方式见 knowtune/previsit/__init__.py 的桥接。
"""

from .engine import PrevisitEngine
from .llm import LLMError, OpenAICompatLLM
from .store import Store

__all__ = ["PrevisitEngine", "OpenAICompatLLM", "LLMError", "Store"]
__version__ = "0.3.0"
