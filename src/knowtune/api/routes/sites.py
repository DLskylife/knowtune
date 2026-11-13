"""前台站点实例路由：实例启停/删除/配置、端口分配。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ... import logbus, promptcfg
from ...frontsite import instcfg
from ..state import state

router = APIRouter()


def _kb_exists(kb_id: int) -> bool:
    return state.conn.execute("SELECT id FROM kb WHERE id=?", (kb_id,)).fetchone() is not None


def _instance_status(name: str) -> str | None:
    return next((i["status"] for i in state.sites.status_list() if i["name"] == name), None)


class InstanceIn(BaseModel):
    """站点配置的增量补丁。

    `None` 和空值必须分开：以前 title="" 会被 PUT 当成"没传"，回退到旧值，
    于是界面上怎么清都清不掉一个标题。现在 None = 不动这一项，
    ""/[] = 明确清空。前端内联编辑也靠这个语义——改一个字段只发那一个。
    """
    name: str | None = None
    kb_id: int | None = None
    classic_kb_id: int | None = None
    port: int | None = None
    lang: str | None = None
    kb_lang: str | None = None
    answer_mode: str | None = None
    provider: str | None = None
    roles: dict[str, str] | None = None
    alt: dict[str, Any] | None = None
    title: str | None = None
    subtitle: str | None = None
    welcome: str | None = None
    samples: list[str] | None = None
    disclaimer: str | None = None
    kb_name: str | None = None
    autostart: bool | None = None
    public: bool | None = None
    # 左侧三个板块的开关与显示名：{"med": {"enabled": true, "name": ""}, ...}
    sections: dict[str, Any] | None = None


@router.get("/api/instances")
def list_instances() -> dict:
    return {
        "items": state.sites.status_list(),
        "langs": list(instcfg.LANGS),
        "port_range": [instcfg.PORT_MIN, instcfg.PORT_MAX],
        "kbs": [dict(r) for r in state.conn.execute(
            "SELECT id, name FROM kb ORDER BY id").fetchall()],
        "providers": list(state.cfg.generation.providers.keys()),
    }


@router.get("/api/instances/defaults")
def instance_defaults(lang: str = "ug") -> dict:
    """新建实例时按语言预填一套文案。

    文案的主来源已经是「提示词配置」里的 copy_ug / copy_zh，这里先取那套；
    提示词没改过的字段才落回出厂默认——否则新建一个站点会拿到一份跟现有站点
    不一样的旧文案，用户得再改一遍。
    """
    picked = lang if lang in instcfg.LANGS else "ug"
    out = instcfg.default_copy(picked)
    out.update(promptcfg.copy_texts(picked))
    return out


@router.get("/api/instances/next-port")
def instance_next_port() -> dict:
    return {"port": instcfg.next_free_port()}


@router.put("/api/instances/{name}")
def put_instance(name: str, req: InstanceIn) -> dict:
    """保存站点配置。只有端口和监听地址变了才重启。

    以前的规则是"只要站点在跑就 restart"，因为 `build_site` 把 spec 闭包捕获
    成了快照，不重启新配置根本不生效。现在 site.py 每次请求现读配置，文案、
    角色、知识库、语言、答案模式全都是热的，只有 uvicorn 启动时钉死的
    socket 绑定（端口 / 0.0.0.0）非重启不可。

    这么做不是省那 3 秒：内联编辑意味着用户改一个标点就触发一次保存，
    如果每次都断服，"直接改"这个交互本身就是不可用的。
    """
    name = (name or req.name or "").strip().lower()
    old = instcfg.get(name)
    port = int(req.port or (old.port if old else instcfg.next_free_port()))

    def keep(attr: str, default):
        """没传的字段沿用旧值。None = 没传；""/[]/=明确清空。"""
        return getattr(old, attr) if old is not None else default

    # 备选语言文案：只下发主语言时保留原来那份
    alt_copy = (instcfg.coerce_alt(req.alt) if req.alt is not None
                else keep("alt", {}))
    spec = instcfg.InstanceSpec(
        name=name,
        kb_id=int(req.kb_id if req.kb_id is not None else keep("kb_id", 0)),
        classic_kb_id=int(req.classic_kb_id if req.classic_kb_id is not None
                          else keep("classic_kb_id", 0)),
        classic_keywords=list(keep("classic_keywords", ["古籍"])),
        port=port,
        lang=(req.lang or keep("lang", "ug")),
        kb_lang=(req.kb_lang or keep("kb_lang", "zh")),
        answer_mode=(req.answer_mode or keep("answer_mode", "translate")),
        provider=req.provider if req.provider is not None else keep("provider", ""),
        roles=(instcfg.coerce_roles(req.roles) if req.roles is not None
               else keep("roles", {})),
        alt=alt_copy,
        title=req.title if req.title is not None else keep("title", ""),
        subtitle=req.subtitle if req.subtitle is not None else keep("subtitle", ""),
        welcome=req.welcome if req.welcome is not None else keep("welcome", ""),
        samples=req.samples if req.samples is not None else keep("samples", []),
        disclaimer=req.disclaimer if req.disclaimer is not None else keep("disclaimer", ""),
        kb_name=req.kb_name if req.kb_name is not None else keep("kb_name", ""),
        # 板块配置整份下发（前端六个控件一次发全）。没传 = 这份配置还不认识
        # 板块（老页面 / 老脚本），沿用旧值，别把用户的开关重置成"全开"。
        sections=(instcfg.coerce_sections(req.sections) if req.sections is not None
                  else keep("sections", instcfg.default_sections())),
        autostart=bool(req.autostart) if req.autostart is not None else keep("autostart", False),
        public=bool(req.public) if req.public is not None else keep("public", False),
    )
    try:
        instcfg.validate(spec, kb_exists=_kb_exists)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    for other in instcfg.load_all():
        if other.name != name and other.port == port:
            raise HTTPException(400, f"端口 {port} 已被实例「{other.name}」占用")

    # 冷字段：只有这两个动到 socket。其余字段下一次请求就生效。
    cold = old is None or old.port != spec.port or old.public != spec.public
    was_running = _instance_status(name) == "running"
    instcfg.upsert(spec)
    status = state.sites.get(name)
    if was_running and cold:
        status = state.sites.restart(name)
    logbus.note(f"站点实例 {name} 已保存（端口 {port}，知识库 #{spec.kb_id}，{spec.lang}"
                + ("，已重启生效）" if was_running and cold else "，热生效）"),
                source="sites")
    return {"ok": True, "spec": spec.as_dict(), "status": status,
            "restarted": bool(was_running and cold)}


@router.post("/api/instances/{name}/start")
def start_instance(name: str) -> dict:
    if instcfg.get(name) is None:
        raise HTTPException(404, "实例不存在")
    status = state.sites.start(name)
    if status["status"] == "error":
        # 已发出去的不是异常：启动失败是"环境问题"，但要让用户看见原因
        logbus.note(f"站点 {name} 启动失败：{status['error']}", level="ERROR", source="sites")
    else:
        logbus.note(f"站点 {name} 已启动：{status['url']}", source="sites")
    return status


@router.post("/api/instances/{name}/stop")
def stop_instance(name: str) -> dict:
    if instcfg.get(name) is None:
        raise HTTPException(404, "实例不存在")
    status = state.sites.stop(name)
    logbus.note(f"站点 {name} 已停止", source="sites")
    return status


@router.post("/api/instances/{name}/restart")
def restart_instance(name: str) -> dict:
    if instcfg.get(name) is None:
        raise HTTPException(404, "实例不存在")
    return state.sites.restart(name)


@router.delete("/api/instances/{name}")
def delete_instance(name: str) -> dict:
    if instcfg.get(name) is None:
        raise HTTPException(404, "实例不存在")
    state.sites.stop(name)
    instcfg.remove(name)
    logbus.note(f"站点实例 {name} 已删除", level="WARNING", source="sites")
    return {"ok": True, "deleted": name}