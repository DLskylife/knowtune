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

# 角色值的完整形态是「后端::模型」。只写后端名也合法（用后端自己的 model），
# 所以这个分隔符必须足够不可能出现在后端名或模型名里——`/` 在 HuggingFace 的
# 模型 id 里到处都是（`Qwen/Qwen2.5-7B-Instruct`），用它就是自找麻烦。
SEP = "::"

ROLE_LABELS: dict[str, str] = {
    "translate": "提问译中文去检索 + 中文成稿译回维吾尔语",
    "generate": "读检索资料组织答案",
}

# 没配任何角色时，生成才是主业：翻译只是跨语言检索时的手段。
FALLBACK_ROLE = "generate"


def split(ref: Any) -> tuple[str, str]:
    """把角色值拆成 `(后端名, 模型名)`。没写模型时第二项是空串。

    这里必须宽容：YAML 手写、前端旧版本、接口直传三种来源都会进来，其中任何一种
    多打一个空格都不该让站点起不来。
    """
    s = str(ref or "").strip()
    if SEP not in s:
        return s, ""
    name, _, model = s.partition(SEP)
    return name.strip(), model.strip()


def join(provider: Any, model: Any = "") -> str:
    """拼回角色值。模型为空就只留后端，保证「没选模型」和「选了默认模型」
       在配置里长得一样——否则同一个意思会有两种写法。"""
    p = str(provider or "").strip()
    m = str(model or "").strip()
    return f"{p}{SEP}{m}" if p and m else p


def normalize(raw: Any, *, known: Mapping[str, Any] | None = None) -> dict[str, str]:
    """清洗配置里的 roles：只留已知角色、校验后端那一半、去空、未知后端丢掉。

    校验只看 `::` 前面的后端名：模型名写错了不该把整个角色丢掉，那会让站点
    悄悄退回默认后端，而用户以为自己配好了。模型名对不对，交给真实调用去打脸。
    """
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for role in ROLES:
        name, model = split(raw.get(role))
        if not name:
            continue
        if known is not None and name not in known:
            continue
        out[role] = join(name, model)
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
    """按"谁更具体谁优先"的顺序找一个可用的角色值（可能带 `::模型`）。"""
    chain = (
        (instance_roles or {}).get(role),
        (global_roles or {}).get(role),
        instance_provider,
        default_provider,
    )
    for cand in chain:
        name, _model = split(cand)
        if not name:
            continue
        # 后端被改名或删掉了：这一级当作没配，继续往下退
        if known is not None and name not in known:
            continue
        return join(name, _model)
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
