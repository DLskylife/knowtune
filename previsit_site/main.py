"""预问诊独立站点入口（隔离部署）。

代码只有一份：`src/previsit/`（主项目里的那个包，含 static/）。
这里不再放逐字节副本，只负责摆好 Python 路径与环境变量：

    <项目根>/src/previsit/            唯一实现 + 唯一静态资源
    previsit_site/config/             本部署自带的领域配置（可单独维护）
    previsit_site/data/               本部署的会话库与知识图谱（不入库）
    previsit_site/main.py             本入口

PREVISIT_HOME 仍指向本目录，所以配置文件、覆盖层、模型清单、会话库都
留在独立部署这一侧，不会和主站互相串味；只有 Python 代码从主项目导入。
把本目录单独拷出去部署时，需要一并带上 `<项目根>/src/previsit/`。

模型：跟随主项目「模型服务」页的固定 /v1 入口；也可设
PREVISIT_LLM_BASE_URL / PREVISIT_LLM_API_KEY / PREVISIT_LLM_MODEL 直连。
没配就纯规则 + 知识库跑完全程。

启动：python main.py（端口取 $PORT，默认 8000）
"""

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent

# 让 kg.db 的相对路径（data/previsit/kg.db）锚定在本目录；配置根同理。
os.chdir(HERE)
os.environ.setdefault("PREVISIT_HOME", str(HERE))
# 独立进程没有主服务的语义检索接口，显式关掉这一路，省得每场问诊撞一次
# 连不上的地址。有后端时把环境变量改成它的地址即可。
os.environ.setdefault("PREVISIT_KB_URL", "")

# 关键：唯一的那份实现在主项目 src 下。删掉本目录的重复包之后，主站挂载
# 与独立部署跑的是同一份 app.py / engine.py / static，改一处两边同时生效。
sys.path.insert(0, str(PROJECT / "src"))

import uvicorn  # noqa: E402

if __name__ == "__main__":
    port = int(os.environ.get("PORT") or 8000)
    uvicorn.run("previsit.app:app", host="0.0.0.0", port=port, log_level="info")
