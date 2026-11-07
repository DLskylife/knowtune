"""previsit —— 预问诊（外壳版）。

2026-11-07 起这个包只剩一层壳：一段提示词 + 模型本身。

- `config/previsit.yaml` 只有界面文案、模型参数和**一段** `prompts.chat`；
  不再有槽位、危险词、分诊规则、追问知识库、图谱库；
- 模型是**必需**的：没有模型就没有预问诊（以前那套"纯规则跑完全程"已经删了，
  规则拼出来的问句听起来像医生，其实没有理解患者说了什么）；
- 会话流水落自己的 SQLite（`data/previsit.db`），不碰主库。

**刻意与 knowtune 主项目零依赖**：这个包不 import 主项目的任何东西，能单独
部署。主项目挂载方式见 knowtune/previsit/。
"""

from .chat import ChatError, ChatEngine
from .llm import LLMError, OpenAICompatLLM
from .store import Store

# 老名字：壳子版本仍叫 PrevisitEngine，调用方不用改 import。
PrevisitEngine = ChatEngine

__all__ = ["ChatEngine", "PrevisitEngine", "ChatError",
           "OpenAICompatLLM", "LLMError", "Store"]
__version__ = "0.4.0"
