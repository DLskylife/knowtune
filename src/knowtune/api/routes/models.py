"""模型服务路由：后端注册/探测、本地实例启停、OpenAI 兼容网关 /v1、密钥与活动端点。"""

from __future__ import annotations

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from ... import logbus, modelcfg
from ... import roles as roles_mod
from ... import services as services_mod
from ... import settings as settings_mod
from ...config import ProviderCfg
from ..state import state

router = APIRouter()


@router.post("/api/services/probe")
def probe_services(name: str | None = None) -> dict:
    """真正去连。前端在首屏渲染完之后才调它，不让网络等待堵住首屏。"""
    if name:
        if name not in state.cfg.generation.providers:
            raise HTTPException(404, "后端不存在")
        item = services_mod.probe_one(name, state.cfg.generation.providers[name])
        services_mod.record(item)  # 写缓存并落盘，重启后仍显示这次结论
        return {"providers": {name: item}}
    result = services_mod.probe_all(state.cfg, force=True)
    ok = sum(1 for v in result.values() if v.get("ok"))
    logbus.note(f"探测 {len(result)} 个后端，{ok} 个可用", source="services")
    return {"providers": result}


@router.get("/api/models")
def list_models() -> dict:
    data = modelcfg.listing(state.cfg)
    for p in data["providers"]:
        probe = services_mod.cached(
            p["name"], provider=state.cfg.generation.providers.get(p["name"])) or {}
        p["status"] = probe.get("status", "unknown")
        p["latency_ms"] = probe.get("latency_ms")
        p["message"] = probe.get("message", "还没探测")
        p["checked_at"] = probe.get("checked_at")
        # 探测回来的模型 id 列表必须带出去。之前这里只取了 status，前端于是只
        # 能显示"4 个模型可用"却点不到任何一个——用户得自己去 LM Studio 抄名字。
        #
        # 字段名是 `probed` 而不是 `models`：`models` 现在是配置里那份带
        # path / port / 运行状态的清单，两者混用一个键会让卡片上的下拉
        # 分不清"我配的"和"服务上有的"。
        p["probed"] = probe.get("models") or []
        p["model_listed"] = probe.get("model_listed")

    # 探测到的模型并进扁平目录：那些是"服务上真实存在、只是我还没加进清单"的，
    # 恰恰是用户最想选的一批。缺了它们，下拉看着就像"少了一大截"。
    meta = {p["name"]: p for p in data["providers"]}
    seen = {(c["provider"], c["model"]) for c in data["catalog"]}
    for p in data["providers"]:
        for mid in p["probed"]:
            if (p["name"], mid) in seen:
                continue
            seen.add((p["name"], mid))
            data["catalog"].append({
                "provider": p["name"], "model": mid,
                "current": False, "provider_active": bool(p["active"]),
                "ready": bool(p["ready"]),
            })
    for c in data["catalog"]:
        m = meta.get(c["provider"]) or {}
        c["provider_label"] = m.get("kind_label") or c["provider"]
        c["local"] = bool(m.get("local"))
        # "当前"= 后端是当前后端 + 模型是这个后端的当前模型，缺一不可
        c["active"] = bool(c.get("provider_active") and c.get("current"))
    data["catalog"].sort(key=lambda c: (
        not c["active"], not c["local"], c["provider"], c["model"]))
    return data


class ProbeUrlRequest(BaseModel):
    base_url: str = ""
    # 探测用的密钥：填了就用这份，留空则退到该后端已存在内存里的密钥
    api_key: str = ""
    kind: str = "custom"
    model: str = ""
    # 正在编辑哪个后端：带上名字才能复用已存的密钥
    name: str = ""


@router.post("/api/services/probe-url")
def probe_url(req: ProbeUrlRequest) -> dict:
    """按用户刚敲进去的地址探测，保存之前就能确认对不对。

    与 `/api/services/probe` 的区别是它不读注册表、不写缓存：这是"试一下"，
    结果不该污染卡片上显示的状态。
    """
    try:
        clean = modelcfg.validate_url(req.base_url, allow_empty=req.kind == "mock") or None
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    tmp = ProviderCfg(
        base_url=clean,
        model=(req.model or "").strip() or "placeholder",
        kind=req.kind,
    )
    # 弹窗里刚敲的密钥只挂在这个临时对象上，不进注册表、更不进配置文件。
    # 留空时带上后端名，探测会自动复用内存里已存的那份密钥。
    tmp.set_inline_key(req.api_key)
    hit = services_mod.probe_one(req.name.strip() or "__probe__", tmp)
    return {
        "ok": hit.get("ok"),
        "status": hit.get("status"),
        "message": hit.get("message"),
        "latency_ms": hit.get("latency_ms"),
        "models": hit.get("models") or [],
    }


class ModelUpsert(BaseModel):
    base_url: str = ""
    model: str = ""
    kind: str = "openai"
    # None = 不改动现有密钥；"" = 清空；非空 = 设为新密钥。
    # 密钥只进内存（knowtune.secrets），永远不写 models.yaml。
    api_key: str | None = None
    # 模型清单（每个模型带 path/port/context），None = 不动。
    # 旧字段名 model_list 继续接受：前端和第三方脚本可能还没跟上来。
    models: list[dict] | None = None
    model_list: list[dict] | None = None
    # 是否把密钥记到本机。None = 不动现有设置；True = 写盘；False = 删掉盘上的
    remember: bool | None = None

    def entries(self) -> list[dict] | None:
        return self.models if self.models is not None else self.model_list


@router.put("/api/models/{name}")
def upsert_model(name: str, req: ModelUpsert) -> dict:
    # 只改「用哪个模型」时不能把探测缓存清掉：缓存里存的是"这台服务上有哪些模型"，
    # 那是服务端的属性，和你选了哪一个无关。以前一律 invalidate，于是用户刚在卡片
    # 下拉里切一个模型，列表立刻消失、卡片退回"还没探测"——看起来就是"一切换就没了"。
    before = state.cfg.generation.providers.get(name)
    sig_before = (before.base_url, before.kind) if before else None
    try:
        result = modelcfg.upsert(
            state.cfg, name, base_url=req.base_url, model=req.model, kind=req.kind,
            models=req.entries(),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    after = state.cfg.generation.providers.get(name)
    sig_after = (after.base_url, after.kind) if after else None
    if sig_before != sig_after:
        services_mod.invalidate(name)

    # 密钥不写进 models.yaml：注册表里永远没有它。
    from ... import secrets as secrets_mod

    if req.api_key is not None:
        key = req.api_key.strip()
        if key:
            secrets_mod.put(name, key)
            services_mod.invalidate(name)     # 换了密钥，之前的探测结论作废
            logbus.note(f"后端 {name} 的密钥已更新", source="models")
        else:
            secrets_mod.drop(name)
            services_mod.invalidate(name)
            logbus.note(f"后端 {name} 的密钥已清空", source="models")

    if req.remember is not None:
        secrets_mod.set_autosave(bool(req.remember))

    logbus.note(
        f"后端 {name} 已保存：{req.model} @ {req.base_url or '-'}", source="models"
    )

    # 密钥一变就同步落盘（默认就是这个行为）。日志必须把"存了 / 只在内存"
    # 说清楚——否则用户只能靠重启一次去猜，而那就是他重填十遍的原因。
    # 注意这里不判 `names()` 非空：把最后一个密钥清掉时，恰恰需要写一次
    # 才能删掉盘上的旧文件（save() 在内存为空时走 forget 分支）。
    if req.api_key is not None or req.remember is not None:
        if not secrets_mod.autosave():
            logbus.note("密钥只保留在本次会话：不落盘，重启后需重填", source="models")
        elif secrets_mod.autosync():
            if secrets_mod.names():
                logbus.note(
                    f"密钥已以密文记到本机（{secrets_mod.backend_label()}）："
                    f"{state.cfg.storage.data_dir / 'secrets.json'}", source="models")
            else:
                logbus.note("已没有需要保存的密钥，本机密钥文件已清除", source="models")
        else:
            logbus.note(
                f"记住密钥失败：{secrets_mod.last_error() or '无法写入 data 目录'}",
                level="WARNING", source="models")
    return {"ok": True, **result, "listing": modelcfg.listing(state.cfg)}


@router.delete("/api/models/{name}")
def delete_model(name: str) -> dict:
    providers = state.cfg.generation.providers
    if name not in providers:
        raise HTTPException(404, "后端不存在")
    if len(providers) <= 1:
        raise HTTPException(400, "至少要留一个后端：先新增一个，再删这个")
    # 删的正好是当前在用的那个时，以前是报错让用户自己先去切——那是把系统的
    # 内部约束变成一道手工题。这里直接切到另一个（优先本地）再删，用户点一次
    # 就完成；切换的结果会落盘，不会重启后又指回一个已删掉的后端。
    if state.cfg.generation.provider == name:
        others = [n for n in providers if n != name]
        picked = min(
            others,
            key=lambda n: not modelcfg.kind_of(providers[n].kind).get("local", False),
        )
        try:
            settings_mod.apply(state.cfg, {"generation.provider": picked})
        except ValueError:
            picked = others[0]
            settings_mod.apply(state.cfg, {"generation.provider": picked})
        settings_mod.save_local(state.cfg)
        state.invalidate_kb_cfg()
        logbus.note(f"删除当前后端 {name}，自动切换到 {picked}", level="WARNING", source="models")
    try:
        result = modelcfg.remove(state.cfg, name)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    services_mod.invalidate(name)
    from ... import secrets as secrets_mod

    secrets_mod.drop(name)      # 后端没了，内存里的密钥也不该留着
    logbus.note(f"后端 {name} 已删除", level="WARNING", source="models")
    return {"ok": True, **result}


@router.get("/api/models/local/scan")
def scan_local_models(dirs: str = "", limit: int = 300) -> dict:
    """扫 .gguf。`dirs` 用逗号分隔；不传就按 localmodels 的默认目录。"""
    from ... import localmodels as local_mod

    want = [d.strip() for d in (dirs or "").split(",") if d.strip()]
    try:
        found = local_mod.scan(want or None, limit=max(1, min(limit, 1000)))
    except OSError as exc:
        raise HTTPException(400, f"扫描失败：{exc}") from exc
    return {
        "models": [m.as_dict() for m in found],
        "dirs": want or local_mod.default_dirs(),
        "env": local_mod.describe_env(),
    }


class LocalAction(BaseModel):
    model: str = Field(min_length=1, max_length=120)
    port: int | None = None
    ctx: int | None = None
    ngl: int | None = None
    # 127.0.0.1 = 只本机调；0.0.0.0 = 同一局域网也能调。
    host: str = "127.0.0.1"


@router.post("/api/models/{name}/start")
def start_local_model(name: str, req: LocalAction, request: Request) -> dict:
    """起这个端点下的某个本地模型，并把实例地址回写到端点配置。

    端口可能由 localmodels 自动挑（模型没配 port 时），所以起完必须把 base_url
    改回来——否则配置里写的是 8090、实际跑在 8091，下一次调用直接连不上。
    """
    from ... import localmodels as local_mod

    p = state.cfg.generation.providers.get(name)
    if p is None:
        raise HTTPException(404, "端点不存在")
    entry = p.entry(req.model)
    if entry is None:
        raise HTTPException(404, f"端点「{name}」下没有模型「{req.model}」")
    if not entry.path:
        raise HTTPException(400, f"「{entry.id}」没有本地路径（path），只能连、不能起")

    inst_name = f"{name}/{entry.id}"
    try:
        inst = local_mod.start(
            name=inst_name,
            path=entry.path,
            port=req.port or entry.port,
            ctx=req.ctx or entry.context or local_mod.DEFAULT_CTX,
            ngl=req.ngl,
            alias=entry.id,
            host=req.host,
        )
    except local_mod.LocalError as exc:
        raise HTTPException(400, str(exc)) from exc

    entry.port = inst.port
    p.base_url = inst.base_url
    p.kind = "local"
    if not (p.model or "").strip():
        p.model = entry.id
    modelcfg.persist(state.cfg)
    services_mod.invalidate(name)
    api = local_mod.api_info(inst)
    gateway = str(request.base_url).rstrip("/") + "/v1"
    logbus.note(f"本地实例已启动：{entry.id} @ {api['base_url']}"
                f"（pid {inst.pid}）· 固定入口 {gateway}", source="models")
    return {"ok": True, "instance": inst.as_dict(), "api": api,
            "gateway": gateway, "listing": modelcfg.listing(state.cfg)}


@router.post("/api/models/{name}/stop")
def stop_local_model(name: str, req: LocalAction) -> dict:
    from ... import localmodels as local_mod

    entry = None
    p = state.cfg.generation.providers.get(name)
    if p is not None:
        entry = p.entry(req.model)
    inst_name = f"{name}/{(entry.id if entry else req.model)}"
    try:
        result = local_mod.stop(inst_name)
    except OSError as exc:
        raise HTTPException(400, f"停止失败：{exc}") from exc
    services_mod.invalidate(name)
    logbus.note(f"本地实例已停止：{inst_name}", source="models")
    return {"ok": True, **result, "listing": modelcfg.listing(state.cfg)}


@router.get("/api/models/local/status")
def local_status() -> dict:
    from ... import localmodels as local_mod

    return {"instances": local_mod.running(), "env": local_mod.describe_env()}


# ----------------------------------------------- 对外 OpenAI 兼容网关 /v1
#
# 存在的理由：本地实例「一个模型一个进程一个端口」，端口还可能因为配置的口被占
# 而变化。让别的程序（dify / ccswitch / 脚本 / 预问诊站点）去记 8090、8091、
# 8092 是不现实的。这里给一个**永远不变**的地址——主服务自己的 /v1——由它
# 转发给当前在跑的实例。换模型、换端口、重启实例，外部调用方一个字都不用改。
#
# 支持显式指定：`/v1/chat/completions?provider=local`、请求头
# `X-KnowTune-Provider: local`，或 body 里写 `model: "local::某模型"`。
# 都没写就用当前活动端点；请求里的 model 命中别的端点清单时自动路由过去。


def _gateway_target(request: Request, payload: dict, allow_empty: bool = False) -> tuple[str, ProviderCfg | None, str]:
    providers = state.cfg.generation.providers
    want = (request.query_params.get("provider")
            or request.headers.get("x-knowtune-provider") or "").strip()
    model = str(payload.get("model") or "")

    if not want and "::" in model:
        base_name, real_model = roles_mod.split(model)
        if base_name in providers:
            want, payload["model"] = base_name, real_model
            model = real_model
    if not want and model:
        for name, p in providers.items():
            if p.entry(model) or p.model == model:
                want = name
                break
    if not want:
        want = state.cfg.generation.provider
    if not want:
        # 列表类接口（/v1/models）允许空：返回哨兵让调用方给客户端空列表，
        # 而不是 400。真正要转发的 chat/completions 不带 allow_empty，仍走 400。
        if allow_empty:
            return "", None, ""
        raise HTTPException(400, f"端点「（空）」不存在；"
                                f"可用：{', '.join(sorted(providers)) or '（还没配任何端点）'}")
    if want not in providers:
        raise HTTPException(400, f"端点「{want}」不存在；"
                                f"可用：{', '.join(sorted(providers)) or '（还没配任何端点）'}")

    p = providers[want]
    base = (p.base_url or "").rstrip("/")
    if not base:
        # 端点还没填地址，但它的本地模型已经跑起来了：用实例的真实地址。
        from ... import localmodels as local_mod

        for inst in local_mod.running().values():
            if inst.get("state") == "running" and str(inst.get("name", "")).startswith(want + "/"):
                base = str((inst.get("api") or {}).get("base_url") or "")
                if base:
                    break
    if not base:
        raise HTTPException(503, f"端点「{want}」没有可调用的地址："
                                "先在「模型服务」页填 base_url，或点本地模型的「启动」。")
    return want, p, base


def _upstream_headers(p: ProviderCfg, name: str) -> dict[str, str]:
    h = {"Content-Type": "application/json"}
    key = p.api_key(name)
    if key:
        h["Authorization"] = f"Bearer {key}"
    return h


def _gateway_timeout() -> float:
    t = getattr(getattr(state.cfg.generation, "timeout", None), "read", 120) or 120
    return float(t)


@router.get("/api/gateway")
def gateway_info(request: Request) -> dict:
    """给界面和文档用：现在有哪些能写死在别处的地址。"""
    from ... import localmodels as local_mod

    root = str(request.base_url).rstrip("/")
    v1 = root + "/v1"
    active = state.cfg.generation.provider
    p = state.cfg.generation.providers.get(active)
    model = (p.model if p else "") or ""
    live = local_mod.running()
    direct = [v["api"] for v in live.values()
              if v.get("state") == "running" and v.get("api")]
    curl = (
        f'curl {v1}/chat/completions -H "Content-Type: application/json" '
        f'-d \'{{"model": "{model}", "messages": [{{"role": "user", '
        f'"content": "你好"}}]}}\''
    ) if model else ""
    return {
        "base_url": v1,
        "chat_url": v1 + "/chat/completions",
        "models_url": v1 + "/models",
        "provider": active, "model": model, "curl": curl,
        "direct": direct,
        "hint": "这个地址不随本地实例端口变化；?provider=<端点名> 或 model 里写"
                " <端点名>::<模型名> 可指定后端。",
    }


@router.get("/v1/models")
async def gateway_models(request: Request):
    """OpenAI 客户端接入时第一个打的接口：告诉它有哪些模型。

    容错优先：没配端点、端点没填地址、上游暂时连不上，都返回配置清单
    （或空列表），而不是把 400/503/502 抛给客户端——符合 OpenAI 规范，
    也方便客户端在配置阶段就拿到可用的 model id。
    """
    name, p, base = _gateway_target(request, {}, allow_empty=True)
    if p is None:
        return JSONResponse({"object": "list", "data": []},
                            headers={"X-KnowTune-Upstream": ""})
    try:
        if base:
            async with httpx.AsyncClient(timeout=_gateway_timeout(), trust_env=False) as c:
                r = await c.get(base + "/models", headers=_upstream_headers(p, name))
            if r.status_code < 400:
                return Response(content=r.content, status_code=r.status_code,
                                media_type=r.headers.get("content-type", "application/json"),
                                headers={"X-KnowTune-Upstream": name})
    except httpx.HTTPError:
        pass
    # 上游没起也别让客户端拿到 502：配置清单本身就是"有哪些模型"的可靠答案。
    ids = [m.id for m in p.model_list]
    if p.model and p.model not in ids:
        ids.insert(0, p.model)
    return JSONResponse({"object": "list", "data": [
        {"id": mid, "object": "model", "owned_by": name} for mid in ids]},
        headers={"X-KnowTune-Upstream": name})


@router.post("/v1/chat/completions")
@router.post("/v1/completions")
async def gateway_chat(request: Request):
    try:
        payload = await request.json()
    except ValueError as exc:
        raise HTTPException(400, "请求体不是合法 JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(400, "请求体必须是 JSON 对象")

    name, p, base = _gateway_target(request, payload)
    tail = "chat/completions" if request.url.path.endswith("chat/completions") else "completions"
    url = base + "/" + tail
    headers = _upstream_headers(p, name)
    timeout = _gateway_timeout()

    if payload.get("stream"):
        async def relay():
            async with httpx.AsyncClient(timeout=timeout, trust_env=False) as c:
                async with c.stream("POST", url, json=payload, headers=headers) as r:
                    async for chunk in r.aiter_raw():
                        yield chunk
        return StreamingResponse(relay(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-KnowTune-Upstream": name})
    try:
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as c:
            r = await c.post(url, json=payload, headers=headers)
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"上游「{name}」连不上：{exc}") from exc
    return Response(content=r.content, status_code=r.status_code,
                    media_type=r.headers.get("content-type", "application/json"),
                    headers={"X-KnowTune-Upstream": name})


@router.post("/api/models/keys/clear")
def clear_all_keys() -> dict:
    """清空所有内存密钥。存在的理由：密钥的唯一归宿是内存，那就得有个
    "现在就把它们全抹掉"的动作，而不是只能靠重启进程。"""
    from ... import secrets as secrets_mod

    gone = secrets_mod.names()
    secrets_mod.wipe_file()      # 连盘上的那份一起删，别留半拉子
    for n in state.cfg.generation.providers:
        services_mod.invalidate(n)
    logbus.note(f"已清空 {len(gone)} 个内存密钥：{', '.join(gone) or '-'}",
                level="WARNING", source="models")
    return {"ok": True, "cleared": gone}


class ActiveModel(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    # 可选：同时指定这个后端用哪个模型。"当前用什么"其实有两段——哪个后端、
    # 这个后端的哪个模型。以前只切后端，模型停在旧值上，用户改完发现"还是
    # 原来那个模型"，因为模型藏在后端的配置字段里，不像"当前"的一部分。
    model: str | None = None


@router.post("/api/models/active")
def set_active_model(req: ActiveModel) -> dict:
    """切换当前后端（可同时切模型），并立刻落盘。

    "设为当前"是一个明确的决定，不该像滑杆那样停在内存里等用户去点保存——
    重启一次又变回旧后端，人会以为是 bug。
    """
    try:
        settings_mod.apply(state.cfg, {"generation.provider": req.name})
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    picked = ""
    if req.model is not None:
        prov = state.cfg.generation.providers.get(req.name)
        want = (req.model or "").strip()
        if prov is not None and want and prov.model != want:
            prov.model = want
            picked = want
            modelcfg.persist(state.cfg)   # 模型名写进 models.yaml，重启后仍是它
    saved = settings_mod.save_local(state.cfg)
    state.invalidate_kb_cfg()
    # 换了后端/模型，上一次的探测结论不再代表"现在这套能不能用"
    services_mod.invalidate(req.name)
    logbus.note(
        f"当前生成后端切换为 {req.name}" + (f" · 模型 {picked}" if picked else ""),
        source="models")
    return {"ok": True, "active": req.name, "model": picked, "saved": saved}