"""模型角色（2026-10-28）。

一次问答里其实是三种完全不同的模型调用，把它们绑在同一个后端上是偷懒：

  translate —— 把维吾尔语提问译成中文去检索，以及把写好的中文答案整段译成
               维吾尔语。这类任务是"照着原文改写"，要的是忠实、不出花样。
  generate —— 读检索到的资料、组织成一段答案。要的是会取舍、会组织。

本地 7B 做后者勉强、做翻译时容易把提示词一起吐出来；云端混元做翻译又快又稳。
既然两边的能力不在一个价位上，就没理由让它们共用一个后端。

角色不是"再加一层配置"的负担，而是把已经存在的三次调用显式化：不配也能跑
（回落到当前后端），配了才分工。

解析顺序（谁更具体谁优先）：
  站点 roles → 系统 roles → 站点 provider → 知识库/系统当前后端
每一级都可能是"改过名 / 已删掉"的后端，所以每一级都要用 known 校验一遍，
对不上就往下退一层 —— 宁可退化成"没分工"，也不能抛异常让整个站点 500。
"""

from __future__ import annotations

from typing import Any, Mapping

ROLES: tuple[str, ...] = ("translate", "generate")

ROLE_LABELS: dict[str, str] = {
    "translate": "提问译中文去检索 + 中文成稿译回维吾尔语",
    "generate": "读检索资料组织答案",
}

# 没配任何角色时，生成才是主业：翻译只是跨语言检索时的手段。
FALLBACK_ROLE = "generate"


def normalize(raw: Any, *, known: Mapping[str, Any] | None = None) -> dict[str, str]:
    """清洗配置里的 roles：只留已知角色、值是后端名、去空、未知后端丢掉。

    手改 YAML 最容易写错的就是这种小字典，而写错它的代价是"整个站点起不来"，
    所以这里一律宽容：不认识的就当没写。
    """
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for role in ROLES:
        val = raw.get(role)
        name = str(val or "").strip()
        if not name:
            continue
        if known is not None and name not in known:
            continue
        out[role] = name
    return out


def resolve(
    role: str,
    *,
    instance_roles: Mapping[str, str] | None = None,
    global_roles: Mapping[str, str] | None = None,
    instance_provider: str = "",
    default_provider: str = "",
    known: Mapping[str, Any] | None = None,
) -> str:
    """按"谁更具体谁优先"的顺序找一个可用的后端名。"""
    chain = (
        (instance_roles or {}).get(role),
        (global_roles or {}).get(role),
        instance_provider,
        default_provider,
    )
    for cand in chain:
        name = str(cand or "").strip()
        if not name:
            continue
        # 后端被改名或删掉了：这一级当作没配，继续往下退
        if known is not None and name not in known:
            continue
        return name
    return str(default_provider or "").strip()


def plan(
    *,
    instance_roles: Mapping[str, str] | None = None,
    global_roles: Mapping[str, str] | None = None,
    instance_provider: str = "",
    default_provider: str = "",
    known: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    """把每个角色解析出来，给 /api/info 和日志用（界面上要能看到实际用了谁）。"""
    return {
        role: resolve(
            role,
            instance_roles=instance_roles,
            global_roles=global_roles,
            instance_provider=instance_provider,
            default_provider=default_provider,
            known=known,
        )
        for role in ROLES
    }
