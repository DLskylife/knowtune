"""前台问答站点（2026-10-28）。

这是给终端用户看的那一层：一个知识库 + 一个端口 + 一套语言，界面上只有提问框
和答案，没有知识库管理、没有参数面板、没有检索调试。它和后台**不共享任何静态
资源**，样式与文案都是另一套（维吾尔语 RTL）。

但它在**同一进程里**跑，直接复用后台已经加载好的检索器、嵌入模型和 LLM 客户端。
这不是偷懒：嵌入模型加载一次要好几十秒、占着几百 MB 显存/内存，前台若另起进程
就得再付一遍，而且两个进程各有一份向量缓存，答案还可能不一致。

问答流程与后台 `/api/ask` 同构但刻意不共用代码：后台那一版带会话预算、调试面板、
反馈 id、审计日志，前台只需要"检索 → 装填 → 流式 → 引用"。把两者强行合成一个
函数，任何一边的改动都会牵动另一边，那才是真正的冗杂。
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from .. import chatctx
from .. import lang as lang_mod
from .. import logbus
from ..generate import cite as cite_mod
from ..generate import evidence
from ..generate.prompt import build_context
from ..ingest.chunker import estimate_tokens
from ..roles import plan as role_plan, resolve as role_resolve
from . import instcfg
from .instcfg import default_copy

STATIC_DIR = Path(__file__).resolve().parent / "static"

# 维吾尔语回答时，模型偶尔会偷懒吐中文（资料是中文的）。这句兜底指令放在
# 用户问题后面再压一次，比只在系统提示里说一次的服从率高得多。
_LANG_TAIL = {
    "ug": "\n\n(جاۋابنى چوقۇم ئۇيغۇر تىلىدا يېزىڭ.)",
    "zh": "\n\n（回答必须用中文。）",
    "en": "\n\n(Answer in English.)",
}

# assistant 位置的续写起点。见 _stream_answer 里的说明。
_PREFILL = {"ug": "جاۋاب: "}


class SiteAsk(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    session_id: str | None = None
    history: list[dict] | None = None
    # 当前界面语言（右上角高亮的那颗）。答案语言跟着它走，不跟提问语言走：
    # 高亮维语却在下面看到一整段中文，是最难解释的一种不一致——用户会以为
    # 翻译坏了，其实是我们把"用哪种语言回答"交给了模型对提问的语种猜测。
    ui_lang: str = ""


class SiteFeedback(BaseModel):
    """前台「有用／无用」。字段与后台 /api/feedback 对齐。

    前台一直往 message 表写问答，却没有收集入口：数据攒了一堆，一条都标注
    不上。而这些恰恰是最真实的用户反馈——终端用户不会跑去后台点按钮。
    """

    message_id: int
    verdict: str = "up"
    comment: str = ""


def _sse(event: str, data: dict | str) -> str:
    payload = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n"


def _draft_messages(bundle, question: str, history: list[dict] | None,
                    draft_lang: str) -> list[dict]:
    """两段式里"先用资料语言写草稿"的消息。"""
    msgs = bundle.messages(question, history or [])
    tail = _LANG_TAIL.get(draft_lang, "")
    if tail and msgs:
        msgs[-1] = {**msgs[-1], "content": msgs[-1]["content"] + tail}
    return msgs


_COPY_KEYS = ("title", "subtitle", "welcome", "disclaimer")


def _content_lang(texts: list[Any], fallback: str = "") -> str:
    """按**文字内容**判语言，而不是按配置里那个 `lang` 字段。

    配置是手填的，文案也是手填的，两者之间没有任何机制保证一致——实测就
    撞上过 `lang: zh` 而标题/欢迎语/示例全是维吾尔语、另一套字段反过来是中
    文的情况。前端拿 `lang` 决定高亮哪颗按钮、拿文案渲染页面，于是"高亮
    中文、内容是维语"；点一下切换，内容换了、标签没换。这类错配不该靠人去
    发现，判据只能落在字符集上。
    """
    blob = " ".join(str(x) for x in texts if x)
    got = lang_mod.detect(blob)
    if got in ("ug", "zh", "en"):
        return got
    return fallback if fallback in ("ug", "zh", "en") else ""


def _fill_copy(base: dict[str, Any], lang: str) -> dict[str, Any]:
    """把一套文案补齐到"每种语言都有话说"，并把混进来的别的语言剔掉。

    缺的字段用内置默认值（与目标语言一致的那一份）填。不这样做的话，用户
    只填了标题、没填欢迎语，切到那套语言就会看到一片空白——比语言不对更糟，
    因为看起来像页面挂了。

    另一个坑是**字段之间**的语言混填：标题是维语、欢迎语却是中文。整套语言
    标签只能有一个，留着这种字段就是"标签说维语、正文一半中文"。所以逐字段
    再判一次，语言对不上的整段丢掉、用该语言的默认值补上——宁可换成一句通用
    文案，也不要在同一页里掺两种字。
    """
    default = default_copy(lang)
    out: dict[str, Any] = {}
    for key in _COPY_KEYS:
        val = str(base.get(key) or "").strip()
        if val and _content_lang([val], fallback=lang) != lang:
            val = ""
        out[key] = val or default.get(key, "")
    samples = [str(s).strip() for s in (base.get("samples") or []) if str(s).strip()]
    samples = [s for s in samples if _content_lang([s], fallback=lang) == lang]
    out["samples"] = samples or default.get("samples", [])
    return out


def _split_for_translate(text: str, limit: int = 300) -> list[str]:
    """按段落/句末标点把草稿切成小段，给"整段翻不动"时的分段重试用。

    整段一次要译文时，模型容易在最开始就把预算烧在思考上（实测一份 168 字
    的草稿能烧掉 2197 个 reasoning token）。切小之后每段单独计费、单独失败，
    坏一段不会连累整篇。
    """
    out: list[str] = []
    for para in (text or "").split("\n"):
        para = para.strip()
        if not para:
            continue
        if len(para) <= limit:
            out.append(para)
            continue
        buf = ""
        for ch in para:
            buf += ch
            if len(buf) >= limit and ch in "。！？；.!?;":
                out.append(buf)
                buf = ""
        if buf.strip():
            out.append(buf)
    return [p for p in out if p.strip()]


def _utcnow() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def build_site(spec, state: Any) -> FastAPI:
    """spec: `instcfg.InstanceSpec`；state: 后台的 `AppState`。

    这里的 `spec` 只是**启动那一刻的快照**，请求期一律走 `_cur()` 现读。

    以前整个 app 闭包捕获这一份快照：后台改个标题、换个翻译模型，都得把站点
    停掉再起一次才生效，于是「保存」按钮背后藏了一次 3 秒的断服。那些字段
    没有一个碰 socket，没有任何理由为它们重启；真正非重启不可的只有端口和
    监听地址（uvicorn 启动时就把绑定关系定死了）。
    """
    app = FastAPI(title=spec.title or spec.name, docs_url=None, redoc_url=None)

    name = spec.name
    _warned = False

    def _cur():
        """请求期现读配置。读坏了就退回启动时的快照，绝不能让一个写到一半的
        配置把整个站点打成 500。

        但**第一次失败必须留痕**：第一版这里光写了 `except Exception: return spec`，
        又忘了 `import instcfg`，于是每次请求都抛 NameError、每次都被吞掉、
        每次都安静地返回启动快照——表现是"保存成功、界面不变"，查了半天配置
        文件和进程，因为没有任何一处报错。
        """
        nonlocal _warned
        try:
            latest = instcfg.get(name)
        except Exception as exc:
            if not _warned:
                _warned = True
                logbus.note(f"站点 {name} 读配置失败，退回启动快照："
                            f"{type(exc).__name__}: {exc}", level="ERROR", source="sites")
            return spec
        return latest or spec

    def _langs(s) -> tuple[str, str]:
        lg = s.lang if s.lang in {"ug", "zh", "en"} else "ug"
        # 知识库内容的语言。与回答语言不同就必须先翻译问题再检索——中文嵌入模型
        # 编码不出维吾尔语的语义。导入了维吾尔语语料时把实例的 kb_lang 改成 ug，
        # 翻译这一步会自动跳过。
        kl = (getattr(s, "kb_lang", "") or "zh").lower()
        return lg, kl

    def _static(name_: str) -> Path:
        # 只放行固定几个文件名：目录穿越在这里没有任何收益
        return STATIC_DIR / name_

    # 和后台同一个教训：FileResponse 不带 Cache-Control，WebView 会启发式缓存
    # index.html/app.js，改版后用户拿到的还是旧界面。前台页面总共三个小文件，
    # 一律 no-cache（协商缓存命中就 304），正确性没有任何理由向零开销妥协。
    @app.middleware("http")
    async def no_stale_static(request: Request, call_next):
        resp = await call_next(request)
        # no-store，不是 no-cache：这轮用户就是拿旧页面在问"按钮在哪"。
        # 三个文件加起来不到 20KB、还都在本机，为省这点字节冒"改了没生效"的
        # 风险不划算。
        resp.headers["Cache-Control"] = "no-store"
        return resp

    def _provider(role: str, kb_cfg: Any, s=None) -> str:
        """按角色挑后端：站点 roles → 系统 roles → 站点 provider → 当前默认。

        每一级都可能指向一个已经改名或删掉的后端，roles.resolve 会逐层退让，
        最坏情况是退化成"不分工"，绝不会让站点 500。
        `s` 是本次请求读到的配置：换了翻译模型不该等重启才生效。
        """
        cur_ = s or _cur()
        return role_resolve(
            role,
            instance_roles=getattr(cur_, "roles", {}) or {},
            global_roles=getattr(state.cfg.generation, "roles", {}) or {},
            instance_provider=cur_.provider or "",
            default_provider=kb_cfg.generation.provider,
            known=getattr(state.cfg.generation, "providers", {}) or {},
        )

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(_static("index.html"))

    @app.get("/app.js")
    def js() -> FileResponse:
        return FileResponse(_static("app.js"), media_type="application/javascript")

    @app.get("/style.css")
    def css() -> FileResponse:
        return FileResponse(_static("style.css"), media_type="text/css")

    @app.get("/api/info")
    def info() -> dict:
        s = _cur()
        lang_, kb_lang_ = _langs(s)
        conn = state.db.conn()
        kb = conn.execute("SELECT id, name, description FROM kb WHERE id=?", (s.kb_id,)).fetchone()
        if kb is None:
            raise HTTPException(404, "实例绑定的知识库已不存在")
        docs = conn.execute(
            "SELECT COUNT(*) c FROM document WHERE kb_id=? AND status='ready'", (s.kb_id,)
        ).fetchone()["c"]
        chunks = conn.execute(
            "SELECT COUNT(*) c FROM chunk c JOIN document d ON d.id=c.doc_id"
            " WHERE d.kb_id=? AND c.generation=d.active_gen", (s.kb_id,)
        ).fetchone()["c"]
        main_copy_raw = {
            "title": s.title, "subtitle": s.subtitle, "welcome": s.welcome,
            "samples": s.samples, "disclaimer": s.disclaimer,
        }
        alt_raw = {k: v for k, v in (s.alt or {}).items() if k != "lang"}
        # 语言标签由内容决定，不由配置里的 lang 决定。详见 _content_lang：
        # 配置和文案都靠人手填，一旦填反，界面上就会出现"高亮中文、正文维语"，
        # 而且点切换只换内容不换高亮——用户完全没有办法自己纠正。
        main_lang = _content_lang(
            [s.title, s.welcome, s.subtitle], fallback=lang_)
        alt_lang = _content_lang([alt_raw.get("title"), alt_raw.get("welcome")],
                                 fallback=str((s.alt or {}).get("lang") or "").lower())
        # 两套内容判成同一种语言 = 这颗切换按钮点下去什么都不会变。此时把
        # 没填全的那边用内置文案补齐（_fill_copy），保证"点一下就真的换语言"。
        if not alt_lang or alt_lang == main_lang:
            alt_lang = "zh" if main_lang != "zh" else "ug"
        main_copy = _fill_copy(main_copy_raw, main_lang)
        alt_copy = _fill_copy(alt_raw, alt_lang)
        # 知识库显示名也是界面文字。库里存的是中文名，切到维语界面还挂着
        # 四个汉字，比按钮文字没翻更显眼——这套名字允许站点单独配。
        main_name = str(getattr(s, "kb_name", "") or "").strip() or kb["name"]
        alt_name = str(alt_raw.get("kb_name") or "").strip() or kb["name"]
        return {
            "name": s.name,
            "lang": main_lang,
            "dir": "rtl" if main_lang == "ug" else "ltr",
            # 配置里声明的那个值：只在排查"为什么判定和填写不一样"时有意义。
            "site_lang": lang_,
            "alt": {"lang": alt_lang, "dir": "rtl" if alt_lang == "ug" else "ltr",
                    "kb_name": alt_name, **alt_copy},
            "kb_id": s.kb_id,
            "kb_name": main_name,
            "kb_name_raw": kb["name"],
            "kb_desc": kb["description"] or "",
            "docs": int(docs),
            "chunks": int(chunks),
            "answer_lang": main_lang,
            "query_lang": main_lang,
            "kb_lang": kb_lang_,
            "answer_mode": getattr(s, "answer_mode", "direct"),
            "cross_lingual": main_lang != kb_lang_,
            "roles": role_plan(
                instance_roles=getattr(s, "roles", {}) or {},
                global_roles=getattr(state.cfg.generation, "roles", {}) or {},
                instance_provider=s.provider or "",
                # 必须用**这个知识库**的有效配置，不能用全局配置：知识库可以
                # 单独钉一个后端（kb.config_json 的 generation.provider），
                # 用全局值来算就会"界面显示百炼、实际连本地端口"，页面上一片
                # 正常、问答全失败，排查时人会先怀疑密钥。
                default_provider=state.effective_cfg(s.kb_id).generation.provider,
                known=getattr(state.cfg.generation, "providers", {}) or {},
            ),
            **main_copy,
        }

    @app.post("/api/ask")
    def ask(req: SiteAsk) -> StreamingResponse:
        trace_id = str(uuid.uuid4())
        s = _cur()
        lang, kb_lang = _langs(s)
        kb_cfg = state.effective_cfg(s.kb_id)
        # 同一个后端干所有活儿是默认值，不是原则：翻译和输出可以用不同的模型。
        # 这里各取一次，/api/info 里能看到实际用了谁。
        gen_provider = _provider("generate", kb_cfg, s)
        tr_provider = _provider("translate", kb_cfg, s)
        q_lang = lang_mod.detect(req.question)
        # 答案语言跟**界面**语言，不跟提问语言。
        # 以前是"中文问就中文答"，理由是省一次翻译；代价是界面明明高亮着
        # 维语，答案却是一整段中文——翻译一旦失败，用户连"是不是没翻"都
        # 判断不了。现在右上角是什么语言，答案就是什么语言，这是一条对用户
        # 可见的承诺，不该为了省几秒让位。
        ui_lang = (req.ui_lang or "").strip().lower()
        site_lang = _content_lang([s.title, s.welcome, s.subtitle], fallback=lang)
        if ui_lang in ("ug", "zh", "en"):
            answer_lang = ui_lang
        elif q_lang in ("ug", "zh", "en"):
            answer_lang = q_lang
        else:
            answer_lang = site_lang or lang

        session_id = req.session_id
        if session_id:
            conn0 = state.db.conn()
            exists = conn0.execute("SELECT id FROM session WHERE id=?", (session_id,)).fetchone()
            if not exists:
                now = _utcnow()
                conn0.execute(
                    "INSERT INTO session (id, kb_id, title, created_at, updated_at)"
                    " VALUES (?,?,?,?,?)",
                    (session_id, s.kb_id, (req.question or "")[:30], now, now),
                )

        def stream() -> Iterator[str]:
            try:
                yield from _impl()
            except Exception as exc:
                # HTTP 200 已经发出去了，不兜住前端只会一直转圈
                yield _sse("error", {"message": f"{type(exc).__name__}: {exc}",
                                     "trace_id": trace_id})

        def _impl() -> Iterator[str]:
            conn = state.db.conn()
            two_stage = (
                getattr(s, "answer_mode", "direct") == "translate"
                and answer_lang != kb_lang
            )
            dst_label = {"ug": "维吾尔语", "zh": "中文", "en": "English"}.get(
                answer_lang, answer_lang)
            if not state.embedder.is_loaded:
                w = state.warm
                if w["state"] in ("pending", "loading"):
                    msg = "بىلىم ئامبىرى تېخى تەييار ئەمەس، بىردەم ساقلاڭ." \
                        if lang == "ug" else "知识库索引还在加载，请稍候再试。"
                else:
                    msg = (w.get("error") or state.embedder.error or "未知原因")
                yield _sse("error", {"message": msg, "trace_id": trace_id})
                return

            timings: dict[str, int] = {}
            # ① 跨语言检索：维语问题先译成中文再检索（嵌入模型是中文的）
            query, terr = lang_mod.translate_query(
                state.llm, req.question, src=q_lang, dst=kb_lang, provider=tr_provider
            )
            yield _sse("query", {"original": req.question, "used": query,
                                 "translated": query != req.question,
                                 # 排查用：谁翻的、从哪种语言翻到哪种。"没翻译"
                                 # 有三种截然不同的原因（不需要翻、模型报错、
                                # 译文被判废），以前它们在这里长得一模一样。
                                 "src": q_lang, "dst": kb_lang,
                                 "provider": tr_provider,
                                 "error": terr})

            t0 = time.perf_counter()
            result = state.engine.search(
                conn, query, kb_id=s.kb_id, cfg=kb_cfg,
            )
            timings.update(result.timings)

            # ② 检索证据分档：把"这批资料有多可信"显式告诉模型。
            # 命中硬 → 严格照资料答；命中差 → 允许用自身知识，但必须标明哪句是资料。
            ref = result.refusal
            top_score = getattr(ref, "top_score", None) if ref else None
            # 前台没有"轮数"概念，预算是唯一闸门：超出就从最早一轮整轮丢，
            # 避免长回答把本轮资料挤成空【资料】段。
            hist = chatctx.trim_history(
                list(req.history or []), budget=int(kb_cfg.chat.history_token_budget)
            )
            grade = evidence.grade_of(
                top_score,
                min_score=kb_cfg.refusal.min_score,
                mid_ratio=kb_cfg.evidence.mid_ratio,
            )
            soft_refused = bool(ref and ref.refused)

            # 这里刻意不做硬拒答。用户要的是答案，不是一句"资料里没有"——
            # 召回不够就交给模型自己的知识去答，但必须把"哪句是资料、哪句是通用知识"
            # 摆在明面上（档位指令在下面 guidance 里下发，界面上也有徽标）。
            # 硬拒答只保留在后台 /api/ask 和评测里：标定 min_score 需要一个能
            # 明确说"不"的通道，而面向用户的前台不该有。

            bundle = build_context(
                result.candidates,
                question=req.question,
                token_budget=kb_cfg.generation.context_token_budget,
                min_context_chars=kb_cfg.generation.min_context_chars,
                prompt_version=state.cfg.prompt_version,
                history_text_tokens=sum(
                    estimate_tokens(h.get("content", "")) for h in hist
                ),
                # 两段式时草稿用资料语言写，系统提示也得是那一版的——
                # 拿维语提示去要中文草稿只会两头不靠。
                # 档位要进系统提示：光写在用户消息里，压不过"禁止外部知识"那条硬约束
                answer_lang=(kb_lang if two_stage else answer_lang),
                evidence=grade,
                # 提示词按"这个角色实际会用到的后端"取：站点的 generate 角色可能
                # 绑在云端、系统默认却是本地 7B，两套提示词不能混用。
                provider=gen_provider,
            )
            guide = evidence.guidance(grade, lang=(kb_lang if two_stage else answer_lang))

            def with_guide(msgs: list[dict]) -> list[dict]:
                if not guide or not msgs:
                    return msgs
                last = msgs[-1]
                return [*msgs[:-1],
                        {**last, "content": f"{last['content']}\n\n{guide}"}]

            yield _sse("meta", {
                "trace_id": trace_id, "mode": "answer",
                "citations": [asdict(c) for c in bundle.citations],
                "context_used": bundle.used, "context_dropped": bundle.dropped,
                "timings_ms": timings, "degraded": result.degraded,
                "query_lang": q_lang, "answer_lang": answer_lang,
                "answer_mode": getattr(s, "answer_mode", "direct"),
                "two_stage": two_stage,
                "evidence": grade,
                "evidence_top": top_score,
                "evidence_min": kb_cfg.refusal.min_score,
                # refused=True 表示检索确实没达标（不再是拒答信号，只是"这批资料不够硬"）
                "soft_refused": soft_refused,
                "refusal_reason": (getattr(ref, "reason", None) if soft_refused else None),
            })
            if two_stage:
                # 先写草稿再翻译，中间有十几秒没有任何字出来，不说一句就是卡死
                yield _sse("stage", {"stage": "drafting"})

            # ② 生成
            acc: list[str] = []
            draft = ""
            error: str | None = None
            looped = False
            # 答案没翻成目标语言 = 用户最终看到的是一段看不懂的中文。
            # 这个标记以前不存在，所以"翻译失败、退回中文草稿"这条路上
            # 前端完全不知情，用户只能自己猜为什么满屏汉字。
            tr_failed = False
            gen_started = time.perf_counter()

            if two_stage:
                # 先按资料语言（中文）组织答案，再整段译成维吾尔语。
                # 小模型"读中文资料写维吾尔语"会跑偏甚至把指令也吐出来，
                # 而"翻译一段已经写好的中文"是它更擅长的事——代价是多一轮生成。
                draft_msgs = with_guide(
                    _draft_messages(bundle, req.question, hist, kb_lang))
                # 空返回要重试：云端后端偶尔给一个"正常、但一个字都没有"的
                # 响应（不是异常），complete 的内部重试根本不触发。此时如果
                # 直接往下走，两段式会被整个跳过，用户拿到一个空气泡，
                # 而 done 事件里还写着 answer_translated=true——"成功"得离谱。
                for _try in range(2):
                    try:
                        draft = state.llm.complete(
                            draft_msgs, provider=gen_provider, temperature=0.2
                        ).text or ""
                    except Exception as exc:
                        draft = ""
                        error = f"{type(exc).__name__}: {exc}"
                    if draft.strip():
                        error = None
                        break
                if not draft.strip() and not error:
                    error = "生成返回空内容（已重试一次）"
                if draft.strip():
                    # 中文草稿随事件下发：前端在中文界面下可以立刻显示它，
                    # 不用盯着一段维吾尔语译文干等（翻译要再花十几秒）。
                    yield _sse("stage", {"stage": "translating", "draft_chars": len(draft),
                                         "draft": draft})
                    tr_msgs = [
                        {"role": "system", "content": (
                            f"你是翻译器。把中文答案翻译成{dst_label}。"
                            "要求：只输出译文；保留 [1]、[2] 这类来源编号原样不动；"
                            "不要复述问题，不要添加原文没有的内容，不要解释。"),
                         },
                        {"role": "user", "content": draft},
                    ]
                    # 翻译预算按"思考 + 译文"给，而且思考那部分非常贵：
                    # 实测 168 个汉字的草稿，deepseek-flash 内部推理烧掉 2197
                    # 个 token（≈ 字数 ×13）。2048 的预算下正文被截成 301 字，
                    # 4096 才拿到完整 658 字译文。给不够时思考先把额度吃光，
                    # 流里一个 content 都不会来——不报错、不抛异常，前端只能
                    # 拿到中文草稿。所以这里是 12 倍 + 兜底 4096，上限 8192。
                    tr_budget = min(8192, max(4096, len(draft) * 12))
                    # 翻译整段也可能失败（网络抖动、SSL、限流），而它一旦失败，
                    # 用户拿到的就是一整段中文。一个字都没出时重试一次成本很低；
                    # 已经出了字就不能重来，否则答案会被接成两遍。
                    for attempt in (0, 1):
                        if acc:
                            break
                        try:
                            for i, piece in enumerate(
                                    state.llm.stream(tr_msgs, provider=tr_provider,
                                                     max_tokens=tr_budget,
                                                     reasoning="none")):
                                acc.append(piece)
                                yield _sse("delta", {"text": piece})
                                if i % 8 == 7 and lang_mod.repeating("".join(acc)):
                                    looped = True
                                    break
                            # 空流不算成功：云端后端偶尔回一个"200、零 token"
                            # 的响应，不抛异常。以前这里无条件 break，于是
                            # 一次空流就把中文草稿交给了维语用户。
                            if acc:
                                break
                        except Exception as exc:
                            error = f"{type(exc).__name__}: {exc}"
                            if acc:
                                # 已经吐出的是译文，只是可能不完整。保留它，
                                # 别标成"未翻译"——那会让提示语说反话。
                                break
                            if attempt == 1:
                                tr_failed = True
                                break
                    if not "".join(acc).strip() and draft.strip():
                        # 流式两次都没出字，最后换非流式再赌一次：走的是另一条
                        # 代码路径（整包返回而不是 SSE），空流/半截的情况常常
                        # 在这里就过去了。整段一次到，晚十几秒，但至少是译文。
                        try:
                            one = state.llm.complete(
                                tr_msgs, provider=tr_provider, temperature=0.2,
                                max_tokens=tr_budget, reasoning="none",
                            ).text or ""
                        except Exception as exc:
                            one = ""
                            error = f"{type(exc).__name__}: {exc}"
                        if one.strip():
                            acc = [one]
                            yield _sse("delta", {"text": one})
                    if not "".join(acc).strip() and draft.strip():
                        # 第四招：整段翻不动就切段翻。整段一次要译文时，模型
                        # 容易在最开始把预算烧在思考上；切成三百字一段，每段
                        # 单独计费、单独失败，坏一段不连累整篇。走到这里说明
                        # 已经不是偶发空响应，而是这一整段的体量压垮了预算。
                        segs = _split_for_translate(draft)
                        got_segs: list[str] = []
                        ok_segs = 0
                        for seg in segs:
                            seg_msgs = [tr_msgs[0], {"role": "user", "content": seg}]
                            try:
                                out = state.llm.complete(
                                    seg_msgs, provider=tr_provider, temperature=0.2,
                                    max_tokens=min(4096, max(1200, len(seg) * 12)),
                                    reasoning="none",
                                ).text or ""
                            except Exception as exc:
                                out = ""
                                error = f"{type(exc).__name__}: {exc}"
                            out = out.strip()
                            # 判"译出来了"用语言检测，不用"非空"：模型完全
                            # 可能把中文原样吐回来，那也是非空的。
                            if out and lang_mod.detect(out) != kb_lang:
                                ok_segs += 1
                                got_segs.append(out)
                            else:
                                got_segs.append(seg)      # 这段没成，留原文
                        if ok_segs:
                            one = "\n\n".join(got_segs)
                            acc = [one]
                            yield _sse("delta", {"text": one})
                            if ok_segs < len(segs):
                                error = error or "部分段落未译出"
                        if not "".join(acc).strip():
                            # 四招全落空，才把中文草稿交给用户：语言不对，
                            # 也比"什么都没答"强，何况来源还挂在下面。
                            # tr_failed 让前端把话说清楚（"以下是原文"），
                            # 而不是让人对着一屏汉字猜是不是翻译坏了。
                            acc = [draft]
                            tr_failed = True
                            yield _sse("delta", {"text": draft})
            else:
                messages = with_guide(bundle.messages(req.question, hist))
                if answer_lang:
                    tail = _LANG_TAIL.get(answer_lang, "")
                    if tail and messages:
                        messages[-1] = {**messages[-1],
                                        "content": messages[-1]["content"] + tail}
                try:
                    for i, piece in enumerate(
                            _stream_answer(messages, gen_provider, answer_lang)):
                        acc.append(piece)
                        yield _sse("delta", {"text": piece})
                        # 本地小模型答维吾尔语时很容易滑进"同一句反复输出"，
                        # 直到撞上 max_tokens。转圈几十秒最后拿到一段复读机
                        # 产物，比截在重复处更糟——这里检测到复读就停并标注。
                        if i % 8 == 7 and lang_mod.repeating("".join(acc)):
                            looped = True
                            break
                except Exception as exc:
                    error = f"{type(exc).__name__}: {exc}"
            timings["gen_ms"] = int((time.perf_counter() - gen_started) * 1000)

            answer = "".join(acc)
            if error and not answer:
                yield _sse("error", {"message": error, "trace_id": trace_id})
                return

            report = cite_mod.verify(answer, len(bundle.citations))
            citations = [asdict(c) for c in bundle.citations]
            msg_id = None
            if session_id:
                msg_id = _save(conn, session_id, req.question, answer, trace_id, citations)
            yield _sse("done", {
                "trace_id": trace_id, "refused": False,
                "citations": citations, "answer": answer,
                "cite_coverage": report.coverage,
                "cite_blanket": report.blanket,
                "cite_blanket": report.blanket,
                "message_id": msg_id,
                "invalid_ids": report.invalid_ids,
                "loop_truncated": looped,
                "evidence": grade,
                # 两段式下答案是否真的译成了目标语言。False = 下面这段是中文
                # 原稿（翻译没成），前端据此提示"以下是原文，翻译未完成"。
                # 草稿本身为空时整段两段式都没发生，这里必须是 None 而不是
                # True——否则就是"答案是空的，但报告说翻译很成功"。
                "answer_translated": (not tr_failed) if (two_stage and draft.strip()) else None,
                # 两段式时的中文原稿：答案看不懂时，它是唯一能判断
                # "检索错了"还是"翻译错了"的依据。
                "draft": draft if two_stage else None,
                "degraded": result.degraded + ([f"llm_error:{error}"] if error else [])
                + (["generation_loop"] if looped else []),
                "timings_ms": timings,
            })

        def _stream_answer(msgs: list[dict], prov: str, want_lang: str):
            """维吾尔语回答时，用 assistant 前缀把模型往维语上"拽"一把。

            资料是中文的，7B 级模型看着满屏中文几乎必然吐中文——只在系统提示
            里说"用维吾尔语"不够，它读到的是几百个汉字。在 assistant 位置预填
            一句维语开头，等于把续写的起点钉在维语上，服从率明显更高。

            不是所有服务端都认末尾的 assistant 消息（OpenAI 官方会忽略，
            个别实现直接 400），所以失败且一个字都还没出时就退回普通调用。
            """
            prefill = _PREFILL.get(want_lang)
            if not prefill:
                yield from state.llm.stream(msgs, provider=prov)
                return
            got: list[str] = []
            for attempt in (msgs + [{"role": "assistant", "content": prefill}], msgs):
                try:
                    for piece in state.llm.stream(attempt, provider=prov):
                        got.append(piece)
                        yield piece
                    return
                except Exception:
                    if got:      # 已经出字了再重试只会把答案接成两遍
                        raise
                    continue     # 一次都没出 = 服务端不认 prefill，退回
            return

        return StreamingResponse(
            stream(), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Trace-Id": trace_id,
                     "X-Accel-Buffering": "no"},
        )

    def _save(conn, session_id: str, question: str, answer: str, trace_id: str,
              citations: list[dict]) -> int:
        """落库一轮问答，返回 assistant 消息 id。

        返回 id 是为了让前台也能做「有用／无用」：反馈接口按 message_id 关联，
        没有它，前台写进 message 表的这批数据就永远标注不上——数据在、入口不在，
        是这轮补的最亏的一处。
        """
        now = _utcnow()
        conn.execute(
            "INSERT INTO message (session_id, role, content, trace_id, created_at)"
            " VALUES (?,?,?,?,?)", (session_id, "user", question, trace_id, now))
        cur = conn.execute(
            "INSERT INTO message (session_id, role, content, trace_id, citations_json,"
            " created_at) VALUES (?,?,?,?,?,?)",
            (session_id, "assistant", answer, trace_id,
             json.dumps(citations, ensure_ascii=False), now))
        conn.execute("UPDATE session SET updated_at=? WHERE id=?", (now, session_id))
        return int(cur.lastrowid)

    @app.post("/api/feedback")
    def feedback(req: SiteFeedback) -> dict:
        """记一条反馈。

        `INSERT OR REPLACE` 与后台同语义：同一条消息改主意（先点无用又点有用）
        应该覆盖而不是堆两条，否则统计里一条回答会被算两次。
        """
        conn = state.db.conn()
        # 先确认这条消息还在：前端的历史是 localStorage 里的一份副本，库里
        # 的 message 可能已经被后台清会话删掉了。不校验的话外键约束会抛出
        # 裸 500，用户那边只会看到"网络连接中断"，而真正的原因是"这条早没了"。
        if not conn.execute(
            "SELECT 1 FROM message WHERE id=?", (req.message_id,)
        ).fetchone():
            raise HTTPException(404, "这条回答已不存在（会话可能被清理），反馈未记录")
        conn.execute(
            "INSERT OR REPLACE INTO feedback (message_id, verdict, comment, created_at)"
            " VALUES (?,?,?,?)",
            (req.message_id, req.verdict, req.comment, _utcnow()),
        )
        return {"ok": True}

    return app
