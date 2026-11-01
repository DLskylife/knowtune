"""会话密钥：只存在内存里，服务一停就没了（2026-10-28）。

为什么不做成配置文件 / .env：

  - 写进 models.yaml 就会被界面的"保存"回写、被导出、被截图、被提交；
  - 写进 .env 还得让人去维护一个额外文件，换台机器又是一轮折腾；
  - 而这类本地/单机问答系统，密钥的生命周期本来就跟服务进程一样长。

所以：**密钥只活在内存里**。管理员在模型页填一次，进程内所有请求共用；
服务重启（或点"清空密钥"）就消失，得重新填一次。这是有意的取舍，不是缺陷：
重启后必须重新授权，才不会有"某台机器上躺着一个谁都能用的密钥"这种事。

键是**后端名**（models.yaml 里的 name，如 deepseek / bailian），不是模型名——
同一个后端换模型不该让密钥失效。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

# 进程级存储。用模块级 dict 而不是挂到 Config 上：配置对象会被"重读配置"
# 整个替换掉，挂在那里密钥会莫名其妙丢；模块级 dict 与进程同寿。
_KEYS: dict[str, str] = {}


def put(name: str, key: str) -> None:
    key = (key or "").strip()
    if not name or not key:
        return
    _KEYS[name] = key


def get(name: str) -> str | None:
    return _KEYS.get(name)


def drop(name: str) -> None:
    _KEYS.pop(name, None)


def clear() -> None:
    _KEYS.clear()


def has(name: str) -> bool:
    return bool(_KEYS.get(name))


def names() -> list[str]:
    return sorted(_KEYS)


# ------------------------------------------------------------------ 可选落盘
#
# 默认**不落盘**：进程一停，密钥就没了，重启必须重填。这是刻意的取舍。
# 但每次重启都重填一遍在开发期很折磨，所以给一个显式开关：只有管理员主动
# 勾了"记住到本机"，才会写这个文件；文件存在 = 开关是开的。
#
# 文件权限尽力收到 0600（Windows 上 chmod 只影响只读位，做不到就退化为默认），
# 并在文件头写清"这里存的是明文密钥"——免得有人在不知道的情况下把它分享出去。

_PATH: Path | None = None
_FILENAME = "secrets.json"


def attach(data_dir: Path | str) -> bool:
    """绑定落盘位置并尝试读回。返回是否读到了内容（= 开关开着）。

    只在这里读：其它任何地方都不该主动去碰盘，否则"默认不落盘"就成了空话。
    """
    global _PATH
    _PATH = Path(data_dir) / _FILENAME
    if not _PATH.exists():
        return False
    try:
        data = json.loads(_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    keys = data.get("keys") if isinstance(data, dict) else None
    if not isinstance(keys, dict):
        return False
    for name, key in keys.items():
        put(str(name), str(key))
    return True


def save() -> bool:
    """把当前内存里的密钥写到磁盘。没 attach 过就什么也不做。"""
    if _PATH is None:
        return False
    try:
        _PATH.parent.mkdir(parents=True, exist_ok=True)
        _PATH.write_text(
            json.dumps({"note": "明文密钥，仅本机使用；删除此文件即回到'不记忆'模式",
                        "keys": dict(_KEYS)}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        try:
            os.chmod(_PATH, 0o600)
        except OSError:
            pass          # Windows 上做不到就算了，别为此让保存失败
        return True
    except OSError:
        return False


def forget() -> None:
    """只删盘上的文件，内存里保持不动：当前会话照常能用，下次重启就不再恢复。"""
    if _PATH is not None and _PATH.exists():
        try:
            _PATH.unlink()
        except OSError:
            pass


def wipe_file() -> None:
    """删掉落盘文件，并清空内存。用于"我不想让它们留在这台机器上"。"""
    global _PATH
    if _PATH is not None and _PATH.exists():
        try:
            _PATH.unlink()
        except OSError:
            pass
    clear()


def persisted() -> bool:
    return bool(_PATH and _PATH.exists())
