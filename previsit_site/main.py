"""预问诊独立站点入口（隔离部署）。

布局（缺一不可）：
    main.py
    src/previsit/      独立包（不依赖 knowtune）
    config/previsit.yaml + previsit_kb.yaml   槽位/规则 + 分域知识库
    data/previsit/kg.db                        医疗知识图谱（可选，缺了降级）

模型（可选）：设环境变量 PREVISIT_LLM_BASE_URL（可配 PREVISIT_LLM_API_KEY /
PREVISIT_LLM_MODEL / PREVISIT_LLM_TIMEOUT）即启用 OpenAI 兼容后端；
不设就纯规则 + 知识库跑完全程。

启动：python main.py（端口取 $PORT，默认 8000）
"""

import os
import sys
from pathlib import Path

# 让 kg.db 的相对路径（data/previsit/kg.db）与包的根目录解析都锚定在本目录
os.chdir(Path(__file__).resolve().parent)
os.environ.setdefault("PREVISIT_HOME", str(Path.cwd()))
# 独立部署没有后端知识库服务（RAG 检索在 knowtune 主服务里），显式关掉语义
# 召回这一路，省得每场问诊去撞一次连不上的地址。有后端时改指向它即可。
os.environ.setdefault("PREVISIT_KB_URL", "")
sys.path.insert(0, str(Path.cwd() / "src"))

import uvicorn  # noqa: E402

if __name__ == "__main__":
    port = int(os.environ.get("PORT") or 8000)
    uvicorn.run("previsit.app:app", host="0.0.0.0", port=port, log_level="info")
