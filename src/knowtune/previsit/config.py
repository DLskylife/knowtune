"""桥接：配置加载的真身在顶层独立包 `previsit`（见 src/previsit/config.py）。

注意：独立包的根目录解析优先 PREVISIT_HOME，其次包路径上推——主项目
src/knowtune 下再调它，`parents[2]` 正好还是项目根，行为一致。
"""

from previsit.config import DEFAULT_KB_PATH, DEFAULT_PATH, load_previsit  # noqa: F401
