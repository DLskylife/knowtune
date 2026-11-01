"""精排与 MMR。

D5 已确认：默认 base 档，复杂任务按请求切 m3。所以重排器必须支持
"同一进程内多模型共存、按需惰性加载"，而不是启动时定死一个。

超时处理：CrossEncoder.predict 是同步阻塞调用，Python 线程无法被强行中断。
这里的做法是把它丢进单线程执行器并 wait(timeout)——超时就**放弃等待**、
走上层降级路径。代价是那个线程会在后台把算力跑完（无法回收），
所以超时值必须设得比正常耗时明显大（默认 10s），避免频繁触发。
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from pathlib import Path

import numpy as np

from ..config import Config


class RerankResult:
    def __init__(self, scores: list[float] | None, model: str, degraded: str | None) -> None:
        self.scores = scores
        self.model = model
        self.degraded = degraded


class Reranker:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._models: dict[str, object] = {}
        self._failed: set[str] = set()
        self._lock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="rerank")
        self._last_error: str | None = None

    @property
    def last_error(self) -> str | None:
        return self._last_error

    def resolve_ref(self, model_name: str) -> str | None:
        """返回可加载的引用；返回 None 表示"不该加载"。

        allow_download=False 时，本地没有模型就立刻放弃——否则 huggingface_hub
        会去连一个大概率连不上的域名，在第一次提问时卡 30 秒才失败。
        这一次失败还会被缓存，进程内不再重试。
        """
        rc = self.cfg.retrieval.rerank
        if rc.local_path:
            p = Path(rc.local_path)
            if p.is_dir() and (p / "config.json").exists():
                return str(p)
        slug_dir = self.cfg.storage.data_dir / "models" / model_name.replace("/", "--")
        if slug_dir.is_dir() and (slug_dir / "config.json").exists():
            return str(slug_dir)
        return model_name if rc.allow_download else None

    def _get(self, model_name: str):
        if model_name in self._models:
            return self._models[model_name]
        with self._lock:
            if model_name in self._models:
                return self._models[model_name]
            ref = self.resolve_ref(model_name)
            if ref is None:
                raise FileNotFoundError(
                    f"精排模型 {model_name} 未在本地找到；放到 "
                    f"{self.cfg.storage.data_dir / 'models' / model_name.replace('/', '--')} "
                    "或设置 retrieval.rerank.allow_download=true"
                )
            from sentence_transformers import CrossEncoder

            model = CrossEncoder(ref, max_length=self.cfg.retrieval.rerank.max_length,
                                 device=self.cfg.embedding.device)
            self._models[model_name] = model
            return model

    def rerank(
        self,
        query: str,
        docs: list[str],
        *,
        model_name: str | None = None,
    ) -> RerankResult:
        cfg = self.cfg.retrieval.rerank
        model_name = model_name or cfg.model
        if not docs:
            return RerankResult([], model_name, None)

        if model_name in self._failed:
            return RerankResult(None, model_name, "rerank_unavailable")

        try:
            model = self._get(model_name)
        except ImportError:
            self._last_error = (
                "未安装 sentence-transformers，精排不可用；"
                '请执行 pip install -e ".[local]"'
            )
            self._failed.add(model_name)
            return RerankResult(None, model_name, "rerank_unavailable")
        except FileNotFoundError as exc:
            self._last_error = str(exc)
            self._failed.add(model_name)
            return RerankResult(None, model_name, "rerank_model_missing")
        except Exception as exc:
            self._last_error = f"精排模型加载失败（{model_name}）：{type(exc).__name__}: {exc}"
            self._failed.add(model_name)
            return RerankResult(None, model_name, "rerank_load_failed")

        pairs = [(query, d) for d in docs]
        future = self._executor.submit(
            model.predict, pairs, batch_size=cfg.batch_size, show_progress_bar=False
        )
        try:
            raw = future.result(timeout=cfg.timeout_s)
        except FutureTimeout:
            self._last_error = f"精排超时（>{cfg.timeout_s}s，模型 {model_name}）"
            return RerankResult(None, model_name, "rerank_timeout")
        except Exception as exc:
            self._last_error = f"精排执行失败：{type(exc).__name__}: {exc}"
            return RerankResult(None, model_name, "rerank_failed")

        scores = np.asarray(raw, dtype=np.float32).reshape(-1).tolist()
        if len(scores) != len(docs):
            self._last_error = f"精排返回数量不符：{len(scores)} vs {len(docs)}"
            return RerankResult(None, model_name, "rerank_bad_output")
        return RerankResult(scores, model_name, None)


def mmr_select(
    ordered_ids: list[int],
    vectors: dict[int, np.ndarray],
    *,
    k: int,
    lambda_: float,
) -> list[int]:
    """Maximal Marginal Relevance：在相关性与多样性之间取平衡。

    lambda_ 越大越偏相关性。0.65 意味着"宁可要一条稍弱但不重复的结果"。
    没有向量的候选（例如被稀疏路召回但缺向量的）直接按原序补位，
    不能让 MMR 把已召回的结果弄丢。
    """
    if k <= 0 or not ordered_ids:
        return []
    if len(ordered_ids) <= k:
        return list(ordered_ids)

    pool = [cid for cid in ordered_ids if cid in vectors]
    no_vec = [cid for cid in ordered_ids if cid not in vectors]
    if len(pool) <= k:
        return list(ordered_ids[:k])

    selected: list[int] = [pool[0]]
    remaining = pool[1:]
    base_rank = {cid: 1.0 - i / max(1, len(pool)) for i, cid in enumerate(pool)}

    while len(selected) < k and remaining:
        best_id = None
        best_val = -1e9
        for cid in remaining:
            rel = base_rank[cid]
            sims = [
                float(np.dot(vectors[cid], vectors[s]))
                for s in selected
                if cid in vectors and s in vectors
            ]
            redundancy = max(sims) if sims else 0.0
            val = lambda_ * rel - (1 - lambda_) * redundancy
            if val > best_val:
                best_val = val
                best_id = cid
        if best_id is None:
            break
        selected.append(best_id)
        remaining.remove(best_id)

    for cid in no_vec:
        if len(selected) >= k:
            break
        selected.append(cid)
    return selected[:k]
