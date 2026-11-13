"""提示词配置路由：模型角色分工、翻译探测、提示词覆盖/回滚/预览。"""

from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ... import logbus, modelcfg, promptcfg
from ... import roles as roles_mod
from ..state import state

router = APIRouter()


class RolesIn(BaseModel):
    roles: dict[str, str] = Field(default_factory=dict)


@router.get("/api/model-roles")
def get_model_roles() -> dict:
    """角色分工的总览：系统默认 + 每个站点的覆盖 + 实际会用到谁。

    界面上必须能看见"实际用了谁"：配了 hunyuan 却因为名字拼错回落到本地后端，
    用户只会觉得"翻译质量没变"，而看不出配置根本没生效。
    """
    providers = state.cfg.generation.providers
    default = state.cfg.generation.provider
    items = []
    from ...frontsite import instcfg
    for spec in instcfg.load_all():
        # 每个站点真正会用的"当前默认"是**它自己那个库**的有效配置，不是全局那个。
        try:
            kb_default = state.effective_cfg(spec.kb_id).generation.provider or default
        except Exception:
            kb_default = default
        resolved = roles_mod.plan(
            instance_roles=spec.roles or {},
            global_roles=state.cfg.generation.roles or {},
            instance_provider=spec.provider or "",
            default_provider=kb_default,
            known=providers,
        )
        # 每个检索型板块各自解析出的实际后端（板块可钉一个与站点不同的后端）。
        section_resolved = {
            key: roles_mod.plan(
                instance_roles=spec.roles or {},
                global_roles=state.cfg.generation.roles or {},
                instance_provider=spec.provider or "",
                default_provider=kb_default,
                known=providers,
                section_roles=spec.section(key)["roles"],
            )
            for key in ("med", "classic")
        }
        items.append({"name": spec.name, "roles": spec.roles or {},
                      "kb_default": kb_default, "resolved": resolved,
                      "section_resolved": section_resolved})
    return {
        "roles": list(roles_mod.ROLES),
        "labels": roles_mod.ROLE_LABELS,
        "providers": sorted(providers.keys()),
        "current": state.cfg.generation.roles or {},
        "default_provider": default,
        "sites": items,
    }


@router.put("/api/model-roles")
def put_model_roles(req: RolesIn) -> dict:
    """保存系统级角色分工。

    只接受"还活着"的后端名：指向一个没注册的后端，界面上下拉里根本没有它，
    留着只会让排查时多绕一圈。
    """
    known = state.cfg.generation.providers
    picked = {}
    rejected = []
    for role, name in (req.roles or {}).items():
        name = (name or "").strip()
        if not name:
            continue
        # 值的形态是「后端::模型」，模型优先级：请求显式指定 > 角色值里的 > 后端默认。
        base, model = roles_mod.split(name)
        if base not in known:
            rejected.append(f"{role}={name}")
            continue
        picked[role] = roles_mod.join(base, model)
    state.cfg.generation.roles = picked
    result = modelcfg.persist(state.cfg)
    logbus.note("模型角色已更新：" + (", ".join(f"{k}→{v}" for k, v in picked.items()) or "（全部清空）"),
                source="models")
    return {"ok": True, "roles": picked, "rejected": rejected, "path": result["path"]}


class TranslateTestIn(BaseModel):
    # 空串 = 用系统级 translate 角色当前指向的后端
    provider: str = ""
    text: str = ""
    # 方向默认对齐前台主链路（中文语料 ← 维语提问；答案再译回维语）
    src: str = "zh"
    dst: str = "ug"


@router.post("/api/translate-test")
def translate_test(req: TranslateTestIn) -> dict:
    """单独测翻译模型：不碰知识库、不碰生成，就发一句真实翻译。

    前台问答 = 翻译 + 检索 + 生成 + 再翻译，四步里翻译挂了两步。对话测试页
    只验生成，翻译模型坏了用户只看到"答案不相关"，根本想不到是它。这里把
    翻译单独拎出来测，一次调用、直接看译文。
    """
    from ... import lang as lang_mod

    providers = state.cfg.generation.providers
    name = req.provider.strip() or (state.cfg.generation.roles or {}).get("translate", "")
    if not name:
        raise HTTPException(400, "还没指定翻译模型：先在「前台站点 → 模型角色」里选一个")
    base, _model = roles_mod.split(name)
    if base not in providers:
        raise HTTPException(400, f"后端 {base} 不存在或已删除")
    text = req.text.strip() or (
        "维吾尔医学认为人体有血液、黏液、黄胆汁、黑胆汁四种体液，"
        "体液失衡是疾病的主要原因。"
    )
    t0 = time.perf_counter()
    meta: dict = {}
    out, err = lang_mod.translate_query(
        state.llm, text, src=req.src, dst=req.dst, provider=name, meta=meta
    )
    ms = int((time.perf_counter() - t0) * 1000)
    ok = not err and bool(out.strip()) and out.strip() != text.strip()
    want_model = _model or ""
    actual_model = str(meta.get("model") or "")
    warn = ""
    if want_model and actual_model and actual_model != want_model:
        warn = f"模型未生效：要求 {want_model}，实际跑的是 {actual_model}"
    return {
        "ok": ok and not warn, "output": out, "error": err, "latency_ms": ms,
        "provider": name, "model": actual_model, "want_model": want_model,
        "warn": warn, "src": req.src, "dst": req.dst,
    }


class PromptSaveIn(BaseModel):
    # 空串 = 角色级（所有没单独配的后端都用它）；非空 = 只管这一个后端
    provider: str = ""
    scope: str = ""
    fields: dict[str, str] = Field(default_factory=dict)


class PromptRollbackIn(BaseModel):
    name: str = ""


def _prompt_role(role: str) -> str:
    if role not in promptcfg.ROLE_FIELDS:
        raise HTTPException(404, f"未知提示词角色：{role}")
    return role


@router.get("/api/prompts")
def list_prompts(provider: str = "") -> dict:
    """提示词总览：每个角色有哪些字段、哪些后端单独配过、当前生效的是哪一层。"""
    providers = list(state.cfg.generation.providers.keys())
    return promptcfg.overview(
        version=state.cfg.prompt_version,
        providers=providers,
        active=provider or state.cfg.generation.provider,
    )


@router.get("/api/prompts/{role}")
def get_prompt(role: str, provider: str = "", scope: str = "") -> dict:
    """取某个角色在某个后端上的实际情况：每字段给出「生效文本 + 来源 + 内置原文」。"""
    _prompt_role(role)
    fields = promptcfg.effective_fields(
        role, provider, version=state.cfg.prompt_version, scope=scope,
    )
    return {
        "role": role,
        "provider": provider,
        "scope": scope,
        "fields": fields,
        "path": str(promptcfg.OVERLAY_PATH),
        "history_count": len(promptcfg.history()),
    }


@router.put("/api/prompts/{role}")
def put_prompt(role: str, req: PromptSaveIn) -> dict:
    """保存覆盖。字段传空串 = 该字段回到上一层（通常是内置默认）。"""
    _prompt_role(role)
    try:
        result = promptcfg.save(role, req.provider, req.fields,
                                version=state.cfg.prompt_version,
                                scope=req.scope)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not result.get("ok"):
        return result
    if req.scope:
        where = f"（板块 {req.scope}）"
    elif req.provider:
        where = f"（后端 {req.provider}）"
    else:
        where = "（角色默认）"
    logbus.note(
        f"提示词已{'更新' if result.get('saved') else '恢复默认'}"
        f"：{role}{where}",
        source="models",
    )
    return result


@router.post("/api/prompts/{role}/reset")
def reset_prompt(role: str, req: PromptSaveIn | None = None) -> dict:
    _prompt_role(role)
    provider = (req.provider if req else "") or ""
    scope = (req.scope if req else "") or ""
    try:
        result = promptcfg.reset(role, provider, version=state.cfg.prompt_version,
                                 scope=scope)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if scope:
        where = f"（板块 {scope}）"
    elif provider:
        where = f"（后端 {provider}）"
    else:
        where = "（角色默认）"
    logbus.note(f"提示词恢复默认：{role}{where}", source="models")
    return result


@router.get("/api/prompts/{role}/history")
def prompt_history(role: str) -> dict:
    _prompt_role(role)
    return {"role": role, "items": promptcfg.history()}


@router.post("/api/prompts/{role}/rollback")
def rollback_prompt(role: str, req: PromptRollbackIn) -> dict:
    """整体回滚。快照是每次保存前的完整副本，所以这里不需要重新体检——
    它本来就是某个曾经生效过的状态。"""
    _prompt_role(role)
    try:
        result = promptcfg.rollback(req.name)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    logbus.note(f"提示词回滚到 {req.name}", source="models")
    return result


_PREVIEW_DOC = (
    "[1] 《晶珠本草》 · 第 12 页 · 药物·性味\n"
    "藏木香，味辛苦，性温。功能理气止痛，用于脘腹胀痛、呕吐泄泻。"
)
_PREVIEW_Q = "藏木香的性味和功效是什么？"


@router.post("/api/prompts/{role}/preview")
def preview_prompt(role: str, req: PromptSaveIn) -> dict:
    """把还没保存的草稿渲染成模型真正收到的东西。

    这一步存在的理由：提示词里写的是规则，模型读到的是"规则 + 系统追加的语言
    指令 + 证据档位例外 + 资料块"。只看规则本身，用户无法判断最终效果——
    尤其是那两段追加，它们会盖住用户刚写的某些约束。
    """
    _prompt_role(role)
    fields = req.fields or {}
    version = state.cfg.prompt_version

    if role == "generate":
        system = fields.get("system") or promptcfg.system_prompt(
            version, None, req.provider)
        append_sample = ""
        try:
            from ...lang import answer_instruction

            append_sample = answer_instruction("ug")
        except Exception:
            append_sample = ""
        return {
            "role": role,
            "provider": req.provider,
            "system": system,
            "append_sample": append_sample,
            "user": f"【资料】\n{_PREVIEW_DOC}\n\n【问题】\n{_PREVIEW_Q}\n\n"
                    f"请依据上述资料作答，并按要求标注来源编号。",
            "note": "预览里的资料是固定样例，不接真实检索。"
                    "实际请求时 system 末尾还会追加两段，且都由运行期决定、不可在此编辑："
                    "① 非中文作答时追加语言指令（下面「实际还会追加」是维吾尔语的例子）；"
                    "② 检索证据不足时追加例外条款，允许用自身知识作答但必须标明哪些不是资料。",
        }

    builtin = promptcfg.builtin_fields("translate")
    user_tpl = fields.get("user") or builtin["user"]
    system = fields.get("system") or builtin["system"]
    return {
        "role": role,
        "provider": req.provider,
        "system": system,
        "append_sample": "",
        "user": promptcfg.fill(user_tpl, src="维吾尔语", dst="中文",
                               question="مىزاج دېگەن نېمە؟"),
        "note": "预览按「维吾尔语 → 中文」填了占位符；实际方向由知识库语言决定。"
                "重试路径（第二次翻译尝试）用的是另一套更硬的约束，不在这里显示。",
    }