"""全局应用状态（单例）。

`AppState` 是后台服务的共享状态：配置、数据库、检索器、LLM、前台站点管理器。
把它单独放一个模块，是为了让拆到 `routes/` 里的路由模块能直接 `from ..state
import state`，而不会跟 `app.py` 形成循环 import——路由模块依赖 state，state 不
依赖任何路由。
"""

from __future__ import annotations

import json
import threading
import time

from .. import kbconfig
from ..config import (
    Config,
    apply_kb_calibration,
    calibration_of_kb,
    calibration_path,
    defaults_config,
    load_config,
)
from ..db import Database
from ..frontsite.runner import InstanceManager
from ..generate.llm import LLMClient
from ..index.embedder import Embedder
from ..index.sparse import SparseIndex
from ..index.vectors import VectorStore
from ..ingest.pipeline import IngestPipeline
from ..retrieve.engine import RetrievalEngine
from ..retrieve.rerank import Reranker


class AppState:
    def __init__(self) -> None:
        self.cfg = load_config()
        self.db = Database(self.cfg)
        self.embedder = Embedder(self.cfg.embedding)
        self.vectors = VectorStore(self.cfg)
        self.sparse = SparseIndex(self.cfg.retrieval.sparse)
        self.reranker = Reranker(self.cfg)
        self.engine = RetrievalEngine(
            self.cfg, self.embedder, self.vectors, self.sparse, self.reranker
        )
        self.pipeline = IngestPipeline(
            self.cfg, self.db, self.embedder, self.sparse, self.vectors
        )
        self.llm = LLMClient(self.cfg)
        self.db.ensure_schema()
        self.started_at = time.time()
        # 知识库级配置的副本缓存：key=kb_id，value=(覆盖值签名, Config 副本)。
        # 每次请求都 deepcopy 一份 Config 也能用，但那是每次提问多花几十微秒
        # 去复制一堆根本不会变的嵌套模型，而这里只按"覆盖值变了没"失效。
        self._kbcfg: dict[int, tuple[str, Config]] = {}
        self._kbcfg_lock = threading.Lock()
        # 预热进度。冷启动 40 秒里，前端必须能说清"为什么还不能问"，
        # 否则用户看到的就是"页面是空的 + 点了发送一直转圈"。
        self.warm: dict = {"state": "pending", "started_at": time.time(),
                           "took_ms": None, "error": None}
        # 前台问答站点（独立端口）。同一进程内复用上面的检索器与 LLM，
        # 所以不用再加载一遍嵌入模型。
        self.sites = InstanceManager(self)

    @property
    def conn(self):
        return self.db.conn()

    # ------------------------------------------------------------ 知识库配置

    def overrides(self, kb_id: int | None) -> dict:
        if not kb_id:
            return {}
        return kbconfig.load(self.conn, int(kb_id))

    def tuned(self, kb_id: int | None) -> dict:
        """与系统默认值不同的那几项 —— 调试面板要显示的是"这个库调过什么"，
        而现在 config_json 里每一项都有值，直接报长度永远是 25。"""
        values = self.overrides(kb_id)
        if not values:
            return {}
        baseline = kbconfig.baseline(self.cfg)
        return {k: v for k, v in values.items() if baseline.get(k) != v}

    def effective_cfg(self, kb_id: int | None) -> Config:
        """该知识库实际生效的配置。没有覆盖值也没有标定时直接返回全局对象本身。"""
        if not kb_id:
            return self.cfg
        overrides = self.overrides(kb_id)
        calib = calibration_of_kb(self.cfg, kb_id)
        if not overrides and not calib:
            return self.cfg
        # 签名里带上标定文件的修改时间：标定脚本重跑后文件变了、参数没变，
        # 不给缓存失效的话界面还会显示上一次的阈值，看起来像"标定没生效"。
        path = calibration_path(self.cfg, kb_id)
        stamp = f"{path.stat().st_mtime_ns}" if path.exists() else "-"
        # 签名里还要带上全局后端清单。clone 是从 self.cfg **深拷贝**来的，
        # 而这份缓存只按 overrides + 标定文件判新旧：刚在「新增后端」里配好
        # 一个端点，overrides 一个字没变，缓存就永远命中，状态栏仍旧报
        # known=[]、一颗红点——用户以为没保存上，反复重填（2026-11-06 实测）。
        gen = json.dumps(
            {
                "provider": self.cfg.generation.provider,
                "backends": {
                    n: [p.base_url, p.model, p.kind]
                    for n, p in self.cfg.generation.providers.items()
                },
            },
            sort_keys=True, ensure_ascii=False,
        )
        signature = stamp + gen + json.dumps(overrides, sort_keys=True, ensure_ascii=False)
        with self._kbcfg_lock:
            hit = self._kbcfg.get(int(kb_id))
            if hit and hit[0] == signature:
                return hit[1]
        clone = kbconfig.effective(self.cfg, overrides)
        # 谁能盖过标定结果：只有**用户显式改过**的参数。
        # 库里的 config_json 通常是全量快照（老库迁移时会写成一整套），
        # 把整份 overrides 当"手动值"就等于标定永远不生效——之前 min_score
        # 明明标成了 0.69，界面也显示 calibrated，实际跑的还是快照里那个 0.35。
        # 所以判据是"与出厂值不同"，而不是"键存在"。
        base = kbconfig.baseline(defaults_config())
        manual = {k for k, v in overrides.items() if k in base and base[k] != v}
        apply_kb_calibration(clone, calib, skip=frozenset(manual))
        with self._kbcfg_lock:
            self._kbcfg[int(kb_id)] = (signature, clone)
        return clone

    def invalidate_kb_cfg(self, kb_id: int | None = None) -> None:
        with self._kbcfg_lock:
            if kb_id is None:
                self._kbcfg.clear()
            else:
                self._kbcfg.pop(int(kb_id), None)


state = AppState()