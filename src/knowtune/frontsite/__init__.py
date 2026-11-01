"""前台问答站点：独立端口、独立界面，只做一件事——针对一个知识库问答。

与后台的关系是"共用内核、分开表皮"：检索器、嵌入模型、LLM 客户端复用后台
进程里已经就绪的那一份，界面与接口则是另一套。
"""

from .instcfg import InstanceSpec, load_all, next_free_port  # noqa: F401
from .runner import InstanceManager  # noqa: F401
from .site import build_site  # noqa: F401
