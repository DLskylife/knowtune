"""前台站点的模型角色解析。"""

from __future__ import annotations

from typing import Any

from ..roles import resolve as role_resolve


def provider_for(
    role: str,
    *,
    spec: Any,
    kb_cfg: Any,
    cfg: Any,
    section_roles: dict[str, str] | None = None,
) -> str:
    """按板块、站点的角色配置解析模型角色。

    项目没有全局后端层：用谁由每个站点（及板块）自己指定，这里不掺系统级
    默认，也不读知识库覆盖的 generation.provider（该字段已删除）。
    """
    return role_resolve(
        role,
        instance_roles=getattr(spec, "roles", {}) or {},
        instance_provider=getattr(spec, "provider", "") or "",
        known=getattr(cfg.generation, "providers", {}) or {},
        section_roles=section_roles or {},
    )
