/* 前台问答界面（2026-10-29，1.7.0：左栏 + 居中欢迎语 + 底部输入框）
   和后台那套 app.js 没有任何共享：这里是给终端用户用的，只有提问框和答案，
   没有知识库、参数、调试面板。文案按 /api/info 返回的语言走。

   1.7.0 只做了一件结构性的事：**切换语言时不让任何元素换边**。
   上一版把欢迎区、顶栏都设成 dir=rtl，一点"中文"→"维语"，徽记、按钮、
   文字全跑到另一边去；用户点完开关就找不到刚才那个按钮，这是位置变化
   带来的真实困惑，不是审美问题。现在：侧栏恒在左、语言开关恒在右上、
   输入框恒在底部居中、发送键恒在右端、用户气泡恒靠右。方向只交给
   具体文本节点（dir=auto），它只影响字形与行内顺序。

   1.6.0 修过的几处仍在：
   1. 引用 [1] 在 RTL 段落里会被双向算法重排，所以一律包成隔离元素；
      顺带做成可点的——点一下展开来源并高亮那一条。
   2. 答案里的 "1. xxx" 原来是纯文本，RTL 下序号会被挪到句末。现在行首编号
      解析成真正的 <ol>/<ul>，序号交给浏览器按书写方向排。
   3. 每个 delta 都强制滚到底部会把正在翻旧答案的用户拽回去。改成"贴底才跟随"。

   跨语言检索时后端会把维语问题译成中文再去查，译文要显示给用户：否则用户
   看到答案里冒出一段自己没问过的话，只会认为是模型在胡说。
*/

/* 构建号：和 index.html 里那份必须一致，不一致说明浏览器拿的是旧 JS，
   页面加载完会强制换一次（见 index.html 里的自检脚本）。 */
window.SITE_BUILD = "1.7.5";

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

/* 语言名，按**界面语言**给。界面是维语就写"ئۇيغۇرچە"，界面是中文就写"维吾尔语"。
   这跟 LANG_NAME（右上角那两颗按钮上的名字，永远用该语言自称）是两回事：
   按钮要"中文"二字，句子里的"维语"要按句子所处的语言来。 */
const LANG_LABEL = {
  ug: { ug: "ئۇيغۇرچە", zh: "خەنزۇچە", en: "ئىنگلىزچە" },
  zh: { ug: "维吾尔语", zh: "中文", en: "英语" },
  en: { ug: "Uyghur", zh: "Chinese", en: "English" },
};

const T = {
  ug: {
    searching: "ئىزدەۋاتىدۇ…",
    thinking: "جاۋاب يېزىلىۋاتىدۇ…",
    translated: "سوئالىڭىز خەنزۇچىغا تەرجىمە قىلىنىپ ئىزدەلدى",
    drafting: "جاۋاب تەييارلىنىۋاتىدۇ…",
    translating: "ئۇيغۇرچىغا تەرجىمە قىلىنىۋاتىدۇ…",
    notFound: "بىلىم ئامبىرىدا مۇناسىۋەتلىك ئاساس تېپىلمىدى.",
    error: "خاتالىق يۈز بەردى",
    sources: "مەنبە",
    srcCount: (n) => `${n} مەنبە`,
    stop: "توختىتىش",
    send: "ئەۋەتىش",
    copy: "كۆچۈرۈش",
    fbUp: "پايدىلىق",
    fbDown: "پايدىسىز",
    fbMarked: "رەھمەت",
    copied: "كۆچۈرۈلدى",
    took: (s) => `${s} سېكۇنت`,
    toBottom: "ئاستىغا",
    welHint: "تۆۋەندىكى سوئاللارنى بېسىڭ ياكى سوئالىڭىزنى يېزىڭ.",
    loopCut: "جاۋاب تەكرارلىنىپ كەتكەنلىكى ئۈچۈن ئۈزۈۋېتىلدى.",
    trFailed: "سوئالىڭىز تەرجىمە قىلىنمىدى، ئەسلى تىلدا ئىزدەلدى.",
    rawFallback: "تەرجىمە تاماملانمىدى، تۆۋەندىكىسى ئەسلى نۇسخا.",
    docs: "ھۆججەت",
    chunks: "پارچە",
    netErr: "تور ئۇلىنىشى ئۈزۈلدى، قايتا سىناڭ.",
    srvErr: "مۇلازىمەت ۋاقىتلىق ئىشلىمەيۋاتىدۇ.",
    modelErr: "جاۋاب مۇلازىمىتى تېخى ئۇلانمىدى، سەل تۇرۇپ قايتا سىناڭ.",
    newChat: "يېڭى سۆھبەت",
    noChats: "سۆھبەت تارىخى يوق",
    today: "بۈگۈن",
    earlier: "ئىلگىرى",
    del: "سۆھبەتنى ئۆچۈرۈش",
    delConfirm: "ئۆچۈرەمسىز؟",
    // 资料语言 · 作答语言。不能写死"维语作答"：答案语言现在跟着右上角走，
    // 高亮中文时那句"维语作答"就是当着用户的面撒谎。
    chip: (src, dst) =>
      `${LANG_LABEL.ug[src] || src} ماتېرىيال · ${LANG_LABEL.ug[dst] || dst} جاۋاب`,
    // 这条答案不是用当前界面语言生成的：标出来，比假装它一直是中文诚实。
    stamp: (lang) => `${LANG_LABEL.ug[lang] || lang} جاۋاب`,
    // 检索证据分档：这个答案有多少来自资料，必须一眼可见
    ev: {
      high: "مەنبەگە ئاساسەن",
      mid: "مەنبە ئاساس قىلىندى",
      low: "مەنبە يېتەرسىز · ئادەتتىكى بىلىم",
    },
  },
  zh: {
    searching: "正在检索…",
    thinking: "正在生成答案…",
    translated: "你的问题已译为中文后检索",
    drafting: "正在组织答案…",
    translating: "正在译为维吾尔语…",
    notFound: "知识库中没有找到足够的依据。",
    error: "出错了",
    sources: "来源",
    srcCount: (n) => `依据 ${n} 处资料`,
    stop: "停止",
    send: "发送",
    copy: "复制",
    fbUp: "有用",
    fbDown: "没用",
    fbMarked: "已记录",
    copied: "已复制",
    took: (s) => `用时 ${s} 秒`,
    toBottom: "回到底部",
    welHint: "点击下方示例，或直接输入你的问题。",
    loopCut: "答案出现重复，已在此截断。",
    trFailed: "问题没能译成中文，本次按原文检索。",
    rawFallback: "翻译未完成，以下是原文（未译成目标语言）。",
    docs: "篇文档",
    chunks: "个片段",
    netErr: "网络连接中断，请重试。",
    srvErr: "服务暂时不可用。",
    modelErr: "答案服务还没连上，稍后再试。",
    newChat: "新对话",
    noChats: "还没有对话",
    today: "今天",
    earlier: "更早",
    del: "删除这条对话",
    delConfirm: "确认删除",
    chip: (src, dst) =>
      `${LANG_LABEL.zh[src] || src}资料 · ${LANG_LABEL.zh[dst] || dst}作答`,
    stamp: (lang) => `${LANG_LABEL.zh[lang] || lang}作答`,
    ev: { high: "依据资料", mid: "资料为主", low: "资料不足 · 含通用知识" },
  },
  en: {
    searching: "Searching…", thinking: "Writing answer…",
    translated: "Your question was translated before search",
    drafting: "Organizing the answer…", translating: "Translating into Uyghur…",
    notFound: "Not enough evidence in the knowledge base.",
    error: "Error", sources: "Sources", srcCount: (n) => `${n} sources`,
    stop: "Stop", send: "Send", copy: "Copy", copied: "Copied",
    fbUp: "Helpful", fbDown: "Not useful", fbMarked: "Thanks",
    took: (s) => `${s}s`, toBottom: "Latest",
    welHint: "Pick a sample below, or type your own question.",
    docs: "docs", chunks: "chunks",
    netErr: "Connection lost. Please retry.",
    srvErr: "Service temporarily unavailable.",
    modelErr: "The answer service is not connected yet. Please retry shortly.",
    newChat: "New chat", noChats: "No conversations yet",
    today: "Today", earlier: "Earlier",
    del: "Delete this chat", delConfirm: "Delete",
    chip: (src, dst) =>
      `${LANG_LABEL.en[src] || src} sources · answer in ${LANG_LABEL.en[dst] || dst}`,
    stamp: (lang) => `answered in ${LANG_LABEL.en[lang] || lang}`,
    ev: { high: "From sources", mid: "Mostly from sources", low: "Sources thin · general knowledge" },
    loopCut: "Answer was cut short because it started repeating.",
    trFailed: "Your question could not be translated; it was searched as-is.",
    rawFallback: "Translation did not finish; the text below is the original.",
  },
};

let info = null;
let t = T.ug;
let busy = false;
let ctrl = null;
let history = [];                 // 这次会话发给后端的上下文（最近 6 条）
let uiLang = "ug";                // 当前界面语言（info.lang 或 info.alt.lang）
let altMode = false;              // true = 当前在用另一套语言（中文）
let follow = true;                // 滚动跟随：只有本来就贴底时才跟着走
let activeAnswer = null;          // 正在流式的那个 .answer，停止时靠它收光标

const LANG_NAME = { ug: "ئۇيغۇرچە", zh: "中文", en: "English" };

/* ------------------------------------------------------------ 会话存档

   只存在本机（localStorage），不碰后端。目的就两个：
   1. 侧栏要有东西列——空白侧栏比没有侧栏更难看；
   2. 刷新页面不该把刚问过的对话全丢掉。
   每条答案正文截到 12000 字：一份几十条记录的存档不该把 localStorage
   配额吃满，而 12000 字已经远超一屏能读的量。 */
const CHAT_KEY = "knowtune.site.chats";
const SESSION_KEY = "knowtune.site.session";
const MAX_CHATS = 30;
const MAX_ANSWER = 12000;

let chats = loadChats();
let sessionId = localStorage.getItem(SESSION_KEY) || null;

function loadChats() {
  try {
    const raw = JSON.parse(localStorage.getItem(CHAT_KEY) || "[]");
    if (!Array.isArray(raw)) return [];
    // 空壳记录（问了没答完就关页面）不占位置
    return raw.filter((c) => c && c.id && Array.isArray(c.msgs) && c.msgs.length).slice(0, MAX_CHATS);
  } catch {
    return [];
  }
}

function saveChats() {
  try {
    const slim = chats.slice(0, MAX_CHATS).map((c) => ({
      id: c.id, title: c.title, at: c.at,
      msgs: c.msgs.map((m) => ({ ...m, a: String(m.a || "").slice(0, MAX_ANSWER) })),
    }));
    localStorage.setItem(CHAT_KEY, JSON.stringify(slim));
  } catch {
    /* 配额满 / 隐私模式禁写：功能降级成"只有当前这一轮"，不该因此报错 */
  }
}

function currentChat() {
  return chats.find((c) => c.id === sessionId) || null;
}

function ensureChat(title) {
  let c = currentChat();
  if (!c) {
    c = { id: sessionId, title: "", at: Date.now(), msgs: [] };
    chats.unshift(c);
  }
  if (!c.title && title) c.title = title.replace(/\s+/g, " ").slice(0, 40);
  c.at = Date.now();
  saveChats();
  return c;
}

/* ------------------------------------------------------------ 会话切换 */

function freshId() {
  return "s-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 8);
}

function newSession() {
  if (busy) stop();
  sessionId = freshId();
  localStorage.setItem(SESSION_KEY, sessionId);
  history = [];
  renderChat();
  renderSessions();
  focusInput();
  closeSide();
}

function openChat(id) {
  if (id === sessionId) { closeSide(); return; }
  if (busy) stop();
  sessionId = id;
  localStorage.setItem(SESSION_KEY, id);
  history = [];
  renderChat();
  renderSessions();
  closeSide();
}

/* 1.7.3：会话历史可以删。
   列表项从单个 <button> 变成「标题 + 删除」两件套：<button> 里再塞 <button> 是
   非法结构，浏览器会把内层踢出去，所以外面套一层 .sess-row 定位。
   删除要两步：历史只存在本机，删掉没有回收站，误点一下丢整段对话的代价太大。 */
const ICON_TRASH = `<svg viewBox="0 0 16 16" width="13" height="13" aria-hidden="true"`
  + ` fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round">`
  + `<path d="M3 4.5h10M6.5 4.5V3.2h3v1.3M4.6 4.5l.6 8.2h5.6l.6-8.2"/></svg>`;

let delTimer = null;

function sessRow(c) {
  const on = c.id === sessionId ? " on" : "";
  const label = esc(c.title || t.newChat);
  return `<div class="sess-row${on}" data-id="${esc(c.id)}">`
    + `<button type="button" class="sess" data-id="${esc(c.id)}" dir="auto">${label}</button>`
    + `<button type="button" class="sess-del" data-del="${esc(c.id)}" tabindex="-1"`
    + ` aria-label="${esc(t.del)}" title="${esc(t.del)}">${ICON_TRASH}</button></div>`;
}

function renderSessions() {
  const box = $("sideScroll");
  if (!info) return;
  if (!chats.length) {
    box.innerHTML = `<p class="side-empty">${esc(t.noChats)}</p>`;
    return;
  }
  const today = new Date().toDateString();
  const groups = [[t.today, []], [t.earlier, []]];
  chats.forEach((c) => {
    groups[new Date(c.at).toDateString() === today ? 0 : 1][1].push(c);
  });
  box.innerHTML = groups.filter((g) => g[1].length).map(([name, list]) =>
    `<p class="side-group">${esc(name)}</p>` + list.map(sessRow).join("")).join("");
}

/* 第一次点：这一行进入待确认态；第二次点同一个按钮才真删。
   四秒不动就退回原样，免得"红色按钮一直挂着"，最后被随手点掉。 */
function armDelete(btn) {
  const row = btn.closest(".sess-row");
  if (!row) return;
  if (row.classList.contains("ask")) { deleteChat(btn.dataset.del); return; }
  document.querySelectorAll(".sess-row.ask").forEach(disarmDelete);
  row.classList.add("ask");
  btn.innerHTML = `<span>${esc(t.delConfirm)}</span>`;
  btn.title = t.delConfirm;
  delTimer = setTimeout(() => disarmDelete(row), 4000);
}

function disarmDelete(row) {
  if (delTimer) { clearTimeout(delTimer); delTimer = null; }
  if (!row || !row.isConnected) return;
  row.classList.remove("ask");
  const b = row.querySelector(".sess-del");
  if (!b) return;
  b.innerHTML = ICON_TRASH;
  b.title = t.del;
  b.setAttribute("aria-label", t.del);
}

/* 删掉一条。删的是当前正在看的那条时，落回一个干净的新对话——
   把用户丢在一条已经不存在的会话里，下一句都不知道写去哪儿。 */
function deleteChat(id) {
  const i = chats.findIndex((c) => c.id === id);
  if (i < 0) return;
  if (delTimer) { clearTimeout(delTimer); delTimer = null; }
  const wasCurrent = id === sessionId;
  chats.splice(i, 1);
  saveChats();
  if (wasCurrent) {
    if (busy) stop();
    sessionId = freshId();
    localStorage.setItem(SESSION_KEY, sessionId);
    history = [];
    renderChat();
  }
  renderSessions();
}


function renderChat() {
  const box = $("msgs");
  box.innerHTML = "";
  activeAnswer = null;
  const chat = currentChat();
  const msgs = (chat && chat.msgs) || [];
  history = [];
  msgs.forEach((m) => {
    const u = document.createElement("div");
    u.className = "msg user";
    u.dir = "auto";
    u.textContent = m.q;
    box.appendChild(u);
    box.appendChild(botCard(m));
    history.push({ role: "user", content: m.q },
                 { role: "assistant", content: m.a || "" });
  });
  const empty = !msgs.length;
  $("welcome").hidden = !empty;
  $("main").classList.toggle("empty", empty);
  $("chat").classList.toggle("empty", empty);
  follow = true;
  syncToBottom();
  if (empty) $("chat").scrollTop = 0;
}

/* ------------------------------------------------------------ 启动 */

(async function boot() {
  try {
    const r = await fetch("/api/info", { cache: "no-store" });
    if (!r.ok) throw new Error("HTTP " + r.status);
    info = await r.json();
  } catch (err) {
    document.body.innerHTML = '<div class="fatal">بىلىم ئامبىرىغا ئۇلىنالمىدى<br>'
      + "服务未就绪：" + esc(err.message) + "</div>";
    return;
  }
  uiLang = info.lang;
  t = T[uiLang] || T.ug;
  // 页面骨架恒为 LTR：切换语言只换文本方向，不换任何元素的位置。
  document.documentElement.dir = "ltr";
  applyChrome();

  // 切换按钮绑在容器上：内部按钮每次重画都会被换掉，绑在按钮上就得每次重绑。
  $("langSwitch").onclick = (e) => {
    const b = e.target.closest("[data-lang]");
    if (!b) return;
    setLangMode(b.dataset.lang === altLang());
  };

  $("samples").onclick = (e) => {
    const b = e.target.closest(".chip");
    if (!b) return;
    const txt = b.querySelector(".txt");
    $("q").value = (txt ? txt.textContent : b.textContent).trim();
    autosize();
    syncSend();
    send();
  };

  $("sideScroll").onclick = (e) => {
    const del = e.target.closest("[data-del]");
    if (del) {
      e.stopPropagation();
      armDelete(del);
      return;
    }
    const b = e.target.closest(".sess");
    if (!b) return;
    document.querySelectorAll(".sess-row.ask").forEach(disarmDelete);
    openChat(b.dataset.id);
  };

  $("btnSide").onclick = () => openSide(true);
  $("scrim").onclick = () => closeSide();

  if (!sessionId) newSession();
  else { renderSessions(); renderChat(); }
})();

/* 自动聚焦只在有鼠标的桌面端做：手机上 focus 会立刻弹出键盘，
   用户还没想好问什么，屏幕下半截就先被键盘吃掉。 */
function focusInput() {
  if (!matchMedia("(hover: hover) and (pointer: fine)").matches) return;
  $("q").focus();
}

/* ------------------------------------------------------------ 侧栏（窄屏抽屉） */

function openSide(on) {
  $("side").classList.toggle("open", on);
  $("scrim").hidden = !on;
}

function closeSide() {
  openSide(false);
}

/* ------------------------------------------------------------ 输入 */

function autosize() {
  const el = $("q");
  el.style.height = "auto";
  el.style.height = Math.min(el.scrollHeight, 176) + "px";
}

function syncSend() {
  // 生成中按钮必须可点：它此刻是"停止"，不是"发送"。原来写成
  // disabled = busy || !有内容，结果一进入生成按钮就锁死，显示着"停止"
  // 却点不动，用户唯一的出路是等它自己跑完——白等几十秒。
  $("btnSend").disabled = busy ? false : !$("q").value.trim();
  $("btnSend").classList.toggle("stopping", busy);
  $("sendLabel").textContent = busy ? t.stop : t.send;
  $("btnSend").title = busy ? t.stop : t.send;
}

$("q").addEventListener("input", () => { autosize(); syncSend(); });
$("q").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); }
});
$("btnNew").onclick = () => newSession();
$("btnSend").onclick = () => (busy ? stop() : send());

/* Esc：先生成中则停止；否则收起侧栏抽屉 */
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if (busy) { e.preventDefault(); stop(); return; }
  closeSide();
});

function stop() {
  if (ctrl) ctrl.abort();
  busy = false;
  ctrl = null;
  if (activeAnswer) activeAnswer.classList.remove("streaming");
  activeAnswer = null;
  $("status").hidden = true;
  syncSend();
}

/* ------------------------------------------------------------ 渲染 */

function bubble(role) {
  const el = document.createElement("div");
  el.className = "msg " + role;
  // dir=auto：用户问维语就是 RTL、问中文就是 LTR，答案同理——
  // 比写死一个方向在双语场景下都正确。注意它只加在**文本块**上，
  // 不加在容器上：容器带 dir 会让 flex 子元素整体换边。
  el.dir = "auto";
  $("msgs").appendChild(el);
  scrollToBottom();
  return el;
}

/* 行内向：先整体转义（任何模型输出都不可能注入标签），再认三种记号。
   [1] 变成可点的小牌，并用 bdi 语义（display:inline-flex + isolate）隔离方向。 */
function inline(s) {
  return esc(s)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
    .replace(/\[(\d+)\]/g, (m, n) =>
      `<button type="button" class="cite" data-n="${n}" title="${esc(t.sources)} ${n}">${n}</button>`);
}

/* 1.7.2：引用牌不能单独被挤到下一行行首。
   中文正文里汉字之间处处可断行，而牌是原子 inline 盒——行尾差几像素放不下时，
   浏览器会把它整个人推到下一行，于是出现"①。"独占一行行首，读者会当成新的
   序号，整句话读断了。先试过在牌前塞 word joiner(U+2060)，实测 Blink 在 CJK
   之间照断不误（420–736px 扫 14 个宽度，掉行次数 14 → 14，毫无变化）。
   真正管用的是把"它注释的那段文字 + 牌"包进 white-space:nowrap 的 span。

   切点得挑，不能一刀切最后一个字符：
   - 中文没有词边界，汉字之间也不连写 → 取最后 1 个汉字（连同其后的空格）。
   - 维吾尔语/英文有空格分词，且阿拉伯字母**跨文本节点不连写**，切在词中间会把
     "ئوخشايدۇ" 劈成孤立字形 → 只能在空格处切，整词跟着牌一起下移。
   在 DOM 上做而不是拼字符串，是为了不把 &amp; 这类转义实体从中间切开。 */
const CJK_TAIL = /[\u3000-\u303f\u3400-\u9fff\uff01-\uff60\u2018\u2019\u201c\u201d\u2026\u2014]/;

function bindCites(root) {
  root.querySelectorAll(".cite").forEach((c) => {
    const prev = c.previousSibling;
    // 连着的两枚牌（"[2][4]"）里，第二枚的前兄弟是刚包好的 .nb（或第一枚牌），
    // 并进同一个不可断片段——否则它自己还是孤立原子，照样能被挤到行首。
    if (prev && prev.nodeType === 1) {
      const holder = prev.classList && prev.classList.contains("cite") ? prev.parentNode : prev;
      if (holder && holder.classList && holder.classList.contains("nb")) {
        holder.appendChild(c);
        return;
      }
    }
    if (!prev || prev.nodeType !== 3 || !prev.textContent) return;
    const txt = prev.textContent;
    // 尾部空白先剥掉：牌前面常有个空格（"word [1]"），它不该被算进那个词
    let end = txt.length;
    while (end > 0 && /\s/.test(txt[end - 1])) end--;
    if (end === 0) return;                       // 前面全是空白，没什么可绑定
    const body = txt.slice(0, end);
    const ch = Array.from(body).pop();
    let cut;
    if (CJK_TAIL.test(ch)) {
      // 中文：标点也得算进来。答案是"…四种赫里提”[1]"，只绑一个右引号的话，
      // 行首会变成 "”①"，同样难看。所以从尾部跨过标点，再带上一个字。
      let k = body.length;
      while (k > 0 && !/[\u3400-\u9fff]/.test(body[k - 1])) k--;
      if (k > 0) {
        const last = Array.from(body.slice(0, k)).pop();
        k -= last.length;
      }
      if (body.length - k > 12) k = body.length - ch.length;  // 一长串标点就退回只取 1 字
      cut = k;
    } else if (end < txt.length) {
      const word = body.split(/\s+/).pop();      // 空格分词：取最后一个词
      if (!word || word.length > 24) return;     // 超长串整段 nowrap 会撑破行
      cut = body.length - word.length;
    } else {
      return;                                    // 紧贴西文又没有空格：不切，宁可不绑
    }
    const tail = prev.splitText(cut);
    const wrap = document.createElement("span");
    wrap.className = "nb";
    tail.parentNode.insertBefore(wrap, tail);
    wrap.appendChild(tail);
    wrap.appendChild(c);
  });
}

/* 块级轻量 Markdown：只认编号列表、无序列表、#### 小标题、空行分段。
   为什么非做不可：答案是维语，行首的 "1." 在 RTL 段落里会被双向算法摆到
   句末，读起来像"序号写在句子后面"。解析成 <ol> 后序号由浏览器按书写方向
   摆到正确的一侧，且缩进对齐。 */
function renderRich(text) {
  const lines = String(text ?? "").replace(/\r/g, "").split("\n");
  const out = [];
  let para = [];
  let list = null;

  const flushPara = () => {
    if (para.length) {
      out.push(`<p>${para.map(inline).join("<br>")}</p>`);
      para = [];
    }
  };
  const flushList = () => {
    if (list) {
      // 首条不是 1 时用 start 保住模型原本的编号（"3. 4. 5." 不能被改写成 1.2.3.）
      const attr = list.start > 1 ? ` start="${list.start}"` : "";
      out.push(`<${list.tag}${attr}>${list.items.map((i) => `<li>${inline(i)}</li>`).join("")}</${list.tag}>`);
      list = null;
    }
  };

  for (const raw of lines) {
    const line = raw.replace(/\s+$/, "");
    if (!line.trim()) {
      flushPara();
      // 空行**不关闭**列表。模型几乎都按"每条一段"排版，一遇空行就 flush，
      // 一条三点答案会变成三个各自从 1 开始的 <ol>——满屏"1. 1. 1."。
      // 真正的列表结束由后面遇到非列表行时的那次 flushList() 判定。
      continue;
    }

    const head = /^(#{1,4})\s+(.*)$/.exec(line);
    if (head) {
      flushPara(); flushList();
      out.push(`<h4>${inline(head[2])}</h4>`);
      continue;
    }

    // 行首编号：1. / 1) / １．都算。中文资料里也常见"（1）"，一并认。
    const ol = /^\s*(?:[（(]?(\d{1,2})[)）.]\s*)(.+)$/.exec(line);
    const ul = /^\s*[-*•·]\s+(.+)$/.exec(line);
    if (ol || ul) {
      flushPara();
      const tag = ol ? "ol" : "ul";
      if (!list || list.tag !== tag) {
        flushList();
        list = { tag, items: [], start: ol ? parseInt(ol[1], 10) || 1 : 1 };
      }
      list.items.push(ol ? ol[2] : ul[1]);
      continue;
    }

    flushList();
    para.push(line);
  }
  flushPara(); flushList();
  return `<div class="md">${out.join("")}</div>`;
}

function sourcesBar(citations) {
  if (!citations || !citations.length) return null;
  const det = document.createElement("details");
  det.className = "srcs";
  const items = citations.map((c) => {
    const head = [c.doc_title, c.page_no ? `${c.page_no}` : "", c.heading_path || ""]
      .filter(Boolean).join(" · ");
    return `<li class="src-item" data-n="${esc(c.n)}">
        <span class="n">${esc(c.n)}</span>
        <div class="src-body">
          <div class="src-head" dir="auto">${esc(head)}</div>
          <div class="src-prev" dir="auto">${esc(c.preview || "")}</div>
        </div>
      </li>`;
  }).join("");
  det.innerHTML = `<summary>${esc(t.srcCount(citations.length))}<span class="caret"></span></summary>
    <ul>${items}</ul>`;
  return det;
}

/* 每条回答存两份文本，渲染时按当前语言挑一份：
     bot.dataset.main  —— 模型原样输出（维吾尔语）
     bot.dataset.draft —— 中文原稿（只有两段式回答才有）
   以前是"切换时把当前 innerHTML 快照一份，再塞原稿"，只要流还没结束，
   后续 delta 就会把原稿冲掉——用户点了按钮却看到内容还在滚维语，等于没切。 */
function paintAnswer(bot, el) {
  const draft = bot.dataset.draft || "";
  const useAlt = altMode && draft;
  el.classList.toggle("alt", !!useAlt);
  el.innerHTML = renderRich(useAlt ? draft : (bot.dataset.main || ""));
  bindCites(el);
  // 证据徽标也是文案，跟着一起换
  const badge = bot.querySelector(".ev-badge");
  if (badge && bot.dataset.ev) {
    badge.textContent = (t.ev && t.ev[bot.dataset.ev]) || bot.dataset.ev;
  }
}

function registerDraft(bot, el) {
  // 中文草稿到了 / 中文原稿随 done 一起到：立刻按当前语言重画一次。
  paintAnswer(bot, el);
}

/* 把一条已完成（或从存档恢复）的回答补齐成完整卡片：
   证据徽标 → 正文 → 来源 → 耗时/复制。流式结束时和从存档重建时走同一条路，
   两边长相不一样才是最常见的"看着像 bug"的来源。 */
function decorateBot(bot, el, rec) {
  bot._rec = rec;                       // 切语言时按它把徽标/来源/耗时重新上字
  bot.dataset.main = rec.a || "";
  if (rec.d) bot.dataset.draft = rec.d;
  if (rec.ev) bot.dataset.ev = rec.ev;

  if (!rec.a) {
    // 拒答 / 空答案：给一句人话，别留个空气泡
    el.className = "answer muted";
    el.textContent = t.notFound;
    return;
  }

  if (rec.ev) {
    const badge = document.createElement("div");
    badge.className = "ev-badge " + rec.ev;
    badge.textContent = (t.ev && t.ev[rec.ev]) || rec.ev;
    bot.insertBefore(badge, el);
  }
  paintAnswer(bot, el);

  const srcs = sourcesBar(rec.cit || []);
  if (srcs) bot.appendChild(srcs);

  if (rec.loop) {
    const w = document.createElement("div");
    w.className = "loop-warn";
    w.textContent = t.loopCut;
    bot.appendChild(w);
  }

  // 翻译没成要有痕迹。没有这一行，"答案是一段看不懂的中文"和"模型就这样答"
  // 在界面上长得一样，只能靠猜。答案没译比问题没译更严重，优先显示。
  const trWarn = rec.tra ? t.rawFallback : (rec.trq ? t.trFailed : "");
  if (trWarn) {
    const w = document.createElement("div");
    w.className = "loop-warn tr-warn";
    w.textContent = trWarn;
    bot.appendChild(w);
  }

  // 底部一行：耗时在左、复制在右。耗时短于 1 秒的不显示——"0.4 秒"
  // 除了让人怀疑计时器坏了，没有别的信息量。
  const foot = document.createElement("div");
  foot.className = "foot";
  const secs = (rec.ms || 0) / 1000;
  // 语言标记只在"不是当前界面语言"时出现：同一语言时它是噪音，
  // 不同语言时它是唯一能让用户明白"为什么这段我看不懂"的线索。
  const stamp = (rec.al && rec.al !== uiLang)
    ? `<span class="alang">${esc(t.stamp(rec.al))}</span>` : "";
  // 反馈键只在有 message_id 时出现：没落库的答案（会话没建成）点了也无处
  // 可记，给一个按不动的按钮比不给更糟。已经标过的那条保持置灰 + 显示"已记录"，
  // 否则切语言、翻历史再回来，用户会以为自己的反馈丢了。
  const fbv = rec.fbv || "";
  const fb = rec.mid
    ? `<button type="button" class="fb" data-fb="up"${fbv ? " disabled" : ""}>`
      + esc(fbv === "up" ? t.fbMarked : t.fbUp) + `</button>`
      + `<button type="button" class="fb" data-fb="down"${fbv ? " disabled" : ""}>`
      + esc(fbv === "down" ? t.fbMarked : t.fbDown) + `</button>`
    : "";
  foot.innerHTML = `<span class="took">${secs >= 1 ? esc(t.took(secs.toFixed(1))) : ""}</span>`
    + stamp
    + `<span class="spacer"></span>`
    + fb
    + `<button type="button" class="copy">${esc(t.copy)}</button>`;
  bot.appendChild(foot);

  if (rec.mid) {
    foot.querySelectorAll("[data-fb]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        if (btn.disabled) return;
        try {
          const r = await fetch("/api/feedback", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              message_id: rec.mid,
              verdict: btn.dataset.fb,
              comment: "",
            }),
          });
          if (!r.ok) throw new Error(String(r.status));
          rec.fbv = btn.dataset.fb;   // 记在 rec 上：切语言重绘时要保持已标记
          foot.querySelectorAll("[data-fb]").forEach((b) => (b.disabled = true));
          btn.textContent = t.fbMarked;
        } catch (_) {
          // 失败不置灰、不改字，让人能再点一次。前台弹窗解释失败会打断阅读，
          // 把原因挂在 title 上，鼠标停一下就看得见。
          btn.title = t.netErr;
        }
      });
    });
  }

  if (rec.d) registerDraft(bot, el);
}

function botCard(rec) {
  const bot = document.createElement("div");
  bot.className = "msg bot";
  const el = document.createElement("div");
  el.className = "answer";
  el.dir = "auto";
  bot.appendChild(el);
  decorateBot(bot, el, rec);
  return bot;
}

/* 切语言时把一条答案里"不是正文"的文案也换掉：证据徽标、来源胶囊的计数、
   耗时、复制键、截断提示。正文由 paintAnswer 负责；这些漏了就会出现
   "正文已经是中文、按钮还写着 ئەۋەتىش"的拼接感。 */
function relabelBot(bot) {
  const rec = bot._rec;
  const badge = bot.querySelector(".ev-badge");
  if (badge && rec && rec.ev) badge.textContent = (t.ev && t.ev[rec.ev]) || rec.ev;

  const muted = bot.querySelector(".answer.muted");
  if (muted && rec && !rec.a) muted.textContent = t.notFound;

  const err = bot.querySelector(".answer.err");
  if (err && bot.dataset.raw) {
    const f = friendly(bot.dataset.raw);
    err.textContent = f.msg;
    if (f.detail) err.title = f.detail;
  }

  const sum = bot.querySelector(".srcs summary");
  if (sum && rec && rec.cit && rec.cit.length) {
    sum.firstChild.textContent = t.srcCount(rec.cit.length);
  }

  const lw = bot.querySelector(".loop-warn:not(.tr-warn)");
  if (lw) lw.textContent = t.loopCut;

  // 切语言时这条提示也要跟着换语言，否则中文界面上会挂一句维语警告
  const tw = bot.querySelector(".tr-warn");
  if (tw) tw.textContent = (bot._rec && bot._rec.tra) ? t.rawFallback : t.trFailed;

  const took = bot.querySelector(".took");
  if (took && rec) {
    const secs = (rec.ms || 0) / 1000;
    took.textContent = secs >= 1 ? t.took(secs.toFixed(1)) : "";
  }

  // 语言标记：切换界面语言后，旧答案不会跟着变（那是历史内容，不该被改写），
  // 所以要在底下重新标明"它是什么语言"，并且随当前界面语言换措辞。
  const foot = bot.querySelector(".foot");
  if (foot && rec) {
    let tag = foot.querySelector(".alang");
    const need = rec.al && rec.al !== uiLang;
    if (need) {
      if (!tag) {
        tag = document.createElement("span");
        tag.className = "alang";
        foot.insertBefore(tag, foot.querySelector(".spacer"));
      }
      tag.textContent = t.stamp(rec.al);
    } else if (tag) {
      tag.remove();
    }
  }

  const cp = bot.querySelector(".copy");
  if (cp && !cp.classList.contains("done")) cp.textContent = t.copy;

  // 反馈键的措辞也要跟着界面语言走。只有被选中的那个显示"已记录"，
  // 另一个保持原文案但仍是置灰态。
  const fbv = (rec && rec.fbv) || "";
  foot?.querySelectorAll("[data-fb]").forEach((b) => {
    b.textContent = (fbv && b.dataset.fb === fbv)
      ? t.fbMarked
      : (b.dataset.fb === "up" ? t.fbUp : t.fbDown);
  });
}

/* 当前该用哪套文案：切到备选语言就用 alt，否则用站点主语言那份 */
function copy() {
  return (altMode && info && info.alt) ? info.alt : (info || {});
}

function dirOf(l) {
  return l === "ug" ? "rtl" : "ltr";
}

function placeholderFor(l) {
  if (l === "ug") return "سوئالىڭىزنى يېزىڭ…";
  if (l === "en") return "Type your question…";
  return "输入你的问题…";
}

function applyChrome() {
  const c = copy();
  document.documentElement.lang = uiLang;   // 顺带换字体：CSS 按 [lang] 给字体栈
  $("siteTitle").textContent = c.title || info.kb_name;
  $("siteSub").textContent = c.subtitle || "";
  document.title = c.title || info.kb_name;
  // 知识库名也是界面文字：库里存的是中文名，切到维语界面还挂四个汉字，
  // 比按钮没翻更显眼。站点可以给另一套语言单独配一个名字（alt.kb_name）。
  $("sideName").textContent = c.kb_name || info.kb_name || "";
  $("sideName").dir = "auto";
  $("sideMeta").textContent =
    `${info.docs} ${t.docs} · ${info.chunks} ${t.chunks}`;
  $("welText").textContent = c.welcome || "";
  $("welHint").textContent = t.welHint || "";
  $("disclaimer").textContent = c.disclaimer || "";
  $("toBottomLabel").textContent = t.toBottom || "";
  // 资料语言取知识库真实语种，作答语言取当前界面语言——这两者都可能不是"中文/维语"，
  // 写死一句"中文资料 · 维语作答"在中文界面下就是自相矛盾。
  $("toolChip").textContent = t.chip(info.kb_lang || "zh", uiLang);
  $("toolChip").dir = "auto";
  $("q").placeholder = placeholderFor(uiLang);
  // 输入框的书写方向跟界面语言走：维语界面里占位符与光标就该在右侧开始。
  // 不能用 dir=auto——输入框为空时 auto 会退回父级 ltr，维语占位符被甩到左边。
  $("q").dir = dirOf(uiLang);
  $("btnNew").lastElementChild.textContent = t.newChat;
  $("btnNew").title = t.newChat;
  syncSend();
  renderLangSwitch();
  const box = $("samples");
  box.innerHTML = (c.samples || []).map((s) =>
    `<button class="chip" type="button" dir="auto"><span class="txt">${esc(s)}</span></button>`).join("");
}

function altLang() {
  return (info && info.alt && info.alt.lang) || "zh";
}

/* 语言切换做成两颗并排的按钮，当前语言高亮。
   以前是一颗按钮、文字写"点了会变成的那种语言"——用户看到"中文"两个字，
   分不清这是现在的语言还是要切去的语言，这是最基本的信息缺失。 */
function renderLangSwitch() {
  const box = $("langSwitch");
  if (!box || !info) return;
  box.innerHTML = [info.lang, altLang()].map((code) =>
    `<button type="button" data-lang="${esc(code)}"`
    + ` class="${code === uiLang ? "on" : ""}"`
    + ` title="${esc(code === info.lang ? "维吾尔语界面" : "中文界面")}">`
    + `${esc(LANG_NAME[code] || code)}</button>`).join("");
}

function setLangMode(on) {
  if (on === altMode) return;
  altMode = on;
  uiLang = on ? altLang() : info.lang;
  t = T[uiLang] || T[info.lang];
  applyChrome();          // 内含 renderLangSwitch：高亮跟着走
  // 按 DOM 重画所有回答（含侧栏里点开的历史）。渲染只看 altMode，
  // 所以流式没结束时后续 delta 也不会把已切过来的原稿冲掉。
  document.querySelectorAll("#msgs .msg.bot").forEach((bot) => {
    const el = bot.querySelector(".answer");
    if (el && !el.classList.contains("err")) paintAnswer(bot, el);
    relabelBot(bot);
  });
  renderSessions();
  scrollToBottom(true);
}

/* ------------------------------------------------------------ 滚动 */

function atBottom() {
  const el = $("chat");
  return el.scrollHeight - el.scrollTop - el.clientHeight < 72;
}

function syncToBottom() {
  $("toBottom").hidden = follow;
}

function scrollToBottom(force = false) {
  if (force) follow = true;
  if (!follow) return;
  const el = $("chat");
  // 不用 smooth：流式期间每帧都触发，平滑动画会互相打断，看着像卡顿
  el.scrollTop = el.scrollHeight;
  syncToBottom();
}

$("chat").addEventListener("scroll", () => {
  const now = atBottom();
  if (now !== follow) { follow = now; syncToBottom(); }
}, { passive: true });

$("toBottom").onclick = () => {
  follow = true;
  syncToBottom();
  $("chat").scrollTo({ top: $("chat").scrollHeight, behavior: "smooth" });
};

/* ------------------------------------------------------------ 引用与复制 */

document.addEventListener("click", (e) => {
  const btn = e.target.closest(".cite");
  if (btn) {
    const bot = btn.closest(".msg.bot");
    const det = bot && bot.querySelector(".srcs");
    if (!det) return;
    det.open = true;
    const item = det.querySelector(`.src-item[data-n="${btn.dataset.n}"]`);
    if (!item) return;
    item.scrollIntoView({ block: "nearest", behavior: "smooth" });
    item.classList.remove("flash");
    void item.offsetWidth;          // 强制回流，同一元素连点也能重播动画
    item.classList.add("flash");
    setTimeout(() => item.classList.remove("flash"), 1600);
    return;
  }

  const cp = e.target.closest(".copy");
  if (cp) {
    const bot = cp.closest(".msg.bot");
    const ans = bot && bot.querySelector(".answer");
    const text = (ans && ans.innerText) || "";
    if (!text) return;
    copyText(text).then((ok) => {
      if (!ok) return;
      const old = cp.textContent;
      cp.textContent = t.copied;
      cp.classList.add("done");
      setTimeout(() => { cp.textContent = old; cp.classList.remove("done"); }, 1600);
    });
  }
});

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    // 非安全上下文（局域网 http）拿不到 clipboard API，退回选中复制
    try {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.style.cssText = "position:fixed;left:-9999px;top:0";
      document.body.appendChild(ta);
      ta.select();
      const ok = document.execCommand("copy");
      ta.remove();
      return ok;
    } catch {
      return false;
    }
  }
}

/* ------------------------------------------------------------ 提问 */

/* 把后端抛上来的异常文本变成用户能读的一句话。
   "RuntimeError: 连接被拒绝" 对终端用户毫无意义，但完全吞掉又会让
   运维无从定位——所以短提示 + 完整原因放 title。 */
function friendly(err) {
  const raw = String((err && err.message) || err || "");
  if (!raw) return { msg: t.error, detail: "" };
  if (/LLM\s*返回\s*\d{3}|HTTP\s*\d{3}/.test(raw)) return { msg: t.srvErr, detail: raw };
  if (/连接失败|ConnectError|10061|Connection/i.test(raw)) return { msg: t.modelErr, detail: raw };
  if (/Failed to fetch|NetworkError|load failed/i.test(raw)) return { msg: t.netErr, detail: raw };
  return { msg: t.error, detail: raw };
}

/* 挂一条错误气泡：短的给用户，完整的给鼠标悬停 */
function errorBubble(bot, raw) {
  const f = friendly(raw);
  bot.dataset.raw = String(raw || "");
  bot.classList.add("failed");     // 失败气泡整体换配色，别和正常答案长得一样
  bot.innerHTML = `<div class="answer err" dir="auto"></div>`;
  const el = bot.querySelector(".answer");
  el.textContent = f.msg;
  if (f.detail) el.title = f.detail;
}

async function send() {
  const q = $("q").value.trim();
  if (!q || busy) return;
  const chat = ensureChat(q);
  $("welcome").hidden = true;
  $("main").classList.remove("empty");
  $("chat").classList.remove("empty");
  busy = true;
  ctrl = new AbortController();
  syncSend();
  $("q").value = "";
  autosize();
  follow = true;                 // 自己刚发的问题，一定跟到底
  syncToBottom();
  renderSessions();

  const user = bubble("user");
  user.textContent = q;
  const bot = bubble("bot");
  bot.innerHTML = `<div class="answer streaming" dir="auto"></div>`;
  const answerEl = bot.querySelector(".answer");
  activeAnswer = answerEl;
  const metaEl = document.createElement("div");
  metaEl.className = "thinking";
  metaEl.textContent = t.searching;
  bot.appendChild(metaEl);
  scrollToBottom();

  const started = Date.now();
  const showStatus = (s) => {
    $("status").hidden = false;
    $("status").textContent = s;
  };
  showStatus(t.searching);

  let acc = "";
  let finalCit = [];

  try {
    const res = await fetch("/api/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        question: q,
        session_id: sessionId,
        history: history.slice(-6),
        // 答案语言 = 右上角高亮的语言。把界面语言随请求发下去，
        // 后端才不会按"它猜的提问语种"作答（猜错一次就是满屏错语言）。
        ui_lang: uiLang,
      }),
      signal: ctrl.signal,
    });
    if (!res.ok) throw new Error("HTTP " + res.status);

    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let i;
      while ((i = buf.indexOf("\n\n")) >= 0) {
        const raw = buf.slice(0, i);
        buf = buf.slice(i + 2);
        handle(raw);
      }
    }
  } catch (err) {
    if (err.name !== "AbortError") errorBubble(bot, err);
  } finally {
    busy = false;
    ctrl = null;
    $("status").hidden = true;
    syncSend();
    const st = bot.querySelector(".answer.streaming");
    if (st) st.classList.remove("streaming");
    if (activeAnswer === answerEl) activeAnswer = null;
  }

  function handle(raw) {
    const ev = parseEvent(raw);
    if (!ev) return;
    if (ev.event === "query") {
      const qd = ev.data || {};
      if (qd.translated) {
        // 维语状态 + 中文检索词拼在一句话里会被双向算法重排成乱序（"倒着的"）。
        // 中文检索词用 <bdi dir="ltr"> 单独隔离，位置就钉死了。
        metaEl.innerHTML = `${esc(t.translated)}`
          + ` <bdi dir="ltr" class="q-zh">${esc(qd.used || "")}</bdi>`;
        showStatus(t.translated);
        bot.dataset.q = qd.used;
      } else if (qd.src && qd.dst && qd.src !== qd.dst) {
        // 该翻却没翻：模型报错、译文被判废、或两次都被判废。以前这里什么
        // 都不写，用户只看到"答案答非所问"，完全不知道检索用的是维语原文。
        metaEl.textContent = t.trFailed;
        bot.dataset.trFail = "1";
      }
      return;
    }
    if (ev.event === "stage") {
      const st = ev.data || {};
      showStatus(st.stage === "translating" ? t.translating : t.drafting);
      metaEl.textContent = st.stage === "translating" ? t.translating : t.drafting;
      // 中文草稿在译文开始流式之前就已完整生成。中文界面下直接上屏，
      // 不让用户盯着一段维吾尔语干等十几秒。
      if (st.stage === "translating" && st.draft) {
        bot.dataset.draft = st.draft;
        registerDraft(bot, answerEl);
      }
      return;
    }
    if (ev.event === "meta") {
      finalCit = (ev.data && ev.data.citations) || [];
      // 这一问的答案是用哪种语言写的。记在气泡上而不是 rec 里，
      // 是因为流式期间就会切语言，收尾时得知道"这条当时是按谁答的"。
      bot.dataset.alang = (ev.data && ev.data.answer_lang) || uiLang;
      showStatus(t.thinking);
      metaEl.textContent = t.thinking;
      return;
    }
    if (ev.event === "delta") {
      acc += ev.data.text || "";
      // 主文进度记在 dataset 上：切到中文后模型继续吐维语也不会把原稿冲掉，
      // 切回来能立刻还原到最新进度，而不是点击那一刻的快照。
      bot.dataset.main = acc;
      paintAnswer(bot, answerEl);
      metaEl.textContent = t.thinking;
      scrollToBottom();
      return;
    }
    if (ev.event === "error") {
      errorBubble(bot, ev.data && ev.data.message);
      return;
    }
    if (ev.event === "done") {
      const d = ev.data || {};
      const st = bot.querySelector(".answer.streaming");
      if (st) st.classList.remove("streaming");
      metaEl.remove();
      const rec = {
        q,
        a: acc,
        d: d.draft || bot.dataset.draft || "",
        ev: d.evidence || "",
        cit: d.citations || finalCit,
        ms: Date.now() - started,
        loop: !!d.loop_truncated,
        // 两种"翻译没成"分开记：问题没译（检索按原文，召回可能不准）和
        // 答案没译（正文是看不懂的中文）严重程度不同，提示语也不同。
        trq: bot.dataset.trFail === "1",
        tra: d.answer_translated === false,
        // 这条答案实际用的语言。切到另一种界面语言时，正文不会被本地"翻译"，
        // 与其让用户对着一段看不懂的文字，不如在底下写清楚它是什么语言。
        al: bot.dataset.alang || uiLang,
        // assistant 消息 id：反馈按它关联。前端不再需要别的信息，
        // 但少了它，这条答案就永远标注不上（也就等于白写了库）。
        mid: d.message_id || null,
      };
      decorateBot(bot, answerEl, rec);
      if (acc.trim()) {
        chat.msgs.push(rec);
        saveChats();
        renderSessions();
        history.push({ role: "user", content: q },
                     { role: "assistant", content: acc });
      }
      scrollToBottom();
    }
  }
}

function parseEvent(raw) {
  let event = "message";
  const dataLines = [];
  for (const line of raw.split("\n")) {
    if (line.startsWith("event: ")) event = line.slice(7).trim();
    else if (line.startsWith("data: ")) dataLines.push(line.slice(6));
  }
  if (!dataLines.length) return null;
  const joined = dataLines.join("\n");
  try {
    return { event, data: JSON.parse(joined) };
  } catch {
    return { event, data: joined };
  }
}
