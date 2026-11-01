"""本地嵌入模型封装。

CPU 优先：默认 bge-small-zh-v1.5（512 维），在 CPU 上批量编码约几十到上百
chunk/秒，对个人库（千级文档）完全够用。GPU 只是可选加速，不是必经路径。

模型缺失时不抛到调用方炸掉整条链路——上层把 dense 路降级掉，用稀疏路兜底，
并明确告知用户"当前为降级检索"。静默降级比直接报错更危险。
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Sequence

import numpy as np

from ..config import EmbeddingCfg


def _tokens(text: str) -> int:
    """与分块器同一套粗估：CJK 约 1 字 1 token，其余约 4 字符 1 token。

    这里只需要"超没超窗"这个判断，不值得为它加载一遍 tokenizer。
    """
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    other = len(text) - cjk
    return cjk + ((other + 3) // 4)


class EmbedderUnavailable(Exception):
    """模型或依赖缺失。message 直接展示给用户。"""


class Embedder:
    def __init__(self, cfg: EmbeddingCfg) -> None:
        self.cfg = cfg
        self._model = None
        self._load_lock = threading.Lock()
        self._error: str | None = None

    @property
    def model_key(self) -> str:
        return f"{self.cfg.model}@{self.cfg.dim}"

    def resolve_ref(self) -> str:
        """本地目录优先。国内网络直连 HF 基本必挂，能离线就别联网。"""
        if self.cfg.local_path:
            p = Path(self.cfg.local_path)
            if p.is_dir() and (p / "config.json").exists():
                return str(p)
        return self.cfg.model

    @property
    def is_loaded(self) -> bool:
        """模型是否已经在内存里——**不触发加载**。

        `available` 会顺手 `_ensure_model()`，那要 import torch + 读 90MB 权重，
        约 40 秒。任何"看一眼状态"的接口（/api/status、首页 dashboard）只要
        调了它，就会在冷启动期间把一个读操作变成 40 秒的阻塞，前端以为服务挂了。
        """
        return self._model is not None

    @property
    def available(self) -> bool:
        if self._model is not None:
            return True
        try:
            self._ensure_model()
        except EmbedderUnavailable:
            return False
        return True

    def load(self) -> None:
        """显式预热。冷启动含 import torch + 读权重，几十秒；
        把它单独拎出来，否则吞吐日志会把加载时间摊进每条文本，得出荒谬的 1.5 chunk/s。"""
        self._ensure_model()

    @property
    def error(self) -> str | None:
        return self._error

    def _ensure_model(self):
        if self._model is not None:
            return self._model
        with self._load_lock:
            if self._model is not None:
                return self._model
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError:
                self._error = (
                    "未安装 sentence-transformers，向量检索不可用；"
                    '请执行 pip install -e ".[local]"（注意 CPU 版 torch 需从 PyTorch 官方 index 安装）'
                )
                raise EmbedderUnavailable(self._error) from None

            try:
                if self.cfg.threads > 0:
                    import torch

                    torch.set_num_threads(self.cfg.threads)
                model = SentenceTransformer(self.resolve_ref(), device=self.cfg.device)
                model.max_seq_length = self.cfg.max_seq_length
                model.eval()
            except Exception as exc:
                ref = self.resolve_ref()
                hint = "" if ref != self.cfg.model else (
                    "\n提示：国内网络可先手动下载模型到本地目录，然后设置 "
                    f"embedding.local_path（当前期望路径：{self.cfg.local_path}）"
                )
                self._error = (
                    f"嵌入模型加载失败（{self.cfg.model} → {ref}）："
                    f"{type(exc).__name__}: {exc}{hint}"
                )
                raise EmbedderUnavailable(self._error) from exc

            self._model = model
            self._error = None
            return model

    def encode(self, texts: Sequence[str], *, batch_size: int | None = None) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.cfg.dim), dtype=np.float32)
        model = self._ensure_model()
        # 超窗文本先切窗、各窗编码、再平均成一条向量。SentenceTransformer 默认
        # 直接**截断**到 max_seq_length，块的后半段等于没进向量——它不报错、不
        # 打日志，唯一的症状是"那段内容永远检索不到"。实测库里 67% 的块被截。
        # 平均会稀释语义（一块讲三件事，向量是三者的中点），但"看不见"比
        # "看不全"糟得多，而且查询侧不受影响（查询本来就短，走单窗原路径）。
        plan = [self._windows(t) for t in texts]
        flat = [w for wins in plan for w in wins]
        vectors = model.encode(
            flat,
            batch_size=batch_size or self.cfg.batch_size,
            normalize_embeddings=self.cfg.normalize,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        arr = np.asarray(vectors, dtype=np.float32)
        if arr.ndim == 1:
            arr = arr.reshape(1, -1)
        if arr.shape[1] != self.cfg.dim:
            raise EmbedderUnavailable(
                f"模型输出维度 {arr.shape[1]} 与配置 embedding.dim={self.cfg.dim} 不一致；"
                "请修正配置后重新索引，否则向量会互相污染"
            )

        out = np.zeros((len(texts), self.cfg.dim), dtype=np.float32)
        pos = 0
        for i, wins in enumerate(plan):
            block = arr[pos : pos + len(wins)]
            pos += len(wins)
            if len(wins) == 1:
                out[i] = block[0]
                continue
            v = block.mean(axis=0)
            if self.cfg.normalize:
                n = float(np.linalg.norm(v))
                if n > 1e-12:
                    v = v / n
            out[i] = v
        return out

    def _windows(self, text: str) -> list[str]:
        """把一段文本切成不超过模型窗口的片段。短文本原样返回（查询走这条）。"""
        text = text or ""
        limit = int(getattr(self.cfg, "max_seq_length", 512) or 512)
        if _tokens(text) <= limit:
            return [text]
        # 按句子边界切；没有边界就按字符硬切，保底不超窗
        parts: list[str] = []
        rest = text
        while _tokens(rest) > limit:
            cut = max(rest.rfind(ch, 0, limit) for ch in "。！？；!?;\n")
            if cut < limit // 2:
                cut = limit
            else:
                cut += 1
            parts.append(rest[:cut])
            rest = rest[cut:]
        if rest:
            parts.append(rest)
        return parts or [text]
