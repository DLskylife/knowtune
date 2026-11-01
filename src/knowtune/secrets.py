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

------------------------------------------------------------------ 落盘加密

"记住到本机"写下去的一律是**密文**，不是明文（2026-11-01 改）。明文文件在
Windows 上收不住权限（chmod 只改只读位），而它会被备份、被同步盘带走、被
截图、被当成普通文件分享出去——任何一种都等于密钥泄露。

加密方式按平台选，且都绑到"当前这台机器 + 当前这个账户"：

  - Windows：DPAPI（CryptProtectData，用户态）。换账户、换机器都解不开，
    把文件拷出去只是一串废数据。零依赖，ctypes 直接调 crypt32。
  - 其它系统：没有同等级的系统级保护可用，就**干脆不落盘**并如实报错，
    而不是拿一个自己造的"加密"糊弄过去——假的安全比没有更坏。

文件里只留后端名与密文；解不开的条目直接跳过（换了账户/机器），不会把密文
当成密钥拿去发请求。旧版明文文件读进来后会立刻升级成密文重写。
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path

# 进程级存储。用模块级 dict 而不是挂到 Config 上：配置对象会被"重读配置"
# 整个替换掉，挂在那里密钥会莫名其妙丢；模块级 dict 与进程同寿。
_KEYS: dict[str, str] = {}

_LAST_ERROR: str | None = None
_WINDOWS = os.name == "nt"
_ENC_DPAPI = "dpapi"
_ENC_PLAIN = "plain"          # 只用于识别旧文件，绝不用于写入


class SecretStoreError(RuntimeError):
    """落盘失败且原因值得让用户看见（不支持加密 / 写入失败）。"""


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


def last_error() -> str | None:
    """最近一次落盘/读取失败的原因。前端拿它说人话，别让按钮点了没反应。"""
    return _LAST_ERROR


# ------------------------------------------------------------------ 加密层


def _dpapi(protect: bool, data: bytes) -> bytes:
    """Windows DPAPI：用当前登录用户的凭据加密/解密一小段数据。

    CRYPTPROTECT_UI_FORBIDDEN 必须带上：否则系统可能弹一个"是否允许"的框，
    而这个过程跑在后台服务里，弹窗没人点，调用就一直挂在那儿。
    """
    import ctypes
    import ctypes.wintypes as wt

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data, len(data))
    src = DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    dst = DATA_BLOB()
    fn = ctypes.windll.crypt32.CryptProtectData if protect \
        else ctypes.windll.crypt32.CryptUnprotectData
    ok = fn(ctypes.byref(src), None, None, None, None, 0x01, ctypes.byref(dst))
    if not ok:
        raise OSError(f"DPAPI {'加密' if protect else '解密'}失败（错误码 {ctypes.GetLastError()}）")
    try:
        return ctypes.string_at(dst.pbData, dst.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(dst.pbData)


def _seal(raw: str) -> tuple[str, str]:
    """加密一个密钥，返回 (编码名, 可写进 JSON 的文本)。"""
    if not _WINDOWS:
        # 不用自造的"加密"兜底：那只是把明文换个样子，出事时反而更难解释。
        raise SecretStoreError(
            "当前系统没有可用的系统级加密（Windows 用 DPAPI），已拒绝明文落盘；"
            "密钥仍在内存里可用，重启后需重新填写"
        )
    return _ENC_DPAPI, base64.b64encode(_dpapi(True, raw.encode("utf-8"))).decode("ascii")


def _unseal(enc: str, blob: str) -> str:
    if enc == _ENC_DPAPI:
        return _dpapi(False, base64.b64decode(blob)).decode("utf-8")
    raise SecretStoreError(f"无法识别的加密方式：{enc}")


def backend_label() -> str:
    """给界面用的简短说明：密钥到底被什么保护着。"""
    return "Windows 账户加密（DPAPI）" if _WINDOWS else "当前系统不支持加密保存"


# ------------------------------------------------------------------ 可选落盘
#
# 默认**不落盘**：进程一停，密钥就没了，重启必须重填。这是刻意的取舍。
# 但每次重启都重填一遍在开发期很折磨，所以给一个显式开关：只有管理员主动
# 勾了"记住到本机"，才会写这个文件；文件存在 = 开关是开的。
#
# 写入的是密文（见文件头说明）。另外尽力收权限（Windows 上只能退化为默认），
# 并在文件头写清"这是密文"，免得有人在不知道的情况下把它分享出去。

_PATH: Path | None = None
_FILENAME = "secrets.json"


def attach(data_dir: Path | str) -> bool:
    """绑定落盘位置并尝试读回。返回是否读到了内容（= 开关开着）。

    只在这里读：其它任何地方都不该主动去碰盘，否则"默认不落盘"就成了空话。
    """
    global _PATH, _LAST_ERROR
    _PATH = Path(data_dir) / _FILENAME
    if not _PATH.exists():
        return False
    try:
        data = json.loads(_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        _LAST_ERROR = f"密钥文件读不出来：{_PATH}"
        return False
    stored = data.get("keys") if isinstance(data, dict) else None
    if not isinstance(stored, dict):
        return False
    enc = str(data.get("enc") or _ENC_PLAIN)
    failed: list[str] = []
    for name, val in stored.items():
        try:
            put(str(name), str(val) if enc == _ENC_PLAIN else _unseal(enc, str(val)))
        except Exception:
            # 解不开（换了账户/机器/文件被改）就跳过：把密文当密钥拿去发请求
            # 只会得到一个莫名其妙的 401，不如直接说"这几个读不回来"。
            failed.append(str(name))
    if failed:
        _LAST_ERROR = f"{len(failed)} 个密钥无法解密（换了账户或机器）：{', '.join(failed)}"
    if enc == _ENC_PLAIN and _KEYS:
        # 旧版明文文件：既然已经读进来了，就趁这次把它升级成密文，别再留着。
        save()
    return True


def save() -> bool:
    """把当前内存里的密钥以密文写到磁盘。没 attach 过就什么也不做。

    一个都加密不成功时**整批不写**：半份密文半份明文比全明文更难解释，
    而写一半这种事恰恰最难被发现。
    """
    global _LAST_ERROR
    if _PATH is None:
        return False
    sealed: dict[str, str] = {}
    enc = ""
    for name, key in _KEYS.items():
        try:
            enc, blob = _seal(key)
        except (SecretStoreError, OSError) as exc:
            _LAST_ERROR = str(exc)
            return False
        sealed[name] = blob
    try:
        _PATH.parent.mkdir(parents=True, exist_ok=True)
        _PATH.write_text(
            json.dumps({
                "v": 2,
                "enc": enc,
                "note": f"密钥密文（{enc}），由本机当前账户加密；换账户或换机器即失效。"
                        "删除此文件就回到「不记忆」模式。",
                "keys": sealed,
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        try:
            os.chmod(_PATH, 0o600)
        except OSError:
            pass          # Windows 上做不到就算了，别为此让保存失败
        _LAST_ERROR = None
        return True
    except OSError as exc:
        _LAST_ERROR = f"写入密钥文件失败：{exc}"
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
