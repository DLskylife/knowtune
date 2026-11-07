"""桥接：预问诊引擎的真身在顶层独立包 `previsit`（见 src/previsit/chat.py）。"""

from previsit.chat import ChatEngine, ChatError      # noqa: F401

# 老名字：壳子版本仍叫 PrevisitEngine，调用方不用改 import。
PrevisitEngine = ChatEngine

__all__ = ["PrevisitEngine", "ChatEngine", "ChatError"]
