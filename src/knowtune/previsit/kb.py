"""桥接：知识检索的真身在顶层独立包 `previsit`（见 src/previsit/kb.py）。"""

from previsit.kb import (                        # noqa: F401
    ask_of,
    detect_domain,
    for_slot,
    retrieve,
)
