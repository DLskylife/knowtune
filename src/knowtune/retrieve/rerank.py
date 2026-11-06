"""精排与 MMR。

D5 已确认：默认 base 档，复杂任务按请求切 m3。所以重排器必须支持
"同一进程内多模型共存、按需惰性加载"，而不是启动时定死一个。

超时处理：CrossEncoder.predict 是同步阻塞调用，Python 线程无法被强行中断。
这里的做法是把它丢进单线程执行器并 wait(timeout)——超时就**放弃等待**、
走上层降级路径。代价是那个线程会在后台把算力跑完（无法回收），
所以超时值必须设得比正常耗时明显大（默认 10s），避免频繁触发。
"""

from __future__ import annotations

import hashlib
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from pathlib import Path

import numpy as np

from ..config import Config


class RerankResult:
    def __init__(self, scores: list[float] | None, model: str, degraded: str | None,
                 cached: int = 0) -> None:
        self.scores = scores
        self.model = model
        self.degraded = degraded
        # 本轮有多少对（问题, 片段）直接命中缓存、没走模型——给排障一个
        # "精排为什么这次是 0ms" 的直读答案，不用去翻日志猜。
        self.cached = cached


class Reranker:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._models: dict[str, object] = {}
        self._failed: set[str] = set()
        self._lock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="rerank")
        self._last_error: str | None = None
        # 逐对缓存：(模型, 问题摘要, 片段摘要) -> 分数。CrossEncoder 给一对文本
        # 打分，与这批候选里还有哪些片段无关，所以同问重提、示例问题、超时重试
        # 都能整轮命中；换个问题也能命中两问共有的那些片段（检索候选高度重叠
        # 是这个库的常态）。键用文本摘要而不是 chunk_id：重建索引后 chunk_id
        # 可能复用，拿旧分给新文本就是悄悄给错答案排序。摘要是 16 字节 hex，
        # 撞上的概率远低于模型自己打错分的概率。
        self._cache: OrderedDict[tuple[str, str, str], float] = OrderedDict()
        self._cache_cap = 4096
        self._cache_lock = threading.Lock()

    @staticmethod
    def _digest(text: str) -> str:
        return hashlib.blake2b(text.encode("utf-8"), digest_size=8).hexdigest()

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

    def warmup(self) -> str | None:
        """提前把权重读进内存。返回错误信息，None 表示就绪。

        1.1G 权重冷读要 5-7 秒，这 7 秒落在第一个提问的人头上就是"点了发送
        干等"，而他并不知道这只发生一次。精排默认开着，这笔账迟早要付，
        放到启动预热的线程里付最便宜。

        失败也在这里落 last_error：不落的话，界面要等到有人提问才发现精排
        一直没生效——和"标定没生效应"一样，是"看起来开着、实际没跑"。
        """
        name = self.cfg.retrieval.rerank.model
        try:
            self._get(name)
            return None
        except ImportError:
            self._last_error = (
                "未安装 sentence-transformers，精排不可用；"
                '请执行 pip install -e ".[local]"'
            )
        except FileNotFoundError as exc:
            self._last_error = str(exc)
        except Exception as exc:
            self._last_error = f"精排模型加载失败（{name}）：{type(exc).__name__}: {exc}"
        self._failed.add(name)
        return self._last_error

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

        q_hash = self._digest(query)
        keys = [(model_name, q_hash, self._digest(d)) for d in docs]
        scores: list[float | None] = [None] * len(docs)
        todo: list[int] = []
        with self._cache_lock:
            for i, k in enumerate(keys):
                hit = self._cache.get(k)
                if hit is None:
                    todo.append(i)
                else:
                    self._cache.move_to_end(k)
                    scores[i] = hit
        cached_n = len(docs) - len(todo)

        if todo:
            pairs = [(query, docs[i]) for i in todo]
            future = self._executor.submit(
                model.predict, pairs, batch_size=cfg.batch_size, show_progress_bar=False
            )
            try:
                raw = future.result(timeout=cfg.timeout_s)
            except FutureTimeout:
                self._last_error = f"精排超时（>{cfg.timeout_s}s，模型 {model_name}）"
                return RerankResult(None, model_name, "rerank_timeout", cached_n)
            except Exception as exc:
                self._last_error = f"精排执行失败：{type(exc).__name__}: {exc}"
                return RerankResult(None, model_name, "rerank_failed", cached_n)

            fresh = np.asarray(raw, dtype=np.float32).reshape(-1).tolist()
            if len(fresh) != len(todo):
                self._last_error = f"精排返回数量不符：{len(fresh)} vs {len(todo)}"
                return RerankResult(None, model_name, "rerank_bad_output", cached_n)
            with self._cache_lock:
                for i, sc in zip(todo, fresh):
                    val = float(sc)
                    scores[i] = val
                    self._cache[keys[i]] = val
                    self._cache.move_to_end(keys[i])
                while len(self._cache) > self._cache_cap:
                    self._cache.popitem(last=False)
        return RerankResult(scores, model_name, None, cached_n)


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
