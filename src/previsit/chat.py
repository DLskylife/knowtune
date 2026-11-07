"""预问诊 = 一个外壳：一段提示词 + 模型本身。

2026-11-07 起，判断层（危险闸门、分诊规则、槽位状态机、语义召回、图谱追问、
病历/摘要生成）整体移除。原因很直接：那一层是一套手写规则，它决定了问什么、
什么时候收网、最后报哪个科室——而这些恰恰是最该由模型自己决定的事。规则越多，
问诊越像填表；模型换了、场景换了，规则却还在。

壳子现在只做三件事：

1. 开一场会话（一行 session、一串 turn，SQLite 存着）；
2. 把历史连同 `config/previsit.yaml` 的 `prompts.chat` 交给模型；
3. 把模型这句话存回去、返回给界面。

没有槽位、没有分诊、没有病历、没有兜底模板：模型说不出来，就如实报错，
而不是拿一套规则拼一句听起来对的话。
"""

from __future__ import annotations

import logging
import time
from typing import Any

from .store import Store

log = logging.getLogger("previsit")

# 历史长度上限。本地 7B 的上下文是 4096 token（含提示词），把整场对话都塞
# 进去必然爆——按字符截断比按轮数稳：患者一句话能写两百字，也能写十个字。
MAX_HISTORY_CHARS = 2400
MAX_HISTORY_TURNS = 24

FALLBACK_UNAVAILABLE = "预问诊服务暂时不可用，请稍后再试或直接到分诊台就诊。"

# 维吾尔语用阿拉伯字母书写（U+0600–U+06FF）。判断"这句要不要翻译"和
# "翻译回来的东西能不能用"都靠它。
_LETTER_LO = 0x0620
_LETTER_HI = 0x064A
_EXTRA_LO = 0x06D5
_EXTRA_HI = 0x06FF


def _letters(text: str) -> int:
    """数阿拉伯字母的个数。标点（، 等）、数字、空格都不算。"""
    n = 0
    for ch in text or "":
        if _LETTER_LO <= ord(ch) <= _LETTER_HI or _EXTRA_LO <= ord(ch) <= _EXTRA_HI:
            n += 1
    return n


def _cjk(text: str) -> int:
    return sum(1 for ch in (text or "") if "\u4e00" <= ch <= "\u9fff")


def looks_uyghur(text: str) -> bool:
    """至少两个阿拉伯字母才算维语。防止一句"，。"或"120"被当成维语去翻。"""
    return _letters(text) >= 2


def _clean_translation(text: str) -> str:
    """去掉模型爱加的那些壳：引号、书名号、"译文："前缀、Markdown 星号。"""
    out = (text or "").strip()
    out = out.strip("\"'“”‘’《》「」").strip()
    for prefix in ("译文：", "译文:", "翻译：", "翻译:", "维吾尔语：",
                   "维吾尔语:", "Uyghur:", "Translation:", "**"):
        if out.startswith(prefix):
            out = out[len(prefix):].strip()
    return out.strip("*` \n\t")


def _acceptable(direction: str, out: str) -> bool:
    """译文合格吗。

    zh2ug：必须有维吾尔字母，且几乎不能剩汉字（剩三个以上基本是半翻，
    患者看到的就是"了解了، 疼痛..."这种混排）。
    ug2zh：必须出汉字，否则等于没翻。
    """
    if not out:
        return False
    if direction == "zh2ug":
        return _letters(out) >= 2 and _cjk(out) < 3
    return _cjk(out) >= 1


class ChatError(RuntimeError):
    """模型没接上 / 调用失败。壳子不兜底，交给调用方报 503。"""


class ChatEngine:
    """接口与旧引擎保持一致（start / reply / view / apply_config），
    面板和路由那一层不用改。"""

    def __init__(self, pv: dict[str, Any], store: Store, llm: Any = None,
                 translator: Any = None, translator_factory: Any = None) -> None:
        self.pv = pv
        self.store = store
        self.llm = llm
        # translator：维语会话专用（7B 用中文推理 + 外部模型翻译）。为 None 时
        # 维语会话**报错**，不退回中文——维语界面上蹦中文等于没做完。
        self.translator = translator
        # 缺了就现场补一个的工厂 `fn(pv, llm) -> translator|None`。为什么要有：
        # 引擎可能由宿主注入（build_engine）、被 reload 重建、或装配那次刚好
        # 失败——凡是"只在某一处挂一次"的依赖，迟早会有一次没挂上，而那一次
        # 的表现就是患者看到中文。这里让它在每个 ug 请求前自检。
        self.translator_factory = translator_factory
        self.translator_error = ""
        self.last_error: str | None = None
        # 这两个属性只留给参数面板的「运行时」那一行；壳子里没有槽位和域。
        self.slots: list[dict[str, Any]] = []
        self.domains: list[dict[str, Any]] = []

    # ---------------------------------------------------------------- 配置
    @property
    def provider(self) -> str:
        return str(self.pv.get("provider") or "")

    @property
    def model(self) -> str:
        return str(getattr(self.llm, "model", "") or self.pv.get("model") or "")

    @property
    def timeout_s(self) -> float:
        try:
            return float(self.pv.get("timeout_s") or 20)
        except (TypeError, ValueError):
            return 20.0

    def apply_config(self, pv: dict[str, Any]) -> None:
        """面板保存后灌新配置。模型客户端由调用方重建（端点/模型可能变了）。"""
        self.pv = pv

    def set_translator(self, translator: Any) -> None:
        """维语翻译模型（外部）。参数面板保存不换它，所以单独一个 setter。"""
        self.translator = translator
        self.translator_error = "" if translator else "未装配"

    def _translator(self) -> Any:
        """拿当前的翻译客户端；没有就按工厂现场补一个。

        「没有」在维语会话里不是降级，是故障：所以这里要留痕（translator_error），
        让参数面板和日志都能看出到底哪一步断了。"""
        if self.translator is None and callable(self.translator_factory):
            try:
                self.translator = self.translator_factory(self.pv, self.llm)
            except Exception as exc:            # 工厂自己炸了也别把问诊带崩
                self.translator = None
                self.translator_error = f"{type(exc).__name__}: {exc}"
                log.warning("预问诊翻译客户端装配失败：%s", self.translator_error)
            else:
                if self.translator is None:
                    self.translator_error = "没有可用的翻译端点"
        return self.translator

    # ---------------------------------------------------------------- 翻译
    def _translate(self, text: str, direction: str) -> str:
        """direction: "zh2ug" 中文→维吾尔语；"ug2zh" 维吾尔语→中文。

        返回合格译文；拿不到就返回空串，由调用方**报错**（不是退回中文）。
        翻译链可以有好几个客户端（显式模型 + 网关默认模型），一个给不出合格
        译文就换下一个——半翻、回中文、回原文都算不合格。"""
        text = (text or "").strip()
        if not text:
            return ""
        tr = self._translator()
        if tr is None:
            self.last_error = f"translate: {self.translator_error or '没有翻译客户端'}"
            return ""
        if direction == "zh2ug":
            sys_p = ("你是一个严谨的医学翻译。把下面这段中文翻译成维吾尔语"
                     "（使用阿拉伯字母书写），只输出译文，不要任何解释、"
                     "不要引号、不要 Markdown 格式：\n\n")
        else:
            sys_p = ("你是一个严谨的翻译。把下面这段维吾尔语（阿拉伯字母）"
                     "翻译成中文，只输出译文，不要任何解释、不要引号：\n\n")

        clients = list(getattr(tr, "clients", None) or [tr])
        last: Exception | None = None
        # 两轮整链：第一轮常撞上"服务刚重启、网关还没开始监听"这种瞬时空窗
        # （连接被拒，不是超时，代价只有几十毫秒）。隔一下再来一轮就能过，
        # 否则患者刚点进来就收到"服务暂时不可用"。
        for attempt in range(2):
            for client in clients:
                for _ in range(2):
                    try:
                        out = client.complete(
                            [{"role": "system", "content": sys_p},
                             {"role": "user", "content": text}],
                            temperature=0.1, max_tokens=800,
                        )
                    except Exception as exc:
                        last = exc
                        continue
                    out = _clean_translation(out or "")
                    if _acceptable(direction, out):
                        self.translator_error = ""
                        return out
                    last = ValueError(f"译文不合格：{(out or '')[:60]}")
            if attempt == 0:
                time.sleep(0.8)
        self.translator_error = f"{type(last).__name__}: {last}" if last else "空译文"
        self.last_error = f"translate:{direction}: {self.translator_error}"
        log.warning("预问诊翻译失败（%s）：%s ← %r", direction,
                    self.translator_error, text[:60])
        return ""

    @staticmethod
    def normalize_lang(value: str | None) -> str | None:
        """把界面发来的语言码收敛成 ug / zh / en。

        为什么要收敛：`lang == "ug"` 这种精确比较，碰到 "UG"、"ug-UY"、
        "ug_CN" 就整段跳过翻译——表现正是维语界面突然回中文。这层不做，
        前端换个写法就又炸一次。"""
        v = (value or "").strip().lower().replace("_", "-")
        if not v:
            return None
        head = v.split("-")[0]
        if head in ("ug", "uy", "uig"):
            return "ug"
        if head in ("en",):
            return "en"
        if head in ("zh", "cn", "chs"):
            return "zh"
        return v

    def system_prompt(self, lang: str | None = None) -> str:
        """系统提示。作答语言跟随界面语言：
        - zh（默认）→ 中文角色提示；
        - en → 整段英语翻译（本地 7B 也跟得住英文指令）；
        - ug → 中文角色提示（见下）。

        ug 的设计：维语会话走"本地 7B 用中文推理 + 外部翻译模型翻译"链路。7B 在
        中文上最强、且看不懂整段维吾尔语系统指令（实测整段维语提示会退回中文甚至
        胡说），所以 ug 也喂中文系统提示，维吾尔语由 translator 在边界处翻译。
        这样患者看到的是维吾尔语，但 7B 始终在它最稳的语言上工作。"""
        prompts = self.pv.get("prompts") or {}
        lang = self.normalize_lang(lang)
        zh = str(prompts.get("chat") or "").strip()
        en = str(prompts.get("chat_en") or "").strip()
        if not zh:
            zh = (
                "你是门诊预问诊助手。通过一问一答了解患者的不适：每次只问一个问题，"
                "先问最关键的（部位、性质、持续多久、伴随症状、既往病史、过敏史、用药）。"
                "口语化、一句话，不要列清单；不下诊断、不判断病情轻重、不推荐药物；"
                "患者说过的不要再问。信息够了就用两三句话复述了解到的情况，"
                "并建议去哪个科室、要不要尽快就医。"
            )
        if lang == "en" and en:
            return en
        if lang == "en" and not en:
            return zh + "\n\nAnswer in English."
        return zh

    # ---------------------------------------------------------------- 会话
    def start(self, complaint: str = "", profile: dict[str, Any] | None = None,
              *, lang: str | None = None) -> dict[str, Any]:
        """开一场会话。

        `complaint` 为空 = 患者刚点进来、还没说话：只建会话，**不调模型**。
        开场问候是界面上的固定文案（前端 `t.pvHello`，和另外两个板块的欢迎语
        同一个来源）；让模型现说第一句，患者面对的是"空白页 + 转圈"多等两次
        调用（生成一次、翻译一次），而那句话本来就是写死的。

        `complaint` 有值 = 患者第一句就是主诉，建会话后直接接着问。
        """
        lang = self.normalize_lang(lang) or "zh"
        chief = (complaint or "").strip()
        self._translator()                    # 顺手完成懒装配，裸引擎也能自愈
        sid = self.store.create(chief=chief, empty_slots={}, lang=lang)
        if not chief:
            sess = self.store.get(sid)
            if sess is None:                  # 理论上不会发生；真发生了别吞
                raise ChatError("会话创建失败")
            return self.view(sess)
        # 患者这句是什么语言，就按什么语言记；内部一律留中文给 7B。
        said, internal, ok = self._intake_text(chief)
        self.store.add_turn(sid, "user", chief, content_int=internal,
                            lang=said, translated=ok)
        # 首答由模型说：壳子里不存任何现成问法。
        self._say(sid, chief, store_nudge=False, lang=lang)
        sess = self.store.get(sid)
        if sess is None:                      # 理论上不会发生；真发生了别吞
            raise ChatError("会话创建失败")
        return self.view(sess)

    def _intake_text(self, text: str) -> tuple[str, str, bool]:
        """患者这句话 → (它是什么语言, 喂给 7B 的中文, 是否拿到了译文)。

        维语输入必须译成中文再进上下文（7B 读不懂维语）；译不出来就退回原文，
        不能因此让整场问诊报错——问答板块的检索翻译失败也是"按原文继续"，
        界面上挂一条本地化提示就够了。
        """
        if not looks_uyghur(text):
            return "zh", text, True
        internal = self._translate(text, "ug2zh")
        if internal:
            return "ug", internal, True
        return "ug", text, False

    def reply(self, sid: str, message: str, *, lang: str | None = None) -> dict[str, Any]:
        sess = self.store.get(sid)
        if sess is None:
            raise KeyError(sid)
        # **作答语言按本次请求走**，和问答板块完全一致（那边 `answer_lang` 也是
        # 每次请求一算，不记在会话上）。以前的写法是"会话语言优先"：界面切了
        # 语言也会被会话按回去，只能靠"换语言就重开一场"绕——那等于给预问诊
        # 单开一套语言系统，和另外两个板块对不上，也把患者的病史一起清掉了。
        # 现在每条 turn 自带 lang，界面按当前语言逐条挑文本（paintAnswer 同款），
        # 切语言只是重画，不重开、不丢内容。
        lang = (self.normalize_lang(lang)
                or self.normalize_lang(sess.get("lang")) or "zh")
        text = (message or "").strip()
        if not text:
            return self.view(sess)
        said, internal, ok = self._intake_text(text)
        self.store.add_turn(sid, "user", text, content_int=internal,
                            lang=said, translated=ok)
        # 会话上的 lang 只留作兜底：前端漏发 ui_lang 时才用它。
        self.store.update(sid, lang=lang)
        self._say(sid, text, store_nudge=False, lang=lang)
        sess = self.store.get(sid)
        if sess is None:
            raise KeyError(sid)
        return self.view(sess)

    def view(self, sess: dict[str, Any]) -> dict[str, Any]:
        """界面只要两样东西：流水和会话 id。state 恒为 open——壳子没有收网。"""
        return {
            "session_id": sess.get("id", ""),
            "state": "open",
            "turns": list(sess.get("turns") or []),
        }

    # ---------------------------------------------------------------- 模型
    def _say(self, sid: str, user_text: str, *, store_nudge: bool,
             lang: str | None = None) -> str:
        """调一次模型，把回复写进流水。store_nudge=False 时 user_text 已在流水里。"""
        if self.llm is None:
            self.last_error = "没有可用的模型后端"
            raise ChatError("预问诊没有接上模型")

        sess = self.store.get(sid) or {}
        turns = list(sess.get("turns") or [])
        if store_nudge:
            turns = turns + [{"role": "user", "content": user_text}]

        msgs = [{"role": "system", "content": self.system_prompt(lang)}]
        msgs.extend(self._trim(turns))

        try:
            text = self.llm.complete(
                msgs,
                temperature=float(self.pv.get("temperature") or 0.2),
                max_tokens=int(self.pv.get("max_tokens") or 320),
            )
        except Exception as exc:               # 网络、超时、4xx/5xx 一律归到这里
            self.last_error = f"{type(exc).__name__}: {exc}"
            raise ChatError(FALLBACK_UNAVAILABLE) from exc

        text = (text or "").strip()
        if not text:
            self.last_error = "模型返回空文本"
            raise ChatError(FALLBACK_UNAVAILABLE)
        self.last_error = None
        # 每条回复都存两份，和问答板块的 `answer` / `draft` 一一对应：
        #   content     —— 患者该看到的那份（目标语言）
        #   content_int —— 中文原稿（喂回 7B 的上下文；中文界面下的备用文本）
        # 再记一个 lang，界面据此决定"这条能不能在当前语言下直接显示"。
        # 翻译失败**不再 503**：问答板块的处理是落原文 + `answer_translated=false`
        # + 一句"翻译未完成"的本地化提示。整场问诊因为一句翻译断掉就报废，
        # 比"看到原文但知道为什么"更糟，而且那样两边的行为就对不上了。
        if lang == "ug":
            display = self._translate(text, "zh2ug")
            if display:
                self.store.add_turn(sid, "assistant", display, content_int=text,
                                    lang="ug", content_ug=display)
                return display
            log.warning("预问诊维语答案没能翻译，按原文返回：%r", text[:60])
            self.store.add_turn(sid, "assistant", text, content_int=text,
                                lang="zh", translated=False)
            return text
        self.store.add_turn(sid, "assistant", text, content_int=text,
                            lang="en" if lang == "en" else "zh")
        return text

    # ------------------------------------------------------------ 语言补齐
    def localize(self, sid: str, *, lang: str | None = None,
                 limit: int = 6) -> dict[str, Any]:
        """把这场问诊里"缺目标语言版本"的答复补齐，并写回库。

        为什么要有这条路：预问诊的会话是长期存在的，患者随时在两种语言之间
        来回切。中文界面问出来的回答只有中文，切到维语时**没有维语版本可显示**
        ——以前的做法是重开一场让模型用维语重新回答，于是每切一次内容都不一样
        （用户原话：「这两个切换每次内容不统一」）。现在改成**翻译同一句话**：
        缺维语就把这条的中文原稿译过去，存进 `content_ug`，只译一次。

        只补 assistant 的条目（患者自己的话照原样显示），最多 `limit` 条，
        并发发出去——一场问诊通常只缺最近一两轮，等一个来回就够。
        译不出来就跳过，界面继续显示原文并挂"某某语作答"的标记。
        """
        sess = self.store.get(sid)
        if sess is None:
            raise KeyError(sid)
        target = self.normalize_lang(lang) or "zh"
        if target != "ug":
            return self.view(sess)          # 中文版永远在 content_int 里
        todo = []
        for row in self.store.turns_raw(sid):
            if str(row.get("role")) != "assistant":
                continue
            if row.get("content_ug"):
                continue
            zh = str(row.get("content_int") or row.get("content") or "").strip()
            if not zh or looks_uyghur(zh):
                continue
            todo.append((int(row["id"]), zh))
        if not todo:
            return self.view(sess)
        todo = todo[-limit:]
        # 并发翻译：一条一句的话，串行翻 3 条要好几秒，患者盯着页面等。
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(4, len(todo))) as pool:
            futures = {pool.submit(self._translate, zh, "zh2ug"): tid
                       for tid, zh in todo}
            for fut in futures:
                tid = futures[fut]
                try:
                    ug = fut.result()
                except Exception:            # 单条失败不影响其它条
                    ug = ""
                if ug:
                    self.store.set_turn_ug(tid, ug)
        sess = self.store.get(sid)
        if sess is None:
            raise KeyError(sid)
        return self.view(sess)

    @staticmethod
    def _trim(turns: list[dict[str, Any]]) -> list[dict[str, str]]:
        """从最近一轮往前取，直到字符预算用完。太长的单条直接截尾。
        喂给模型的是 content_int（内部语言，维语会话里是中文），没有就退回 content。"""
        picked: list[dict[str, str]] = []
        total = 0
        for turn in reversed(turns[-MAX_HISTORY_TURNS:]):
            content = str(turn.get("content_int") or turn.get("content") or "")
            if total + len(content) > MAX_HISTORY_CHARS and picked:
                break
            if len(content) > MAX_HISTORY_CHARS:
                content = content[:MAX_HISTORY_CHARS]
            picked.append({"role": str(turn.get("role") or "user"), "content": content})
            total += len(content)
        picked.reverse()
        return picked
