/* 构建号：与 index.html 里那份一致，不一致说明浏览器拿到的是旧 JS，
   页面加载完会强制换一次（见 index.html 的自检脚本）。 */
window.ADMIN_BUILD = "0.9.118";

const $ = (id) => document.getElementById(id);

const state = {
  kbId: null,
  kbs: [],
  docs: [],
  status: null,
  // 会话历史归前端：后台不建会话、不落库。这一页的对话只活在 state.history，
  // 换页/刷新即清空；要接着聊的是前台站点，它有浏览器 localStorage。
  history: [],        // [{role:"user"|"assistant", content}, ...] 按时间正序
  lastCitations: [],
  lastCtx: null,
  lastConfidence: null,
  busy: false,
  view: "chat",
  models: null,        // /api/models 的快照，对话配置与模型页共用
  dash: null,
  presets: null,
  noModelWarned: false, // 「没配模型」只弹一次，反复弹会变成噪音
};

// 索引器预热轮询句柄（见 startWarmPoll）
let warmTimer = null;

/* ------------------------------------------------------------------ HTTP */

/* 切页为什么以前慢：每次 setView 都会把整页数据重拉一遍，其中
   /api/models 和 /api/gateway 要探本地模型进程，冷的时候接近 2 秒；站点页
   还把这三四个请求排成串行、先把卡片清空成「正在加载…」。用户看到的就是
   「点一下，白两秒」。

   切页是「看一眼」，不是「重新测一遍」。这些纯只读的展示型 GET 在 3 秒内
   复用上次结果，页面当帧就能画出来；任何写操作自动作废快照，保存完立刻
   拿到新值。轮询类接口（任务进度、/api/status）不在白名单里，永远实时。 */
const API_CACHE_TTL_MS = 3000;
const API_CACHEABLE = [
  /^\/api\/dashboard$/,
  /^\/api\/models$/,
  /^\/api\/gateway$/,
  /^\/api\/instances$/,
  /^\/api\/model-roles$/,
  /^\/api\/previsit\/config$/,
  /^\/api\/kb$/,
  /^\/api\/kb\/\d+\/detail$/,
];
const apiCache = new Map();
const apiInflight = new Map();

function invalidateApiCache() { apiCache.clear(); }

async function _apiFetch(path, options) {
  const res = await fetch(path, options);
  if (!res.ok) {
    let msg = `${res.status}`;
    let body = null;
    try {
      body = await res.json();
      msg = body.error || body.detail || msg;
    } catch (_) { /* 非 JSON 响应就只报状态码 */ }
    // 把原始响应体带出去：上层要能区分"500 系统炸了"和"409 源文件没了"，
    // 只靠 message 字符串匹配是脆的。
    const err = new Error(typeof msg === "string" ? msg : `HTTP ${res.status}`);
    err.status = res.status;
    err.body = body;
    throw err;
  }
  return res.status === 204 ? null : await res.json();
}

async function api(path, options = {}) {
  const { force, ...fetchOptions } = options;
  const method = (fetchOptions.method || "GET").toUpperCase();
  if (method !== "GET") invalidateApiCache();   // 写过了，旧快照一律不信
  const cacheable = method === "GET" && !force && API_CACHEABLE.some((re) => re.test(path));
  if (cacheable) {
    const hit = apiCache.get(path);
    if (hit && Date.now() - hit.t < API_CACHE_TTL_MS) return hit.v;
    const running = apiInflight.get(path);
    if (running) return running;                 // 同一个请求并发只发一次
    const pending = _apiFetch(path, fetchOptions).then((v) => {
      apiCache.set(path, { t: Date.now(), v });
      return v;
    }).finally(() => {
      if (apiInflight.get(path) === pending) apiInflight.delete(path);
    });
    apiInflight.set(path, pending);
    return pending;
  }
  return _apiFetch(path, fetchOptions);
}

const jsonPost = (path, data) =>
  api(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });

const jsonSend = (method, path, data) =>
  api(path, {
    method,
    headers: { "Content-Type": "application/json" },
    body: data === undefined ? undefined : JSON.stringify(data),
  });

/* 表单上传（清洗预览 / 带方案的导入都走它）。错误口径与 api() 保持一致：
   上层要能分清「400 规则写错了」和「413 文件太大」。 */
async function apiForm(path, fd) {
  const res = await fetch(path, { method: "POST", body: fd });
  if (!res.ok) {
    let msg = `${res.status}`;
    let body = null;
    try {
      body = await res.json();
      msg = body.error || body.detail || msg;
    } catch (_) { /* 非 JSON 响应就只报状态码 */ }
    const err = new Error(typeof msg === "string" ? msg : `HTTP ${res.status}`);
    err.status = res.status;
    err.body = body;
    throw err;
  }
  return res.status === 204 ? null : res.json();
}

/* ------------------------------------------------------------------ 渲染 */

function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/* 行内标记：代码 → 粗体 → 引用编号。
   顺序有讲究：先把 `code` 抽成占位符，否则代码里的 ** 会被当粗体；
   而引用编号必须在 esc 之后替换（esc 不动方括号，但 span 一旦先插进去，
   后面的正则就会把 span 里的数字也当成引用）。 */
function inlineMd(raw) {
  const codes = [];
  let s = String(raw).replace(/`([^`\n]+)`/g, (_m, code) => {
    codes.push(code);
    return `\u0000${codes.length - 1}\u0000`;
  });
  s = esc(s);
  // 只吃成对的、单行内的 **…**：古籍资料里常有孤立星号，贪婪匹配会把整段吃掉
  s = s.replace(/\*\*([^*\n]{1,160})\*\*/g, "<strong>$1</strong>");
  s = s.replace(/\[(\d{1,2})\]/g, (_m, n) => `<span class="cite-chip" data-n="${n}">${n}</span>`);
  s = s.replace(/\u0000(\d+)\u0000/g, (_m, i) => `<code>${esc(codes[Number(i)])}</code>`);
  return s;
}

/* 块级：标题 / 有序列表 / 无序列表 / 段落。
   模型（尤其 7B 这一档）几乎一律用 Markdown 分点作答，只换行不解析的话，
   用户看到的就是"1. **使用硝石**：……"这种带星号的原始字符串——答案内容
   没变，观感直接掉一个档次。 */
function mdBlocks(src) {
  const lines = String(src).replace(/\r/g, "").split("\n");
  const out = [];
  let para = [];
  let list = null;

  const flushPara = () => {
    if (para.length) {
      out.push(`<p>${para.map(inlineMd).join("<br>")}</p>`);
      para = [];
    }
  };
  const flushList = () => {
    if (list) {
      const tag = list.type;
      out.push(`<${tag}>${list.items.map((i) => `<li>${inlineMd(i)}</li>`).join("")}</${tag}>`);
      list = null;
    }
  };

  for (const raw of lines) {
    const t = raw.trim();
    if (!t) { flushPara(); flushList(); continue; }

    const h = t.match(/^(#{1,4})\s+(.+)$/);
    if (h) {
      flushPara(); flushList();
      const lv = Math.min(h[1].length + 2, 6);
      out.push(`<h${lv}>${inlineMd(h[2])}</h${lv}>`);
      continue;
    }
    const ul = t.match(/^[-*•]\s+(.+)$/);
    if (ul) {
      flushPara();
      if (!list || list.type !== "ul") { flushList(); list = { type: "ul", items: [] }; }
      list.items.push(ul[1]);
      continue;
    }
    const ol = t.match(/^(\d{1,2})[.、)]\s+(.+)$/);
    if (ol) {
      flushPara();
      if (!list || list.type !== "ol") { flushList(); list = { type: "ol", items: [] }; }
      list.items.push(ol[2]);
      continue;
    }
    // 列表项之间的普通行，说明列表结束了，别再往里塞
    flushList();
    para.push(t);
  }
  flushPara(); flushList();
  return out.join("");
}

function renderBody(text) {
  if (!text) return "";
  const parts = text.split(/```/);
  let html = "";
  parts.forEach((part, i) => {
    if (i % 2 === 1) {
      const body = part.replace(/^[\w-]*\n/, "");
      html += `<pre class="src-text u-my8">${esc(body.trim())}</pre>`;
    } else {
      html += mdBlocks(part);
    }
  });
  return html;
}

function renderKbList() {
  const box = $("kbList");
  box.innerHTML = "";
  state.kbs.forEach((kb) => {
    const el = document.createElement("div");
    el.className = "kb-item" + (kb.id === state.kbId ? " active" : "");
    el.innerHTML = `<span class="nm">${esc(kb.name)}</span><span class="ct">${kb.docs}</span>
      <button class="act" title="编辑这个知识库" aria-label="编辑">${ICON_EDIT}</button>`;
    el.onclick = (e) => {
      if (e.target.closest(".act")) { e.stopPropagation(); openKbInfo(kb); return; }
      selectKb(kb.id);
    };
    box.appendChild(el);
  });
  const cur = state.kbs.find((k) => k.id === state.kbId);
  $("currentKb").textContent = cur ? cur.name : "未选择知识库";
}

/* 图标比两个字更省地方，288px 的侧栏里"61 chunk · text@utf-8 + 重索引 + 删除"
   四段文字挤一行必然挤爆。图标配 title，鼠标停一下就有中文说明。 */
const ICON_REINDEX = `<svg viewBox="0 0 16 16" fill="none" stroke="currentColor"
  stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
  <path d="M13.4 8a5.4 5.4 0 1 1-1.7-3.9"/><path d="M13.4 2.4v3.4H10"/></svg>`;
const ICON_DELETE = `<svg viewBox="0 0 16 16" fill="none" stroke="currentColor"
  stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
  <path d="M2.8 4.4h10.4M6.4 4.4V2.9h3.2v1.5"/>
  <path d="M4.3 4.4l.55 8.1a1 1 0 0 0 1 .93h4.3a1 1 0 0 0 1-.93l.55-8.1"/></svg>`;
const ICON_REFRESH = `<svg viewBox="0 0 16 16" fill="none" stroke="currentColor"
  stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
  <path d="M13.4 8a5.4 5.4 0 1 1-1.7-3.9"/><path d="M13.4 2.4v3.4H10"/></svg>`;
const ICON_EYE = `<svg viewBox="0 0 16 16" fill="none" stroke="currentColor"
  stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
  <path d="M1.6 8S4 3.6 8 3.6 14.4 8 14.4 8 12 12.4 8 12.4 1.6 8 1.6 8Z"/>
  <circle cx="8" cy="8" r="2"/></svg>`;
const ICON_EDIT = `<svg viewBox="0 0 16 16" fill="none" stroke="currentColor"
  stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
  <path d="M11.1 2.6a1.5 1.5 0 0 1 2.1 0l.2.2a1.5 1.5 0 0 1 0 2.1L5.9 12.4l-3 .8.8-3 7.4-7.6Z"/>
  <path d="M10.2 3.5l2.3 2.3"/></svg>`;

function renderDocs() {
  const box = $("docList");
  box.innerHTML = "";
  $("docCount").textContent = state.docs.length;
  if (!state.docs.length) {
    box.innerHTML = `<p class="list-hint">还没有文档，点标题栏的 ↑ 导入。</p>`;
    return;
  }
  state.docs.forEach((d) => {
    const el = document.createElement("div");
    el.className = `doc-item ${d.status}`;
    const name = d.title || d.source_uri.split(/[\\/]/).pop();
    el.innerHTML = `
      <div class="nm-row">
        <span class="nm" title="${esc(d.source_uri)}">${esc(name)}</span>
        <span class="doc-actions">
          <button class="act" data-act="preview" title="预览切分结果" aria-label="预览">${ICON_EYE}</button>
          <button class="act" data-act="reindex"
            title="${d.missing_source ? "原始文件已不在，点这里重新上传并索引" : "重新切分与向量化"}"
            aria-label="重索引">${ICON_REINDEX}</button>
          <button class="act" data-act="delete" title="删除文档" aria-label="删除">${ICON_DELETE}</button>
        </span>
      </div>
      <div class="meta">
        <span class="meta-stat" title="提取器 ${esc(d.extractor || "-")}">${
          d.status === "ready" ? `${d.n_chunks} chunk · ${esc(d.extractor || "-")}` : esc(d.status)
        }</span>
        ${d.missing_source ? `<span class="meta-stat warn">源文件缺失</span>` : ""}
      </div>
      ${d.missing_source
        ? `<div class="reason">原始文件已不在，点 ↻ 重新上传同一个文件即可重新索引</div>`
        : d.fail_reason ? `<div class="reason">${esc(d.fail_reason)}</div>` : ""}`;

    el.onclick = (e) => {
      const btn = e.target.closest("[data-act]");
      const act = btn?.dataset.act;
      if (act === "preview") { previewDoc(d.id); return; }
      if (act === "reindex") { reindexDoc(d, btn); return; }
      if (act === "delete") { deleteDoc(d); return; }
      // 点行本身也是预览：这是"看一眼里面是什么"最自然的动作
      if (d.status === "ready") { previewDoc(d.id); return; }
      if (d.fail_reason) alert(`${d.title}\n\n${d.fail_reason}`);
    };
    box.appendChild(el);
  });
}

async function reindexDoc(d, btn) {
  // 源文件没了的时候，"重索引"这个动作注定失败。与其让用户点一次看一次报错，
  // 不如直接把入口换成"重新上传"。
  if (d.missing_source) { await restoreDocSource(d, btn); return; }
  if (btn) btn.disabled = true;
  try {
    const r = await reindexDocTask(d);
    if (r.error?.body?.missing_source) {
      if (btn) btn.disabled = false;
      await restoreDocSource(d, btn);
      return;
    }
    await loadKb();
    if (r.ok) toast(`${d.title}：${r.message}`);
    else alert(`${d.title}：${r.message}`);
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function deleteDoc(d) {
  if (!(await askConfirm(`删除「${esc(d.title)}」及其全部 chunk？<b>此操作不可撤销。</b>`,
      { title: "删除文档", okLabel: "删除" }))) return;
  const t0 = await api(`/api/doc/${d.id}`, { method: "DELETE" });
  // 删除也走后台任务：几千块的文档要清一阵子索引，给张进度卡而不是干等
  if (t0?.task_id) {
    followTask(t0.task_id, `删除 ${d.title}`);
  } else {
    await loadKb();
  }
}

/* ------------------------------------------------------------------ 预览 */
/*
 * 文档预览：直接看切出来的块长什么样。
 *
 * 分块是最容易"参数都对、结果不对"的一环——重叠把句子吃了一半、标题路径
 * 丢了、表格被从中间切开。以前只能靠提问时偶然撞见，或者去翻库。
 */
function chunkCards(list) {
  return (list || []).map((c) => `
    <article class="pv-chunk${c.hidden ? " hidden-chunk" : ""}">
      <header>
        <span class="seq">#${c.seq + 1}</span>
        <span class="n">${c.tokens} tok · ${c.chars} 字符</span>
        ${c.page_no ? `<span class="n">第 ${c.page_no} 页</span>` : ""}
        ${c.heading_path ? `<span class="n ell" title="${esc(c.heading_path)}">${esc(c.heading_path)}</span>` : ""}
        ${c.hidden ? `<span class="badge err">已隐藏</span>` : ""}
        <button class="mini" data-copy-chunk="${c.id}">复制原文</button>
      </header>
      <div class="pv-text">${esc(c.text)}</div>
    </article>`).join("");
}

/* 完整性自检的两处渲染共用一套话术：预览弹窗里的「完整性自检」，和内容页
   文档行的「自检」。结论必须说人话——"ratio 0.9989" 回答不了"导全了没有"。 */
function covHTML(r) {
  if (!r || !r.ok) return `<div class="reason">${esc(r?.reason || "无法校验")}</div>`;
  const pct = (Number(r.ratio || 0) * 100).toFixed(2);
  const clean = Number(r.big_gap_count || 0) === 0;
  // 百分比天生到不了 100%：标题的 ##、表格的竖线、代码块的缩进在比对前会被剥掉
  // （不剥的话每个标题都会被报成"缺了一段"）。所以主结论给"有没有成段缺失"，
  // 覆盖率只作旁证，并说明差值从哪来 —— 一个 98.42% 摆在那里不说清楚，
  // 比不摆更让人不安。
  return `<span class="badge ${clean ? "ok" : "warn"}">${clean
      ? "没有成段缺失" : `覆盖 ${pct}%`}</span>
    <span class="n">源文件 ${fmtInt(r.source_chars)} 字 · ${clean
      ? `覆盖 ${pct}%，差值来自标题标记与表格排版，不成段`
      : `成段缺失 ${r.big_gap_count} 处 / 最大 ${fmtInt(r.max_gap)} 字`}</span>
    ${r.stale_source ? `<div class="pv-gap">源文件在导入之后被改过：库里存的是上一版，
       点「重索引」即可同步最新版。</div>` : ""}
    ${r.doc_extractor && r.extractor && r.doc_extractor !== r.extractor
      ? `<div class="pv-gap">抽取器已升级（${esc(r.doc_extractor)} → ${esc(r.extractor)}），
         点「重索引」后内容会更全。</div>` : ""}
    ${clean ? "" : (r.gaps || []).map((g) =>
      `<div class="pv-gap">…${esc(g.text)}…</div>`).join("")}`;
}

/* 任务卡上那一小行：导入/重索引结束时后端已经顺手算过覆盖率，这里只负责
   把它翻译成一句话。 */
function covLine(cov) {
  if (!cov) return "";
  const pct = (Number(cov.ratio || 0) * 100).toFixed(2);
  if (cov.stale_source) return ` · 源文件改过 · 覆盖 ${pct}%，重索引即同步`;
  if (cov.big_gap_count) {
    return ` · 覆盖 ${pct}% · 成段缺失 ${cov.big_gap_count} 处（最大 ${fmtInt(cov.max_gap)} 字）`;
  }
  return ` · 内容完整（没有成段缺失，覆盖 ${pct}%）`;
}

async function previewDoc(docId) {
  let d;
  try {
    d = await api(`/api/doc/${docId}/preview?offset=0&limit=200`);
  } catch (err) {
    alert(`预览失败：${err.message}`);
    return;
  }
  const doc = d.doc || {};
  const name = doc.title || String(doc.source_uri || "").split(/[\\/]/).pop();
  const first = d.chunks || [];
  const total = Number(d.total || first.length);
  let loaded = first.length;
  const avg = first.length ? Math.round((d.tokens || 0) / first.length) : 0;

  const meta = [
    ["来源", doc.source_uri || "-"],
    ["类型", `${doc.source_type || "-"} · ${doc.extractor || "-"}`],
    ["状态", doc.status || "-"],
    ["切分", `${total} 块`],
    ["平均块长", first.length ? `${avg} tok` : "-"],
    ["页数", doc.n_pages ? String(doc.n_pages) : "-"],
    ["导入", String(doc.imported_at || "").replace("T", " ").slice(0, 16)],
  ].map(([k, v]) => `<div class="kv2">
      <span class="k2">${esc(k)}</span>
      <span class="v2 ell" title="${esc(v)}">${esc(v)}</span></div>`).join("");

  // 「已加载 M / 共 N」必须写在明面上：以前后端一次最多给 500 块，一本
  // 1280 块的书预览只显示前 39%，用户看到的就是"我的文件没全进去"。
  // 现在分批加载，进度摆在眼前，缺不缺一眼能判断。
  const body = `<div class="pv-meta">${meta}</div>
    ${doc.fail_reason ? `<div class="reason u-my8">${esc(doc.fail_reason)}</div>` : ""}
    <div class="pv-bar">
      <span>已加载 <b id="pvLoaded">${loaded}</b> / ${total} 块</span>
      <button class="mini" id="pvMore"${d.has_more ? "" : " disabled"}>${d.has_more
        ? `再加载 ${Math.min(500, total - loaded)} 块` : "已全部加载"}</button>
      <button class="mini" id="pvCheck">完整性自检</button>
      <span class="pv-check-out" id="pvCheckOut"></span>
    </div>
    ${first.length
      ? `<div class="pv-list" id="pvList">${chunkCards(first)}</div>`
      : `<p class="empty-tip" style="padding:16px 4px">这个文档还没有可展示的 chunk。
         ${doc.status === "ready" ? "可能是旧代次残留，试试点「重索引」。" : ""}</p>`}`;

  showModal(`预览 · ${name}`, body, null, { ok: false, wide: true });

  // 预览里**不提供跳去对话页的入口**。原来的「原文面板」会把视图切到 chat
  // 再弹引用面板：在知识库页核对分块的人被强行带走，还得手动切回来。预览
  // 本身就把原文摆在下面了，需要带走内容的场景给「复制」就够了。
  const bindCopy = (root) => {
    root.querySelectorAll("[data-copy-chunk]").forEach((btn) => {
      if (btn.dataset.bound) return;
      btn.dataset.bound = "1";
      btn.onclick = async () => {
        const text = btn.closest(".pv-chunk")?.querySelector(".pv-text")?.textContent || "";
        try {
          await navigator.clipboard.writeText(text);
          toast("已复制该块原文");
        } catch {
          // 非 HTTPS / 无剪贴板权限时退回选中，用户自己 Ctrl+C
          const r = document.createRange();
          r.selectNodeContents(btn.closest(".pv-chunk").querySelector(".pv-text"));
          const sel = getSelection();
          sel.removeAllRanges();
          sel.addRange(r);
          toast("已选中该块，按 Ctrl+C 复制", "warn");
        }
      };
    });
  };
  bindCopy($("modalBody"));

  const more = $("pvMore");
  if (more) more.onclick = async () => {
    more.disabled = true;
    const label = more.textContent;
    more.textContent = "加载中…";
    try {
      const nd = await api(`/api/doc/${docId}/preview?offset=${loaded}&limit=500`);
      const got = nd.chunks || [];
      const list = $("pvList");
      if (list) list.insertAdjacentHTML("beforeend", chunkCards(got));
      loaded += got.length;
      const el = $("pvLoaded");
      if (el) el.textContent = loaded;
      bindCopy(list || $("modalBody"));
      if (nd.has_more) {
        more.disabled = false;
        more.textContent = `再加载 ${Math.min(500, total - loaded)} 块`;
      } else {
        more.textContent = "已全部加载";
      }
    } catch (err) {
      more.disabled = false;
      more.textContent = label || "重试";
    }
  };

  const chk = $("pvCheck");
  if (chk) chk.onclick = async () => {
    const out = $("pvCheckOut");
    chk.disabled = true;
    out.textContent = "正在重新抽取源文件比对…";
    try {
      const r = await api(`/api/doc/${docId}/coverage`);
      out.innerHTML = covHTML(r);
    } catch (err) {
      out.textContent = `校验失败：${err.message}`;
    } finally {
      chk.disabled = false;
    }
  };
}

/* ------------------------------------------------------------------ 会话 */
/*
 * 后台不存会话历史：这一页的对话只活在内存里（state.history），换页/刷新即
 * 清空。要长期接着聊的是前台站点，它有自己的浏览器 localStorage 会话列表。
 * 侧栏这张卡片只报这一页的轮数和上下文占用，加一个"重新开始"按钮。
 */

function renderSessions() {
  const box = $("sessionList");
  if (!box) return;
  const turns = Math.ceil(state.history.length / 2);
  const ctx = state.lastCtx;
  // 把"还能聊多少"直接画出来：上下文满没满，用户不该靠模型突然变笨去猜。
  const used = ctx && ctx.budget
    ? Math.min(100, Math.round((Number(ctx.history_chars || 0) / Number(ctx.budget)) * 100))
    : 0;
  const lvl = used >= 100 ? "err" : used >= 70 ? "warn" : "ok";
  box.innerHTML = `
    <div class="session-card">
      <div class="sc-head">
        <span class="sc-title">本次对话（刷新页面即清空）</span>
        <span class="badge${turns ? "" : " muted"}">${turns ? `${turns} 轮` : "空"}</span>
      </div>
      <div class="sc-meta">
        <span>${state.history.length} 条消息</span>
        <span class="sc-ctx ${lvl}">上下文 ${used}%</span>
      </div>
      <div class="sc-bar"><i style="width:${used}%"></i></div>
      <button class="sc-refresh" data-act="refresh" title="清空这页的对话，重新开始">
        ${ICON_REFRESH}<span>重新开始</span>
      </button>
    </div>`;
  const btn = box.querySelector("[data-act=refresh]");
  if (btn) btn.onclick = (e) => { e.stopPropagation(); refreshSession(); };
}

/* 重新开始：只清内存里的这一页对话和消息区，不碰任何库表。 */
function refreshSession() {
  if (state.busy) {
    alert("这一轮还在回答，等结束再重新开始。");
    return;
  }
  state.history = [];
  state.lastCtx = null;
  renderSessions();
  renderEmptyChat("已重新开始", "这一页的对话已清空，从这里重新开始。");
  $("question")?.focus();
}

/* 追加一轮问答到本次对话。done 时才追加——中途流式输出不算一轮，
   回答失败也不把半截答案喂给下一轮。 */
function rememberTurn(question, answer) {
  state.history.push({ role: "user", content: question });
  state.history.push({ role: "assistant", content: answer || "" });
  renderSessions();
}

/* 能不能发问：模型没配好就不发。发出去也是 500 或空回答，还不如当场说清楚。 */
function canChat() {
  if (!state.models) return { ok: true, unknown: true };  // 还没拉到注册表，不拦
  const p = providerNow();
  if (!p) return { ok: false, reason: "还没有任何生成后端，先去「模型管理」加一个" };
  if (!p.ready) {
    return { ok: false, provider: p.name,
      reason: p.ready_reason || `「${p.name}」还不能用` };
  }
  return { ok: true, provider: p.name };
}

function noModelCard(reason) {
  return `<div class="empty-state gate" id="emptyState">
    <span class="es-mark">!</span>
    <h2>还没配模型</h2>
    <p>${esc(reason)}。<b>没有可用的生成后端，提问只会返回检索片段，不会生成答案。</b></p>
    <div class="gate-actions">
      <button class="primary-btn" data-gate="models">去配置模型</button>
      <button class="ghost-btn" data-gate="mock">先用 Mock 看检索链路</button>
      <button class="ghost-btn" data-gate="kb">先去建知识库</button>
    </div>
    <div class="es-keys">
      <span>Mock 不联网，只跑检索和装填，输出不是答案</span>
    </div>
  </div>`;
}

function bindGateActions() {
  document.querySelectorAll("[data-gate]").forEach((btn) => {
    btn.onclick = async () => {
      const act = btn.dataset.gate;
      if (act === "models") return setView("models");
      if (act === "kb") return setView("kb");
      if (act === "mock") {
        btn.disabled = true;
        try {
          await pickProvider("mock");
          toast("这一问改用 Mock 后端，可以开始了");
        } catch (err) {
          alert(err.message);
        } finally {
          btn.disabled = false;
        }
      }
    };
  });
}

/* 第一次进对话页时弹一次。反复弹会把"提醒"变成"噪音"，用户会连点关掉
   而不再看它写的是什么。 */
function maybeWarnNoModel() {
  const c = canChat();
  if (c.ok || state.noModelWarned) return;
  state.noModelWarned = true;
  const close = showModal("还没有可用的模型", `
    <p class="hint u-mb12">
      ${esc(c.reason)}。<br>没有可用的生成后端，提问只会返回检索片段。
    </p>
    <div class="gate-actions" style="justify-content:flex-start;margin:0">
      <button class="ghost-btn" id="warnMock">用 Mock 看检索链路</button>
      <button class="ghost-btn" id="warnKb">先去建知识库</button>
    </div>
    <p class="hint u-mt12">
      Mock 不联网，只跑检索和装填，输出不是答案。
    </p>`,
    async () => { await setView("models"); },
    { okLabel: "去配置模型" });

  // 弹窗里说的每一个出口都得是能点的按钮，否则那句"先点 Mock 跑通"就是在
  // 让用户去找一个不存在的东西。
  $("warnMock").onclick = async () => {
    close();
    await pickProvider("mock");
    toast("这一问改用 Mock 后端，可以开始了");
  };
  $("warnKb").onclick = async () => { close(); await setView("kb"); };
}

function renderEmptyChat(title = "问点什么",
  sub = "先导入文档，再直接提问。每个回答都标注来源编号，点编号可回查原文片段。") {
  const c = canChat();
  if (!c.ok) {
    $("chat").innerHTML = noModelCard(c.reason);
    bindGateActions();
    return;
  }
  $("chat").innerHTML = `<div class="empty-state" id="emptyState">
    <span class="es-mark">R</span>
    <h2>${esc(title)}</h2>
    <p>${esc(sub)}</p>
    <div class="es-keys">
      <span><kbd>Enter</kbd> 发送</span>
      <span>顶栏「对话配置」切换知识库与后端</span>
      <span><kbd>Alt</kbd>+<kbd>,</kbd> 对话配置</span>
    </div>
  </div>`;
}

function scrollToBottom() {
  const box = $("chat");
  if (box) box.scrollTop = box.scrollHeight;
}


/* 状态按库取：标定是逐库做的，不带 kb_id 拿到的是全局配置，
   换库后界面会继续显示上一个库的阈值和 AUC。 */
function statusUrl() {
  return state.kbId ? `/api/status?kb_id=${state.kbId}` : "/api/status";
}

function startWarmPoll() {
  if (warmTimer) return;
  // 2 秒一次，直到就绪。轮询而不是等长超时：冷启动 30 秒和 70 秒都可能，
  // 固定 sleep 要么白等要么不够。
  warmTimer = setInterval(async () => {
    try {
      const s = await api(statusUrl());
      state.status = s;
      renderStatus();
      if (s.embedder?.available) {
        stopWarmPoll();
        toast("索引器就绪，可以提问了");
        await loadKb();
      }
    } catch (_) { /* 服务还没起来，下一轮再试 */ }
  }, 2000);
}

function stopWarmPoll() {
  if (warmTimer) { clearInterval(warmTimer); warmTimer = null; }
}

function renderStatus() {
  const s = state.status;
  if (!s) return;
  // 侧栏底部只留两条——「现在能不能提问」和「由谁回答」。旧版那排里 chunk
  // 数在知识库「信息」页、精排模型与候选数在本库「参数」页、后端数量在模型页，
  // 同一件事不该有两个权威位置，所以那几行连同全局参数按钮一起不再回来。
  //
  // 这两条和「发送按钮能不能点」共用同一份判断：索引没准备好就别让点发送，
  // 同一句话也写进侧栏，省得用户对着灰按钮猜原因。
  const ok = s.embedder.available;
  const warm = s.embedder.warmup || {};
  const send = $("btnSend");
  const fe = $("footEmbed");
  const fd = $("footEmbedDot");
  if (ok) {
    send.title = "";
    stopWarmPoll();
    // 就绪后必须把按钮放回来 —— 只关不开，用户会以为还卡在启动中。
    // 用 busy 兜底：正在回答时不能被状态刷新重新点亮。
    send.disabled = state.busy;
    if (fe) fe.textContent = "就绪";
    if (fd) fd.className = "dot ok";
  } else if (warm.state === "pending" || warm.state === "loading") {
    // 冷启动 40 秒。这段时间按钮必须是"等"而不是"点下去转圈"：
    // 用户能看见还剩多久，就不会以为程序坏了。
    const sec = Math.round((warm.elapsed_ms || 0) / 1000);
    send.title = `索引器启动中 ${sec}s，就绪后按钮自动亮`;
    send.disabled = true;
    startWarmPoll();
    if (fe) fe.textContent = `启动中 ${sec}s`;
    if (fd) fd.className = "dot warn";
  } else {
    send.title = "嵌入模型不可用：到「模型」页把它修好再提问";
    send.disabled = state.busy;
    stopWarmPoll();
    if (fe) fe.textContent = "不可用";
    if (fd) fd.className = "dot err";
  }

  const pill = $("refusalPill");
  const pillText = $("refusalText");
  // 四种状态必须分开说，别再显示一个"看起来像个决定"的数字：
  // advisory 时那个 0.35 低于本语料全部可答分（最低 0.541），除了检索完全空
  // 一次都不会触发。把它渲染成"阈值 0.35"，等于告诉用户有个判定在保护他。
  // calibrated 时也别只给数字：AUC 才是这个判据有多可信的依据，
  // 阈值只是 AUC 达标之后才敢用的那个切点。
  const rmode = s.refusal?.mode || (s.refusal_calibrated ? "calibrated" : "advisory");
  const note = s.refusal?.note;
  const auc = s.refusal?.auc;
  const n = s.refusal?.n_samples;
  if (rmode === "off") {
    pill.hidden = true;
    pill.title = "";
  } else if (rmode === "invalid") {
    pill.hidden = false;
    pillText.textContent = "拒答判据不成立";
    pill.title = `留一法标定 AUC ${auc != null ? auc.toFixed(2) : "-"}，`
      + "低于 0.7 意味着这个分数分不开「库里有答案」和「库里没有」，"
      + "调阈值只能在误拒和漏拒之间二选一。已停用该判据，只保留「明显不沾边」粗筛。";
  } else if (rmode === "advisory") {
    pill.hidden = false;
    pillText.textContent = "无有效拒答判定";
    pill.title = (note ? `${note}。` : "拒答阈值还没标定过。")
      + "top1 余弦量的是「话题像不像」，主题相邻的问题分数照样高，分不开该拒的和该答的。"
      + "所以现在只在「明显不沾边」时拦，真正的依据看每份答案的支撑度。";
  } else if (rmode === "calibrated") {
    pill.hidden = false;
    const weak = s.refusal?.verdict === "weak";
    pillText.textContent = weak
      ? `阈值 ${s.refusal.min_score}（弱）`
      : `阈值 ${s.refusal.min_score}`;
    pill.title = `留一法标定：AUC ${auc != null ? auc.toFixed(2) : "-"}，${n || 0} 条样本。`
      + (weak
        ? "0.70~0.80 属于区分力有限，只当粗筛用。"
        : "误拒率压到 5% 以内取的最小漏拒点，只当粗筛用——真正的依据看支撑度。");
  } else {
    pill.hidden = true;
    pill.title = "";
  }


  // 后端下拉不在这里渲染：它的数据源是模型注册表（/api/models），而不是
  // /api/status 里的 providers。两处各渲染一次的结果是——在模型页新增一个
  // 后端，对话页的下拉里始终没有它，因为没人再拉一次 status。
  syncProviderOptions();
  // 生成后端取「当前下拉选中的那个」，不是 /api/status 的 generation：
  // 后者只剩 known/count，早就不带 configured/provider 了，照旧字段读会得到
  // 一个永远红着写「未选择」的假故障。
  renderFootGen();
}

/* 侧栏「生成」那行：此刻用谁回答。和下拉共用 providerNow()，切后端即时跟着变。 */
function renderFootGen() {
  const fg = $("footGen");
  if (!fg) return;
  const gd = $("footGenDot");
  const p = providerNow();
  if (!p) {
    fg.textContent = "未选择";
    fg.title = "还没有可用的生成后端，到「模型服务」页添加一个。";
    if (gd) gd.className = "dot err";
    return;
  }
  fg.textContent = p.name;
  fg.title = `${p.name}（${p.kind_label || p.kind || "自定义"}）\n`
    + `${p.model || "未指定模型"} @ ${p.base_url || "未配置地址"}\n`
    + `${p.message || "还没探测"}`;
  // 配置齐全就够了，不必等探测：本地模型没启动时 status=unknown，但后端本身
  // 是能用的。灰点在这个位置太淡，看着像少了一个。
  if (gd) gd.className = p.ready ? "dot ok" : dotOf(p.status);
}

/* 后端下拉的唯一数据源：/api/models。
   模型页增删改、探测完、切回对话，都调这里重刷，保证"我配过的就是我能选的"。 */
async function loadProviders() {
  try {
    const d = await api("/api/models");
    state.models = d;
    syncProviderOptions(d);
    return d;
  } catch (_) {
    // 拉不到就保留上一次的选项，不要把下拉清成空的——那等于把当前后端也弄丢了
    return state.models;
  }
}

function syncProviderOptions(d = state.models) {
  const sel = $("providerSel");
  if (!sel) return;
  const list = (d && d.providers) || [];
  if (!list.length) return;
  const prev = sel.value || localStorage.getItem("knowtune.provider") || "";
  sel.innerHTML = "";
  list.forEach((p) => {
    const o = document.createElement("option");
    o.value = p.name;
    // 状态用极短后缀，不占宽度：150px 的下拉塞不下"● 就绪 · 2 个模型"。
    // 未探测不打标——首屏四个选项全挂"（未探测）"读起来像四个故障。
    // 缺配置优先于连通状态：密钥没设的云端后端"连不上"是配置问题，重试没用。
    const mark = !p.ready ? "（缺配置）"
      : p.status === "unset" ? "（未配置）"
      : p.status === "mock" ? "（mock）"
      : (p.status === "down" || p.status === "timeout" || p.status === "error")
        ? "（未连通）" : "";
    // 谁是"当前"已经不标了：没有全局当前这回事，选项末尾再挂个「当前」
    // 只会让人以为选了它就等于改了全局。
    o.textContent = `${p.name} · ${shortModel(p.model)}${mark}`;
    o.title = `${p.name}（${p.kind_label || p.kind || "自定义"}）\n`
      + `${p.model} @ ${p.base_url || "未配置地址"}\n${p.message || "还没探测"}`;
    sel.appendChild(o);
  });
  // 没有记忆值时不要盲选第一个：后端顺序是按名字排的，"第一个"很可能是个
  // 缺密钥的云端。优先落在当前真的能用的那个上——这本来就是"后台自己决定
  // 用谁"的意思，用户不需要为此做一次选择。
  const fallback = list.find((p) => p.ready) || list[0];
  const want = prev && list.some((p) => p.name === prev) ? prev : fallback.name;
  sel.value = want;
  const cur = list.find((p) => p.name === sel.value);
  sel.title = cur ? `${cur.model} @ ${cur.base_url || "未配置"}` : "";
  // 探测完模型列表会变（比如刚探出 67 个可用模型），生成模型的下拉要跟着换
  syncGenModelOptions();
  renderFootGen();
}

function shortModel(m) {
  return String(m || "")
    .replace(/^.*\//, "")
    .replace(/[-_ ]?(instruct|chat|it)$/i, "");
}

/* 单个模型名的极短显示：shortModel 再去掉量化尾巴。用在站点「当前运行」
   那颗模型牌上——只有一个名字，没法像 optgroup 那样算共同后缀。 */
function trimQuant(m) {
  return shortModel(m).replace(/[-_ ]?Q\d[_\w]*$/i, "");
}

/* 一组模型名人人都有、且在连字符后开始的共同尾巴。只有"全体都有"才算：
   一个组里混了 Q4/Q8 时共同后缀为空，量化信息本来就该各自留着。 */
function commonSuffix(list) {
  if (list.length < 2) return "";
  let suf = list[0];
  for (const s of list.slice(1)) {
    let i = suf.length - 1, j = s.length - 1;
    while (i >= 0 && j >= 0 && suf[i] === s[j]) { i--; j--; }
    suf = suf.slice(i + 1);
    if (!suf) return "";
  }
  const m = /[-_]/.exec(suf);
  // 尾巴本身以分隔符开头（如 -Q4_K_M）时整段都要；分隔符在中间说明前面
  // 那截也是共有的，只从分隔符起算；没有分隔符（如共同尾巴是 "7B"）不切。
  return m ? (m.index === 0 ? suf : suf.slice(m.index)) : "";
}

/* -------------------------------------------------------- 对话配置弹层 */

function providerNow() {
  const list = state.models?.providers || [];
  return list.find((p) => p.name === $("providerSel")?.value)
    || list.find((p) => p.ready) || list[0] || null;
}

function syncKbOptions() {
  const sel = $("kbSel");
  if (!sel) return;
  // 「不挂载」必须是第一个且永远存在的选项：只有一个库时如果没有它，
  // 想"只测模型不检索"就只能去改配置。空字符串 = kb_id=null。
  sel.innerHTML = `<option value="">（不挂载知识库）</option>` + (state.kbs.length
    ? state.kbs.map((k) =>
        `<option value="${k.id}">${esc(k.name)}</option>`).join("")
    : `<option value="" disabled>（还没有知识库）</option>`);
  sel.value = state.kbId ? String(state.kbId) : "";

  const kb = state.kbs.find((k) => k.id === state.kbId);
  $("kbMountNote").textContent = kb ? `${state.docs.length} 篇文档` : "";
  $("kbSelHint").textContent = kb
    ? (state.docs.length
        ? `只在这一个库里检索，当前 ${state.docs.length} 篇文档。`
        : "这个库还没有文档，先去「知识库」页导入。")
    : "不挂载也能问：下面选「直接问模型」就是纯对话，不检索、不带引用编号。";
  syncNoKbRow();
}

/* 不挂载知识库时才有意义的两行：这一问到底走不走检索。
   藏起来是因为挂载了库的时候这两行纯属噪音。 */
/* ------------------------------------------------------ 高级参数（临时覆盖）

   这些旋钮的共同点：只想在这一问上试试，不想写进配置。
   温度、历史轮数、拒答开关的落点是后台「站点 → 回答行为」，按站点主库保存；
   调试对话不再另摆一份。这里只剩模型 / 最大输出 / 流式这类 per-call 参数，
   以及走 overrides 点分路径的检索项。后端用 kbconfig.effective 合成临时副本，
   白名单外的键直接丢掉。 */
const ADV_KEY = "knowtune.chatOpts";

const advEls = () => ({
  model: $("genModelSel"), maxTok: $("optMaxTok"),
  stream: $("optStream"), fusion: $("optFusion"),
  wdense: $("optWdense"), pool: $("optPool"), mmr: $("optMmr"),
  noKbMode: $("noKbMode"),
});

const advNum = (v) => {
  const s = String(v ?? "").trim();
  if (s === "" || Number.isNaN(Number(s))) return null;
  return Number(s);
};

/* 留空一律"跟随配置"：前端不猜默认值，后端那份才是准的。
   传了空字符串过去会被当成 0，比不传更糟。 */
function collectOverrides() {
  const el = advEls();
  const ov = {};
  if (el.fusion.value) ov["retrieval.fusion.method"] = el.fusion.value;
  const w = advNum(el.wdense.value);
  if (w !== null) ov["retrieval.fusion.w_dense"] = w;
  const pool = advNum(el.pool.value);
  if (pool !== null) ov["retrieval.fusion.top_k"] = pool;
  if (el.mmr.value !== "") ov["retrieval.mmr.enabled"] = el.mmr.value === "1";
  return ov;
}

function saveAdv() {
  const el = advEls();
  try {
    localStorage.setItem(ADV_KEY, JSON.stringify({
      model: el.model.value || "", modelCustom: $("genModelCustom")?.value || "",
      maxTok: el.maxTok.value, stream: el.stream.checked, fusion: el.fusion.value,
      wdense: el.wdense.value, pool: el.pool.value, mmr: el.mmr.value,
      noKbMode: el.noKbMode.value,
    }));
  } catch (_) { /* 隐私模式下 localStorage 会抛，存不下就算了，不影响提问 */ }
}

function restoreAdv() {
  let saved = {};
  try { saved = JSON.parse(localStorage.getItem(ADV_KEY) || "{}") || {}; } catch (_) { saved = {}; }
  const el = advEls();
  el.maxTok.value = saved.maxTok ?? "";
  el.stream.checked = saved.stream !== false;
  el.fusion.value = saved.fusion || "";
  el.wdense.value = saved.wdense ?? "";
  el.pool.value = saved.pool ?? "";
  el.mmr.value = saved.mmr ?? "";
  el.noKbMode.value = saved.noKbMode || "direct";
  const ci = $("genModelCustom");
  if (ci) ci.value = saved.modelCustom || "";
  // 老版本存的是空字符串（那一项叫「跟随当前后端」）。默认模型这项已经撤掉，
  // 首屏迁成明确的「后端::模型」并写回，之后换后端不会再偷偷跟着变。
  if (!saved.model) {
    const pv = providerNow();
    if (pv?.name && pv?.model) {
      saved.model = genValue(pv.name, pv.model);
      try { localStorage.setItem(ADV_KEY, JSON.stringify(saved)); } catch (_) {}
    }
  }
  // 生成模型要等后端下拉填完才有选项，这里先记下，syncGenModelOptions 里回填
  el.model.dataset.want = saved.model || "";
}

function resetAdv() {
  const el = advEls();
  el.maxTok.value = "";
  el.wdense.value = ""; el.pool.value = "";
  el.fusion.value = ""; el.mmr.value = "";
  el.stream.checked = true;
  const ci = $("genModelCustom");
  if (ci) ci.value = "";
  saveAdv();
  renderCfgSummary();
}

/* 对话页的生成模型下拉 = 全部后端 × 全部已知模型，一层平铺。
   以前它只列"当前后端"的候选，用户想用另一个后端上的模型，得先去上面切后端、
   再回来切模型——两层嵌套，而第二层还常常是空的（探测缓存被清过）。看起来
   就是"我加过的模型根本选不到"。现在按后端分组，选哪个模型就自动带上哪个
   后端，不用再分两步。 */
const GEN_CUSTOM = "__custom__";
const GEN_SEP = "\u001f";

function genValue(provider, model) {
  return model ? `${provider}${GEN_SEP}${model}` : "";
}

function parseGenPick() {
  const sel = $("genModelSel");
  if (!sel || !sel.value || sel.value === GEN_CUSTOM) return { provider: null, model: null };
  const [p, m] = sel.value.split(GEN_SEP);
  return { provider: p || null, model: m || null };
}

function toggleGenCustom(on) {
  const inp = $("genModelCustom");
  if (!inp) return;
  inp.hidden = !on;
  if (on) setTimeout(() => inp.focus(), 0);
}

function syncGenModelOptions() {
  const sel = $("genModelSel");
  if (!sel) return;
  const d = state.models || {};
  const cat = d.catalog || [];
  const rawWant = sel.dataset.want || sel.value;

  const byProv = new Map();
  cat.forEach((c) => {
    if (!byProv.has(c.provider)) byProv.set(c.provider, []);
    byProv.get(c.provider).push(c);
  });
  // 按名字排，不再把某个后端顶到第一：页面没有"当前"这个概念了，
  // 还按它排序，等于在暗处维持一个已经在界面上撤掉的东西。
  const names = [...byProv.keys()].sort((a, b) => a.localeCompare(b));

  const groups = names.map((n) => {
    const items = byProv.get(n);
    // 与站点角色下拉同一套短名规则：显示去厂商前缀 / -Instruct / 全组共有的
    // 量化尾巴，完整 id 留 title，value 一个字不动（保存读的是 value）。
    const shorts = items.map((c) => shortModel(String(c.model || "")));
    const common = commonSuffix(shorts);
    const labels = shorts.map((s) =>
      shortModel(common && s.length > common.length ? s.slice(0, -common.length) : s));
    const counts = new Map();
    labels.forEach((s) => counts.set(s, (counts.get(s) || 0) + 1));
    const body = items.map((c, i) => {
      // 短名撞车时两边都退回全名：只让后一个退回，前一个仍显示短名，
      // 展开后仍是两个看起来一样、却指向不同模型的选项。
      const dup = counts.get(labels[i]) > 1;
      const text = dup ? c.model : labels[i];
      return `<option value="${esc(genValue(c.provider, c.model))}" title="${esc(c.model)}">${
        esc(text)}</option>`;
    }).join("");
    return `<optgroup label="${esc(n)}">${body}</optgroup>`;
  }).join("");

  // 不再提供「跟随当前后端 · 默认模型」这一项：选了等于没选，换后端时
  // 它还会偷偷跟着变。老 localStorage 里那条空值在 restoreAdv() 里一次性
  // 迁成具体的「后端::模型」，这里只负责渲染，不在每次刷新下拉时改用户选择。
  const has = (v) => cat.some((c) => genValue(c.provider, c.model) === v);
  // 迁移出来的模型未必在清单里（比如后端自己的 model 没被加进 models）。
  // 补一条占位把选中值留住，不然浏览器会退回占位，用户一保存就丢。
  let legacy = "";
  if (rawWant && rawWant !== GEN_CUSTOM && !has(rawWant) && rawWant.includes(GEN_SEP)) {
    const legacyModel = rawWant.split(GEN_SEP)[1] || "";
    legacy = `<option value="${esc(rawWant)}" title="${esc(legacyModel)}">${
      esc(shortModel(legacyModel))}（未加入清单）</option>`;
  }
  sel.innerHTML = `<option value="" disabled>选择模型…</option>`
    + groups + legacy
    + `<option value="${GEN_CUSTOM}">＋ 手动输入模型名…</option>`;

  sel.value = rawWant === GEN_CUSTOM ? GEN_CUSTOM : (has(rawWant) || legacy ? rawWant : "");
  sel.dataset.want = "";
  toggleGenCustom(sel.value === GEN_CUSTOM);
}

function syncNoKbRow() {
  const off = !state.kbId;
  $("noKbRow").hidden = !off;
  $("noKbHint").hidden = !off;
  if (!off) return;
  $("noKbHint").textContent = $("noKbMode").value === "direct"
    ? "完全不检索：模型凭自身知识作答，无引用编号，也不会因相关度不足而拒答。"
    : "在全部知识库的字块里检索（不限定某一个库），回答仍带引用编号。";
}

function renderCfgSummary() {
  if (!$("cfgSummary")) return;
  const kb = state.kbs.find((k) => k.id === state.kbId);
  const prov = providerNow();
  const bits = [kb ? kb.name : (state.kbs.length ? "不挂载知识库" : "未挂载知识库")];
  bits.push(prov ? prov.name : "未配模型");
  // 这一问用的模型也是"当前配置"的一部分：只显示后端名时，用户看不出
  // 自己刚才选的是 A 后端的 qwen 还是 B 后端的 deepseek。
  const gm = parseGenPick();
  const typed = ($("genModelCustom")?.value || "").trim();
  const mSel = $("genModelSel")?.value;
  if (gm.model) bits.push(shortModel(gm.model));
  else if (mSel === GEN_CUSTOM && typed) bits.push(shortModel(typed));
  if (!kb && $("noKbMode")?.value === "direct") bits.push("直问模型");
  if ($("deepMode").checked) bits.push("深度");
  if ($("topKSel").value) bits.push(`Top${$("topKSel").value}`);
  const ov = collectOverrides();
  const nOv = Object.keys(ov).length;
  if (nOv) bits.push(`+${nOv} 项覆盖`);
  $("cfgSummary").textContent = bits.join(" · ");

  // 一个圆点同时表达"有没有选"和"连不连得通"：选都没选是灰，选了但连不上是红。
  const st = prov ? prov.status : "unset";
  $("cfgDot").className = "dot " + (prov && !prov.ready ? "unknown" : dotOf(st));

  const hint = $("providerHint");
  if (hint) {
    // 这里只描述"这一问会用谁"。以前还要跟一句"当前是 X ＋ 设为当前"，
    // 因为页面上另有一套全局默认；那套撤掉之后，下拉就是唯一答案。
    hint.innerHTML = !prov
      ? "还没有可用的后端，去「模型管理」配一个。"
      : !prov.ready
        ? `这个后端还不能用：${esc(prov.ready_reason || "配置不完整")}`
        : esc(prov.message || "还没探测");
    hint.className = "cfg-hint" + (prov && !prov.ready ? " warn" : "");
  }
  syncKbOptions();
}

function toggleCfgPop(force) {
  const pop = $("cfgPop");
  const btn = $("btnChatCfg");
  if (!pop || !btn) return;
  const next = force === undefined ? pop.hidden : Boolean(force);
  pop.hidden = !next;
  btn.setAttribute("aria-expanded", String(next));
  btn.classList.toggle("on", next);
  if (next) {
    syncKbOptions();
    renderCfgSummary();
  }
}

/* -------------------------------------------------------------- 调试面板 */

/* 降级码是给日志看的（rerank_unavailable），界面要给人看。码表以
   engine.py / rerank.py 里实际 append 的为准；新增码落了库这里漏了
   也只是退回显示原码，不会错。 */
const DEGRADED_LABELS = {
  rerank_unavailable: "精排模型未就绪，已跳过精排（按融合分数排名）",
  rerank_model_missing: "精排模型未下载，跳过精排（按融合分数排名）",
  rerank_failed: "精排执行失败，已跳过",
  dense_unavailable: "稠密检索不可用（嵌入模型未就绪）",
  dense_disabled: "稠密检索已被参数关闭",
  sparse_failed: "稀疏检索（BM25）执行失败",
  mmr_failed: "多样性重排（MMR）失败，按原序返回",
  refusal_uncalibrated: "拒答阈值未经标定，判定仅供参考",
  llm_error: "生成模型出错，答案可能不完整",
  embedder_loading: "嵌入模型正在加载，本次检索质量受限",
};
const degLabel = (code) => DEGRADED_LABELS[code] || code;

function renderDebug(debug) {
  if (!debug) return;
  const t = debug.timings_ms || {};
  const deg = debug.degraded || [];
  const ref = debug.refusal || {};
  const maxDense = Math.max(0.0001, ...(debug.stages.dense || []).map((x) => x.score));
  const maxRrf = Math.max(0.0001, ...(debug.stages.fused || []).map((x) => x.rrf));

  const rows = (arr, render) =>
    arr.length ? arr.map(render).join("") : `<p class="hint">无命中</p>`;

  let html = "";

  if (deg.length || ref.reason) {
    html += `<div class="dbg-block"><h4>状态</h4>`;
    deg.forEach((d) => {
      html += `<span class="tag deg" title="${esc(d)}">${esc(degLabel(d))}</span>`;
    });
    if (ref.refused) html += `<span class="tag neg">拒答:${esc(ref.reason)}</span>`;
    else if (ref.low_confidence) {
      html += `<span class="tag deg" title="top ${ref.top_score ?? "-"} · ${esc(ref.confidence_reason || "")}">低置信：${esc(ref.confidence_reason || "相关度偏低")}</span>`;
    }
    html += `</div>`;
  }

  html += `<div class="dbg-block"><h4>耗时 <span>${t.total_ms || 0}ms</span></h4>
    ${["dense_ms", "sparse_ms", "fusion_ms", "rerank_ms", "gen_ms"].filter((k) => k in t).map((k) =>
      `<div class="kv"><span>${k.replace("_ms", "")}</span><b>${t[k]}ms</b></div>`).join("")}
  </div>`;

  /* 调试行的意义在于"这条为什么排这儿"。只有 chunk 编号 + 分数的话，用户
     面对一排条形图什么结论都下不了——所以每行都带文档名、章节路径和原文摘要。 */
  const hitRows = (arr, opts) => {
    const { barMax, barVal, sub } = opts;
    if (!arr.length) return `<p class="hint">无命中</p>`;
    return arr.map((h) => {
      const doc = [h.doc_title, h.page_no ? `p${h.page_no}` : "", h.heading_path]
        .filter(Boolean).map(esc).join(" · ");
      const bar = barMax
        ? `<div class="bar"><i style="width:${Math.max(2, (barVal(h) / barMax) * 100)}%"></i></div>`
        : "";
      return `
      <div class="row dbg-hit" title="${esc(h.preview || "")}">
        <span class="rk">${h.rank ?? h.chunk_id}</span>
        <div class="mid">
          <div class="tx hit-doc-line">${doc || `chunk ${h.chunk_id}`}</div>
          ${sub ? `<div class="tx hit-sub">${esc(sub(h))}</div>` : ""}
          ${h.preview ? `<div class="tx hit-snip">${esc(h.preview.slice(0, 64))}…</div>` : ""}
          ${bar}
        </div>
        <span class="sc">${(opts.fmt ? opts.fmt(h) : h.score.toFixed(3))}</span>
      </div>`;
    }).join("");
  };

  html += `<div class="dbg-block"><h4>dense top${(debug.stages.dense || []).length}
      <span>${debug.params.rerank_used ? "已精排" : "未精排"}</span></h4>
    ${hitRows(debug.stages.dense || [], {
      barMax: maxDense,
      barVal: (h) => h.score,
      sub: (h) => `chunk ${h.chunk_id}`,
    })}
  </div>`;

  html += `<div class="dbg-block"><h4>sparse (bm25)
      <span>负值升序=更相关</span></h4>
    ${hitRows(debug.stages.sparse || [], {
      barMax: 0,
      fmt: (h) => h.score.toFixed(3),
    })}
  </div>`;

  const fm = debug.params.fusion_method === "weighted"
    ? `加权融合 <span>归一化加权 w=${debug.params.w_dense}/${debug.params.w_sparse}</span>`
    : `RRF 融合 <span>k=${debug.params.rrf_k} w=${debug.params.w_dense}/${debug.params.w_sparse}</span>`;
  html += `<div class="dbg-block"><h4>${fm}</h4>
    ${hitRows(debug.stages.fused || [], {
      barMax: maxRrf,
      barVal: (f) => f.rrf,
      sub: (f) => `chunk ${f.chunk_id} · d#${f.dense_rank ?? "-"} s#${f.sparse_rank ?? "-"}`,
      fmt: (f) => f.rrf.toFixed(5),
    })}
  </div>`;

  html += `<div class="dbg-block"><h4>最终入选 <span>${(debug.final || []).length} 条</span></h4>
    ${rows(debug.final || [], (c) => `
      <div style="padding:6px;border-radius:5px" class="row-item">
        <div class="tx" style="color:var(--text-dim)">[${c.n}] ${esc(c.doc_title)}
          ${c.page_no ? "· p" + c.page_no : ""}</div>
        <div class="tx" style="color:var(--text-3);font-size:11px">
          ${esc((c.heading_path || "").slice(0, 40))}</div>
        <div class="tx" style="white-space:normal;color:var(--text-3);font-size:11px;margin-top:3px">
          ${esc(c.preview.slice(0, 90))}…</div>
      </div>`)}
  </div>`;

  if (ref.top_score !== null && ref.top_score !== undefined) {
    const rs = state.status?.refusal || {};
    html += `<div class="dbg-block"><h4>拒答判定 <span>${ref.calibrated ? "已标定" : "未标定"}</span></h4>
      <div class="kv"><span>top 相似度</span><b>${Number(ref.top_score).toFixed(4)}</b></div>
      <div class="kv"><span>top1 - 均值(top2..5)</span><b>${ref.margin === null ? "-" : Number(ref.margin).toFixed(4)}</b></div>
      <div class="kv"><span>阈值 score / margin</span><b>${rs.min_score ?? "-"} / ${rs.min_margin ?? "-"}</b></div>
      <div class="kv"><span>调整</span><b><button class="mini" data-open-settings>参数面板</button></b></div>
    </div>`;
  }

  $("panelDebug").innerHTML = html;
}

/* ------------------------------------------------------------------ 对话 */

function scrollIfNearBottom() {
  const chat = $("chat");
  const near = chat.scrollHeight - chat.scrollTop - chat.clientHeight < 160;
  if (near) chat.scrollTop = chat.scrollHeight;
}

function appendUser(text) {
  const el = document.createElement("div");
  el.className = "msg user";
  el.innerHTML = `<div class="role">你</div><div class="bubble">${esc(text)}</div>`;
  $("chat").appendChild(el);
  scrollIfNearBottom();
}

function appendAssistant() {
  const el = document.createElement("div");
  el.className = "msg assistant";
  el.innerHTML = `<div class="role">knowtune</div>
    <div class="bubble"><span class="cursor"></span></div>
    <div class="msg-foot"></div>`;
  $("chat").appendChild(el);
  scrollIfNearBottom();
  return el;
}

function parseSSE(raw) {
  let event = "message";
  const data = [];
  raw.split("\n").forEach((line) => {
    if (line.startsWith("event: ")) event = line.slice(7).trim();
    else if (line.startsWith("data: ")) data.push(line.slice(6));
  });
  return { event, data: data.join("\n") };
}

/* 上下文占用的两档提示。
 *
 * 后端 chatctx.py 给的是同一套字段，这里只负责措辞：
 *   exhausted —— 历史把预算吃穿了，这轮基本没有文档片段的空间
 *   truncated —— 轮次超过 chat.history_turns，早期对话不再带入
 * 两者都不能用一句"上下文已满"糊过去：前者要用户清会话，后者只是告知边界。 */
function ctxNoticeText(c) {
  if (!c) return "";
  if (c.exhausted) {
    return `上下文已满：历史 ${c.turns_kept} 轮已占用约 `
      + `${fmtInt(c.history_chars)} / ${fmtInt(c.budget)} 字符，本轮几乎装不进文档片段。`
      + `点侧栏「刷新会话」可以重新开始。`;
  }
  if (c.truncated) {
    return `早期对话已不再带入：只保留最近 ${c.turns_kept} 轮`
      + `（共 ${c.turns_total} 轮，上限 ${c.history_turns_limit} 轮）。`;
  }
  return "";
}

function applyCtxNotice(el, ctx) {
  const text = ctxNoticeText(ctx);
  if (!text || !el) return;
  el.querySelector(".ctx-note")?.remove();
  const note = document.createElement("div");
  note.className = "ctx-note";
  note.textContent = text;
  const bubble = el.querySelector(".bubble");
  el.insertBefore(note, bubble || null);
}

async function ask(question) {
  if (state.busy) return;
  const gate = canChat();
  if (!gate.ok) {
    // 不发出去。发出去只会拿回一个 500，然后把"没配模型"伪装成"服务出错"。
    state.noModelWarned = false;
    maybeWarnNoModel();
    toast(gate.reason, "warn");
    return;
  }
  state.busy = true;
  state.lastConfidence = null;
  state.lastGen = null;
  $("btnSend").disabled = true;
  $("emptyState")?.remove();
  appendUser(question);
  const el = appendAssistant();
  const bubble = el.querySelector(".bubble");
  const foot = el.querySelector(".msg-foot");

  let acc = "";
  let citations = [];
  let lastPaint = 0;
  const paint = (force) => {
    const now = performance.now();
    if (!force && now - lastPaint < 70) return;
    lastPaint = now;
    bubble.innerHTML = renderBody(acc) + (force ? "" : '<span class="cursor"></span>');
    scrollIfNearBottom();
  };

  const opt = advEls();
  // 模型这一项可能来自"手动输入"：值不在下拉里时以输入框为准。
  const gm = parseGenPick();
  const typed = ($("genModelCustom")?.value || "").trim();
  const pickModel = gm.model || (opt.model.value === GEN_CUSTOM ? typed : null);
  const payload = {
    question,
    // 不挂载知识库时 kb_id 必须是 null（不是 0 / ""）：0 会被当成"查一个不存在的库"
    kb_id: state.kbId || null,
    // 后台不存会话：把这一页内存里的对话带过去，轮数/预算由后端 chat.* 配置裁。
    history: state.history,
    // 平铺目录里选了某个后端的模型 → 后端跟着它走，而不是停在上面那个下拉
    provider: gm.provider || $("providerSel").value || null,
    top_k: $("topKSel").value ? Number($("topKSel").value) : null,
    rerank: $("deepMode").checked ? true : null,
    rerank_model: $("deepMode").checked ? "BAAI/bge-reranker-v2-m3" : null,
    model: pickModel || null,
    // 温度由后端按库配置决定（编辑入口在「站点 → 回答行为」）；这里是检索
    // 调试台，不另开旋钮，不传 temperature 即跟随配置。
    max_tokens: advNum(opt.maxTok.value),
    stream: opt.stream.checked ? null : false,   // 默认流式，只有关掉才需要显式传
    // 不挂载知识库 + 选了「直接问模型」才跳过检索；跨全库检索仍走正常链路
    direct: !state.kbId && opt.noKbMode.value === "direct",
    overrides: collectOverrides(),
  };

  try {
    const res = await fetch("/api/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!res.ok) {
      throw new Error(`请求失败 ${res.status}`);
    }
    let reader;
    if ((res.headers.get("content-type") || "").includes("application/json")) {
      // 后端把 generation.stream 关掉时（参数面板可调）返回的是一份完整 JSON。
      // 这里不另写一套渲染，而是把它还原成 meta/delta/done 三段 SSE 再喂给下面
      // 同一个循环：引用脚注、覆盖率、反馈按钮的逻辑只有一份，少一处分叉就少
      // 一处"只在某个模式下才复现"的 bug。
      const data = await res.json();
      const seq =
        (data.meta ? `event: meta\ndata: ${JSON.stringify(data.meta)}\n\n` : "") +
        (data.answer
          ? `event: delta\ndata: ${JSON.stringify({ text: data.answer })}\n\n`
          : "") +
        `event: done\ndata: ${JSON.stringify(data)}\n\n`;
      const bytes = new TextEncoder().encode(seq);
      let sent = false;
      reader = {
        read: async () =>
          sent ? { done: true } : ((sent = true), { done: false, value: bytes }),
      };
    } else {
      if (!res.body) throw new Error(`请求失败 ${res.status}`);
      reader = res.body.getReader();
    }
    const decoder = new TextDecoder();
    let buf = "";

    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf("\n\n")) >= 0) {
        const raw = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        if (!raw.trim()) continue;
        const { event, data } = parseSSE(raw);
        let obj = {};
        try { obj = JSON.parse(data); } catch (_) { continue; }

        if (event === "progress") {
          // 检索 + CPU 精排是同步的，服务端在检索前 / 后各推一条阶段。
          // 还没出字时把阶段写进气泡，delta 一来 paint() 自然覆盖。
          if (!acc && obj.label) {
            bubble.innerHTML = `<span class="hint">${esc(obj.label)}</span><span class="cursor"></span>`;
          }
        } else if (event === "meta") {
          citations = obj.citations || [];
          state.lastCitations = citations;
          // 直问模型没有检索过程，右侧调试面板那一堆"dense/sparse/融合"
          // 全是空的，留着只会让人以为检索坏了。
          state.lastDirect = obj.mode === "direct";
          // 服务端回显"这一问到底用的哪个后端/模型"。界面选了 A 却跑在 B 上，
          // 只有服务端看得见；不回显的话"选哪个就是测哪个"永远无法自证。
          state.lastGen = (obj.provider || obj.model)
            ? { provider: obj.provider || "", model: obj.model || "" } : null;
          if (obj.debug && !state.lastDirect) renderDebug(obj.debug);
          if (state.lastDirect) {
            $("panelDebug").innerHTML =
              `<div class="dbg-block"><h4>未检索</h4><p class="hint">这一问不挂载知识库，`
              + `直接问模型：不检索、不引用、不做拒答判定。</p></div>`;
          }
          if (obj.context) {
            state.lastCtx = obj.context;
            applyCtxNotice(el, obj.context);
          }
          if (obj.mode === "refuse") {
            el.classList.add("refused");
            foot.innerHTML = `<span class="warn">已拒答 · ${esc(obj.reason || "")}
              ${obj.calibrated === false ? "（阈值未标定）" : ""}</span>`;
          } else if (obj.confidence?.low) {
            // 答了，但检索到的证据不够硬。这种"模棱两可"最容易被当成模型瞎编，
            // 所以把原因留着，等 done 时和其它脚注一起画（这里画会被覆盖）。
            state.lastConfidence = obj.confidence;
            el.classList.add("lowconf");
          }
        } else if (event === "delta") {
          acc += obj.text || "";
          paint(false);
        } else if (event === "done") {
          paint(true);
          // 这一问完成后才把问答记进本次对话：下一轮带上它才有指代对象。
          rememberTurn(question, obj.answer || acc);
          // meta 没带回显（比如老版本服务端）时用 done 里的那份兜底
          if (!state.lastGen && obj.provider) {
            state.lastGen = { provider: obj.provider, model: obj.model || "" };
          }
          // 直问模型（不挂载库）没有资料、没有编号，引用类脚注全是误导：
          // 覆盖 0% 会让人以为"引用没引上"，其实这一问本来就不引用。
            if (state.lastDirect) {
              foot.innerHTML = `<span class="ok" title="未挂载知识库，模型凭自身知识作答">直问模型 · 无引用</span>`;
              if (state.lastGen?.provider) {
                foot.innerHTML += genTag(state.lastGen);
              }
            bindFootActions(foot, obj);
          } else if (!obj.refused) {
            const bits = [];
            // 支撑度与覆盖率是两个问题：覆盖率只看"这句有没有编号、编号超没超范围"，
            // 编号挂对了但指向的段落根本没在讲这件事，它照样算 100%。支撑度才算
            // "这句话在引用的资料里找不找得到"。两个数一起看才有意义。
            const sup = obj.support;
            const supLow = sup && !sup.skipped && sup.grounded != null
              && sup.verdict === "unsupported";
            if (supLow) {
              bits.push(`<span class="err" title="${esc(supTooltip(sup))}">资料未支撑</span>`);
            }
            if (obj.cite_coverage !== undefined) {
              const pct = Math.round(obj.cite_coverage * 100);
              bits.push(`<span class="${pct >= 80 ? "ok" : "warn"}">引用覆盖 ${pct}%</span>`);
            }
            if (sup && !sup.skipped && sup.grounded != null) {
              const pct = Math.round(sup.grounded * 100);
              const cls = sup.verdict === "grounded" ? "ok"
                : (sup.verdict === "unsupported" ? "err" : "warn");
              bits.push(`<span class="${cls}" title="${esc(supTooltip(sup))}">${pct}% 句有据</span>`);
            }
            // 引用存疑：模型嘴上说没依据、编号挂一串（或单句全引）。
            // 覆盖率数字解释不了的事，这句话解释。
            if (obj.cite_blanket) {
              bits.unshift(`<span class="warn" title="${esc(obj.cite_blanket)}">引用存疑</span>`);
            }
            // 历史占比只在超过 70% 时出现：平时挂一个"上下文 8%"纯属噪音，
            // 但它一旦逼近上限必须可见，否则模型"突然变笨"没人能解释。
            if (state.lastCtx && state.lastCtx.budget) {
              const used = Math.min(100,
                Math.round((state.lastCtx.history_chars / state.lastCtx.budget) * 100));
              if (used >= 70) {
                bits.push(`<span class="${used >= 100 ? "err" : "warn"}">历史占上下文 ${used}%</span>`);
              }
            }
            if (obj.invalid_ids?.length) bits.push(`<span class="err">越界编号 ${obj.invalid_ids.join(",")}</span>`);
            // 低置信排在前面：它决定"这段回答要不要信"，比降级信息重要
            const conf = state.lastConfidence;
            if (conf?.low) {
              bits.unshift(`<span class="warn" title="最佳片段相关度 ${conf.top_score ?? "-"}">
                低置信 · ${esc(conf.reason || "")}</span>`);
            }
            if (obj.degraded?.length) {
              bits.push(`<span class="warn" title="${esc(obj.degraded.join(","))}">降级 ${esc(obj.degraded.map(degLabel).join("、"))}</span>`);
            }
            if (state.lastGen?.provider) bits.push(genTag(state.lastGen));
            if (obj.citations?.length) {
              bits.push(`<button data-src="cites">查看 ${obj.citations.length} 条来源</button>`);
            }
            foot.innerHTML = bits.join("");
            bindCiteBtn(foot, obj);
            bindFeedback(foot, obj);
          }
        } else if (event === "error") {
          bubble.innerHTML = renderBody(acc) +
            `<p class="u-err">生成失败：${esc(obj.message)}</p>`;
          scrollIfNearBottom();
        }
      }
    }
    paint(true);
  } catch (err) {
    bubble.innerHTML = `<p class="u-err">${esc(err.message)}</p>`;
  } finally {
    state.busy = false;
    $("btnSend").disabled = false;
    loadKb();
    renderSessions();
  }
}

/* 脚注里的两类按钮（跳来源、反馈）在"正常带引用"和"直问模型"两条分支里
   都要用，各写一份必然发散——直问那一支漏绑反馈是最容易发生的那种 bug。 */
/* 脚注里的"这一问用的是谁"：后端名 + 模型短名（全名挂 title）。
   存在的理由：对话页下拉选的后端只对这一问生效，不在答案上回显的话，
   "我选的到底跑没跑"这件事没有任何证据。 */
function genTag(g) {
  if (!g || !g.provider) return "";
  const m = g.model ? shortModel(g.model) : "";
  return `<span class="gen-tag" title="本次实际调用：${esc(g.provider)}${
    g.model ? " / " + esc(g.model) : ""}">${esc(g.provider)}${m ? " · " + esc(m) : ""}</span>`;
}

function bindFootActions(foot, obj) {
  bindCiteBtn(foot, obj);
  bindFeedback(foot, obj);
}

/* 支撑度的 tooltip。两件事必须说清，否则这个百分比会被当成又一个人为设定的分数：
   1. 判据是什么（句子与证据块的余弦，超过阈值才算被接住）；
   2. 阈值有没有标定过（没有，是经验值）。
   顺带把「证据里根本没出现的事实」列出来——那是最能说服人的部分。 */
function supTooltip(sup) {
  const parts = [];
  if (sup.n_facts) {
    parts.push(`${sup.n_facts} 句里 ${Math.round(sup.grounded * 100)}% 语义对得上证据`);
  }
  if (sup.n_cited) {
    const s = sup.score == null ? "—" : `${Math.round(sup.score * 100)}%`;
    parts.push(`带引用的 ${sup.n_cited} 句中被接住 ${s}`);
  }
  if (sup.anchor_rate != null) {
    // 锚点单独说：它查的是"句子里的型号/剂量/版本号有没有在原文出现过"，
    // 和余弦是两件事。余弦高但锚点全落空，正是"术语堆砌、事实现编"的形态。
    parts.push(`事实锚点命中 ${Math.round(sup.anchor_rate * 100)}%`);
  }
  if (sup.missed && sup.missed.length) {
    parts.push(`证据里没有的事实：${sup.missed.slice(0, 6).join("、")}`);
  }
  parts.push(`判据：句子与证据块（150 字窗口取最大）余弦 ≥ ${sup.threshold}，`
    + "且句中事实锚点不得半数落空");
  if (!sup.calibrated) {
    parts.push("阈值是经验值，未用留一法标定");
  } else if (sup.auc != null) {
    parts.push(`阈值来自留一法标定，AUC ${Number(sup.auc).toFixed(2)}`);
  }
  if (sup.auc_fact != null && sup.auc_fact < 0.7) {
    parts.push("注：余弦对「同一段话里改掉数字」几乎无感，那一层只靠锚点兜");
  }
  return parts.join("；");
}

function bindCiteBtn(foot, obj) {
  foot.querySelector("[data-src=cites]")?.addEventListener("click", () => {
    switchTab("source");
    showCitation(obj.citations[0]);
  });
}

function bindFeedback(foot, obj) {
  foot.querySelectorAll("[data-fb]").forEach((btn) => {
    btn.onclick = async () => {
      const verdict = btn.dataset.fb;
      const send = async (comment) => {
        await jsonPost("/api/feedback", {
          message_id: obj.message_id,
          verdict,
          comment,
        });
        foot.querySelectorAll("[data-fb]").forEach((b) => (b.disabled = true));
        btn.textContent = verdict === "up" ? "已标记有用" : "已记录";
      };
      if (verdict === "up") {
        try { await send(""); } catch (err) { alert(err.message); }
        return;
      }
      // 原生 prompt() 的白框跟整个界面格格不入，还看不出提交到哪；
      // 换成界面内浮层，顺便用 placeholder 把"写什么有用"提示出来。
      showModal("这条回答哪里不对？（可留空）",
        `<textarea id="fbComment" class="fb" placeholder="比如：答非所问 / 引用了不相关的片段 / 结论有错 / 漏了关键内容…"></textarea>`,
        async () => { await send($("fbComment").value.trim()); },
        { okLabel: "提交反馈" });
    };
  });
}

/* ------------------------------------------------------------------ 引用 */

async function showCitation(c) {
  if (!c) return;
  try {
    const chunk = await api(`/api/chunk/${c.chunk_id}`);
    renderChunkPanel(c, chunk);
    switchTab("source");
  } catch (err) {
    $("panelSource").innerHTML = `<p class="hint">${esc(err.message)}</p>`;
    switchTab("source");
  }
}

function renderChunkPanel(c, chunk) {
  $("panelSource").innerHTML = `
    <div class="src-head">
      <div class="t">[${c.n}] ${esc(chunk.title || chunk.source_uri)}</div>
      <div class="m">
        chunk ${chunk.id} · 第 ${chunk.seq + 1} 段
        ${chunk.page_no ? " · p" + chunk.page_no : ""}
        ${chunk.heading_path ? " · " + esc(chunk.heading_path) : ""}
        · ${chunk.token_count} tokens
      </div>
    </div>
    <div class="src-actions">
      <button class="ghost-btn" data-cact="edit">编辑</button>
      <button class="ghost-btn" data-cact="split">拆分</button>
      <button class="ghost-btn" data-cact="delete">删除</button>
    </div>
    <div class="src-text">${esc(chunk.text)}</div>`;

  $("panelSource").querySelectorAll("[data-cact]").forEach((btn) => {
    btn.onclick = () => {
      const act = btn.dataset.cact;
      if (act === "edit") editChunk(c, chunk);
      if (act === "split") splitChunk(c, chunk);
      if (act === "delete") deleteChunk(chunk);
    };
  });
}

function editChunk(c, chunk) {
  showModal(`编辑 chunk ${chunk.id}`,
    `<textarea id="ct" style="min-height:260px">${esc(chunk.text)}</textarea>
     <p class="hint">改完会立即重算这一条的分词、FTS 与向量，不需要重索引整篇文档。</p>`,
    async () => {
      const text = $("ct").value.trim();
      if (!text) throw new Error("内容不能为空");
      await api(`/api/chunk/${chunk.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text }),
      });
      showCitation(c);
    },
    { okLabel: "保存" });
}

function splitChunk(c, chunk) {
  const half = Math.max(1, Math.floor(chunk.text.length / 2));
  showModal(`拆分 chunk ${chunk.id}`,
    `<input type="number" id="off" value="${half}" min="1" max="${chunk.text.length - 1}">
    <p class="hint">在第 N 个字符处切开，后半段成为新 chunk。</p>`,
    async () => {
      const offset = parseInt($("off").value, 10);
      const r = await jsonPost(`/api/chunk/${chunk.id}/split`, { offset });
      alert(`已拆成 ${r.left_id} 与 ${r.right_id}`);
      showCitation(c);
    },
    { okLabel: "拆分" });
}

async function deleteChunk(chunk) {
  if (!(await askConfirm(`删除 chunk ${chunk.id}？它不会再被检索到（可回滚）。`,
      { title: "删除 chunk", okLabel: "删除" }))) return;
  await api(`/api/chunk/${chunk.id}`, { method: "DELETE" });
  $("panelSource").innerHTML = `<p class="hint">chunk ${chunk.id} 已删除，不再参与检索。</p>`;
  await loadKb();
}

function switchTab(name) {
  document.querySelectorAll(".tab").forEach((t) =>
    t.classList.toggle("active", t.dataset.tab === name));
  $("panelDebug").hidden = name !== "debug";
  $("panelSource").hidden = name !== "source";
}

/* ------------------------------------------------------------------ 数据 */

async function loadKb() {
  // 先定库再拉状态：/api/status 不带 kb_id 返回的是全局配置，而标定是逐库
  // 做的。顺序反过来，首屏会先渲染一次"未标定"，就算随后被纠正也会闪一下。
  state.kbs = await api("/api/kb");
  if (!state.kbId && state.kbs.length) state.kbId = state.kbs[0].id;
  state.status = await api(statusUrl());
  renderStatus();
  renderKbList();
  if (state.kbId) await loadDocs();
  renderSessions();
  renderCfgSummary();
}

async function loadDocs() {
  if (!state.kbId) return;
  state.docs = await api(`/api/kb/${state.kbId}/docs`);
  renderDocs();
}

async function selectKb(id) {
  state.kbId = id;
  localStorage.setItem("knowtune.kb", String(id));
  // 换库必须丢掉参数草稿，否则 A 库改了一半的滑杆会出现在 B 库上
  state.kbCfgDraftFor = null;
  state.kbDetail = null;
  renderKbList();
  // 标定是按库做的：状态里的阈值和 AUC 属于上一个库，不重拉就会一直显示别人的数字
  state.status = await api(statusUrl());
  renderStatus();
  await loadDocs();
  // 会话是这一页的内存对话，换库就清掉——别把 A 库的问答喂给 B 库。
  state.history = [];
  state.lastCtx = null;
  renderSessions();
  renderEmptyChat();
  renderCfgSummary();
  if (state.view === "kb") await renderKbPage();
}

/* ------------------------------------------------------------------ 弹窗 */

function showModal(title, bodyHTML, onOk, opts = {}) {
  const modal = $("modal");
  const okBtn = $("modalOk");
  const cancelBtn = $("modalCancel");

  $("modalTitle").textContent = title;
  $("modalBody").innerHTML = bodyHTML;
  const box = modal.querySelector(".modal-box");
  box.classList.toggle("wide", Boolean(opts.wide));
  // 自定义宽度/皮肤：每次开弹窗先清掉上一颗留下的，否则先开「站点配置」
  // 再开「导入」会继承 1020px 的宽板。类名统一 mdl- 前缀便于清理。
  [...box.classList].filter((c) => c.startsWith("mdl-")).forEach((c) => box.classList.remove(c));
  if (opts.cls) box.classList.add(`mdl-${opts.cls}`);
  modal.hidden = false;

  // opts.ok === false 时这是一个纯选择型弹窗（比如"导入"让你挑方式），
  // 半残的"确定"按钮点下去什么也不做，比没有这个按钮更糟。
  const hasOk = opts.ok !== false;
  okBtn.hidden = !hasOk;
  okBtn.textContent = opts.okLabel || "确定";
  // 破坏性操作（删库）的确认按钮要长得不一样：一眼能看出这颗和「保存」不是一回事。
  okBtn.classList.toggle("danger", Boolean(opts.danger));
  cancelBtn.textContent = hasOk ? "取消" : "关闭";
  // 上一颗弹窗可能是"成功后保持 disabled 关闭"的（见下面的成功分支），
  // 不在这里复位，下一颗弹窗的确定键会莫名其妙点不动。
  okBtn.disabled = false;

  const prevFocus = document.activeElement;
  let closed = false;
  const close = () => {
    if (closed) return;
    closed = true;
    modal.hidden = true;
    document.removeEventListener("keydown", onKey);
    $("modalBody").removeEventListener("input", syncGate);
    if (opts.onClose) opts.onClose();   // Esc / 点遮罩关闭时，调用方要能收尾
    if (prevFocus && prevFocus.focus) prevFocus.focus();
  };
  // opts.gate：有些确认不该点一下就过（删库要手打库名），按钮先灰着，
  // 条件满足了才亮 —— 比"点了才告诉你不行"省一次惊吓。
  const syncGate = () => { if (opts.gate) okBtn.disabled = !opts.gate(); };
  syncGate();
  $("modalBody").addEventListener("input", syncGate);
  const onKey = (e) => {
    if (e.key === "Escape") { e.preventDefault(); close(); }
  };
  document.addEventListener("keydown", onKey);
  // 点遮罩**不关闭**。以前点一下就关，鼠标稍微一滑出框子，刚填的地址、
  // 模型名、密钥全没了——这类弹窗里全是手打的内容，误关的代价远大于
  // "想关却没关上"。要关只有两条明路：取消/关闭按钮，或 Esc。
  modal.onclick = (e) => { if (e.target === modal) e.stopPropagation(); };

  cancelBtn.onclick = close;
  okBtn.onclick = async () => {
    if (!hasOk) { close(); return; }
    if (opts.gate && !opts.gate()) return;
    okBtn.disabled = true;
    try {
      await onOk();
      close();
    } catch (err) {
      // 失败时留在弹窗里（不 close），用户能看到刚才填的东西还在。
      // 用页内提示条而不是原生 alert：原生弹窗在这套界面里是另一套皮肤。
      toast(err.message || "操作失败", "err");
      okBtn.disabled = opts.gate ? !opts.gate() : false;
      return;
    }
  };

  const first = $("modalBody").querySelector("input, textarea");
  if (first) first.focus();
  return close;
}

/*
 * 原生 confirm() 的坟场。删库那次已经证明：内嵌 WebView 里 confirm 可能直接被
 * 吞（返回 undefined），表现是"点了没反应"。这里剩的每一处 confirm 都有同样的
 * 风险，统一换成页内弹窗 —— resolve(true) 只会发生在真正点了确认键的时候。
 */
function askConfirm(messageHTML, opts = {}) {
  return new Promise((resolve) => {
    showModal(opts.title || "请确认", `<p class="confirm-text">${messageHTML}</p>`,
      () => resolve(true), {
        okLabel: opts.okLabel || "确定",
        danger: opts.danger !== false,
        onClose: () => resolve(false),
      });
  });
}

/* --------------------------------------------------------- 参数控件（共用） */
/*
 * 全局参数抽屉已经退役：每个库在「知识库 → 参数」里持有完整一套值（见
 * kbconfig.py），再在外面挂一层"全局值"只会让人以为改它能影响所有库。
 * 但下面这几个控件是**共用的** —— 库参数页每一行都用它渲染，别顺手删了。
 *
 * 数值控件不用滑杆：拖动一条 range 很难精确落在 0.35，而阈值的 0.05 之差
 * 就是"拒答"与"硬答"的分界。现在是「− 数字框 ＋」按 step 精确走，外加
 * 默认 / 下限 / 上限三个档位一键到位。
 */

function fmtParamVal(it, v) {
  if (v === null || v === undefined) return "-";
  const n = Number(v);
  if (!Number.isFinite(n)) return "-";
  return it.kind === "int" ? String(Math.round(n)) : String(Number(n.toFixed(4)));
}

/* 按 step 走一格。步长是小数时（0.05 / 0.005）必须先按小数位收敛，
   否则 0.35 + 0.05 会给出 0.39999999999999997 这种值。 */
function stepParamVal(it, dir) {
  const step = Number(it.step || 1);
  const dec = (String(step).split(".")[1] || "").length;
  let v = Number((Number(it.value ?? it.default) + dir * step).toFixed(dec));
  if (it.lo !== null && it.lo !== undefined) v = Math.max(Number(it.lo), v);
  if (it.hi !== null && it.hi !== undefined) v = Math.min(Number(it.hi), v);
  return it.kind === "int" ? Math.round(v) : v;
}

/* 档位 chip：滑杆能一眼看出"大概在哪"，但**点不出精确值**——拒答阈值差 0.05
   就是"拒答"和"硬答"的区别，分块长度差 20 就要重索引一遍。所以改成
   「− 数字框 ＋」按 step 精确走 + 三个常用档位一键到位（默认 / 下限 / 上限）。 */
function pickChipsHTML(it, cur, attrKey, attrVal, disabled = false) {
  const dis = disabled ? " disabled" : "";
  const cand = [["默认", it.default], ["下限", it.lo], ["上限", it.hi]];
  const seen = new Set([String(cur)]);
  return cand.filter(([, v]) => {
    if (v === null || v === undefined) return false;
    const k = String(v);
    if (seen.has(k)) return false;
    seen.add(k);
    return true;
  }).map(([lab, v]) => `<button class="pick-chip" ${attrKey}="${esc(attrVal)}"
      data-pick="${esc(String(v))}"${dis} title="${lab}">${esc(lab)} ${
      esc(fmtParamVal(it, v))}</button>`).join("");
}

function numCtlHTML(it, value, disabled, dataAttr, chipAttr = "data-pickk") {
  const dis = disabled ? " disabled" : "";
  const unit = it.unit ? `<span class="unit">${esc(it.unit)}</span>` : "";
  return `<div class="num-ctl">
      <button class="step-btn" ${dataAttr} data-step="-1"${dis} title="减 ${it.step || 1}">−</button>
      <input type="number" class="num" ${dataAttr} min="${it.lo}" max="${it.hi}"
        step="${it.step || 1}" value="${value}"${dis} aria-label="${esc(it.label)}">
      <button class="step-btn" ${dataAttr} data-step="1"${dis} title="加 ${it.step || 1}">＋</button>
      ${unit}
      <span class="picks">${pickChipsHTML(it, value, chipAttr, it.key, disabled)}</span>
    </div>`;
}

/* ------------------------------------------------------------------ 事件 */

$("composer").addEventListener("submit", (e) => {
  e.preventDefault();
  const q = $("question").value.trim();
  if (!q) return;
  // 没配模型时不清空输入框：用户辛苦打的问题不该在提示"没配模型"的同时消失。
  const gate = canChat();
  if (!gate.ok) {
    state.noModelWarned = false;
    maybeWarnNoModel();
    toast(gate.reason, "warn");
    return;
  }
  $("question").value = "";
  $("question").style.height = "auto";
  ask(q);
});

$("question").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    $("composer").requestSubmit();
  }
});

$("question").addEventListener("input", (e) => {
  e.target.style.height = "auto";
  e.target.style.height = Math.min(160, e.target.scrollHeight) + "px";
});

document.addEventListener("click", (e) => {
  const chip = e.target.closest(".cite-chip");
  if (!chip) return;
  const n = Number(chip.dataset.n);
  const c = state.lastCitations.find((x) => x.n === n);
  if (c) showCitation(c);
});

$("btnNewKb").onclick = () => newKbWizard();

// 刷新入口只剩会话卡片里那个按钮（侧栏顶部那个小图标已删：同一个动作两个入口，
// 一个在标题栏一个在卡片里，用户还得猜它们是不是一回事）。

$("btnImport").onclick = () => {
  if (!state.kbId) { alert("先创建一个知识库"); return; }
  const close = showModal("导入",
    `<div class="dropzone" id="dz" role="button" tabindex="0">
       <div class="dz-icon" aria-hidden="true">⇪</div>
       <div class="dz-title">把文件拖到这里</div>
       <div class="dz-sub">或点击选择 · 可一次拖多个</div>
       <div class="dz-note">PDF / Markdown / TXT / Word / Excel / PPT / 源码。
         大文件自动分片、中断可续传；扫描件 PDF 会明确报错而不是静默失败。</div>
     </div>
     <div class="import-alt">
       <button type="button" class="linkish" data-mode="path">填路径</button><i>·</i>
       <button type="button" class="linkish" data-mode="url">网页 URL</button><i>·</i>
       <button type="button" class="linkish" data-mode="text">粘贴文本</button>
     </div>`,
    null,
    { ok: false });
  wireDropzone($("dz"), (files) => {
    close();
    uploadFiles(files, state.kbId);
  });
  $("modalBody").querySelectorAll("[data-mode]").forEach((btn) => {
    btn.onclick = () => {
      close();
      const mode = btn.dataset.mode;
      if (mode === "path") importByPath();
      if (mode === "url") importByUrl();
      if (mode === "text") importByText();
    };
  });
};

/* 拖拽区的通用接线。弹窗里的小区域和整页覆盖层共用同一套，免得两处行为
   慢慢分叉——一处能拖、一处不能，用户只会觉得"时灵时不灵"。

   dragenter/dragleave 会随鼠标划过子元素反复触发，所以用计数器而不是直接
   toggle，否则高亮一直闪。 */
function wireDropzone(el, onFiles) {
  if (!el) return;
  const pick = () => $("fileInput").click();
  el.addEventListener("click", (e) => {
    if (e.target.closest("[data-mode]")) return;
    pick();
  });
  el.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); pick(); }
  });

  let depth = 0;
  const hot = (on) => el.classList.toggle("hot", on);
  el.addEventListener("dragenter", (e) => {
    e.preventDefault();
    depth += 1;
    hot(true);
  });
  el.addEventListener("dragover", (e) => {
    e.preventDefault();
    if (e.dataTransfer) e.dataTransfer.dropEffect = "copy";
  });
  el.addEventListener("dragleave", (e) => {
    e.preventDefault();
    depth = Math.max(0, depth - 1);
    if (!depth) hot(false);
  });
  el.addEventListener("drop", (e) => {
    e.preventDefault();
    e.stopPropagation();  // 别让整页覆盖层再收一次，否则同一批文件进两遍
    depth = 0;
    hot(false);
    const files = [...(e.dataTransfer?.files || [])];
    if (files.length) onFiles(files);
  });
}

/* 整页拖入。既然上传的主入口是拖拽，就不该要求先打开弹窗再拖——从资源
   管理器把文件甩到页面上才是最顺手的动作。

   只认"拖的是文件"：拖选中的文字、拖链接、卡片排序都会触发 dragenter，
   不加这个判断，随手选一段字整页就闪一下遮罩。 */
function setupGlobalDrop() {
  const overlay = $("dropOverlay");
  if (!overlay) return;
  let depth = 0;
  const hasFiles = (e) => [...(e.dataTransfer?.types || [])].includes("Files");

  document.addEventListener("dragenter", (e) => {
    if (!hasFiles(e)) return;
    e.preventDefault();          // 不拦的话浏览器会直接把文件当页面打开
    depth += 1;
    // 弹窗开着时它自己就有拖拽区。两个高亮目标同时出现，用户不知道该往哪放，
    // 而且这层遮罩会把弹窗整个盖住——宁可不开。
    if (!state.kbId || !$("modal").hidden) return;
    if (overlay.hidden) overlay.hidden = false;
  });
  document.addEventListener("dragover", (e) => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    if (e.dataTransfer) e.dataTransfer.dropEffect = "copy";
  });
  document.addEventListener("dragleave", (e) => {
    if (!hasFiles(e)) return;
    depth = Math.max(0, depth - 1);
    if (!depth) overlay.hidden = true;
  });
  overlay.addEventListener("drop", (e) => {
    e.preventDefault();
    depth = 0;
    overlay.hidden = true;
    const files = [...(e.dataTransfer?.files || [])];
    if (!files.length) return;
    if (!state.kbId) { toast("先创建一个知识库", "warn"); return; }
    // 弹窗正开着就交给它自己处理，别让两批文件抢同一个上传锁
    if (!$("modal").hidden) return;
    uploadFiles(files, state.kbId);
  });
}


/* 路径 / 文本 / 网页导入里的清洗面板：同一套规则，同一个预览接口。
   文件导入走的是独立的 cleanPreviewDialog（要先落盘 staged），其余三种
   就地预览即可——文本和路径本来就在手边，不用先传一份。 */
function cleanBoxHTML(plan) {
  return `<details class="clean-box">
    <summary>导入前清洗（可选）</summary>
    <p class="hint" style="margin:6px 0 8px">只影响入库的文本，源文件不改动。</p>
    <div class="clean-rules">${cleanRulesHTML(plan)}</div>
    <div class="clean-report" data-cleanreport></div>
    <button class="mini" type="button" data-cleanpreview>预览清洗效果</button>
  </details>`;
}

function wireCleanBox(root, buildFd) {
  const btn = root.querySelector("[data-cleanpreview]");
  const out = root.querySelector("[data-cleanreport]");
  if (!btn || !out) return;
  btn.onclick = async () => {
    out.innerHTML = `<span class="n">预览中…</span>`;
    btn.disabled = true;
    try {
      const fd = buildFd();
      fd.append("plan", JSON.stringify(readCleanPlan(root)));
      const r = await apiForm("/api/clean/preview", fd);
      out.innerHTML = cleanReportHTML(r.report);
    } catch (err) {
      out.innerHTML = `<span class="n u-warn">${esc(err.message)}</span>`;
    } finally {
      btn.disabled = false;
    }
  };
}

async function importByUrl() {
  if (!state.kbId) { toast("先创建一个知识库", "warn"); return; }
  await loadCleanRules();
  showModal("导入网页",
    `<p class="hint" style="margin-bottom:8px">一行一个 URL，最多 50 条。只抓 http/https 页面，
      内网与本机地址会被拒绝。</p>
     <textarea id="u" placeholder="https://docs.python.org/3/library/sqlite3.html"></textarea>
     ${cleanBoxHTML(cleanState.plan)}`,
    async () => {
      const urls = $("u").value.split("\n").map((s) => s.trim()).filter(Boolean);
      if (!urls.length) throw new Error("URL 不能为空");
      const root = $("modalBody");
      const res = await jsonPost("/api/import/url", {
        kb_id: state.kbId, urls, plan: readCleanPlan(root),
      });
      await loadKb();
      refreshCurrentView();
      const bad = res.results.filter((r) => r.status !== "ready");
      if (bad.length) toast(`失败 ${bad.length} 条：${bad[0].url}`, "err");
    },
    { okLabel: "抓取" });
  wireCleanBox($("modalBody"), () => {
    const fd = new FormData();
    const first = $("u").value.split("\n").map((s) => s.trim()).filter(Boolean)[0] || "";
    fd.append("url", first);
    return fd;
  });
}

async function importByPath() {
  if (!state.kbId) { toast("先创建一个知识库", "warn"); return; }
  await loadCleanRules();
  const body = `<input type="text" id="p" placeholder="D:\\docs 或 D:\\docs\\a.md">
     ${cleanBoxHTML(cleanState.plan)}`;
  const close = showModal("导入路径", body, async () => {
    const p = $("p").value.trim();
    if (!p) throw new Error("路径不能为空");
    const res = await jsonPost("/api/import/path", {
      kb_id: state.kbId, path: p, plan: readCleanPlan($("modalBody")),
    });
    await loadKb();
    refreshCurrentView();
    if (res.mode === "dir") toast(`目录导入完成：${res.ready}/${res.total} 成功`);
  });
  wireCleanBox($("modalBody"), () => {
    const fd = new FormData();
    fd.append("path", $("p").value.trim());
    return fd;
  });
  return close;
}

async function importByText() {
  if (!state.kbId) { toast("先创建一个知识库", "warn"); return; }
  await loadCleanRules();
  showModal("粘贴文本",
    `<input type="text" id="t" placeholder="标题（可选）">
     <textarea id="x" placeholder="粘贴 Markdown / 纯文本，支持 # 标题结构"></textarea>
     ${cleanBoxHTML(cleanState.plan)}`,
    async () => {
      const text = $("x").value;
      if (!text.trim()) throw new Error("内容不能为空");
      await jsonPost("/api/import/text", {
        kb_id: state.kbId, title: $("t").value.trim(), text,
        plan: readCleanPlan($("modalBody")),
      });
      await loadKb();
      refreshCurrentView();
    });
  wireCleanBox($("modalBody"), () => {
    const fd = new FormData();
    fd.append("text", $("x").value);
    return fd;
  });
}

$("fileInput").addEventListener("change", async (e) => {
  const files = [...e.target.files];
  e.target.value = "";
  if (!files.length) return;
  if (!state.kbId) { alert("先创建一个知识库"); return; }
  await uploadFiles(files, state.kbId);
});

/* ============================================================ 导入前清洗 */
/*
 * 清洗 = 入库前把不该进库的内容剔掉：页码、目录点线行、页眉页脚、图片占位、
 * 重复块、自定义关键词/正则命中的段落……
 *
 * 三条不能让步的：
 *   ① **源文件一个字节都不改**。清洗只作用在内存里那份抽取结果上，上传副本
 *      也只当原料（写回会让重索引拿到洗过一遍的输入，二次结果不可预期）。
 *   ② **删了什么必须列出来**。规则删错就是永久丢失，chunk 里没有的内容永远
 *      检索不到，且没有任何迹象。所以每条规则报"几个块 / 多少字"，附样例。
 *   ③ **规则定义由后端下发**。前端不写死任何一条规则的语义，加规则只改后端。
 *
 * 交互上：文件导入先落到服务端（staged），之后勾选规则只是重新预览同一份
 * 文件——不这么做，每勾一次都要重传几十 MB。
 */

const cleanState = { rules: null, plan: { enabled: {}, options: {} } };

async function loadCleanRules() {
  if (cleanState.rules) return cleanState.rules;
  cleanState.rules = await api("/api/clean/rules");
  return cleanState.rules;
}

function cleanRuleOn(rule, plan) {
  const v = plan.enabled[rule.id];
  return v === undefined ? Boolean(rule.default) : Boolean(v);
}

function cleanRulesHTML(plan) {
  const d = cleanState.rules;
  if (!d) return `<p class="n">规则加载中…</p>`;
  const rowOf = (r) => {
    const on = cleanRuleOn(r, plan);
    const p = r.param;
    let ctl = "";
    if (p) {
      const cur = plan.options[p.key] === undefined ? p.default : plan.options[p.key];
      ctl = p.kind === "int"
        ? `<input type="number" class="cn-opt" data-copt="${esc(p.key)}" value="${esc(cur)}"
             min="${p.min ?? 0}" max="${p.max ?? 9999}" style="width:72px">`
        : `<input type="text" class="cn-opt" data-copt="${esc(p.key)}" value="${esc(cur)}"
             placeholder="${esc(p.placeholder || "")}" style="flex:1;min-width:160px">`;
    }
    return `<label class="clean-row${on ? " on" : ""}">
      <input type="checkbox" data-clean="${esc(r.id)}"${on ? " checked" : ""}>
      <span class="cn-main"><b>${esc(r.label)}</b><span class="cn-hint">${esc(r.hint)}</span></span>
      ${ctl}
    </label>`;
  };
  const levels = [["text", "文本内替换"], ["line", "行级清理"], ["block", "整段丢弃"]];
  return levels.map(([lv, title]) => {
    const rows = d.rules.filter((r) => r.level === lv);
    if (!rows.length) return "";
    return `<div class="clean-group"><div class="cn-title">${title}</div>
      ${rows.map(rowOf).join("")}</div>`;
  }).join("");
}

/** 把面板里当前的勾选与参数读成后端要的 plan。 */
function readCleanPlan(root) {
  const plan = { enabled: {}, options: {} };
  root.querySelectorAll("[data-clean]").forEach((el) => {
    plan.enabled[el.dataset.clean] = el.checked;
  });
  root.querySelectorAll("[data-copt]").forEach((el) => {
    plan.options[el.dataset.copt] = el.value;
  });
  return plan;
}

function cleanReportHTML(rep) {
  if (!rep) return "";
  const pct = rep.removed_pct;
  const head = `<div class="cn-sum">
    <b>清洗后 ${fmtInt(rep.after.chars)} 字</b>
    <span class="n">原文 ${fmtInt(rep.before.chars)} 字 · 删掉 ${fmtInt(rep.removed_chars)} 字（-${pct}%）</span>
    <span class="n">${rep.before.blocks} 块 → ${rep.after.blocks} 块</span></div>`;
  if (!rep.rules.length) {
    return head + `<p class="n u-mt6">这套规则没有删掉任何内容。</p>`;
  }
  const rows = rep.rules.map((r) => `
    <div class="cn-rule">
      <div class="cn-rhead"><b>${esc(r.label)}</b>
        <span class="n">${r.blocks ? `${r.blocks} 块` : ""}${r.lines ? `${r.blocks ? " · " : ""}${r.lines} 行` : ""}${
          r.chars ? `${r.blocks || r.lines ? " · " : ""}${fmtInt(r.chars)} 字` : ""}</span></div>
      ${(r.samples || []).map((s) => `<div class="pv-gap">${esc(s)}</div>`).join("")}
    </div>`).join("");
  return head + `<div class="cn-rules">${rows}</div>`;
}

/* 文件导入前的清洗对话框。返回 {plan, staged}；plan=null 表示"不清洗"；
   返回 null 表示用户取消了整个导入。 */
async function cleanPreviewDialog(file, kbId, preStaged = null) {
  await loadCleanRules();
  const plan = cleanState.plan;
  // preStaged：字节已经在服务端了（大文件走分片传完得到的路径）。
  // 有它就绝不再把整个文件塞进 FormData 传一遍——那是白白多等一倍时间。
  let staged = preStaged || null;
  let timer = null;

  return new Promise((resolve) => {
    let settled = false;
    // Esc / 点遮罩 / 关闭按钮都走这里：不收口的话 Promise 永远悬着，
    // 导入既不开始也不报错，用户只能干等。
    const settle = (value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      close();
      resolve(value);
    };
    const box = () => $("cleanReport");
    const refresh = async () => {
      const out = box();
      if (out) out.innerHTML = `<span class="n">预览中…</span>`;
      const fd = new FormData();
      if (staged) fd.append("staged", staged);
      else fd.append("file", file);
      fd.append("plan", JSON.stringify(plan));
      try {
        const r = await apiForm("/api/clean/preview", fd);
        staged = r.staged || staged;
        if (box()) box().innerHTML = cleanReportHTML(r.report);
        const go = $("cleanGo");
        if (go) go.disabled = false;
      } catch (err) {
        if (box()) {
          box().innerHTML = `<span class="n u-warn">${esc(err.message)}</span>`;
        }
        const go = $("cleanGo");
        if (go) go.disabled = true;
      }
    };
    const refreshSoon = () => { clearTimeout(timer); timer = setTimeout(refresh, 260); };

    const close = showModal(
      `导入前清洗 · ${file.name}`,
      `<p class="hint" style="margin-bottom:10px">
         清洗只作用于<b>入库的文本</b>：磁盘上的原文件、以及上传的那份副本，
         一个字节都不会改。规则可以逐条勾，下面的预览每次都会重算。</p>
       <div id="cleanRules">${cleanRulesHTML(plan)}</div>
       <div id="cleanReport" class="clean-report"><span class="n">预览中…</span></div>
       <div class="clean-actions">
         <button class="ghost-btn" id="cleanReset">恢复默认</button>
         <button class="ghost-btn" id="cleanSkip">不清洗，直接导入</button>
         <button id="cleanGo" disabled>按此方案导入</button>
       </div>`,
      null,
      { ok: false, wide: true, onClose: () => settle(null) },
    );
    $("cleanRules").addEventListener("change", () => {
      const p = readCleanPlan($("cleanRules"));
      plan.enabled = p.enabled;
      plan.options = p.options;
      $("cleanRules").querySelectorAll("[data-clean]").forEach((el) => {
        el.closest(".clean-row").classList.toggle("on", el.checked);
      });
      refreshSoon();
    });
    $("cleanReset").onclick = () => {
      plan.enabled = {};
      plan.options = {};
      $("cleanRules").innerHTML = cleanRulesHTML(plan);
      refreshSoon();
    };
    $("cleanGo").onclick = () => settle({ plan, staged });
    $("cleanSkip").onclick = () => settle({ plan: null, staged });
    // 「关闭」= 放弃这次导入，而不是"按默认继续"——少一次误导入
    $("modalCancel").onclick = () => settle(null);
    refresh();
  });
}

/*
 * 上传拆成两段进度，因为它们是两件事：
 *   ① 传输   —— 浏览器把字节推给服务端，XHR 的 upload.onprogress 给真实百分比；
 *   ② 解析   —— 抽取、切分、嵌入、写库，服务端按阶段回报（本机嵌入约 21 块/秒）。
 * 只做①的后果就是"100% 停在那里转圈"，用户分不清是在跑还是死了。所以②
 * 走后台任务 + 轮询，进度条上是真实百分比 + "128/300 块"这样的具体数字。
 */
/* ---------------------------------------------------------------- 分片上传
 *
 * 单请求上传的天花板不在服务端，在链路上：几十 MB 就开始撞反向代理的 body
 * 上限和浏览器对"长时间无进展请求"的超时，几百 MB 基本必挂。这时
 * `max_file_mb` 设多大都没意义，因为根本走不到判大小那一步。
 *
 * 分片把"一次大传输"拆成"多次小传输"：失败代价从"整个文件重来"降到"重传
 * 一片"，进度也变成真实字节累计而不是估的。
 *
 * 阈值 8MiB：这以下的单请求在任何链路上都稳，再往上就该拆了。
 */
const CHUNK_THRESHOLD = 8 * 1024 * 1024;

/* 文件指纹 = 名字 + 大小 + 修改时间。
   不读内容算哈希：为了一个 id 把 500MB 从头读一遍，等于让用户多等一倍传输
   时间。这三项足够认出"是不是同一个文件"，续传本来就只需要身份、不需要
   内容级强校验（真拼错了后端还有总字节校验兜底）。 */
function fingerprintOf(file) {
  const s = `${file.name}|${file.size}|${file.lastModified}`;
  let h = 0x811c9dc5;
  for (let i = 0; i < s.length; i += 1) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 0x01000193) >>> 0;
  }
  let g = 0x9e3779b9;
  for (let i = s.length - 1; i >= 0; i -= 1) {
    g ^= s.charCodeAt(i);
    g = Math.imul(g, 0x85ebca6b) >>> 0;
  }
  return h.toString(16).padStart(8, "0") + g.toString(16).padStart(8, "0");
}

async function putPart(uploadId, index, blob) {
  const res = await fetch(`/api/upload/${uploadId}/part/${index}`, {
    method: "PUT",
    body: blob,
    headers: { "Content-Type": "application/octet-stream" },
  });
  if (res.ok) return;
  let msg = `第 ${index + 1} 片没传上去（HTTP ${res.status}）`;
  try {
    const body = await res.json();
    msg = body.error || body.detail || msg;
  } catch (_) { /* 非 JSON 就只报状态码 */ }
  const err = new Error(typeof msg === "string" ? msg : `HTTP ${res.status}`);
  err.status = res.status;
  throw err;
}

/* 跑完返回 staged 路径，后续导入直接用它，不再传一遍字节。
   单片失败重试两次——大文件传一半断掉是常态，重传 4MB 远好过让用户从头再来。
   但 4xx 不重试：那是"再试也一样"的确定性失败，耗三次只是让人多等。 */
async function uploadChunked(file, kbId, onProgress) {
  const init = await api("/api/upload/init", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      name: file.name,
      size: file.size,
      kb_id: kbId,
      fingerprint: fingerprintOf(file),
    }),
  });
  const uid = init.upload_id;
  const cs = init.chunk_size;
  const total = init.total_chunks;
  const done = new Set(init.received || []);
  const sizeOf = (i) => Math.min(cs, file.size - i * cs);
  let sent = 0;
  done.forEach((i) => { sent += sizeOf(i); });
  if (sent) onProgress((sent / file.size) * 100);

  for (let i = 0; i < total; i += 1) {
    if (done.has(i)) continue;
    const blob = file.slice(i * cs, Math.min((i + 1) * cs, file.size));
    let failed = null;
    for (let attempt = 0; attempt < 3; attempt += 1) {
      try {
        await putPart(uid, i, blob);
        failed = null;
        break;
      } catch (err) {
        failed = err;
        if (err.status && err.status < 500) break;
        await sleep(400 * (attempt + 1));
      }
    }
    if (failed) throw failed;
    sent += blob.size;
    onProgress(Math.min(100, (sent / file.size) * 100));
  }
  const fin = await api(`/api/upload/${uid}/finish`, { method: "POST" });
  return fin.staged;
}

/*
 * 上传拆成两段进度，因为它们是两件事：
 *   ① 传输   —— 浏览器把字节推给服务端，XHR 的 upload.onprogress 给真实百分比；
 *   ② 解析   —— 抽取、切分、嵌入、写库，服务端按阶段回报（本机嵌入约 21 块/秒）。
 * 只做①的后果就是"100% 停在那里转圈"，用户分不清是在跑还是死了。所以②
 * 走后台任务 + 轮询，进度条上是真实百分比 + "128/300 块"这样的具体数字。
 */
function uploadOne(url, file, onProgress, fields = null) {
  return new Promise((resolve, reject) => {
    const fd = new FormData();
    if (file) fd.append("file", file);
    // 清洗方案随文件一起提交；staged 那条路连文件都不用重传
    if (fields) {
      Object.entries(fields).forEach(([k, v]) => { if (v !== null && v !== undefined) fd.append(k, v); });
    }
    const xhr = new XMLHttpRequest();
    xhr.open("POST", url);
    if (!file && onProgress) onProgress(100);  // 没有字节要传，别让进度条停在 0
    xhr.upload.onprogress = (ev) => {
      if (ev.lengthComputable) onProgress((ev.loaded / ev.total) * 100);
    };
    xhr.onload = () => {
      let body = null;
      try { body = JSON.parse(xhr.responseText); } catch (_) { /* 非 JSON */ }
      if (xhr.status >= 200 && xhr.status < 300) { resolve(body); return; }
      const detail = body?.error || body?.detail;
      const err = new Error(
        typeof detail === "string" ? detail : `上传失败（HTTP ${xhr.status}）`);
      err.body = body;
      err.status = xhr.status;
      reject(err);
    };
    xhr.onerror = () => reject(new Error("网络中断，上传没完成"));
    xhr.ontimeout = () => reject(new Error("上传超时"));
    xhr.send(fd);
  });
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/* 任务进度卡：一个任务一张，各写各的 DOM。

   以前整页只有一张卡（ucName/ucPct 是写死的 id），同时跑两个重索引时，
   后发起的 start() 会把前一张的标题和百分比整个顶掉，而**两个轮询都还在
   往这同一处写**——看到的现象就是百分比来回跳、名字乱换。现在每张卡自建
   元素、自己持有引用，谁的数据进谁的卡。 */
const followedTasks = new Map();   // task_id -> card，避免同一任务建两张卡

function newTaskCard(name, queue) {
  const dock = $("taskDock");
  if (!dock) return null;
  const el = document.createElement("div");
  el.className = "upload-card waiting";
  el.innerHTML = `<div class="uc-head">
      <span class="uc-name"></span><span class="uc-pct">0%</span></div>
    <div class="uc-track"><i class="uc-fill"></i></div>
    <div class="uc-foot"><span class="uc-phase">准备上传</span><span class="uc-queue"></span></div>`;
  dock.appendChild(el);
  dock.hidden = false;
  const nm = el.querySelector(".uc-name");
  const pct = el.querySelector(".uc-pct");
  const fill = el.querySelector(".uc-fill");
  const phase = el.querySelector(".uc-phase");
  nm.textContent = name || "-";
  el.querySelector(".uc-queue").textContent = queue || "";

  const card = {
    el,
    _t0: performance.now(),
    _task: null,
    _phaseText: "上传中",
    _tick: null,
    _hide: null,
    bar(p) {
      const v = Math.max(0, Math.min(100, Math.round(p || 0)));
      fill.style.width = `${v}%`;
      pct.textContent = `${v}%`;
    },
    paint() {
      const sec = ((performance.now() - this._t0) / 1000).toFixed(0);
      const t = this._task;
      if (t) {
        if (t.state === "queued") {
          const ahead = Number(t.queue_pos || 0);
          phase.textContent = ahead > 0
            ? `排队 · 前面还有 ${ahead} 个 · ${sec}s`
            : `排队 · 马上开始 · ${sec}s`;
          return;
        }
        const detail = t.note ? ` · ${t.note}` : "";
        phase.textContent = `${t.stage || "处理中"}${detail} · ${sec}s`;
      } else {
        phase.textContent = `${this._phaseText}… ${sec}s`;
      }
    },
    transport(p) {
      this._task = null;
      this._phaseText = "上传中";
      this.bar(p);
    },
    ingest(task) {
      if (!task) return;
      this._task = task;
      this.bar((task.pct || 0) * 100);
      this.paint();
    },
    finish(text, ok) {
      clearInterval(this._tick);
      this.el.classList.remove("waiting");
      this.el.classList.add(ok ? "done" : "err");
      if (ok) this.bar(100);
      phase.textContent = text;
      clearTimeout(this._hide);
      const c = this.el;
      this._hide = setTimeout(() => {
        c.remove();
        if (!$("taskDock").children.length) $("taskDock").hidden = true;
      }, ok ? 1600 : 6000);
    },
  };
  // 计时器只负责"秒数"和文字；百分比由真正的数据源驱动
  card._tick = setInterval(() => card.paint(), 400);
  card.paint();
  return card;
}

/* 把已经在跑的任务接回卡片：切走页面再回来，原来的轮询已经随卡片一起没了，
   但服务端的任务还在排。不补的话用户会以为它丢了，再点一次重索引。 */
async function _syncRunningTasks() {
  let data;
  try {
    data = await api("/api/import/tasks");
  } catch {
    return;
  }
  (data.items || []).forEach((t) => {
    if (t.state !== "queued" && t.state !== "running") return;
    if (followedTasks.has(t.task_id)) return;
    followTask(t.task_id, t.label || "后台任务");
  });
}

function followTask(taskId, label) {
  const card = newTaskCard(label, "");
  if (!card) return null;
  followedTasks.set(taskId, card);
  card.ingest({ state: "queued", queue_pos: 0, stage: "排队", pct: 0, note: "" });
  waitTask(taskId, (snap) => card.ingest(snap)).then((t) => {
    const ok = t.state === "ready";
    card.finish(`${ok ? "完成" : "失败"} · ${t.message || ""}${
      ok ? covLine(t.result?.coverage) : ""}`, ok);
    followedTasks.delete(taskId);
    loadKb().then(() => refreshCurrentView());
  }).catch((err) => {
    card.finish(`失败 · ${err.message}`, false);
    followedTasks.delete(taskId);
  });
  return card;
}

/* 轮询导入任务，直到 ready / failed。onUpdate 每帧都能拿到最新快照。 */
async function waitTask(taskId, onUpdate) {
  const t0 = performance.now();
  for (;;) {
    const t = await api(`/api/import/task/${taskId}`);
    if (onUpdate) onUpdate(t);
    if (t.state === "ready" || t.state === "failed") return t;
    if (performance.now() - t0 > 30 * 60 * 1000) {
      throw new Error("等待超过 30 分钟，任务可能卡住了");
    }
    await sleep(320);
  }
}

/* 一个"上传 + 等任务"的完整回合，返回 {ok, message, result}

   大文件的字节不走这趟 multipart：先分片落到服务端，再把 staged 路径交给
   导入接口。改造前这里只有一条路——把整个文件塞进 FormData，几百 MB 的
   请求在链路上就没了，`max_file_mb` 再大也救不回来。 */
async function runUpload(url, file, queue, fields = null, label = null, kbId = null) {
  const card = newTaskCard(label || (file && file.name) || "导入", queue);
  let started;
  try {
    if (file && file.size > CHUNK_THRESHOLD) {
      const staged = await uploadChunked(
        file, kbId || state.kbId, (p) => card && card.transport(p));
      // 字节已经在盘上了，这个请求只是"告诉服务端去导入哪个文件"，
      // 所以进度直接给满，别让用户看着 99% 停住
      started = await uploadOne(url, null, () => {}, { ...(fields || {}), staged });
    } else {
      started = await uploadOne(url, file, (p) => card && card.transport(p), fields);
    }
  } catch (err) {
    card?.finish(`失败 · ${err.message}`, false);
    return { ok: false, message: err.message, error: err };
  }
  if (!started?.task_id) {
    // 兼容同步返回（老接口）：直接当结果用
    const ok = started?.status === "ready";
    card?.finish(`${ok ? "完成" : "失败"} · ${started?.message || ""}`, ok);
    return { ok, message: started?.message || "", result: started };
  }
  // 建卡之后立刻登记：syncRunningTasks 靠这张表判断"这个任务已经有人在跟了"
  if (card) followedTasks.set(started.task_id, card);
  try {
    const t = await waitTask(started.task_id, (snap) => card && card.ingest(snap));
    const ok = t.state === "ready";
    card?.finish(`${ok ? "完成" : "失败"} · ${t.message || ""}${
      ok ? covLine(t.result?.coverage) : ""}`, ok);
    return { ok, message: t.message || "", result: t.result };
  } catch (err) {
    card?.finish(`失败 · ${err.message}`, false);
    return { ok: false, message: err.message, error: err };
  } finally {
    followedTasks.delete(started.task_id);
  }
}

let uploading = false;

async function uploadFiles(files, kbId) {
  if (uploading) { toast("上一批还在上传，等它结束", "warn"); return []; }
  uploading = true;
  const results = [];
  const total = files.length;

  // 先过一遍清洗：以第一个文件预览，方案对本批全部文件生效。
  // 放在上传之前而不是之后——入库之后再谈"哪些不该进"就晚了，只能重索引。
  let plan = null;
  let staged = null;

  // 大文件得先分片传上来。预览接口本身是 multipart 收字节，几百 MB 在那儿
  // 一样走不通；小文件不必提前传，预览会顺手落盘并把 staged 回给我们。
  if (files[0].size > CHUNK_THRESHOLD) {
    const card = newTaskCard(files[0].name, total > 1 ? `第 1/${total} 个` : "");
    try {
      staged = await uploadChunked(files[0], kbId, (p) => card && card.transport(p));
      card?.finish("已传完，正在预览清洗效果", true);
    } catch (err) {
      card?.finish(`失败 · ${err.message}`, false);
      uploading = false;
      return [];
    }
  }

  try {
    const choice = await cleanPreviewDialog(files[0], kbId, staged);
    if (choice === null) {   // 用户放弃
      toast("已取消导入", "warn");
      return [];
    }
    plan = choice.plan;
    staged = choice.staged;
  } catch (err) {
    toast(`清洗预览失败：${err.message}`, "err");
    return [];
  } finally {
    uploading = false;
  }

  uploading = true;
  try {
    for (let i = 0; i < total; i += 1) {
      const f = files[i];
      const queue = total > 1 ? `第 ${i + 1}/${total} 个` : "";
      const fields = plan
        ? { plan: JSON.stringify(plan) }
        : null;
      // 第一个文件已经在预览时落盘了，直接拿 staged 导入，不再传一遍字节
      if (i === 0 && staged) {
        const r = await runUpload(`/api/import/upload?kb_id=${kbId}`, null, queue,
          { ...(fields || {}), staged }, f.name);
        results.push({ file: f.name, ok: r.ok, message: r.message });
      } else {
        // 其余文件交给 runUpload 自己判断要不要分片
        const r = await runUpload(
          `/api/import/upload?kb_id=${kbId}`, f, queue, fields, null, kbId);
        results.push({ file: f.name, ok: r.ok, message: r.message });
      }
      // 让进度卡动效走完一格再进入下一个，否则多个文件看起来像一阵抽搐
      if (i < total - 1) await sleep(260);
    }
  } finally {
    uploading = false;
  }

  await loadKb();
  refreshCurrentView();
  const bad = results.filter((r) => !r.ok);
  if (bad.length) {
    // 失败必须逐条说清楚，批量上传里"3 个成功 2 个失败"是最需要解释的场景
    alert(bad.map((r) => `${r.file}：${r.message}`).join("\n"));
  } else if (results.length > 1) {
    toast(`已导入 ${results.length} 个文档`);
  }
  return results;
}

/* 选文件（给"重新上传源文件"用）。取消时 Promise 永远不 resolve——没有
   await 之后必须做的事，悬挂比硬造一个"取消"异常更干净。 */
function pickFiles({ accept = "", multiple = false } = {}) {
  return new Promise((resolve) => {
    const inp = document.createElement("input");
    inp.type = "file";
    inp.multiple = multiple;
    if (accept) inp.accept = accept;
    inp.hidden = true;
    document.body.appendChild(inp);
    inp.onchange = () => {
      const picked = [...inp.files];
      inp.remove();
      resolve(picked);
    };
    inp.click();
  });
}

/* 给丢了原始文件的文档补文件。绑在原路径上重索引，doc_id 与已有引用编号都不变。 */
async function restoreDocSource(doc, btn) {
  const picked = await pickFiles();
  if (!picked.length) return [];
  if (btn) btn.disabled = true;
  try {
    const r = await runUpload(`/api/doc/${doc.id}/source`, picked[0], "补回原始文件");
    await loadKb();
    refreshCurrentView();
    if (!r.ok) alert(`重新索引失败：${r.message || "未知原因"}`);
    else toast(`${doc.title} 已重新索引`);
    return r.result || null;
  } finally {
    if (btn) btn.disabled = false;
  }
}

/* 重索引：同样是后台任务，慢的时候（几百块）能看见"嵌入 128/300 块" */
async function reindexDocTask(doc) {
  let t0 = null;
  let card = null;
  try {
    t0 = await jsonPost(`/api/doc/${doc.id}/reindex`, {});
    if (!t0?.task_id) {
      const ok = t0?.status === "ready";
      return { ok, message: t0?.message || "", result: t0 };
    }
    // 登记这张卡：syncRunningTasks 靠 followedTasks 判断"已有人在跟"
    card = newTaskCard(doc.title || `doc#${doc.id}`, "重索引");
    followedTasks.set(t0.task_id, card);
    card.ingest({ state: "queued", queue_pos: t0.queue_pos || 0, stage: "排队", pct: 0 });
    const t = await waitTask(t0.task_id, (snap) => card && card.ingest(snap));
    const ok = t.state === "ready";
    card?.finish(`${ok ? "完成" : "失败"} · ${t.message || ""}`, ok);
    return { ok, message: t.message || "", result: t.result };
  } catch (err) {
    toast(`重索引失败：${err.message}`);
    return { ok: false, message: err.message, error: err };
  } finally {
    if (t0?.task_id) followedTasks.delete(t0.task_id);
  }
}

document.querySelectorAll(".tab").forEach((t) => {
  t.onclick = () => switchTab(t.dataset.tab);
});

$("btnChatCfg").onclick = () => toggleCfgPop();

// 点面板外面关掉。用 mousedown 而不是 click：面板里的 <select> 弹原生列表时，
// click 会先冒泡到 document 把面板收掉，用户刚点开的下拉瞬间消失。
document.addEventListener("mousedown", (e) => {
  const pop = $("cfgPop");
  if (!pop || pop.hidden) return;
  if (pop.contains(e.target) || $("btnChatCfg").contains(e.target)) return;
  toggleCfgPop(false);
});

$("cfgPop").querySelectorAll("[data-goto]").forEach((btn) => {
  btn.onclick = () => { toggleCfgPop(false); setView(btn.dataset.goto); };
});

$("kbSel").onchange = async (e) => {
  const raw = e.target.value;
  if (!raw) {
    // 不挂载知识库：清掉库与会话，消息区留着——刚问出来的那几轮不该被换库冲掉。
    state.kbId = null;
    localStorage.removeItem("knowtune.kb");
    state.kbCfgDraftFor = null;
    state.kbDetail = null;
    state.docs = [];
    state.history = [];
    state.lastCtx = null;
    renderKbList();
    renderDocs();
    renderSessions();
    renderCfgSummary();
    return;
  }
  const id = Number(raw);
  if (!id || id === state.kbId) return;
  await selectKb(id);          // 里面已经重取会话并重绘知识库页
  // 在知识库页换库就留在知识库页。原来一律跳回对话页，等于把正在核对文档的
  // 人从当前页面拽走——换库跟"去聊天"没有必然关系。
  if (state.view !== "chat" && state.view !== "kb") await setView("chat");
  renderCfgSummary();
};

$("dbgToggle").onchange = (e) => {
  document.querySelector(".app").classList.toggle("panel-hidden", !e.target.checked);
  localStorage.setItem("knowtune.debug", e.target.checked ? "1" : "0");
};

$("providerSel").addEventListener("change", (e) => {
  localStorage.setItem("knowtune.provider", e.target.value);
  const opt = e.target.selectedOptions[0];
  e.target.title = opt?.title || opt?.textContent || "";
  // 换后端 = 换一批可用模型：还留着上一个后端的模型名，等于埋一个 404
  $("genModelSel").value = "";
  syncGenModelOptions();  saveAdv();
  renderCfgSummary();
  renderFootGen();  // 侧栏那行跟着下拉即时变，不等下一次 /api/status
});

/* 拿对话页当前选中的后端单独测一次翻译。
   为什么放这里：站点页那个「测翻译」测的是角色分工指向的后端，跟对话里选的
   不是同一个；用户想验证"我现在用的这个能不能翻"，就得有个跟着选择走的入口。 */
$("btnTrTest").onclick = async () => {
  const out = $("trTestOut");
  const btn = $("btnTrTest");
  const name = $("providerSel").value;
  if (!name) return;
  btn.disabled = true;
  out.hidden = false;
  out.innerHTML = `<span class="hint">${esc(name)} 翻译中…（一句往返，云端可能几秒）</span>`;
  try {
    const r = await jsonPost("/api/translate-test", { provider: name });
    out.innerHTML = r.ok
      ? `<div dir="auto">${esc(r.output)}</div>`
        + `<span class="hint">${esc(r.provider)} · ${r.src}→${r.dst} · ${r.latency_ms}ms</span>`
      : `<span class="warn">翻译失败：${esc(r.error || "输出为空或与原文相同")}</span>`
        + (r.output ? `<div dir="auto">${esc(r.output)}</div>` : "");
  } catch (err) {
    out.innerHTML = `<span class="warn">${esc(err.message)}</span>`;
  } finally {
    btn.disabled = false;
  }
};

$("topKSel").addEventListener("change", () => {
  localStorage.setItem("knowtune.topk", $("topKSel").value);
  renderCfgSummary();
});

$("deepMode").addEventListener("change", () => renderCfgSummary());

/* 高级参数：任何一处改动都存下来并刷新摘要。逐个绑定而不是委托到整个弹层，
   是因为这些控件散在 <details> 里，委托容易把 summary 的展开也当成改动。 */
["optMaxTok", "optStream", "optFusion", "optWdense", "optPool", "optMmr"]
  .forEach((id) => {
    const el = $(id);
    if (el) el.addEventListener("change", () => { saveAdv(); renderCfgSummary(); });
  });
// 生成模型：选到"手动输入"就露出输入框；选到某个后端的模型，把上面的后端
// 下拉一起带过去——否则界面显示"用 A 后端"，实际却拿 B 后端的模型名去请求。
$("genModelSel").addEventListener("change", () => {
  const v = $("genModelSel").value;
  toggleGenCustom(v === GEN_CUSTOM);
  const gm = parseGenPick();
  if (gm.provider && $("providerSel").value !== gm.provider) {
    $("providerSel").value = gm.provider;
    localStorage.setItem("knowtune.provider", gm.provider);
  }
  saveAdv();
  renderCfgSummary();
});
$("genModelCustom")?.addEventListener("input", () => saveAdv());
$("noKbMode").addEventListener("change", () => {
  saveAdv(); syncNoKbRow(); renderCfgSummary();
});
$("btnAdvReset").onclick = resetAdv;

/* ------------------------------------------------------------ 抽屉与快捷键 */
/*
 * 全局参数抽屉已经退役（见上面「参数控件」那段）。现在参数只有两个落点：
 *   · 每个库一套 —— 知识库页的「参数」栏目；
 *   · 出厂默认 —— config/default.yaml，或直接 PATCH /api/settings。
 * 所以这里只剩提示词抽屉的遮罩、以及把 Alt+, 还给「对话配置」。
 */

$("scrim").onclick = () => {
  if (!$("promptDrawer").hidden) closePromptDrawer();
};

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("promptDrawer").hidden) closePromptDrawer();
  if (e.key === "Escape" && !$("cfgPop").hidden) toggleCfgPop(false);
  // Alt+, 开「对话配置」。顶栏那颗按钮的 title 一直这么写着，以前却接到了
  // 全局参数抽屉上 —— 键盘和鼠标打开的是两个面板，这个不叫快捷键。
  if (e.altKey && (e.key === "," || e.code === "Comma")) {
    e.preventDefault();
    // 这个浮层挂在聊天页顶栏下面，在别的页里弹出来会是一个没有锚点的空盒子
    if (state.view === "chat") toggleCfgPop();
  }
});

document.addEventListener("click", (e) => {
  // 检索调试面板里的「调整」：从前开全局抽屉，现在直接落到本库参数页 ——
  // 那条调试信息本来就是"这个库这一问怎么检的"，改也该改这个库。
  if (e.target.closest("[data-open-settings]")) {
    e.preventDefault();
    setView("kb", { tab: "params" });
  }
});

/* ============================================================== 视图外壳 */
/*
 * 四个视图共用一套外壳，但布局需求完全不同：
 *   对话  = 消息流 + 输入框 + 右侧调试面板（三列，整块不动）
 *   其余  = 可滚动的文档页（两列，右面板收起）
 * 所以用 .app[data-view] 切换列数，而不是把所有页面都塞进聊天布局里打补丁。
 */

Object.assign(state, {
  view: "chat",
  dash: null,
  models: null,
  kbTab: "params",
  kbDetail: null,
  kbCfg: null,
  kbDraft: {},
  kbBase: {},        // 上次保存后的一套值 —— 用来算"有哪些改动没保存"
  kbOpen: {},        // 参数分组的展开状态，默认全部折叠
  logTab: "queries",
  presets: null,
  lastParams: null,
  kbCfgDraftFor: null,
});

const VIEW_BOX = { home: "viewHome", chat: "viewChat", kb: "viewKb",
  models: "viewModels", site: "viewSite" };

function setView(name, opts = {}) {
  if (!VIEW_BOX[name]) name = "chat";
  state.view = name;
  $("app").dataset.view = name;
  Object.entries(VIEW_BOX).forEach(([key, id]) => { $(id).hidden = key !== name; });

  document.querySelectorAll(".nav-item").forEach((b) =>
    b.classList.toggle("active", b.dataset.view === name));

  const chat = name === "chat";
  $("topbarChat").hidden = !chat;
  $("topbarPage").hidden = chat;
  toggleCfgPop(false);
  // 侧栏的会话与文档列表只服务对话视图；首页/模型页留着它们只是噪音
  $("secSession").hidden = !chat;
  $("secDoc").hidden = !chat;
  $("pageActions").innerHTML = "";

  let pending = null;
  if (name === "chat") {
    renderCfgSummary();
    // 不在这里弹「还没有可用的模型」：对话区本来就有引导卡说得清清楚楚，
    // 进站先糊一个模态框，等于每次打开都要先点掉一次。只有真的点发送
    // （submit / ask）时才弹，那是用户确实要提问的时刻。
    // 刚在模型页配好再切回来，别让"还没配模型"的引导卡赖在屏幕上。
    if (document.querySelector(".empty-state.gate") && canChat().ok) {
      renderEmptyChat();
    }
    $("question").focus();
  } else if (name === "home") {
    pending = renderHome();
  } else if (name === "kb") {
    pending = renderKbPage(opts.tab);
  } else if (name === "models") {
    pending = renderModels();
  } else if (name === "site") {
    pending = renderSites();
  }
  // 切页时把还在跑的任务接回卡片：留在知识库页点完重索引再切到对话页，
  // 原来的卡片是跟着那次调用走的，切页不会带走它，但有可能刚好看不见。
  syncRunningTasks();
  return pending || Promise.resolve();
}

/* 节流：syncRunningTasks 会被 setView 频繁触发，而它只是"补卡"，
   1 秒一次足够，不需要每次切页都打一次接口。 */
let _syncTasksAt = 0;
function syncRunningTasks() {
  const now = performance.now();
  if (now - _syncTasksAt < 1000) return;
  _syncTasksAt = now;
  _syncRunningTasks();
}

function refreshCurrentView() {
  // 这是「配置刚改完 / 后台任务刚跑完」的刷新，必须拿到新值，不吃展示快照。
  invalidateApiCache();
  if (state.view === "home") renderHome().catch(() => {});
  else if (state.view === "kb") renderKbPage().catch(() => {});
  else if (state.view === "models") renderModels().catch(() => {});
  else if (state.view === "site") renderSites().catch(() => {});
}

/* 极简 toast：成功提示不该弹 alert —— alert 会打断操作，而"已保存"并不需要
   用户点击确认。错误仍然用 alert，因为那是必须被看见的。 */
let toastTimer = null;
function toast(text, kind = "ok") {
  let el = $("toastBox");
  if (!el) {
    el = document.createElement("div");
    el.id = "toastBox";
    el.className = "toast";
    document.body.appendChild(el);
  }
  el.textContent = text;
  el.className = `toast show ${kind}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.className = "toast"; }, 2200);
}

const fmtInt = (n) => (n === null || n === undefined ? "-" : Number(n).toLocaleString("zh-CN"));

function uptime(s) {
  s = Number(s || 0);
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (d) return `${d} 天 ${h} 小时`;
  if (h) return `${h} 小时 ${m} 分`;
  return `${m} 分 ${Math.floor(s % 60)} 秒`;
}

function hhmm(iso) {
  if (!iso) return "-";
  const raw = String(iso);
  const d = new Date(raw.endsWith("Z") || raw.includes("+") ? raw : raw + "Z");
  if (Number.isNaN(d.getTime())) return raw.slice(11, 19) || raw;
  return d.toLocaleTimeString("zh-CN", { hour12: false });
}

function dotOf(status) {
  return ({ ok: "ok", warn: "warn", mock: "mock", down: "err", timeout: "err", error: "err",
    no_key: "err", unset: "unknown" })[status] || "unknown";
}

/* "探测于 3 分钟前"：没有时间戳的结论没法判断新不新——十分钟前测的"就绪"
   和刚刚测的"就绪"根本不是一回事。 */
function agoText(ts) {
  if (!ts) return "";
  // 后端给的是 epoch 秒。万一下次换成 ISO 字符串，这里会算出 NaN 并在界面上
  // 打印"NaN 天前"——宁可整段不显示，也别输出这种东西。
  const n = Number(ts);
  if (!Number.isFinite(n)) return "";
  const s = Math.max(0, (Date.now() / 1000) - n);
  if (s < 60) return "刚刚";
  if (s < 3600) return `${Math.floor(s / 60)} 分钟前`;
  if (s < 86400) return `${Math.floor(s / 3600)} 小时前`;
  return `${Math.floor(s / 86400)} 天前`;
}

function statusText(status) {
  return ({ ok: "就绪", warn: "需注意", mock: "Mock", down: "未启动", timeout: "超时",
    error: "错误", no_key: "缺密钥", unset: "未配置", unknown: "未探测" })[status] || status || "未探测";
}

const kpi = (k, v, s) => `<div class="kpi"><span class="k">${esc(k)}</span>
  <b>${v}</b><span class="s">${s}</span></div>`;
const kvRow = (k, v) => `<div class="kv"><span>${esc(k)}</span><b>${v}</b></div>`;

/* ================================================================== 首页 */

function svcRows(list) {
  if (!list || !list.length) return `<p class="empty-tip">还没有配置任何生成后端。</p>`;
  return list.map((s) => `
    <div class="svc-row">
      <i class="dot ${dotOf(s.status)}"></i>
      <span class="nm">${esc(s.name)}</span>
      <span class="u ell" title="${esc(s.base_url || "")}">${esc(s.base_url || "—")}</span>
      <span class="state" title="${esc(s.message || "")}">${esc(s.message || statusText(s.status))}</span>
      <span class="lat">${s.latency_ms ? s.latency_ms + "ms" : ""}</span>
    </div>`).join("");
}

function logRows(d, tab) {
  if (tab === "system") {
    const items = (d.logs && d.logs.system) || [];
    if (!items.length) return `<p class="empty-tip">还没有日志。进程重启会清空这里。</p>`;
    return items.slice(0, 40).map((i) => `
      <div class="log-row ${String(i.level || "").toLowerCase()}">
        <span class="tm">${esc(i.at)}</span>
        <span class="src">${esc(i.source)}</span>
        <span class="txt" title="${esc(i.message)}">${esc(i.message)}</span>
        <span class="rt">${esc(i.level)}</span>
      </div>`).join("");
  }
  const q = (d.logs && d.logs.queries) || [];
  if (!q.length) return `<p class="empty-tip">还没有检索记录。</p>`;
  return q.map((r) => `
    <div class="log-row${r.refused ? " warning" : ""}">
      <span class="tm">${esc(hhmm(r.created_at))}</span>
      <span class="src">${esc(r.kb_name || "—")}</span>
      <span class="txt">${r.question_chars || 0} 字 · ${esc(shortModel(r.model || "-"))}${
        r.refused ? " · 已拒答" : ""}${r.degraded ? " · 降级" : ""}</span>
      <span class="rt">${r.total_ms == null ? "-" : r.total_ms + " ms"}</span>
    </div>`).join("");
}

async function renderHome() {
  $("pageTitle").textContent = "系统总览";
  $("pageSub").textContent = "本地知识库 · 资产、服务与日志";
  $("pageActions").innerHTML =
    `<button class="ghost-btn" id="dashProbe">探测服务</button>
     <button class="ghost-btn" id="dashRefresh">刷新</button>`;

  const box = $("viewHome");
  if (!state.dash) box.innerHTML = `<div class="page"><p class="empty-tip">加载中…</p></div>`;

  let d;
  try {
    d = state.dash = await api("/api/dashboard");
  } catch (err) {
    box.innerHTML = `<div class="page"><p class="empty-tip">后端未就绪：${esc(err.message)}</p></div>`;
    return;
  }

  const a = d.assets;
  const h = d.health;

  box.innerHTML = `<div class="page">
    <div class="kpi-grid">
      ${kpi("知识库", fmtInt(a.kbs), `${a.docs} 个文档`)}
      ${kpi("已就绪文档", fmtInt(a.docs_ready),
        a.docs_failed ? `<span class="u-err">${a.docs_failed} 个失败</span>` : "全部成功")}
      ${kpi("Chunk", fmtInt(a.chunks), a.hidden_chunks ? `隐藏 ${a.hidden_chunks}` : "无隐藏片段")}
      ${kpi("向量", fmtInt(a.embeddings), `${a.embedding_mb} MB · ${d.embedder.dim}d`)}
      ${kpi("会话", fmtInt(a.sessions), `${fmtInt(a.messages)} 条消息`)}
      ${kpi("检索次数", fmtInt(a.queries), `${a.feedback_up} 赞 / ${a.feedback_down} 踩`)}
    </div>

    <div class="split">
      <section class="card">
        <div class="card-head"><h3>后端服务</h3>
          <button class="mini" id="probeAll">探测</button></div>
        <div id="homeSvc">${svcRows(d.services)}</div>
      </section>
      <section class="card">
        <div class="card-head"><h3>运行状态</h3><span class="aside">已运行 ${uptime(d.server.uptime_s)}</span></div>
        <div class="kv-list">
          ${kvRow("主服务", `:${d.server.port || "-"} · pid ${d.server.pid}`)}
          ${kvRow("嵌入模型", `<span class="ell" title="${esc(d.embedder.model)}">${esc(shortModel(d.embedder.model))}</span> · ${d.embedder.dim}d @ ${esc(d.embedder.device)}`)}
          ${kvRow("精排", d.rerank.enabled ? esc(d.rerank.model) : "已关闭")}
          ${kvRow("Python", esc(d.server.python))}
          ${kvRow("数据库", `${a.db_mb} MB`)}
        </div>
      </section>
    </div>

    <div class="split">
      <section class="card">
        <div class="card-head"><h3>近 24 小时</h3>
          <span class="aside">${h.queries} 次检索</span></div>
        <div class="kv-list">
          ${kvRow("拒答", h.refusal_rate == null ? "—" : `${h.refused} 次 · ${Math.round(h.refusal_rate * 100)}%`)}
          ${kvRow("平均耗时", h.avg_ms ? `${h.avg_ms} ms` : "—")}
          ${kvRow("p50 耗时", h.p50_ms ? `${h.p50_ms} ms` : "—")}
          ${kvRow("降级次数", String(h.degraded))}
          ${kvRow("最近一次", hhmm(h.last_query_at))}
        </div>
      </section>
      <section class="card">
        <div class="card-head"><h3>存储与评估</h3>
          <span class="aside">${a.data_files} 个文件</span></div>
        <div class="kv-list">
          ${kvRow("数据目录", `${a.data_mb} MB`)}
          ${kvRow("数据库文件", `${a.db_mb} MB`)}
          ${kvRow("数据路径", `<span class="ell" title="${esc(d.server.db_path)}">${esc(d.server.db_path)}</span>`)}
        </div>
      </section>
    </div>

    <section class="card">
      <div class="card-head">
        <h3>日志</h3>
        <div class="card-head" style="margin:0">
          <button class="mini" data-logtab="queries"${state.logTab === "queries" ? ' style="color:var(--text);border-color:var(--card-line)"' : ""}>请求</button>
          <button class="mini" data-logtab="system"${state.logTab === "system" ? ' style="color:var(--text);border-color:var(--card-line)"' : ""}>系统</button>
        </div>
      </div>
      <div id="homeLogs">${logRows(d, state.logTab)}</div>
    </section>
  </div>`;

  $("dashRefresh").onclick = () => {
    state.dash = null;
    invalidateApiCache();   // 点「刷新」就是要新数据，别拿 3 秒内的快照糊弄
    renderHome();
  };
  $("dashProbe").onclick = probeServices;
  $("probeAll").onclick = probeServices;
  box.querySelectorAll("[data-logtab]").forEach((btn) => {
    btn.onclick = () => {
      state.logTab = btn.dataset.logtab;
      box.querySelectorAll("[data-logtab]").forEach((b) =>
        b.removeAttribute("style"));
      btn.setAttribute("style", "color:var(--text);border-color:var(--card-line)");
      $("homeLogs").innerHTML = logRows(d, state.logTab);
    };
  });
}

async function probeServices() {
  const btns = ["probeAll", "dashProbe"].map($).filter(Boolean);
  btns.forEach((b) => (b.disabled = true));
  try {
    const r = await api("/api/services/probe", { method: "POST" });
    if (state.dash) {
      state.dash.services = state.dash.services.map((s) =>
        ({ ...s, ...(r.providers[s.name] || {}) }));
      if ($("homeSvc")) $("homeSvc").innerHTML = svcRows(state.dash.services);
    }
    const ok = Object.values(r.providers || {}).filter((p) => p.ok).length;
    toast(`${ok}/${Object.keys(r.providers || {}).length} 个后端可用`);
  } catch (err) {
    alert(`探测失败：${err.message}`);
  } finally {
    btns.forEach((b) => (b.disabled = false));
  }
}

/* ============================================================== 知识库页 */

function kbPaneHead() {
  const d = state.kbDetail;
  if (!d) return "";
  const s = d.stats;
  const avg = s.avg_tokens || 0;
  return `<div class="stat-strip">
    <div class="st"><span class="k">文档</span><b>${s.docs}</b></div>
    <div class="st"><span class="k">就绪</span><b>${s.docs_ready}</b></div>
    <div class="st"><span class="k">Chunk</span><b>${fmtInt(s.chunks)}</b></div>
    <div class="st"><span class="k">平均块长</span><b>${avg} tok</b></div>
    <div class="st"><span class="k">已建向量</span><b>${s.chunks ? Math.round((s.embedded / s.chunks) * 100) : 0}%</b></div>
    <div class="st" title="与系统默认值不同的参数项数"><span class="k">已调参数</span><b>${d.tuned_count || 0}</b></div>
  </div>`;
}

async function renderKbPage(tab) {
  if (tab) state.kbTab = tab;
  const box = $("viewKb");

  if (!state.kbId) {
    $("pageTitle").textContent = "知识库";
    $("pageSub").textContent = "";
    $("pageActions").innerHTML = "";
    box.innerHTML = `<div class="page"><div class="empty-tip">
      还没有选中知识库。点左下角「+」新建一个，或从侧栏选一个。</div></div>`;
    return;
  }

  let d;
  try {
    d = state.kbDetail = await api(`/api/kb/${state.kbId}/detail`);
  } catch (err) {
    box.innerHTML = `<div class="page"><p class="empty-tip">${esc(err.message)}</p></div>`;
    return;
  }

  state.kbName = d.kb.name;
  if (state.kbSearchFor !== state.kbId) { state.kbSearch = null; state.kbSearchFor = state.kbId; }
  $("pageTitle").textContent = d.kb.name;
  // 不再有"跟随全局"一说：每个库一套独立值，这里只报"与默认值不同的有几项"
  $("pageSub").textContent = d.tuned_count ? `${d.tuned_count} 项已调整` : "系统默认参数";
  // 操作按钮**不放顶栏**：顶栏只有 54px 高，四颗按钮齐排直接溢出被裁掉一半
  // （截图里就是上半截没了）。挪到页面内容里，跟标题、统计放在一起。
  $("pageActions").innerHTML = "";

  box.innerHTML = `<div class="page">
    ${d.kb.description ? `<p class="page-lead" style="color:var(--text-3);margin-bottom:12px">${esc(d.kb.description)}</p>` : ""}
    <div class="kb-actions">
      <button class="ghost-btn" data-kbact="import">导入文档</button>
      <button class="ghost-btn" data-kbact="chat">去提问</button>
      <button class="ghost-btn" data-kbact="delete" data-danger>删除知识库</button>
    </div>
    ${kbPaneHead()}
    <div class="tabs-bar">
      <button data-kbtab="info" class="${state.kbTab === "info" ? "active" : ""}">信息</button>
      <button data-kbtab="params" class="${state.kbTab === "params" ? "active" : ""}">参数</button>
      <button data-kbtab="docs" class="${state.kbTab === "docs" ? "active" : ""}">内容<span class="count">${d.stats.docs}</span></button>
      <button data-kbtab="search" class="${state.kbTab === "search" ? "active" : ""}">检索</button>
    </div>
    <div id="kbPane"><p class="empty-tip">加载中…</p></div>
  </div>`;

  box.querySelectorAll("[data-kbact]").forEach((btn) => {
    const act = btn.dataset.kbact;
    btn.onclick = () => {
      if (act === "import") $("btnImport").click();
      if (act === "chat") setView("chat");
      if (act === "delete") deleteKb(d.kb);
    };
  });

  box.querySelectorAll("[data-kbtab]").forEach((btn) => {
    btn.onclick = () => {
      state.kbTab = btn.dataset.kbtab;
      box.querySelectorAll("[data-kbtab]").forEach((b) =>
        b.classList.toggle("active", b === btn));
      renderKbPane();
    };
  });

  await renderKbPane();
}

async function renderKbPane() {
  const pane = $("kbPane");
  if (!pane) return;
  if (state.kbTab === "info") return renderKbInfoPane();
  if (state.kbTab === "docs") return renderKbDocs();
  if (state.kbTab === "search") return renderKbSearch();
  return renderKbParamsPane();
}

/* ------------------------------------------------------------------ 信息 */
/*
 * 「编辑信息」以前是顶栏一颗按钮，点了弹个窗。它和参数本来就是同一层的东西
 * （都是"这个库怎么配"），一个在顶栏弹窗、一个在下面的栏目里，改个名字要抬头
 * 找、改个参数要低头找。现在并排放进同一排栏目：信息 / 参数 / 内容 / 检索 /
 * 会话，编辑信息也不用弹窗挡住整页。
 *
 * 这里只留名称与描述 —— 分块、检索、生成那些参数在旁边的「参数」栏目里，
 * 两处都能改同一项只会让人不知道刚才那一下到底改了哪边。
 */

function fmtKbTime(v) {
  if (!v) return "—";
  const raw = String(v).trim();
  const d = /^\d+$/.test(raw) ? new Date(Number(raw) * 1000) : new Date(raw.replace(" ", "T"));
  return Number.isNaN(d.getTime()) ? raw : d.toLocaleString("zh-CN", { hour12: false });
}

function renderKbInfoPane() {
  const pane = $("kbPane");
  if (!pane) return;
  const d = state.kbDetail;
  if (!d) return;
  const s = d.stats || {};
  const emb = d.embedding || {};
  const src = (d.sources || []).map((x) => `${x.source_type} ${x.n}`).join(" · ") || "—";

  pane.innerHTML = `<div class="card">
      <div class="card-head">
        <h3>基本信息</h3><span class="aside">改名、改描述不影响已导入的内容</span>
      </div>
      <div class="form-grid">
        <div class="form-row"><label>名称</label>
          <input id="kiName" value="${esc(d.kb.name)}" maxlength="60"></div>
        <div class="form-row"><label>描述（可选）</label>
          <input id="kiDesc" value="${esc(d.kb.description || "")}" placeholder="这个库里放什么"></div>
      </div>
      <div class="form-actions">
        <button class="primary-btn" id="kiSave" disabled>保存</button>
        <button class="ghost-btn" id="kiRevert">还原</button>
        <span class="hint" id="kiHint">没有未保存的改动</span>
      </div>
    </div>
    <div class="card">
      <div class="card-head"><h3>只读信息</h3><span class="aside">由系统维护</span></div>
      <div class="kv-list">
        ${kvRow("知识库 ID", d.kb.id)}
        ${kvRow("创建时间", fmtKbTime(d.kb.created_at))}
        ${kvRow("文档", `${s.docs || 0} 个 · 就绪 ${s.docs_ready || 0}${
          s.docs_failed ? ` · 失败 ${s.docs_failed}` : ""}`)}
        ${kvRow("Chunk", fmtInt(s.chunks || 0))}
        ${kvRow("平均块长", `${s.avg_tokens || 0} tok`)}
        ${kvRow("已建向量", `${s.chunks ? Math.round((s.embedded / s.chunks) * 100) : 0}%`)}
        ${kvRow("来源类型", src)}
        ${kvRow("嵌入模型", `${esc(emb.model || "-")} @ ${emb.dim || "-"}d`)}
      </div>
      <p class="hint u-mt10">
        分块、检索、生成这几类参数在旁边的「参数」栏目里改；分块类保存后会自动重建本库索引。
      </p>
    </div>`;

  const nameEl = $("kiName");
  const descEl = $("kiDesc");
  const origName = d.kb.name || "";
  const origDesc = d.kb.description || "";
  const refresh = () => {
    const dirty = nameEl.value !== origName || descEl.value.trim() !== origDesc;
    $("kiSave").disabled = !dirty;
    $("kiHint").textContent = dirty ? "有改动还没保存" : "没有未保存的改动";
  };
  nameEl.oninput = refresh;
  descEl.oninput = refresh;
  $("kiRevert").onclick = () => {
    nameEl.value = origName; descEl.value = origDesc; refresh();
  };
  $("kiSave").onclick = () => saveKbInfo(d.kb.id, nameEl, descEl);
}

async function saveKbInfo(id, nameEl, descEl) {
  const name = nameEl.value.trim();
  if (!name) { toast("名称不能为空"); nameEl.focus(); return; }
  const btn = $("kiSave");
  btn.disabled = true;
  try {
    await api(`/api/kb/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, description: descEl.value.trim() }),
    });
    await loadKb();
    // 不带 tab 参数：停在「信息」栏目，别把人甩到参数页
    await renderKbPage();
    toast("知识库信息已更新");
  } catch (err) {
    alert(`保存失败：${err.message}`);
    renderKbInfoPane();
  } finally {
    if ($("kiSave")) $("kiSave").disabled = false;
  }
}

/* 侧栏那支铅笔：直接落到「信息」栏目，不再弹窗。 */
async function openKbInfo(kb) {
  setView("kb");
  if (state.kbId !== kb.id) await selectKb(kb.id);
  state.kbTab = "info";
  await renderKbPage();
}

/* ------------------------------------------------------------------ 参数 */
/*
 * 每个库持有一套**完整**参数，界面上没有"覆盖 / 跟随全局"这个维度。
 *
 * 为什么砍掉它：那套设计要在每一行回答"这一项跟不跟全局"，于是每行多一个
 * 「覆盖」开关、每个布尔项变成三态下拉，7 组 24 项就长得没法扫，改一个值
 * 要点两下。而它换来的"全局改了本库跟着变"更常制造困惑：改完全局发现某个
 * 库没跟着变，或者反过来。现在建库时按预设复制一套独立值，之后互不影响。
 *
 * 移除这个维度之后界面才腾得出手做两件正事：
 *   1. 分组默认折叠，组标题上直接给当前值 —— 不展开也能一眼扫完 7 组；
 *   2. 分块类参数保存后自动重建本库索引（不然改了等于没改）。
 */

/* 这一页只回答"这个库的资料怎么切、怎么检索"。拒答、生成、会话是问答行为，
   已经挪到对话配置浮层；它们仍按库存储，所以保存时整份草稿照旧提交，
   这里只是不把三组不属于知识库的旋钮摆出来。 */
const KB_PAGE_GROUPS = new Set(["chunking", "recall", "fusion", "rerank"]);
const kbVisibleGroups = (data) => (data?.groups || []).filter((g) => KB_PAGE_GROUPS.has(g.id));
const kbVisibleKeys = (data) => new Set(
  kbVisibleGroups(data).flatMap((g) => g.items.map((it) => it.key)));

function kbFind(key) {
  const data = state.kbCfg;
  if (!data) return null;
  for (const g of data.groups) {
    const hit = g.items.find((i) => i.key === key);
    if (hit) return hit;
  }
  return null;
}

function kbVal(it) {
  return it.key in state.kbDraft ? state.kbDraft[it.key] : it.value;
}

function kbText(it, v) {
  if (it.kind === "bool") return v ? "开" : "关";
  if (it.kind === "enum") return String(v);
  return `${fmtParamVal(it, v)}${it.unit ? " " + it.unit : ""}`;
}

/* 未保存的改动。保存按钮的可用状态、行高亮、组上的"N 项已改"都靠它。 */
function kbDirty() {
  const base = state.kbBase || {};
  const visible = kbVisibleKeys(state.kbCfg);
  return Object.keys(state.kbDraft).filter(
    (k) => visible.has(k) && state.kbDraft[k] !== base[k]);
}

/* 组标题上的一行摘要：折叠着也能看出这个库是什么口径。
   只取前三 —— 摘要写满七项就又变成一堵墙了。 */
function kbSummary(g) {
  return g.items.slice(0, 3).map((it) => {
    const v = kbVal(it);
    if (it.kind === "bool") return `${it.label} ${v ? "开" : "关"}`;
    if (it.kind === "enum") return `${it.label} ${v}`;
    return `${it.label} ${kbText(it, v)}`;
  }).join(" · ");
}

function kbRowHTML(it) {
  const v = kbVal(it);
  const dirty = (state.kbBase || {})[it.key] !== v;
  let ctl = "";

  if (it.kind === "bool") {
    ctl = `<label class="toggle sm"><input type="checkbox" data-kbbool="${it.key}"${
      v ? " checked" : ""}><span class="sw"></span>
      <span class="toggle-label">${v ? "开" : "关"}</span></label>`;
  } else if (it.kind === "enum") {
    // 枚举值在配置里是 size / heading / paragraph，直接给用户看这三个英文词
    // 等于让人猜"paragraph 到底按段还是按句"。界面只负责翻译，存的值不变。
    const STRATEGY = { size: "按长度打包", heading: "按章节切", paragraph: "按段落切" };
    const labelOf = (o) => {
      if (it.key === "chunking.strategy") return STRATEGY[o] || o;
      return o;
    };
    ctl = `<select data-kbctl="${it.key}">${
      it.options.map((o) => `<option value="${esc(o)}"${o === v ? " selected" : ""}>${
        esc(labelOf(o))}</option>`).join("")
    }</select>`;
  } else {
    ctl = numCtlHTML(it, v, false, `data-kbctl="${it.key}"`, "data-kbpickk");
  }

  return `<div class="kb-row${dirty ? " dirty" : ""}" data-kbrow="${it.key}">
    <div class="kb-main">
      <div class="kb-label">${esc(it.label)}${
        it.calib ? `<i class="mark cal" title="已标定，实际生效 ${esc(String(it.calib.value))}。${
          esc(it.calib.detail || "")}">标定 ${esc(String(it.calib.value))}</i>` : ""}${
        it.effect === "reindex" ? `<i class="mark rebuild" title="改后需要重建本库索引才生效">重建</i>` : ""}</div>
      <div class="kb-desc" title="${esc(it.desc || "")}">${esc(it.desc || "")}</div>
    </div>
    <div class="kb-ctl">${ctl}</div>
  </div>`;
}

async function renderKbParamsPane() {
  const pane = $("kbPane");
  if (!pane) return;
  // 「生成模型」这个下拉要显示 name · model，标签来自模型注册表。
  // 没拉到就只剩裸名字，用户分不清 lmstudio 上是哪个模型。
  if (!state.models) await loadModels();
  try {
    state.kbCfg = await api(`/api/kb/${state.kbId}/config`);
  } catch (err) {
    pane.innerHTML = `<p class="empty-tip">${esc(err.message)}</p>`;
    return;
  }
  if (!state.kbCfgDraftFor || state.kbCfgDraftFor !== state.kbId) {
    // 换库了才重置草稿，否则用户切走再切回来改动就没了
    state.kbDraft = { ...(state.kbCfg.values || {}) };
    state.kbBase = { ...state.kbDraft };
    state.kbCfgDraftFor = state.kbId;
  }
  paintKbParams();
}

function paintKbParams() {
  const pane = $("kbPane");
  const data = state.kbCfg;
  if (!pane || !data) return;
  const dirty = kbDirty();
  const docs = state.kbDetail?.stats?.docs || 0;
  const shown = kbVisibleGroups(data);
  const allOpen = shown.every((g) => state.kbOpen[g.id]);

  const groups = shown.map((g) => {
    const open = !!state.kbOpen[g.id];
    const n = g.items.filter((it) => dirty.includes(it.key)).length;
    return `<section class="card kb-group${open ? " open" : ""}">
      <button class="kb-ghead" data-kgtog="${g.id}" aria-expanded="${open}">
        <span class="chev">${open ? "▾" : "▸"}</span>
        <span class="g-title">${esc(g.label)}</span>
        <span class="g-sum ell">${esc(kbSummary(g))}</span>
        ${n ? `<span class="badge warn">${n} 项已改</span>` : ""}
      </button>
      ${open ? `<div class="kb-gbody">
        ${g.note ? `<p class="hint">${esc(g.note)}</p>` : ""}
        ${g.items.map(kbRowHTML).join("")}
      </div>` : ""}
    </section>`;
  }).join("");

  pane.innerHTML = `
    <div class="card">
      <div class="card-head">
        <h3>这个库的参数</h3>
        <span class="aside">独立一套 · 改全局不影响本库</span>
      </div>
      <p class="hint">
        这里只管这个库的切块与检索口径。拒答、生成、会话参数在「站点 → 回答行为」，
        按站点主库保存，前台网站与调试对话共用。<br>
        带「重建」标记的分块参数保存后重建本库索引${docs ? `（当前 ${docs} 篇）` : ""}；
        嵌入模型系统级：${esc(data.embedding.model)} @ ${data.embedding.dim}d。
      </p>
      <div class="form-actions">
        <button class="primary-btn" id="kbSave"${dirty.length ? "" : " disabled"}>${
          dirty.length ? `保存（${dirty.length} 项）` : "保存"}</button>
        <button class="ghost-btn" id="kbReset">恢复默认</button>
        <button class="ghost-btn" id="kbToggleAll">${allOpen ? "全部收起" : "全部展开"}</button>
        <span class="hint">${dirty.length ? "" : "没有未保存的改动"}</span>
      </div>
    </div>
    ${groups}`;

  pane.querySelectorAll("[data-kgtog]").forEach((btn) => {
    btn.onclick = () => {
      const id = btn.dataset.kgtog;
      state.kbOpen[id] = !state.kbOpen[id];
      paintKbParams();
    };
  });

  pane.querySelectorAll("[data-kbbool]").forEach((cb) => {
    cb.onchange = () => {
      const key = cb.dataset.kbbool;
      state.kbDraft[key] = cb.checked;
      const lab = cb.closest(".toggle")?.querySelector(".toggle-label");
      if (lab) lab.textContent = cb.checked ? "开" : "关";
      paintKbParams();
    };
  });

  // 整块重绘，chip 与步进按钮每次都是新节点，走委托避免"点一下生效两次"
  if (!pane.dataset.numBound) {
    pane.dataset.numBound = "1";
    pane.addEventListener("click", (e) => {
      const pick = e.target.closest("[data-kbpickk]");
      if (pick) {
        const it = kbFind(pick.dataset.kbpickk);
        if (!it) return;
        state.kbDraft[it.key] = it.kind === "int"
          ? Math.round(Number(pick.dataset.pick)) : Number(pick.dataset.pick);
        paintKbParams();
        return;
      }
      const stepBtn = e.target.closest("[data-step]");
      if (stepBtn) {
        const it = kbFind(stepBtn.dataset.kbctl);
        if (!it) return;
        const v = stepParamVal({ ...it, value: kbVal(it) }, Number(stepBtn.dataset.step));
        state.kbDraft[it.key] = v;
        paintKbParams();
      }
    });
  }

  pane.querySelectorAll("[data-kbctl]").forEach((el) => {
    const key = el.dataset.kbctl;
    if (el.tagName === "BUTTON") return;   // 步进按钮走上面的委托
    el.onchange = () => {
      if (el.type === "number") {
        const v = Number(el.value);
        if (!Number.isFinite(v)) { paintKbParams(); return; }
        state.kbDraft[key] = v;
      } else {
        state.kbDraft[key] = el.value;
      }
      paintKbParams();
    };
  });

  $("kbSave").onclick = saveKbParams;
  $("kbReset").onclick = resetKbParams;
  $("kbToggleAll").onclick = () => {
    const open = !shown.every((g) => state.kbOpen[g.id]);
    shown.forEach((g) => { state.kbOpen[g.id] = open; });
    paintKbParams();
  };
}

/* 保存后如果后端起了重建任务，把它接进任务坞 —— 用户改完分块就该看到
   "正在重建第 3/5 篇"，而不是只有一个 toast 说"已保存"。 */
function followReindex(reindex, fallback) {
  const ri = reindex || {};
  if (!ri.task_id) return;
  const card = followTask(ri.task_id, `重建索引 · ${fallback}`);
  if (card) toast(`已保存 · 正在重建索引（${ri.docs || 0} 个文档）`);
}

async function saveKbParams() {
  const btn = $("kbSave");
  btn.disabled = true;
  try {
    const r = await api(`/api/kb/${state.kbId}/config`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ values: state.kbDraft, replace: true }),
    });
    state.kbCfg = r.snapshot;
    state.kbBase = { ...(r.snapshot.values || {}) };
    state.kbDraft = { ...state.kbBase };
    await loadKb();
    await renderKbPage();
    followReindex(r.reindex, state.kbName || `库 #${state.kbId}`);
    if (!r.reindex?.task_id) toast(`已保存 ${(r.changed || []).length} 项`);
  } catch (err) {
    alert(`保存失败：${err.message}`);
    await renderKbParamsPane();
  } finally {
    if ($("kbSave")) $("kbSave").disabled = false;
  }
}

async function resetKbParams() {
  if (!(await askConfirm(
      "只把这个库的分块与检索参数恢复默认，回答、拒答、会话参数保持不动？<br>"
      + "（分块参数若与现在不同，会触发一次重建索引）",
      { title: "恢复默认参数", okLabel: "恢复默认" }))) return;
  // 整份提交：只把这一页可见的键换成默认值，隐藏三组照旧带回。走 DELETE 会
  // 把回答/会话也一起清掉——那三组现在归对话配置管，不在这里顺手重置。
  const values = { ...state.kbDraft };
  for (const g of kbVisibleGroups(state.kbCfg)) {
    for (const it of g.items) values[it.key] = it.default ?? it.value;
  }
  try {
    const r = await api(`/api/kb/${state.kbId}/config`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ values, replace: true }),
    });
    state.kbCfg = r.snapshot;
    state.kbBase = { ...(r.snapshot.values || {}) };
    state.kbDraft = { ...state.kbBase };
    await loadKb();
    await renderKbPage();
    followReindex(r.reindex, state.kbName || `库 #${state.kbId}`);
    if (!r.reindex?.task_id) toast("分块与检索参数已恢复默认");
  } catch (err) {
    alert(err.message);
  }
}

/* ------------------------------------------------------------ 内容 / 会话 */

/* 文档换库：只改归属，不重新导入。弹窗里把"不用重索引"说清楚，否则用户
   会以为这一下要等十分钟，或者担心引用编号全变了。 */
async function moveDoc(doc) {
  let kbs = state.kbs || [];
  if (!kbs.length) kbs = await api("/api/kb").catch(() => []);
  const others = kbs.filter((k) => k.id !== state.kbId);
  if (!others.length) { toast("还没有别的知识库，先在侧栏新建一个", "err"); return; }
  showModal(`移动「${esc(doc.title || `文档 ${doc.id}`)}」`, `
    <p class="hint">连同 chunk、向量、引用编号一起搬，不用重建索引，问答里的来源也不会断。</p>
    <div class="form-row"><label>目标知识库</label>
      <select id="moveKbSel">${others.map((k) =>
        `<option value="${k.id}">${esc(k.name)}（${Number(k.docs || 0)} 篇）</option>`).join("")}</select>
    </div>`,
    async () => {
      const r = await jsonPost(`/api/doc/${doc.id}/move`, {
        kb_id: Number($("moveKbSel").value),
      });
      toast(r.moved ? `已移到「${r.kb_name}」` : "这篇文档本来就在那个库里");
      await loadKb();
      await renderKbPage();
    }, { okLabel: "移动" });
}

async function renderKbDocs() {
  const pane = $("kbPane");
  if (!pane) return;
  const docs = await api(`/api/kb/${state.kbId}/docs`).catch(() => []);
  if (!docs.length) {
    pane.innerHTML = `<div class="card"><p class="empty-tip">
      这个库还没有文档。点上方「导入文档」，或侧栏的 ↑。</p></div>`;
    return;
  }
  pane.innerHTML = `<div class="card">
    <div class="card-head"><h3>文档 ${docs.length}</h3>
      <span class="aside">失败文档会给出可读原因</span></div>
    ${docs.map((d) => `
      <div class="doc-row${d.missing_source ? " no-src" : ""}">
        <span class="t" title="${esc(d.source_uri)}">${esc(d.title || d.source_uri.split(/[\\/]/).pop())}${
          d.stale_extractor ? `<span class="badge warn u-ml6"
             title="抽取器已升级（Word 表格与文本框现在也会入库），当前索引是旧版抽取结果，重索引后内容更全">内容可补全</span>` : ""}${
          d.missing_source ? `<span class="badge err u-ml6">源文件缺失</span>`
            : d.fail_reason ? `<span class="badge err u-ml6">失败</span>` : ""}${
          d.clean && d.clean.count ? `<span class="badge u-ml6"
             title="导入时按 ${d.clean.count} 条规则清洗过，库里是清洗后的文本">已清洗 ${d.clean.count} 项</span>` : ""}</span>
        <span class="n">${d.status === "ready" ? d.n_chunks + " chunk" : esc(d.status)}</span>
        <span class="ext n">${esc(d.source_type)}</span>
        <span class="time n">${esc(String(d.updated_at || "").replace("T", " ").slice(5, 16))}</span>
        <span class="acts">
          <button class="mini" data-dact="preview" data-id="${d.id}">预览</button>
          <button class="mini" data-dact="check" data-id="${d.id}"
            title="重新抽取源文件，核对里面的文字有多少真的进了库">自检</button>
          <button class="mini" data-dact="move" data-id="${d.id}"
            title="移到另一个知识库（chunk 与向量原样保留，不用重索引）">移动</button>
          <button class="mini" data-dact="reindex" data-id="${d.id}">${
            d.missing_source ? "重新上传" : "重索引"}</button>
          <button class="mini" data-dact="delete" data-id="${d.id}">删除</button>
        </span>
      </div>
      <div class="pv-check-out doc-check" id="dcheck-${d.id}"></div>
      ${d.missing_source
        ? `<div class="reason u-neg4-top6">
             原始文件已不在：${esc(d.source_uri)}。点「重新上传」选同一个文件，会放回原位再索引，
             已有引用编号不变。</div>`
        : d.fail_reason ? `<div class="reason u-neg4-top6">${esc(d.fail_reason)}</div>` : ""}`).join("")}
  </div>`;

  pane.querySelectorAll("[data-dact]").forEach((btn) => {
    btn.onclick = async () => {
      const doc = docs.find((x) => x.id === Number(btn.dataset.id));
      if (btn.dataset.dact === "preview") {
        previewDoc(doc.id);
        return;
      }
      // 自检不重绘列表：重绘会把刚算出来的结论连同按钮一起刷掉
      if (btn.dataset.dact === "check") {
        const out = document.getElementById(`dcheck-${doc.id}`);
        btn.disabled = true;
        const old = btn.textContent;
        btn.textContent = "核对中…";
        try {
          const r = await api(`/api/doc/${doc.id}/coverage`);
          if (out) out.innerHTML = covHTML(r);
        } catch (err) {
          if (out) out.innerHTML = `<div class="reason">${esc(err.message)}</div>`;
        } finally {
          btn.disabled = false;
          btn.textContent = old;
        }
        return;
      }
      if (btn.dataset.dact === "move") { moveDoc(doc); return; }
      if (btn.dataset.dact === "reindex") {
        if (doc.missing_source) {
          const r = await restoreDocSource(doc, btn);
          if (r) await renderKbPage();
          return;
        }
        btn.disabled = true;
        const r = await reindexDocTask(doc);
        if (r.error?.body?.missing_source) {
          btn.disabled = false;
          const rr = await restoreDocSource(doc, btn);
          if (rr) await renderKbPage();
          return;
        }
        if (r.ok) toast(`${doc.title}：${r.message}`);
        else alert(`${doc.title}：${r.message}`);
        btn.disabled = false;
      } else {
        if (!(await askConfirm(`删除「${esc(doc.title)}」及其全部 chunk？`,
            { title: "删除文档", okLabel: "删除" }))) return;
        const t0 = await api(`/api/doc/${doc.id}`, { method: "DELETE" });
        if (t0?.task_id) {
          followTask(t0.task_id, `删除 ${doc.title}`);
          // 行先淡出给即时反馈，数据等任务完成、refreshCurrentView 重绘时收口
          const row = btn.closest(".doc-row");
          if (row) { row.style.opacity = ".35"; row.style.pointerEvents = "none"; }
        }
      }
      await loadKb();
      await renderKbPage();
    };
  });
}

/* -------------------------------------------------------------- 知识库检索 */
/*
 * 这一页回答的是"为什么这条被召回、那条没有"。
 * 不调生成模型：生成慢且不可解释，而检索是能一眼看懂的——两路召回各自的排名、
 * 融合后的顺序、最终入选的几条。调分块参数、改 overlap、换预设之后，
 * 来这里搜一下就知道有没有变好。
 */
async function renderKbSearch() {
  const pane = $("kbPane");
  if (!pane) return;
  const q = state.kbQuery || "";
  const topK = state.kbTopK || "";
  pane.innerHTML = `
    <div class="card">
      <div class="card-head"><h3>知识库检索</h3>
        <span class="aside">只在「${esc(state.kbName || "当前库")}」里召回，不调用生成模型</span></div>
      <div class="kb-search-bar">
        <input id="kbQ" placeholder="输入问题或关键词，Enter 检索" value="${esc(q)}"
               autocomplete="off">
        <select id="kbQTop" title="本次召回条数（融合后）">
          ${["", "3", "5", "10", "15", "20"].map((v) =>
            `<option value="${v}"${String(topK) === v ? " selected" : ""}>${v || "跟随配置"}</option>`).join("")}
        </select>
        <button class="primary-btn" id="kbQGo">检索</button>
      </div>
      <p class="hint">和提问走同一条检索链路，只是不生成答案；点任一条看完整分块。</p>
    </div>
    <div id="kbQOut"></div>`;

  const go = async () => {
    await doKbSearch($("kbQ")?.value.trim());
  };
  $("kbQGo").onclick = go;
  $("kbQ").addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); go(); }
  });
  $("kbQTop").onchange = (e) => { state.kbTopK = e.target.value; };

  if (state.kbSearch) paintKbSearch();
  else $("kbQOut").innerHTML = `<div class="card"><p class="empty-tip">
    还没有检索结果。输入一句话试试——比如你平时会问的那个问题。</p></div>`;
}

async function doKbSearch(question) {
  if (!question) { toast("先输入要检索的内容", "warn"); return; }
  state.kbQuery = question;
  const out = $("kbQOut");
  if (out) out.innerHTML = `<div class="card"><p class="hint">检索中…</p></div>`;
  try {
    const d = await jsonPost("/api/search/debug", {
      question,
      kb_id: state.kbId,
      top_k: state.kbTopK ? Number(state.kbTopK) : null,
    });
    // 三组结果只给 chunk_id，原文要另外取。逐条请求是 N 次往返，一次批量就够。
    const ids = new Set();
    (d.final || []).forEach((x) => ids.add(x.chunk_id));
    (d.stages?.dense || []).forEach((x) => ids.add(x.chunk_id));
    (d.stages?.sparse || []).forEach((x) => ids.add(x.chunk_id));
    const rows = ids.size
      ? await api(`/api/chunks?ids=${[...ids].join(",")}`).catch(() => [])
      : [];
    state.kbSearch = { q: question, d, map: new Map(rows.map((r) => [r.id, r])) };
    paintKbSearch();
  } catch (err) {
    if (out) out.innerHTML = `<div class="card"><p class="empty-tip">检索失败：${esc(err.message)}</p></div>`;
  }
}

function hitSnippet(cid, fallback) {
  const c = state.kbSearch?.map.get(cid);
  const text = (c?.text || fallback || "").replace(/\s+/g, " ").trim();
  return text.length > 160 ? `${text.slice(0, 160)}…` : text;
}

function hitHTML({ n, cid, scoreText, docLabel, extra = "" }) {
  const c = state.kbSearch?.map.get(cid);
  return `<article class="hit" data-cid="${cid}">
      <div class="hit-top">
        <span class="hit-rank">${n}</span>
        <span class="hit-score">${scoreText}</span>
        <span class="hit-doc ell" title="${esc(c?.source_uri || "")}">${esc(docLabel)}</span>
      </div>
      <p class="hit-text">${esc(hitSnippet(cid))}</p>
      ${extra ? `<div class="hit-extra">${extra}</div>` : ""}
    </article>`;
}

function paintKbSearch() {
  const out = $("kbQOut");
  const s = state.kbSearch;
  if (!out || !s) return;
  const { d } = s;
  const dense = d.stages?.dense || [];
  const sparse = d.stages?.sparse || [];
  const final = d.final || [];
  const ref = d.refusal || {};
  const p = d.params || {};

  const num = (v, d = 3) => (v === null || v === undefined ? "-" : Number(v).toFixed(d));
  const refuse = ref.refused
    ? `<span class="badge err">会拒答</span> ${esc(ref.reason || "")}`
    : `<span class="badge ok">会作答</span> top ${num(ref.top_score)} · margin ${num(ref.margin)}`;
  const degraded = (d.degraded || []).length
    ? `<span class="warn">降级 ${esc((d.degraded || []).join(","))}</span>` : "";

  // 顶行只放"文档名"，章节路径挪到下面一行：挤在顶行会把文档名截成
  // "MinerU_mar…"，而它恰恰是"这条来自哪篇"的唯一线索。
  const finalHTML = final.length ? final.map((f) => hitHTML({
    n: f.n,
    cid: f.chunk_id,
    scoreText: `rrf ${f.rrf}`,
    docLabel: `${f.doc_title || "?"}${f.page_no ? ` · p${f.page_no}` : ""}`,
    extra: [
      f.heading_path ? `§ ${f.heading_path}` : "",
      f.dense_rank ? `稠密 #${f.dense_rank}（${f.dense_score ?? "-"}）` : "",
      f.sparse_rank ? `稀疏 #${f.sparse_rank}（${f.sparse_score ?? "-"}）` : "",
      f.rerank_score !== null ? `精排 ${f.rerank_score}` : "",
    ].filter(Boolean).join(" · "),
  })).join("") : `<p class="empty-tip">没有入选任何分块。</p>`;

  // 两路召回各 50 条，全画出来是几百个方块，没人会滚到底。只给前 20 条，
  // 表头写清总数——想看完整排序是调试面板的活，这里回答的是"召回了什么"。
  const SHOW = 20;
  const colHTML = (list, scoreFn, label) => (list.length ? list.slice(0, SHOW).map((h) => {
    const c = state.kbSearch.map.get(h.chunk_id);
    return hitHTML({
      n: h.rank,
      cid: h.chunk_id,
      scoreText: scoreFn(h),
      docLabel: c?.doc_title || "?",
    });
  }).join("") : `<p class="empty-tip">空</p>`);

  out.innerHTML = `
    <div class="card">
      <div class="card-head">
        <h3>「${esc(d.query)}」召回 ${final.length} 条</h3>
        <span class="aside">${d.total_ms} ms · 稠密 top${p.dense_top_k} + 稀疏 top${p.sparse_top_k}
          → 融合 top${p.fusion_top_k}${p.rerank_used ? " · 已精排" : " · 未精排"}</span>
      </div>
      <div class="hit-verdict">${refuse}${degraded ? ` · ${degraded}` : ""}</div>
      <div class="hit-cols">
        <section class="hit-col">
          <div class="hit-col-head">最终入选 <span>${final.length}</span></div>
          ${finalHTML}
        </section>
        <section class="hit-col">
          <div class="hit-col-head">稠密召回 <span>${dense.length}${
            dense.length > SHOW ? ` · 显示前 ${SHOW}` : ""}</span></div>
          ${colHTML(dense, (h) => `cos ${h.score}`)}
        </section>
        <section class="hit-col">
          <div class="hit-col-head">稀疏召回 <span>${sparse.length}${
            sparse.length > SHOW ? ` · 显示前 ${SHOW}` : ""}</span></div>
          ${colHTML(sparse, (h) => `bm25 ${h.score}`)}
        </section>
      </div>
    </div>`;

  out.querySelectorAll(".hit").forEach((el) => {
    el.onclick = () => showHitText(Number(el.dataset.cid));
  });
}

function showHitText(cid) {
  const c = state.kbSearch?.map.get(cid);
  if (!c) return;
  showModal(`${c.doc_title} · 第 ${c.seq + 1} 块`, `
    <div class="hit-detail">
      <div class="kv2"><span class="k2">章节</span>
        <span class="v2">${esc(c.heading_path || "-")}</span></div>
      <div class="kv2"><span class="k2">规模</span>
        <span class="v2">${c.chars} 字 · ${c.tokens} tok${c.page_no ? ` · 第 ${c.page_no} 页` : ""}</span></div>
      <div class="kv2"><span class="k2">来源</span>
        <span class="v2 ell" title="${esc(c.source_uri || "")}">${esc(c.source_uri || "-")}</span></div>
    </div>
    <pre class="src-text" style="max-height:50vh;overflow:auto;white-space:pre-wrap">${esc(c.text)}</pre>`,
    null, { ok: false, wide: true });
}


/* 库的基本信息现在在知识库页的「信息」栏目里改（renderKbInfoPane），
   参数在旁边的「参数」栏目 —— 两件事各归各位，不再塞进同一个弹窗。 */

/*
 * 删库确认。以前这里是一句原生 confirm()：在部分内嵌 WebView 里它直接被吞掉
 * （返回 undefined），表现就是"点了删除，什么也没发生"——看上去像功能没做。
 * 换成页内弹窗，并且把代价摆出来：不是问"你确定吗"，而是告诉你"会删掉这些"。
 *
 * 库里有内容时要求手打库名。这一下不是刁难：删错的代价是整个库重建一遍索引，
 * 而误点的概率并不低（删除按钮就在「导入」旁边）。空库没有可丢的东西，直接点。
 */
async function deleteKb(kb) {
  const d = state.kbDetail && state.kbDetail.kb && state.kbDetail.kb.id === kb.id
    ? state.kbDetail
    : await api(`/api/kb/${kb.id}/detail`).catch(() => null);
  const s = (d && d.stats) || {};
  const docs = Number(s.docs || 0);
  const chunks = Number(s.chunks || 0);
  const sessions = Number(s.sessions || 0);
  const heavy = docs > 0 || chunks > 0;

  const body = `
    <div class="del-warn">
      <b>删除「${esc(kb.name)}」会一并删掉这些，且无法撤销：</b>
      <ul>
        <li>${docs} 篇文档 · ${chunks} 个 chunk（连同全文索引）</li>
        <li>这个库单独调过的参数</li>
      </ul>
      <p class="del-keep">这个库下的 ${sessions} 条会话会<b>保留</b>，只是解除与知识库的绑定
        （再打开这些会话时要重新选一个库）。向量缓存按内容存，别的库还能复用，不会一起清。</p>
    </div>
    ${heavy ? `<div class="form-row u-mt12">
      <label>请输入库名确认：<code>${esc(kb.name)}</code></label>
      <div style="display:flex;gap:6px;align-items:center">
        <input id="delName" placeholder="${esc(kb.name)}" autocomplete="off" spellcheck="false">
        <button type="button" class="mini" id="delNameFill">填入</button>
      </div>
    </div>` : `<p class="del-keep u-mt10">这是个空库，没有内容可丢。</p>`}`;

  showModal("删除知识库", body, async () => {
    const r = await api(`/api/kb/${kb.id}`, { method: "DELETE" });
    // 删的正好是当前库：清掉本地选中的痕迹，别让界面停在一个已经不存在的库上。
    if (state.kbId === kb.id) {
      state.kbId = null;
      state.kbDetail = null;
      state.kbCfgDraftFor = null;
      state.kbSearch = null;
      localStorage.removeItem("knowtune.kb");
    }
    toast(`已删除「${kb.name}」：${r.docs} 篇文档 / ${r.chunks} 个 chunk`);
    await loadKb();
    if (!state.kbId && state.kbs.length) await selectKb(state.kbs[0].id);
    setView("kb");
  }, {
    okLabel: heavy ? "确认删除" : "删除空库",
    danger: true,
    gate: () => !heavy || (($("delName") || {}).value || "").trim() === kb.name,
  });

  const fill = $("delNameFill");
  if (fill) {
    fill.onclick = () => {
      const input = $("delName");
      input.value = kb.name;
      input.dispatchEvent(new Event("input"));
      input.focus();
    };
  }
}

/* ============================================================== 模型服务 */

/* ------------------------------------------------------------ 提示词配置 */

/* 提示词跟后端放在同一页，是因为两者互相决定对方的效果：同一份约束，本地 7B
   要拆成编号条款才照做，云端强模型一句话就够。把"这份是给谁配的"摆在眼前，
   比单开一页"提示词管理"更不容易配错对象。

   三层结构：后端级覆盖 → 角色默认 → 内置。抽屉里一次只编辑其中一层，
   顶上那排按钮决定改哪一层——混着改最容易出现"我以为改的是这个后端"。 */
const prState = {
  provider: "",   // 空串 = 角色默认：所有没单独配过的后端共用这一份
  model: "",      // 从哪张卡片进来的
  scope: "",      // 板块维度（med/classic），空串 = 不按板块
  scopeLabel: "", // 板块显示名，仅用于抽屉标题
  roles: [],
  drafts: {},     // role -> { field: 草稿文本 }
  meta: {},       // role -> /api/prompts/<role> 的返回
  panel: "",      // "" | "history" | "preview"
  panelData: null,
  dirty: false,
};

let prBound = false;

async function openPromptDrawer(name, scope) {
  let ov = null;
  try {
    ov = await api("/api/prompts");
  } catch (err) {
    toast(err.message, "err");
    return;
  }
  prState.model = name || "";
  prState.provider = name || "";
  prState.scope = scope || "";
  prState.scopeLabel = scope ? (scope === "med" ? "维医问答" : scope === "classic" ? "古籍问答" : scope) : "";
  prState.roles = ov.roles || [];
  prState.panel = "";
  prState.panelData = null;
  const t = prState.scopeLabel
    ? "提示词 · " + prState.scopeLabel + (name ? "（" + name + "）" : "")
    : (name ? "提示词 · " + name : "提示词");
  $("prTitle").textContent = t;
  $("promptDrawer").hidden = false;
  $("scrim").hidden = false;
  prBindOnce();
  await prLoad();
}

function closePromptDrawer() {
  $("promptDrawer").hidden = true;
  $("scrim").hidden = true;
  prState.panel = "";
  prState.panelData = null;
}

function prBindOnce() {
  if (prBound) return;
  prBound = true;
  $("prClose").onclick = closePromptDrawer;
  $("prSave").onclick = prSave;
  $("prReset").onclick = prReset;
  $("prPreview").onclick = prPreview;
  $("prHistory").onclick = prHistory;

  // 输入用委托：每次重绘都换掉 textarea，绑在元素上就得每次重绑，
  // 漏绑一次的表现是"改了但保存的还是旧的"。
  $("prBody").addEventListener("input", (e) => {
    const t = e.target.closest("textarea[data-field]");
    if (!t) return;
    const role = t.dataset.role;
    if (!prState.drafts[role]) return;
    prState.drafts[role][t.dataset.field] = t.value;
    prAutoSize(t);
    prUpdateCount(t);
    prState.dirty = true;
    $("prNote").textContent = "有未保存的修改";
    $("prNote").className = "dirty-note warn";
  });

  $("prBody").addEventListener("click", (e) => {
    const ph = e.target.closest("[data-ph]");
    if (ph) { prInsertPlaceholder(ph); return; }
    const scope = e.target.closest("[data-scope]");
    if (scope) { prSwitchScope(scope.dataset.scope); return; }
    const rb = e.target.closest("[data-rollback]");
    if (rb) { prRollback(rb.dataset.rollback); return; }
    if (e.target.closest("[data-panel-close]")) {
      prState.panel = "";
      prState.panelData = null;
      prRender();
    }
  });
}

async function prLoad() {
  $("prBody").innerHTML = `<p class="hint">加载中…</p>`;
  const q = "?provider=" + encodeURIComponent(prState.provider)
    + "&scope=" + encodeURIComponent(prState.scope);
  const meta = {};
  const drafts = {};
  for (const r of prState.roles) {
    let got = null;
    try { got = await api("/api/prompts/" + r.role + q); } catch (_) { got = null; }
    meta[r.role] = got;
    drafts[r.role] = {};
    for (const f of r.fields) {
      drafts[r.role][f.key] = (got && got.fields && got.fields[f.key] && got.fields[f.key].text) || "";
    }
  }
  prState.meta = meta;
  prState.drafts = drafts;
  prState.dirty = false;
  let scope = "";
  if (prState.scope) scope = "作用范围：板块「" + prState.scopeLabel + "」";
  else if (prState.provider) scope = "作用范围：" + prState.provider;
  else scope = "作用范围：角色默认";
  $("prNote").textContent = scope;
  $("prNote").className = "dirty-note";
  prRender();
}

function prSourceBadge(src) {
  if (src === "scope") return `<span class="pr-src on">本板块已自定义</span>`;
  if (src === "provider") return `<span class="pr-src on">本后端已自定义</span>`;
  if (src === "role") return `<span class="pr-src on">角色默认已自定义</span>`;
  return `<span class="pr-src">内置默认</span>`;
}

/* 高度跟着内容走。固定 rows 的结果是 qa_v1 那份只能看到前 5 条，
   而"第 6 条写了什么"恰恰是用户最需要核对的东西——提示词是契约，
   看不全就等于没法审。上限 460px 是为了长提示词别把抽屉撑成一屏一屏翻。 */
function prAutoSize(area) {
  area.style.height = "auto";
  const h = Math.min(area.scrollHeight + 2, 460);
  area.style.height = h + "px";
}

function prUpdateCount(area) {
  const n = (area.value || "").length;
  const box = $("prBody").querySelector(
    `[data-count="${area.dataset.role}-${area.dataset.field}"]`);
  if (box) box.textContent = n + " 字";
}

function prRender() {
  const p = prState.provider;
  const sc = prState.scope;
  const scope = sc
    ? `<div class="pr-scope">
         <button class="mini on" data-scope="__scope__">板块「${esc(prState.scopeLabel)}」</button>
         <button class="mini" data-scope="__default__">全站（不按板块）</button>
       </div>
       <p class="hint">优先于后端级提示词${p ? `；当前后端 ${esc(p)}` : "；不按后端区分"}。</p>`
    : p
    ? `<div class="pr-scope">
         <button class="mini on" data-scope="__model__">仅 ${esc(p)}</button>
         <button class="mini" data-scope="__default__">所有未单独配置的后端</button>
       </div>
       <p class="hint">只对这个后端生效。切到别的后端时，它自己的提示词或内置默认照旧。</p>`
    : `<div class="pr-scope">
         <button class="mini" data-scope="__model__">仅 ${esc(prState.model)}</button>
         <button class="mini on" data-scope="__default__">所有未单独配置的后端</button>
       </div>
       <p class="hint">没单独配置过的后端都用这一份。</p>`;

  const blocks = prState.roles.map((r) => {
    const m = prState.meta[r.role] || {};
    const fields = r.fields.map((f) => {
      const info = (m.fields && m.fields[f.key]) || {};
      const chips = (f.placeholders || []).map((ph) =>
        `<button class="pr-ph" data-ph="${esc(ph.key)}" type="button"
           title="点一下插入到光标处">${esc("{" + ph.key + "}")}
           <em>${esc(ph.label)}</em></button>`).join("");
      return `<div class="pr-field">
        <div class="pr-field-head">
          <b>${esc(f.label)}</b>
          ${prSourceBadge(info.source || "builtin")}
          <span class="pr-count" data-count="${esc(r.role)}-${esc(f.key)}"
            >${(prState.drafts[r.role][f.key] || "").length} 字</span>
        </div>
        <p class="pr-hint">${esc(f.hint || "")}</p>
        ${chips ? `<div class="pr-chips">${chips}</div>` : ""}
        <textarea data-role="${esc(r.role)}" data-field="${esc(f.key)}"
          spellcheck="false" rows="${r.per_provider === false ? (f.key === "samples" ? 4 : 2) : 8}"
        >${esc(prState.drafts[r.role][f.key] || "")}</textarea>
        ${info.source !== "builtin"
          ? `<div class="pr-diff">内置原文 ${(info.builtin || "").length} 字，
               当前 ${(info.text || "").length} 字
               <button class="mini" data-scope="${p ? "__default__" : "__model__"}">看看内置那份</button>
             </div>`
          : ""}
      </div>`;
    }).join("");
    return `<section class="pr-role">
      <header>
        <h4>${esc(r.label)}</h4>
        <span class="hint">${esc(r.role)}</span>
        ${r.per_provider === false
          ? `<span class="hint">全站共用 · 与模型后端无关</span>` : ""}
      </header>
      ${fields}
    </section>`;
  }).join("");

  let panelHtml = "";
  if (prState.panel === "preview" && prState.panelData) {
    const d = prState.panelData;
    panelHtml = `<section class="pr-panel">
      <header>
        <b>模型实际收到的东西</b>
        <button class="icon-btn" data-panel-close title="关闭">×</button>
      </header>
      <p class="hint">${esc(d.note || "")}</p>
      <div class="pr-panel-label">system</div>
      <pre>${esc(d.system || "")}</pre>
      ${d.append_sample ? `<div class="pr-panel-label">实际还会追加（按回答语言，这里以维吾尔语为例）</div>
      <pre class="dim">${esc(d.append_sample)}</pre>` : ""}
      <div class="pr-panel-label">user</div>
      <pre>${esc(d.user || "")}</pre>
    </section>`;
  } else if (prState.panel === "history" && prState.panelData) {
    const items = prState.panelData.items || [];
    panelHtml = `<section class="pr-panel">
      <header>
        <b>保存历史</b>
        <button class="icon-btn" data-panel-close title="关闭">×</button>
      </header>
      <p class="hint">保存前自动留一份快照，最多 30 份。回滚是整体替换，不做字段合并。</p>
      ${items.length ? items.map((it) => `<div class="pr-hist">
        <span class="pr-hist-ts">${esc(it.ts)}</span>
        <span class="pr-hist-why ell">${esc(it.reason)}</span>
        <button class="mini" data-rollback="${esc(it.name)}">回滚到这份</button>
      </div>`).join("") : `<p class="hint">还没有历史快照。第一次保存之后才会出现。</p>`}
    </section>`;
  }

  $("prBody").innerHTML = `<div class="pr-wrap">${scope}${panelHtml}${blocks}</div>`;
  // 必须在挂进 DOM 之后再量：元素不在文档里时 scrollHeight 恒为 0，
  // 那样每个框都会塌成一行高。
  $("prBody").querySelectorAll("textarea[data-field]").forEach((t) => {
    prAutoSize(t);
    prUpdateCount(t);
  });
}

async function prSwitchScope(v) {
  if (v === "__scope__") { prRender(); return; }  // 板块层：已在板块，仅重绘
  const next = v === "__default__" ? "" : prState.model;
  if (next === prState.provider && !prState.scope) { prRender(); return; }
  if (prState.dirty && !confirm("当前这层有没保存的修改，切换作用范围会丢掉它们。继续？")) return;
  prState.provider = next;
  prState.scope = "";
  prState.scopeLabel = "";
  prState.panel = "";
  prState.panelData = null;
  await prLoad();
}

function prInsertPlaceholder(btn) {
  const key = "{" + btn.dataset.ph + "}";
  const area = $("prBody").querySelector(`textarea[data-role="translate"][data-field="user"]`)
    || $("prBody").querySelector("textarea[data-field]");
  if (!area) return;
  const s = area.selectionStart ?? area.value.length;
  const e = area.selectionEnd ?? area.value.length;
  area.value = area.value.slice(0, s) + key + area.value.slice(e);
  area.selectionStart = area.selectionEnd = s + key.length;
  area.focus();
  // 直接赋 value 不会触发 input 事件，草稿得手动同步一次，
  // 否则用户点完占位符直接保存，存下去的还是旧文本。
  const role = area.dataset.role;
  if (prState.drafts[role]) prState.drafts[role][area.dataset.field] = area.value;
  prState.dirty = true;
  $("prNote").textContent = "有未保存的修改";
  $("prNote").className = "dirty-note warn";
}

async function prSave() {
  const btn = $("prSave");
  btn.disabled = true;
  $("prNote").textContent = "保存中…";
  $("prNote").className = "dirty-note";
  try {
    let warns = 0;
    for (const r of prState.roles) {
      const res = await jsonSend("PUT", "/api/prompts/" + r.role, {
        provider: prState.provider,
        scope: prState.scope,
        fields: prState.drafts[r.role] || {},
      });
      if (!res || res.ok === false) {
        const first = (res && res.errors && res.errors[0]) || null;
        $("prNote").textContent = "保存被拒绝";
        $("prNote").className = "dirty-note warn";
        alert(first ? first.text : "体检未通过，没有写入");
        return;
      }
      warns += (res.warnings || []).length;
    }
    toast(warns ? "已保存，有 " + warns + " 条提醒" : "已保存");
    await prLoad();
  } catch (err) {
    toast(err.message, "err");
    $("prNote").textContent = "";
  } finally {
    btn.disabled = false;
  }
}

async function prReset() {
  const where = prState.scope
    ? "板块「" + prState.scopeLabel + "」这一层"
    : prState.provider ? "「" + prState.provider + "」这一层" : "角色默认这一层";
  if (!confirm("删掉" + where + "的覆盖，回到内置默认？内置文件不会被改动。")) return;
  try {
    for (const r of prState.roles) {
      // 文案角色没有"后端/板块"这一层。在某个后端或板块的标签页上点恢复默认，
      // 不该顺手把全站文案一起清掉。
      if ((prState.provider || prState.scope) && r.per_provider === false) continue;
      await jsonSend("POST", "/api/prompts/" + r.role + "/reset",
        { provider: prState.provider, scope: prState.scope });
    }
    toast("已恢复默认");
    await prLoad();
  } catch (err) {
    toast(err.message, "err");
  }
}

async function prPreview() {
  const role = (prState.roles[0] || {}).role;
  if (!role) return;
  try {
    const res = await jsonSend("POST", "/api/prompts/" + role + "/preview", {
      provider: prState.provider,
      fields: prState.drafts[role] || {},
    });
    prState.panel = "preview";
    prState.panelData = res;
    prRender();
    $("prBody").scrollTop = 0;
  } catch (err) {
    toast(err.message, "err");
  }
}

async function prHistory() {
  if (prState.panel === "history") {
    prState.panel = "";
    prState.panelData = null;
    prRender();
    return;
  }
  const role = (prState.roles[0] || {}).role;
  if (!role) return;
  try {
    prState.panelData = await api("/api/prompts/" + role + "/history");
    prState.panel = "history";
    prRender();
    $("prBody").scrollTop = 0;
  } catch (err) {
    toast(err.message, "err");
  }
}

async function prRollback(name) {
  const role = (prState.roles[0] || {}).role;
  if (!role) return;
  if (!confirm("整体回滚到 " + name + "？当前这层的设置会被它替换掉。")) return;
  try {
    await jsonSend("POST", "/api/prompts/" + role + "/rollback", { name });
    toast("已回滚");
    prState.panel = "";
    prState.panelData = null;
    await prLoad();
  } catch (err) {
    toast(err.message, "err");
  }
}

/* 卡片上的模型下拉。候选 = 我配的清单 + 服务上探测到的 + 当前值，三个来源缺一不可：
   只看清单时，刚探测出来还没加进清单的模型就选不到（"我明明看见有，就是点不着"）；
   只看探测时，服务没起、探测缓存被清掉后连当前值都会丢。
   `models` 是配置里的实体（带 path/port），`probed` 是服务回报的裸 id。 */
function modelPick(p) {
  const ids = [...new Set([
    ...((p.models || []).map((m) => m.id)),
    ...((p.probed || [])),
    p.model,
  ].filter(Boolean))].sort((a, b) => a.localeCompare(b));
  const stray = Boolean(p.model) && !ids.includes(p.model);
  const opts = stray ? [p.model, ...ids] : ids;
  const value = p.model || "";
  const more = ids.length > 1 ? ` · ${ids.length} 个` : "";
  return `<label class="pick" title="这个端点默认用哪个模型。切换只改它的默认值，不影响别的端点">
      <span>模型${more}</span>
      <select data-mpick="${esc(p.name)}">
        ${opts.map((id) => `<option value="${esc(id)}"${id === value ? " selected" : ""}>${esc(id)}</option>`).join("")}
        <option value="__custom__">＋ 手动输入…</option>
      </select>
    </label>`;
}

/* 本地模型：扫描出来的东西，自己挑，挑中的摆成一张清单。
 *
 * 这一节原来跟着外部模型学：端点是可以新建/编辑/删除的实体，里面有地址、密钥、
 * 提示词、模型清单。可本机跑 llama.cpp 的地址从来只有 `http://127.0.0.1:端口`，
 * 端口是启动时自己挑的，密钥不存在，提示词在站点页配——那套端点管理在这里
 * 一条都用不上，摆着只会让人以为"本地也得先建一个端点"。
 *
 * 所以在本地这一节里「端点」这个概念直接消失，只剩三件事：
 *   扫描（磁盘上有什么）→ 挑（加进清单 / 移出）→ 启动 + 测联通。
 * 清单不分组：一个端点一条端口，端口本来就写在行里，分组只会多一层要读的框。
 *
 * 「当前用哪个模型」不再是一颗按钮。本机同一时刻只有一个进程在吃显存，起了
 * 哪个就是用哪个——后端在 start 时会把端点 model 改成它。再挂一颗「设为当前」
 * 只会让人以为"配置里选一个"和"实际在跑的那个"可以是两回事，而它们不是。
 */
function localTable(list) {
  const rows = [];
  list.forEach((p) => (p.models || [])
    .filter((m) => m.local)
    .forEach((m) => rows.push({ p, m })));
  return `<div class="lm-flat">${
    rows.map(({ p, m }) => localItem(p, m)).join("")}</div>`;
}

/* 一个模型文件一行。
 *
 * 第二行那串 `:8097 · 4096 ctx · 4466 MB` 是这一节真正的信息量所在：本地模型
 * 只有三件事会出事——端口被占、上下文开太大、文件装不进显存。三样并排写出来，
 * 出问题的时候用户第一眼看的就是这里，而不是去翻编辑弹窗。
 */
function localItem(p, m) {
  const st = m.state || "unknown";
  const running = st === "running";
  const starting = st === "starting";
  const stalled = st === "stalled";
  const waited = Number(m.waited_s || 0);
  const stateText = running ? "运行中"
    : starting ? (waited ? `启动 ${waited}s` : "启动中")
    : stalled ? `卡住 ${Math.max(1, Math.floor(waited / 60))}m`
    : "未启动";
  const dotCls = running ? "ok" : starting ? "warn" : stalled ? "err" : "off";
  const act = (running || starting || stalled)
    ? `<button class="mini" data-lmact="stop" data-name="${esc(p.name)}" data-model="${esc(m.id)}">${running ? "停止" : starting ? "取消" : "停止"}</button>`
    : `<button class="mini" data-lmact="start" data-name="${esc(p.name)}" data-model="${esc(m.id)}">启动</button>`;
  const meta = [
    m.port ? `:${m.port}` : "端口自动",
    m.context ? `${m.context} ctx` : "",
    m.size_mb ? `${m.size_mb} MB` : "",
  ].filter(Boolean).join(" · ");
  // 实例实际跑在别的端口（配置端口被占时自动让位）：多一行直连，不然同一串
  // 地址在头上和行里出现两遍；没起时不必补——地址本来就不随进程变化。
  const direct = m.running_url && m.running_url !== (p.base_url || "") ? m.running_url : "";
  return `<div class="lm-item${running ? " is-running" : ""}">
    <i class="dot ${dotCls}"></i>
    <div class="lm-item-main">
      <div class="lm-item-name">
        <b class="ell" title="${esc(m.path || m.label)}">${esc(m.label)}</b>
      </div>
      <div class="lm-item-meta">${esc(meta)}</div>
      ${direct ? `<div class="lm-item-meta">实跑在 <code>${esc(direct)}</code></div>` : ""}
    </div>
    <span class="lm-item-state ${dotCls}">${esc(stateText)}</span>
    ${act}
  </div>`;
}

/* 卡片底部那句「通不通」。
 *
 * 实例状态和端点探测是两条独立信息，会打架：刚点启动，实例是「启动中」，
 * 而探测缓存里还躺着上一轮的「连接超时」。两行一起摆出来，用户根本没法判断
 * 到底起没起。规则只有一条——**实例正在启动时，探测结论让位**，因为那条结论
 * 描述的时刻已经过去了。
 */
function statusNote(p) {
  const ls = (p.models || []).filter((m) => m.local);
  const starting = ls.find((m) => m.state === "starting");
  const stalled = ls.find((m) => m.state === "stalled");
  const running = ls.find((m) => m.state === "running");
  if (starting || stalled) {
    const m = starting || stalled;
    const waited = Number(m.waited_s || 0);
    // 不重复模型名和秒数：下拉里写着是哪个模型，左边那行写着等了多少秒。
    // 这里只补一件行里放不下的事——接下来它会自己好，还是得人来处理。
    return `<div class="msg svc-note"><i class="dot ${starting ? "warn" : "err"}"></i>
      <span class="txt">${starting
        ? `正在读权重，端口 :${m.port} 通了会自动可用`
        : `进程卡在加载：端口 :${m.port} 已经 ${Math.max(1, Math.round(waited / 60))} 分钟没通。点「停止」再启动，还不行就看日志`}</span></div>`;
  }
  if (running && p.status !== "ok" && p.status !== "mock") {
    // 实例在跑、端点却探测失败：多半是刚起来，探测缓存还是启动前那次。
    // 行里已经写着「运行中 :8097」，这句不再重复端口和"实例在跑"——只用
    // 一句话说清这条结论是从哪来的、下一步点哪儿。
    const why = (!p.status || p.status === "unknown")
      ? "启动后还没探测，点 ⟳ 测一次"
      : `上次探测没通：${statusShort(p)}。多半是启动前的旧结论，点 ⟳ 重测`;
    return `<div class="msg svc-note"><i class="dot warn"></i>
      <span class="txt">${esc(why)}</span></div>`;
  }
  // 实例根本没起，探测却报"连接超时"——端口当然没响应，因为没人监听。
  // 这句话会把用户引向防火墙、网络配置，唯独不指向"你还没点启动"。
  const idle = ls.length > 0 && ls.every((m) => m.state !== "running"
    && m.state !== "starting" && m.state !== "stalled");
  if (idle && p.status !== "ok" && p.status !== "mock") {
    const ever = ls.some((m) => m.state === "stopped");
    return `<div class="msg svc-note"><i class="dot"></i>
      <span class="txt">${ever ? "实例已退出" : "实例还没启动"}，点「启动」后自动探测</span></div>`;
  }
  return `<div class="msg svc-note"><i class="dot ${dotOf(p.status)}"></i>
    <span class="txt">${esc(statusShort(p))}</span>
    ${p.checked_at ? `<span class="ago">${agoText(p.checked_at)}</span>` : ""}</div>`;
}

function statusShort(p) {
  if (p.status === "mock") return "不联网";
  if (!p.status || p.status === "unknown") return "还没测";
  if (p.status === "ok") {
    // 只说通不通 + 耗时。以前这里还跟一句"N 个模型可用（另有 M 个已隐藏）"，
    // 每测一次就被灌一遍和"能不能用"无关的统计。
    return `连通${p.latency_ms ? ` · ${p.latency_ms}ms` : ""}`;
  }
  if (p.status === "unset") return "还没填地址";
  if (p.status === "no_key") return "缺密钥";
  return (p.message || "未连通").replace(/^服务在跑，但\s*/, "");
}

/* 后端卡片：只管"这个后端能不能用、用哪个模型"。
 *
 * 以前每张卡底部挂一个「设为当前使用」，顶部还有一条全局「当前使用」。
 * 那套动作已经删掉（2026-11-04）：实际用哪个后端由调用方决定——对话页的
 * 「这一问用谁」、站点卡的角色分工、预问诊自己的 provider 配置——页面不再
 * 维护一份"全局当前"。所以卡片上不该出现任何"当前"的痕迹：既不是按钮，
 * 也不是徽标。 */
function modelCard(p, total, d) {
  return `
    <article class="svc-card" data-card="${esc(p.name)}">
      <header>
        <i class="dot ${dotOf(p.status)}"></i>
        <b class="ell">${esc(p.name)}</b>
        <span class="sp"></span>
        <button class="icon-btn" data-mact="probe" data-name="${esc(p.name)}"
          title="测试联通：先拉 /models，再真发一次一句话，能出字才算通">⟳</button>
        <button class="icon-btn" data-mact="prompt" data-name="${esc(p.name)}"
          title="给这个后端单独配提示词（小模型要拆成条款，云端强模型一句话就够）">Aa</button>
        <button class="icon-btn" data-mact="edit" data-name="${esc(p.name)}" title="编辑地址 / 密钥 / 模型清单">✎</button>
        ${total > 1
          ? `<button class="icon-btn danger" data-mact="del" data-name="${esc(p.name)}" title="删除这个后端">✕</button>`
          : `<button class="icon-btn" disabled title="至少要留一个后端：先新增一个，再删这个">✕</button>`}
      </header>
      <div class="url" title="${esc(p.base_url || "")}">${esc(p.base_url || "（无地址）")}</div>
      ${p.local || !p.key_present ? "" : `<div class="url">密钥 ${
        d && d.keys_persisted ? "已存本机（密文）" : "仅本次会话"}</div>`}
      ${modelPick(p)}
      ${p.used_by && p.used_by.length
        ? `<div class="model-usage">使用中：${esc(p.used_by.join(" · "))}</div>` : ""}
      ${statusNote(p)}
      ${p.ready ? "" : `<div class="miss">${esc(p.ready_reason || "字段不全")}</div>`}
    </article>`;
}

/* 一组端点。分开传 list 而不是从 d.providers 里自己筛：本地和外部现在各占
   一节，谁调用谁决定放哪几个——筛的条件只写在一处，卡片本身不必知道自己是
   "本地"还是"外部"。 */
function modelCards(list, total, d) {
  return list.map((p) => modelCard(p, total, d)).join("");
}

/* 固定 API 入口。本地实例一个模型一个端口，让外部程序去记 8090/8091 是不现实
   的——这块卡片给的是主服务自己的 /v1，端口不随实例变化。直连地址也一并列
   出来：绕过转发少一跳，但只在实例活着时有效。

   2026-11-13 更新：示例不再是「当前活动端点」一份。有 4 个端点时，用户想调
   local 就只能自己猜 model 该写什么。现在每个端点一颗 chip，点谁就把「默认
   模型 + curl」换成谁的；直连列表也改成按配置列全（含未启动的），没起的标
   「未启动」而不是整条消失——地址是固定的，消失只会让人以为这个模型没配。 */
let gatewayLan = false;    // 「启动时允许局域网调用」；renderModels 重绘不丢
let gatewayPick = "";      // 接入卡上选中的端点；空 = 跟随后端给的活动端点

function gwCurl(chatUrl, model) {
  if (!model) return "";
  // 手拼而不是 JSON.stringify 整个对象：示例是给人读的，紧凑序列化会把
  // 空格全吃掉（{"model":"x","messages":[…]}），抄进终端很难一眼核对。
  const body = `{"model": ${JSON.stringify(model)}, `
    + `"messages": [{"role": "user", "content": "你好"}]}`;
  return `curl ${chatUrl} -H "Content-Type: application/json" -d '${body}'`;
}

function gwPickOf(g) {
  const list = (g && g.providers) || [];
  return list.find((x) => x.name === gatewayPick)
    || list.find((x) => x.name === g.provider)
    || list[0] || null;
}

function gatewayCard(g) {
  if (!g) return "";
  const provs = g.providers || [];
  const pick = gwPickOf(g);
  const direct = g.direct || [];
  const line = (label, value) => `<div class="gw-line">
      <span class="hint">${esc(label)}</span>
      <code>${esc(value)}</code>
      <button class="mini" data-copy="${esc(value)}">复制</button>
    </div>`;
  const chips = provs.length > 1 ? `<div class="gw-chips" id="gwChips">
      ${provs.map((x) => `<button type="button"
        class="gw-chip${pick && x.name === pick.name ? " active" : ""}"
        data-gwpick="${esc(x.name)}"
        title="${x.local ? "本地端点" : "云端端点"} · 默认模型 ${esc(x.model || "未选")}"
        >${esc(x.name)}</button>`).join("")}
    </div>` : "";
  const model = pick ? pick.model : g.model;
  const curl = gwCurl(g.chat_url, model);
  return `<div class="card gateway-card">
    <div class="kb-actions">
      <b>API 接入</b>
    </div>
    ${line("Base URL", g.base_url)}
    ${line("对话", g.chat_url)}
    ${line("模型列表", g.models_url)}
    ${chips}
    ${model ? `<p class="hint gw-model" id="gwModelLine">
      <b>${esc(pick ? pick.name : (g.provider || ""))}</b> 的默认模型
      <code>${esc(model)}</code></p>` : ""}
    ${curl ? `<pre class="launch" id="gwCurlText">${esc(curl)}</pre>
      <button class="mini u-mt6" id="gwCurlCopy" data-copy="${esc(curl)}"
        >复制 curl 示例</button>` : ""}
    ${direct.length ? `<p class="hint u-mt10">实例直连</p>
      ${direct.map((a) => `<div class="gw-line${a.running ? "" : " is-off"}">
        <span class="hint">${esc(a.endpoint || "")} / ${esc(a.model || "model")}${
          a.running ? "" : `<em class="gw-off">未启动</em>`}</span>
        <code>${esc(a.base_url)}</code>
        <button class="mini" data-copy="${esc(a.base_url)}">复制</button>
        ${a.lan_base_url ? `<code>${esc(a.lan_base_url)}</code>
          <button class="mini" data-copy="${esc(a.lan_base_url)}">复制局域网</button>` : ""}
      </div>`).join("")}` : ""}
    <label class="pick" title="允许局域网访问">
      <input type="checkbox" id="gwLan"${gatewayLan ? " checked" : ""}>
      允许局域网访问
    </label>
    <p class="hint u-mt8">${esc(g.hint || "")}</p>
  </div>`;
}

/* 点 chip 只换「模型名 + curl」两处，不重绘整张卡：重绘会打断用户刚选的
 * 复制按钮焦点，也会让复选框闪一下。 */
function bindGatewayChips(g) {
  const box = $("gwChips");
  if (!box) return;
  box.querySelectorAll("[data-gwpick]").forEach((btn) => {
    btn.onclick = () => {
      gatewayPick = btn.dataset.gwpick;
      const pick = gwPickOf(g);
      if (!pick) return;
      box.querySelectorAll("[data-gwpick]").forEach((b) => {
        b.classList.toggle("active", b.dataset.gwpick === pick.name);
      });
      const modelLine = $("gwModelLine");
      if (modelLine) {
        modelLine.innerHTML = `<b>${esc(pick.name)}</b> 的默认模型
          <code>${esc(pick.model)}</code>`;
      }
      const curl = gwCurl(g.chat_url, pick.model);
      const pre = $("gwCurlText");
      if (pre) pre.textContent = curl;
      // 复制按钮读的是 dataset，改它就够了，不必重绑处理器。
      const copy = $("gwCurlCopy");
      if (copy) copy.dataset.copy = curl;
    };
  });
}

/* 密钥记没记住，页面上必须有一句话。
 *
 * 这件事原先只写在编辑弹窗的一个勾选框里：取消一次就落下 `data/secrets.off`
 * 标记，此后**每次重启都要重填密钥**，而页面上一处都不提。用户的体感只能是
 * "我的 key 又被删了"——一条只在弹窗里出现过的选择，不该有这种长期代价。
 *
 * 现在两种状态各给一句话：没记住 → 提醒 + 一颗「记住到本机」；记住了 → 说明
 * 存在哪、怎么保护的。刻意不给"改为不保存"的按钮：要关这个就去编辑弹窗里关，
 * 页面上不摆一颗一比就能把钥匙删掉的按钮。
 */
function keyNoteHtml(d) {
  const has = (d.providers || []).some((p) => p.key_present);
  if (d.key_autosave === false) {
    return `<div class="key-note warn">
      <i class="dot warn"></i>
      <span class="txt">密钥只存在本次会话，服务一重启就得重填。</span>
      <span class="sp"></span>
      <button class="mini" id="keysRemember">记住到本机</button>
    </div>`;
  }
  if (!d.keys_persisted || !has) return "";
  const n = (d.providers || []).filter((p) => p.key_present).length;
  return `<div class="key-note">
    <i class="dot ok"></i>
    <span class="txt">${n} 个密钥已记住：${esc(d.key_cipher || "系统加密")}，存
      <code>data/secrets.json</code>，重启自动恢复</span>
  </div>`;
}

async function renderModels() {
  $("pageTitle").textContent = "模型服务";
  // 操作按钮不放顶栏：顶栏固定 54px 且不换行，两颗按钮加长文件名挤一行会被裁掉
  // 半截（知识库页已经踩过一次）。挪进内容区，跟说明放一起。
  $("pageActions").innerHTML = "";
  const box = $("viewModels");
  if (!state.models) box.innerHTML = `<div class="page"><p class="empty-tip">加载中…</p></div>`;

  let d;
  try {
    d = await api("/api/models");
    state.models = d;
  } catch (err) {
    box.innerHTML = `<div class="page"><p class="empty-tip">${esc(err.message)}</p></div>`;
    return;
  }
  // 副标题不再报"当前用的是谁"：页面不维护全局当前，报出来反而像在暗示
  // 这里能改。只报规模，且按两类分开报——一页里两节，副标题说总数等于没说。
  const localList = d.providers.filter((p) => p.local);
  const extList = d.providers.filter((p) => !p.local);
  // 本地这一节按"模型文件"计数，不按端点：端点在这里只是端口的容器，数它没有意义
  const localModels = localList.flatMap((p) => (p.models || []).filter((m) => m.local));
  const runningN = localModels.filter((m) => m.state === "running").length;
  $("pageSub").textContent = `${localModels.length} 个本地模型 · ${extList.length} 个外部`;
  // 密钥现在默认落盘了（密文），所以这个按钮的职责是"内存 + 盘上一起抹掉"，
  // 而不是像以前那样只能靠重启进程把内存清空。
  $("pageActions").innerHTML = d.providers.some((p) => p.key_present)
    ? `<button class="ghost-btn" id="keysClear" title="清空内存和 data/secrets.json 里的所有密钥">清空密钥</button>`
    : "";
  if ($("keysClear")) {
    $("keysClear").onclick = async () => {
      const r = await jsonPost("/api/models/keys/clear", {}).catch((e) => ({ error: e.message }));
      if (r.error) { toast(r.error, "err"); return; }
      toast(`已清空 ${(r.cleared || []).length} 个密钥`, "warn");
      state.models = null;
      await renderModels();
    };
  }
  syncProviderOptions(d);

  box.innerHTML = `<div class="page">
    ${keyNoteHtml(d)}
    <section class="svc-sec">
      <header class="sec-head">
        <h3>本地模型</h3>
        <span class="hint">${localModels.length
          ? `${localModels.length} 个模型文件${runningN ? ` · ${runningN} 个在跑` : ""} · 都在本机`
          : "扫描磁盘上的 .gguf，选进来的才能启动"}</span>
        <span class="sp"></span>
        ${localModels.length ? `<button class="ghost-btn" id="modelsProbeLocal"
          title="真连一次：拉 /models，再让当前模型回一句话">测试联通</button>` : ""}
      </header>
      <div class="svc-local-layout${scanOpen ? "" : " is-collapsed"}">
        <div class="svc-local-main">
          ${localModels.length
            ? localTable(localList)
            : `<p class="empty-tip">清单里还没有模型文件。扫描右边的 .gguf，点「＋ 加入」。</p>`}
        </div>
        ${scanPanelHtml(d)}
      </div>
    </section>
    <section class="svc-sec">
      <header class="sec-head">
        <h3>外部模型</h3>
        <span class="hint">${extList.length
          ? `${extList.length} 个端点 · 云服务或别的机器上的 OpenAI 兼容地址`
          : "云服务或别的机器上的 OpenAI 兼容地址"}</span>
        <span class="sp"></span>
        ${extList.length ? `<button class="ghost-btn" id="modelsProbe">全部测试</button>` : ""}
        <button class="primary-btn" id="modelsAdd">＋ 新增外部端点</button>
      </header>
      ${extList.length
        ? `<div class="svc-grid" id="modelGrid">${modelCards(extList, d.providers.length, d)}</div>`
        : `<p class="empty-tip">还没有外部端点。点「＋ 新增外部端点」，填上地址和密钥。</p>`}
    </section>
  </div>`;

  // 按钮跟着各自的节走：在"本地"这一节点新增，编辑器就默认本地类型，
  // 免得存完还要在类型下拉里改一次、也在意料之外落到另一节里去。
  $("modelsAdd").onclick = () => openModelEditor(null, "openai");
  if ($("modelsProbe")) {
    $("modelsProbe").onclick = () => probeAllModels($("modelsProbe"), extList);
  }
  if ($("modelsProbeLocal")) {
    $("modelsProbeLocal").onclick = () => probeAllModels($("modelsProbeLocal"), localList);
  }
  // 「记住到本机」：把"填了就写盘"打开，并把内存里现有的密钥立刻写下去——
  // 不开这一步，用户以为按了就已经记好，重启发现还是空的。
  if ($("keysRemember")) {
    $("keysRemember").onclick = async (ev) => {
      const btn = ev.currentTarget;
      btn.disabled = true;
      btn.textContent = "保存中…";
      try {
        const r = await jsonPost("/api/models/keys/autosave", { on: true });
        if (r.error) toast(r.error, "err");
        else toast((d.providers || []).some((p) => p.key_present)
          ? "已记住到本机，重启不用重填"
          : "已改为记住：下次填的密钥会存到本机");
        await renderModels();
      } catch (err) {
        toast(err.message, "err");
        btn.disabled = false;
        btn.textContent = "记住到本机";
      }
    };
  }
  if ($("gwLan")) $("gwLan").onchange = (e) => { gatewayLan = Boolean(e.target.checked); };
  bindGatewayChips(state.gateway);
  bindScanPanel();
  bindModelCardActions();

  box.querySelectorAll("[data-copy]").forEach((btn) => {
    btn.onclick = async () => {
      try {
        await navigator.clipboard.writeText(btn.dataset.copy);
        toast("地址已复制");
      } catch (_) {
        alert(btn.dataset.copy);
      }
    };
  });
  pollLocalStates(d);
}

/* 启动中必须自己刷。
 *
 * 不刷的话，「启动中 12s」就永远停在 12 秒——用户只能手动刷新页面才知道到底
 * 起没起来，而那正是他最不确定的时刻。打扰要压到最小：5 秒一次、最多 3 分钟
 * （和 start() 的等待上限对齐）、页面在后台不刷、有输入框聚焦时不重绘（重绘会
 * 把正在填的扫描目录冲掉）。状态一变（running / stopped），busy 为假，自然停。
 */
let localPollTimer = null;
let localPollLeft = 0;

function pollLocalStates(d) {
  const busy = (d.providers || []).some((p) => (p.models || []).some(
    (m) => m.local && m.state === "starting"));
  if (localPollTimer) { clearTimeout(localPollTimer); localPollTimer = null; }
  if (!busy) { localPollLeft = 0; return; }
  if (!localPollLeft) localPollLeft = 36;
  if (localPollLeft <= 0) return;
  localPollTimer = setTimeout(() => {
    localPollTimer = null;
    localPollLeft -= 1;
    if (localPollLeft <= 0) return;
    const el = document.activeElement;
    const typing = el && /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName);
    if (document.hidden || typing) {
      pollLocalStates(d);          // 这一拍让出去，别打断输入
      return;
    }
    reloadModels().catch(() => { localPollLeft = 0; });
  }, 5000);
}

async function reloadModels() {
  state.models = null;
  await loadProviders();
  if (state.view === "models") await renderModels();
  syncGenModelOptions();
}

/* 测一节里的端点。分节之后「全部测试」必须只测这一节：按钮摆在哪一节，
   测的就该是哪一节——局部按钮却把全局都跑一遍，结果里一半条目根本不在
   视野里，报数也对不上（"4 个连通"而眼前只有 1 张卡）。 */
async function probeAllModels(btn, list) {
  const names = (list || []).map((p) => p.name).filter(Boolean);
  // 按钮上的字两节不一样（本地是「测试联通」、外部是「全部测试」），复位时
  // 不能写死一个，否则本地那颗测完就变名字了。
  const label = btn ? btn.textContent : "";
  if (btn) { btn.disabled = true; btn.textContent = "测试中…"; }
  let got = [];
  try {
    if (!names.length) {
      const r = await api("/api/services/probe", { method: "POST" });
      got = Object.values(r.providers || {});
    } else {
      for (const name of names) {
        const r = await api(
          `/api/services/probe?name=${encodeURIComponent(name)}`, { method: "POST" });
        got = got.concat(Object.values(r.providers || {}));
      }
    }
    await reloadModels();
    const ok = got.filter((p) => p.ok).length;
    // 一句话就好：几个通、几个不通。
    toast(`${ok}/${got.length} 连通`, ok === got.length ? "ok" : "warn");
  } catch (err) {
    alert(err.message);
  } finally {
    // 重绘之后这颗按钮已经不在文档里了，别再往老节点上写状态。
    if (btn && document.body.contains(btn)) {
      btn.disabled = false;
      btn.textContent = label || "测试";
    }
  }
}

function bindModelCardActions() {
  const box = $("viewModels");
  box.querySelectorAll("[data-mact]").forEach((btn) => {
    const name = btn.dataset.name;
    const act = btn.dataset.mact;
    btn.onclick = () => {
      if (act === "probe") return probeOneModel(name);
      if (act === "prompt") return openPromptDrawer(name);
      if (act === "edit") return openModelEditor(name);
      if (act === "del") return removeModel(name);
    };
  });
  // 卡片上的"切换模型"下拉：探测列出来的 id 直接能选，不用进编辑弹窗。
  // 只改 model 一个字段，其余照抄现有值——否则切个模型会把 base_url 一起改掉。
  box.querySelectorAll("[data-lmact]").forEach((btn) => {
    btn.onclick = () => localModelAction(btn);
  });
  box.querySelectorAll("[data-mpick]").forEach((sel) => {
    sel.onchange = async () => {
      const p = state.models.providers.find((x) => x.name === sel.dataset.mpick);
      if (!p) return;
      let want = sel.value;
      if (want === "__custom__") {
        want = (prompt(`给后端 ${p.name} 输入模型名（服务商文档里的那个 id）`, p.model || "") || "").trim();
        if (!want) { sel.value = p.model || ""; return; }
      }
      if (!want || want === p.model) return;
      sel.disabled = true;
      try {
        // 选中的模型同时写进清单：站点那边的模型下拉只列清单里的东西，
        // 不写进来就会出现"这里刚选过、切到站点却选不到"。
        const known = new Set((p.models || []).map((m) => (m && m.id) || "").filter(Boolean));
        const models = known.has(want) ? (p.models || []) : [...(p.models || []), { id: want }];
        await api(`/api/models/${encodeURIComponent(p.name)}`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            base_url: p.base_url || "", model: want, kind: p.kind,
            // 带上清单，别让它因为"没传"而被当成清空
            models,
          }),
        });
        await reloadModels();
        toast(known.has(want) ? `${p.name} 已切到 ${want}` : `${p.name} 已加入并切到 ${want}`);
      } catch (err) {
        toast(err.message, "err");
        sel.disabled = false;
        sel.value = p.model || "";
      }
    };
  });
}

/* 启动 / 停止一个本地模型。
   启动要等 llama-server 把权重读进内存（小模型几秒，7B 从机械盘能到一分钟），
   所以按钮上必须显示进度并禁掉——否则用户会连点几次，第二次报"已经在跑"。 */
async function localModelAction(btn) {
  const { name, model, lmact } = btn.dataset;
  const old = btn.textContent;
  btn.disabled = true;
  btn.textContent = lmact === "start" ? "启动中…" : "停止中…";
  try {
    const r = await api(`/api/models/${encodeURIComponent(name)}/${lmact}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ model, host: gatewayLan ? "0.0.0.0" : "127.0.0.1" }),
    });
    if (lmact === "start") {
      const addr = (r.api && r.api.base_url) || (r.instance || {}).base_url || "";
      toast(`${model} 已启动 · ${addr}${r.gateway ? ` · 固定入口 ${r.gateway}` : ""}`);
      // 实例起来了，探测缓存里却还是启动前那次「连接超时」（TTL 15s）。不立刻
      // 重探，卡片会继续显示连不上，看着像启动失败。失败也无所谓：下一次
      // 手动 ⟳ 或 15s 后的自动探测会补上。
      await api(`/api/services/probe?name=${encodeURIComponent(name)}`,
        { method: "POST" }).catch(() => {});
    } else {
      toast(r.message || "已停止", r.stopped ? "ok" : "warn");
    }
    await reloadModels();
  } catch (err) {
    toast(err.message, "err");
    btn.disabled = false;
    btn.textContent = old;
  }
}

/* 扫描目录由用户自己添加，写在本机浏览器里——它是「这台机器上模型放哪」，
   属于使用者，不属于项目配置。clone 到别人机器上默认空，各自填各自的。 */
const SCAN_DIRS_KEY = "knowtune.scanDirs";
let scanDirs = (() => {
  try {
    const v = JSON.parse(localStorage.getItem(SCAN_DIRS_KEY) || "[]");
    return Array.isArray(v) ? v.filter((x) => typeof x === "string" && x.trim()) : [];
  } catch (_) { return []; }
})();
let lastScan = null;   // 最近一次结果：增删目录后还能留在面板里回看

/* 扫描区收没收起，记在这台机器的浏览器里。
 * 扫描是「往清单里添东西」时才做的事，平时看的是清单本身——所以允许收起。
 * 默认展开：第一次来的人得先看见"这台机器上有哪些 gguf"这扇门。 */
const SCAN_OPEN_KEY = "knowtune.scanOpen";
let scanOpen = (() => {
  try { return localStorage.getItem(SCAN_OPEN_KEY) !== "0"; } catch (_) { return true; }
})();

function saveScanOpen() {
  try { localStorage.setItem(SCAN_OPEN_KEY, scanOpen ? "1" : "0"); } catch (_) {}
}

/* 收起/展开只搬两个 class，不重画整页——重画会重新拉一次 /api/models，
 * 而这项操作跟后端一点关系都没有，闪一下纯属白花功夫。
 * 收起的箭头在面板里，主区的宽度却由外层栅格决定，所以两处都得改。 */
function applyScanOpen() {
  const panel = $("scanPanel");
  if (!panel) return;
  panel.classList.toggle("is-collapsed", !scanOpen);
  const layout = panel.closest(".svc-local-layout");
  if (layout) layout.classList.toggle("is-collapsed", !scanOpen);
  const t = $("scanToggle");
  if (t) {
    t.textContent = scanOpen ? "▾" : "▸";
    t.setAttribute("aria-expanded", scanOpen ? "true" : "false");
    t.title = scanOpen ? "收起模型文件区" : "展开模型文件区";
  }
}

/* 扫描本地 .gguf。扫出来的东西不进配置——「我看到有哪些」和「我要用哪些」
   是两件事，自动全加进去等于替用户做决定，还会把 30 个量化版本塞进下拉。 */

/* 输入框草稿存在内存里。整页会因为启动轮询重绘，光标不在输入框时（刚点完
   添加、切走一下再回来）重绘会把填了一半的路径清掉。 */
let scanDirDraft = "";

function saveScanDirs() {
  try { localStorage.setItem(SCAN_DIRS_KEY, JSON.stringify(scanDirs)); } catch (_) {}
}

/* 扫描区常驻在页面上，不藏在一颗按钮后面。
 *
 * 原来的形态是「点『扫描本地模型』→ 下面长出一块面板」：扫完想再扫一次还得
 * 再点一遍，面板一关就不知道这台机器上到底有哪些 gguf。要看的和要改的本来
 * 就是同一件事，摆在一起才不用来回点——目录、扫描、结果、加入，一条线走完。
 *
 * llama-server 的位置和 GPU 也从底部折叠块搬进来了：它决定"扫出来的能不能
 * 起"，和扫描结果是一回事；藏起来只会让"点了启动没反应"变成谜。
 */
function scanPanelHtml(d) {
  const dd = d || state.models || {};
  const env = dd.env || {};
  const dirRows = scanDirs.length
    ? scanDirs.map((dir, i) => `
        <span class="chip scan-dir" title="${esc(dir)}">${esc(dir)}
          <button class="chip-x" data-rmdir="${i}" title="移除目录">×</button>
        </span>`).join("")
    : `<span class="scan-empty">还没添加目录，点「浏览…」选一个放 .gguf 的文件夹</span>`;
  const dev = (env.devices || [])[0];
  // 显卡名原样是 "NVIDIA GeForce RTX 3060 Laptop GPU"：这串里能帮人判断"够不够"
  // 的只有 "RTX 3060" 和显存。NVIDIA / GeForce 前缀与 GPU 尾巴每块卡都带着，
  // 留着只是把这行推到第二行，让 "6G" 孤零零掉下去。
  const devText = dev
    ? `${dev.name.replace(/\(.*?\)/g, "")
      .replace(/^NVIDIA\s+/i, "")
      .replace(/^GeForce\s+/i, "")
      .replace(/\s+GPU$/i, "").trim()} · ${Math.round(dev.total_mb / 1024)}G`
    : (env.available ? "只有 CPU，出词会慢" : "");
  const envText = env.available
    ? `llama-server 就绪${devText ? ` · ${devText}` : ""}`
    : "没找到 llama-server：能扫，但起不了";
  // 标题行只留「这是什么 + 环境结论 + 唯一的动作」；添加目录那一整套控件
  // 挪到下一行单独成排——挤在一行时「开始扫描」会被顶到换行、孤零零落在
  // 左边，看着像第二个标题。环境结论单行省略，鼠标悬停看全文，这样窄窗口
  // 下按钮也不会被文字推走。
  //
  // 最左那颗箭头是收起键：扫描是"要添东西"时才做的事，平时清单才是主角。
  // 收起后整块塌成一行（名字 + 环境结论），主区顺势占满整行。
  return `<section class="card scan-panel${scanOpen ? "" : " is-collapsed"}" id="scanPanel">
    <div class="scan-head">
      <button class="scan-toggle" id="scanToggle" aria-expanded="${scanOpen ? "true" : "false"}"
        title="${scanOpen ? "收起模型文件区" : "展开模型文件区"}">${scanOpen ? "▾" : "▸"}</button>
      <b>模型文件</b>
      <i class="dot ${env.available ? "ok" : "warn"}"></i>
      <span class="hint ell" title="${esc(envText)}">${esc(envText)}</span>
      <button class="primary-btn" id="scanDo" ${scanDirs.length ? "" : "disabled"}>开始扫描</button>
    </div>
    <div class="scan-body">
      <div class="scan-add">
        <button class="ghost-btn" id="scanDirPick" title="打开系统的「选择文件夹」窗口">浏览…</button>
        <input id="scanDirInput" class="scan-dir-input" placeholder="或粘贴路径，多个用逗号隔开"
          title="一次可以粘多个，用逗号或换行隔开" value="${esc(scanDirDraft)}">
        <button class="ghost-btn" id="scanDirAdd" ${scanDirDraft.trim() ? "" : "disabled"}>添加</button>
      </div>
      ${env.available ? "" : `<p class="hint warn-note u-mt8">把 llama-server 放进 <code>tools/llama/</code>，或用环境变量 <code>KNOWTUNE_LLAMA_SERVER</code> 指向它。</p>`}
      <div class="chip-row">${dirRows}</div>
      ${lastScan ? scanResultHtml(lastScan)
        : (scanDirs.length
          ? `<p class="hint u-mt8">扫描只读：扫出来的 .gguf 不会自动进清单，点「＋ 加入」才加。</p>`
          : "")}
    </div>
  </section>`;
}

function scanResultHtml(r) {
  const rows = r.models || [];
  if (!rows.length) {
    const ds = (r.dirs || []).map((x) => `<code>${esc(x)}</code>`).join(" · ");
    return `<p class="hint u-mt8">这些目录里没有 .gguf${ds ? `：${ds}` : ""}</p>`;
  }
  // 已经在清单里的直接标出来。不标的话，用户会对着同一个模型反复点「加入」，
  // 每次都弹一句"已经在清单里了"——等于界面承认自己没记住状态。
  const known = new Set(((state.models && state.models.providers) || [])
    .filter((p) => p.kind === "local")
    .flatMap((p) => (p.models || []).map((m) => m.id)));
  const list = rows.map((m) => {
    const has = known.has(m.name);
    return `
    <div class="lm-row">
      <i class="dot ${has ? "ok" : "off"}"></i>
      <b class="ell" title="${esc(m.path)}">${esc(m.name)}</b>
      <span class="lm-meta">${m.size_mb ? `${m.size_mb} MB` : ""}${m.quant ? ` · ${esc(m.quant)}` : ""}</span>
      <span class="sp"></span>
      ${has
        ? `<button class="mini" data-rm="${esc(m.name)}" title="从清单移出，不删磁盘上的文件">移出</button>`
        : `<button class="mini" data-add='${esc(JSON.stringify({ id: m.name, path: m.path }))}'>＋ 加入</button>`}
    </div>`;
  }).join("");
  // 有几个本地端点时必须说清楚「加入」加到哪一份清单，否则用户加完了在
  // 另一个端点上找不到，只会以为扫描没生效。
  const eps = ((state.models && state.models.providers) || [])
    .filter((p) => p.local || p.kind === "local");
  const tgt = localTarget(state.models || {});
  const where = eps.length > 1 && tgt
    ? `，全部加进端点 <code>${esc(tgt.name)}</code> 的清单` : "";
  return `<p class="hint u-mt8">扫到 ${rows.length} 个 .gguf${where}</p>
    <div class="local-models">${list}</div>`;
}

function bindScanPanel() {
  const panel = $("scanPanel");
  if (!panel) return;
  const toggle = $("scanToggle");
  if (toggle) {
    toggle.onclick = () => {
      scanOpen = !scanOpen;
      saveScanOpen();
      applyScanOpen();
    };
  }
  applyScanOpen();
  const input = $("scanDirInput");
  if (!input) return;
  input.oninput = () => {
    scanDirDraft = input.value;
    // 没内容时「添加」是死按钮，灰掉比让它点出来一句"先填一个目录"更干净。
    const add = $("scanDirAdd");
    if (add) add.disabled = !input.value.trim();
  };
  $("scanDirAdd").onclick = () => addScanDirText(input.value);
  input.onkeydown = (e) => {
    if (e.key === "Enter") { e.preventDefault(); addScanDirText(input.value); }
  };
  const pickBtn = $("scanDirPick");
  if (pickBtn) pickBtn.onclick = () => pickScanDir(pickBtn);
  panel.querySelectorAll("[data-rmdir]").forEach((btn) => {
    btn.onclick = () => {
      const i = Number(btn.dataset.rmdir);
      if (Number.isInteger(i) && i >= 0 && i < scanDirs.length) {
        scanDirs.splice(i, 1);
        saveScanDirs();
        refreshScanArea();
      }
    };
  });
  $("scanDo").onclick = runScan;
  panel.querySelectorAll("[data-add]").forEach((btn) => {
    btn.onclick = async () => {
      btn.disabled = true;
      try {
        await addScanned(JSON.parse(btn.dataset.add));
        btn.textContent = "已加入";
      } catch (err) {
        toast(err.message, "err");
        btn.disabled = false;
      }
    };
  });
  // 移出：选择权的另一半。只在扫描结果里给——清单行要留干净，删不删模型是
  // "我挑过"这件事的反悔，发生在挑的那一刻最合理。
  panel.querySelectorAll("[data-rm]").forEach((btn) => {
    btn.onclick = async () => {
      btn.disabled = true;
      try {
        await removeLocalModel(btn.dataset.rm);
      } catch (err) {
        toast(err.message, "err");
        btn.disabled = false;
      }
    };
  });
}

/* 把一段文本里的目录并进列表：一次可以粘多个（逗号/分号/换行分开）。
   返回是否真的加了新目录——「浏览…」据此决定要不要顺手扫一次。 */
function addScanDirText(text) {
  const parts = String(text || "").split(/[,，;；\n]+/).map((x) => x.trim()).filter(Boolean);
  if (!parts.length) { toast("先选一个目录，或粘贴路径", "warn"); return false; }
  const fresh = parts.filter((x) => !scanDirs.includes(x));
  if (!fresh.length) { toast("目录已经在列表里了", "warn"); return false; }
  scanDirs = scanDirs.concat(fresh);
  scanDirDraft = "";
  saveScanDirs();
  refreshScanArea();        // 重绘，保留上次扫描结果
  return true;
}

/* 让系统自己弹「选择文件夹」窗口——就是资源管理器里那个。
 *
 * 以前这里让人手敲 "D:/models"：路径带空格、带中文、盘记错一个字符就扫不到，
 * 用户还得先开资源管理器复制一遍再粘回来。而 Windows 的规矩从来是点「浏览…」
 * 弹选择框，界面照抄这条规矩就行，不自己另造一套。
 *
 * 窗口只能由后端弹：浏览器永远不会把绝对路径交给页面（`webkitdirectory` 只给
 * 相对路径，`showDirectoryPicker()` 连盘符都不给），而后端要照着路径扫盘。
 * 所以这个 POST 会一直挂着，直到用户在系统窗口里点完确定。
 */
async function pickScanDir(btn) {
  const old = btn.textContent;
  btn.disabled = true;
  btn.textContent = "选择中…";
  try {
    const r = await api("/api/models/local/pick-dir", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        initial: scanDirs[0] || "",          // 上次选过哪个，窗口就定位到哪
        title: "选择放 .gguf 的目录",
      }),
    });
    if (r.cancelled) return;                 // 按了取消：不用解释什么
    if (addScanDirText(r.path)) await runScan();   // 选完就扫，省掉"再点一次"
  } catch (err) {
    toast(`${err.message}。也可以直接粘贴路径`, "err");
  } finally {
    btn.disabled = false;
    btn.textContent = old;
  }
}

/* 只重画扫描这一块。整页重绘会把滚动位置和输入焦点一起冲掉——用户在填目录的
   时候被自己的界面打断，是最没必要的那种打断。 */
function refreshScanArea() {
  const box = $("scanPanel");
  if (!box) return;
  box.outerHTML = scanPanelHtml(state.models || {});
  bindScanPanel();
}

async function runScan() {
  if (!scanDirs.length) { toast("先添加扫描目录", "warn"); return; }
  const btn = $("scanDo");
  if (btn) { btn.disabled = true; btn.textContent = "扫描中…"; }   // 大目录要十几秒
  try {
    lastScan = await api(`/api/models/local/scan?dirs=${encodeURIComponent(scanDirs.join(","))}`);
  } catch (err) {
    toast(err.message, "err");
  }
  refreshScanArea();
}

/* 「＋ 加入」落到哪个端点。本地端点可以有好几个（一个本地 8097、一个连
   LM Studio 的 1234），扫出来的 gguf 只能进其中一份清单；默认给名字就叫
   local 的那个，没有就取第一个——和扫描区里标出来的目标保持一致。 */
function localTarget(d) {
  const eps = ((d && d.providers) || []).filter((p) => p.local || p.kind === "local");
  return eps.find((p) => p.name === "local") || eps[0] || null;
}

/* 给新加进来的本地模型挑一个没被占用的端口。没有 port 的条目在清单里
   点「启动」是启不起来的（进程不知道该监听哪儿），所以必须在加的时候定下来。 */
function nextLocalPort(d) {
  const used = new Set();
  ((d && d.providers) || []).forEach((p) => {
    (p.models || []).forEach((m) => { if (m.port) used.add(Number(m.port)); });
  });
  let port = 8097;
  while (used.has(port)) port += 1;
  return port;
}

/* 把扫到的一个 .gguf 追加进本地端点的模型清单。
   已经有这个 id 就不动——重复点两次不该产生两条一样的记录。 */
async function addScanned(m) {
  const d = state.models || (await api("/api/models"));
  const ep = localTarget(d);
  const name = ep ? ep.name : "local";
  const list = ep
    ? (ep.models || []).map((x) => ({
        id: x.id, alias: x.alias || "", context: x.context || undefined,
        max_output: x.max_output || undefined, path: x.path || undefined,
        port: x.port || undefined,
      }))
    : [];
  if (list.some((x) => x.id === m.id)) {
    toast("已经在清单里了", "warn");
    return;
  }
  // 路径先收编进 data/models：清单只写项目内相对路径，写盘符等于给下一个
  // clone 的人留一个死链。同分区的文件用硬链接，零拷贝。
  const at = await jsonPost("/api/models/local/attach", { path: m.path });
  // 端口必须给：没有 port 的本地模型在清单里点"启动"会直接说不出话
  list.push({ id: m.id, path: at.path, context: 4096, port: nextLocalPort(d) });
  await api(`/api/models/${encodeURIComponent(name)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      base_url: (ep && ep.base_url) || "http://127.0.0.1:8090/v1",
      model: (ep && ep.model) || m.id,
      kind: "local",
      models: list,
    }),
  });
  toast(`已加入 ${m.id}`);
  // reloadModels 会按最新清单重绘扫描区，那一行自动变成「已在清单」——
  // 不用再扫一遍磁盘来刷新按钮状态。
  await reloadModels();
}

/* 从清单里移出一个模型文件。只改 `models.yaml` 里这份清单，磁盘上的 .gguf
   一个字节都不动——「移出」是"我不想在列表里看到它"，不是"删掉我的模型"。
   移出的恰好是当前那一个时，顺手把当前改成剩下的第一个；否则配置里会留一个
   指向已不在清单里的 id，页面上就会出现"当前"没落在任何一行上的空转状态。 */
async function removeLocalModel(id) {
  const d = state.models || (await api("/api/models"));
  const p = (d.providers || []).find(
    (x) => (x.models || []).some((m) => m.id === id && m.local));
  if (!p) { toast("清单里没有这个模型", "warn"); return; }
  const rest = (p.models || []).filter((m) => m.id !== id).map((x) => ({
    id: x.id, alias: x.alias || "", context: x.context || undefined,
    max_output: x.max_output || undefined, path: x.path || undefined,
    port: x.port || undefined,
  }));
  // 最后一个不给移出：清单空了会把 path / port 一起丢掉，想加回来得重走一次
  // 扫描 + attach，端口还可能被重新分配。真要换，先加新的再移旧的。
  if (!rest.length) {
    toast("清单里至少要留一个模型文件", "warn");
    return;
  }
  const model = p.model === id ? (rest[0] ? rest[0].id : p.model) : p.model;
  await api(`/api/models/${encodeURIComponent(p.name)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      base_url: p.base_url || "", model, kind: "local", models: rest,
    }),
  });
  toast(`已移出 ${id}`);
  await reloadModels();
}

async function probeOneModel(name) {  const card = document.querySelector(`.svc-card[data-card="${CSS.escape(name)}"]`);
  const btn = card?.querySelector("[data-mact=probe]");
  if (btn) { btn.disabled = true; btn.textContent = "…"; card.classList.add("probing"); }
  try {
    const r = await api(`/api/services/probe?name=${encodeURIComponent(name)}`, { method: "POST" });
    const hit = r.providers[name] || {};
    await reloadModels();
    // 用户原话："我测哪个你说他连上就行"。通就一句，不通才给原因。
    toast(hit.ok ? `${name} 已连通` : `${name} 不通：${hit.message || "原因未知"}`,
          hit.ok ? "ok" : "warn");
  } catch (err) {
    toast(`${name} 测试失败：${err.message}`, "err");
  } finally {
    const b = document.querySelector(`.svc-card[data-card="${CSS.escape(name)}"] [data-mact=probe]`);
    if (b) { b.disabled = false; b.textContent = "⟳"; }
    document.querySelector(`.svc-card[data-card="${CSS.escape(name)}"]`)?.classList.remove("probing");
  }
}

/* 切「这一问用谁」。**只改本地选择**（下拉 + localStorage），不写后端的
 * 全局 provider —— 全局那套已经从界面上撤掉了（见 modelCard 上面的注释）。
 * 引导卡上的「先用 Mock 跑通」走的就是这里：它要的是"现在能跑通一次"，
 * 不是"以后都用 mock"；顺手把全局改掉，用户连切回去的入口都没有。 */
async function pickProvider(name) {
  const sel = $("providerSel");
  if (sel && [...sel.options].some((o) => o.value === name)) sel.value = name;
  localStorage.setItem("knowtune.provider", name);
  // 后端换了，模型候选也得跟着换：还留着上一个后端的模型名，这一问就会
  // 拿 A 的模型名去打 B 的接口，回一个 404，而界面上看不出哪里不对。
  syncGenModelOptions();
  renderCfgSummary();
  if (state.view === "models") await renderModels();
  // 只在"引导卡还在屏幕上"时重绘：切后端不该把用户正在看的对话擦掉。
  if (document.querySelector(".empty-state.gate") && canChat().ok) renderEmptyChat();
}

async function removeModel(name) {
  if (!(await askConfirm(
      `删除后端「${esc(name)}」？只是从注册表移除，不影响正在运行的服务。`,
      { title: "删除后端", okLabel: "删除" }))) return;
  try {
    await api(`/api/models/${encodeURIComponent(name)}`, { method: "DELETE" });
    state.models = null;
    await renderModels();
    await loadKb();
    toast("已删除");
  } catch (err) {
    toast(err.message, "err");
  }
}

async function openModelEditor(name, defaultKind) {
  // 从别处（知识库参数的「＋ 新增模型」）跳进来时 state.models 可能还没拉过，
  // 那时直接 return 的表现就是"点了没反应"。
  if (!state.models) await loadModels().catch(() => {});
  const d = state.models;
  if (!d) { toast("后端列表还没加载出来，稍等一下再点", "warn"); return; }
  const cur = name ? d.providers.find((p) => p.name === name) : null;
  const kinds = d.kinds;
  // 从哪一节点的「＋ 新增」，就先落成哪一类：本地节新增出来的默认是本地，
  // 外部节默认是云端。默认值写死成 local 的话，在外部节点新增要先改类型，
  // 忘了改就多出一个 local 端点，还得回本地节去删。
  const kindDef = defaultKind || "local";
  // 名字不是给用户出题的：新增时按类型直接给一个没被占用的（lmstudio 已存在
  // 就给 lmstudio2），想改再改。以前留空让用户自己想，想出来的"测试""MyModel"
  // 十有八九过不了校验，保存时才弹一条看不懂的英文规则。
  const suggest = (kindId) => {
    const used = new Set(d.providers.map((p) => p.name));
    if (!used.has(kindId)) return kindId;
    let i = 2;
    while (used.has(`${kindId}${i}`)) i += 1;
    return `${kindId}${i}`;
  };

  // 按了"清空密钥"：保存时显式传空串才表示清空，否则留空一律理解为"不改"
  let keyCleared = false;

  showModal(cur ? `编辑后端 · ${name}` : "新增后端", `
    <div class="form-grid">
      <div class="form-row"><label>名字</label>
        <input id="mName" value="${cur ? esc(cur.name) : esc(suggest(kindDef))}"
          placeholder="myserver" autocomplete="off" spellcheck="false">
        <span class="hint" id="mNameTip"></span></div>
      <div class="form-row"><label>类型</label>
        <select id="mKind">${kinds.map((k) =>
          `<option value="${esc(k.id)}"${(cur ? cur.kind : kindDef) === k.id ? " selected" : ""}>${esc(k.label)}</option>`).join("")}</select></div>
      <div class="form-row u-col2"><label>API 请求地址 base_url</label>
        <input id="mBase" value="${esc(cur?.base_url || "")}" placeholder="https://dashscope.aliyuncs.com/compatible-mode/v1"
          autocomplete="off" spellcheck="false">
        <span class="hint" id="mBaseTip">照厂商文档填，别套别家的（OpenAI 地址配百炼必 404）</span></div>
      <div class="form-row u-col2"><label>API 密钥（云端才需要）</label>
        <div class="input-row">
          <input id="mKey" type="password" placeholder="${cur && cur.key_present ? "已填写（留空 = 不改）" : "sk-…"}"
            autocomplete="new-password" spellcheck="false">
          <button class="ghost-btn" id="mKeyTest" type="button" title="用上面的地址 + 密钥 + 默认模型发一次真实对话">测试</button>
          ${cur && cur.key_present ? `<button class="ghost-btn" id="mKeyClear" type="button">清空</button>` : ""}
        </div>
        <span class="hint" id="mKeyTip"></span>
        <label class="remember-row"><input type="checkbox" id="mRemember"${d.key_autosave === false ? "" : " checked"}><span>记住密钥（<b>密文</b>存 data/secrets.json，${esc(d.key_cipher || "系统级加密")}；换机器即失效。不勾 = 只在本次会话有效）</span></label>
        ${d.key_autosave === false
          ? `<span class="hint warn-note u-mt6">现在是「只保留本次会话」：填了也不会落盘，重启后要重填。想改就勾上这一项，或回模型页点「记住到本机」。</span>`
          : ""}</div>
      <div class="form-row u-col2"><label>默认模型</label>
        <div class="input-row">
          <select id="mModel"></select>
          <button class="ghost-btn" id="mProbe" title="验证地址 + 密钥 + 默认模型能不能真的对话">验证</button>
        </div>
        <span class="hint">选中的这个才真正用于问答</span></div>
      <div class="form-row u-col2"><label>模型清单（可增删）</label>
        <input id="mPoolFilter" placeholder="筛选候选：输入关键字，如 qwen2.5" autocomplete="off" spellcheck="false">
        <div id="mList" class="model-list"></div>
        <div class="u-mt6"><button class="ghost-btn" id="mListAdd" type="button">＋ 添加模型</button></div>
        <span class="hint">清单只是候选表，不影响调用；每行可单独测能不能对话。</span></div>
    </div>
    <div class="probe-box" id="mProbeBox" hidden></div>
    <div class="form-row u-mt10">
      <span class="hint" id="mHint"></span>
    </div>`,
    async () => {
      const newName = ($("mName").value || "").trim().toLowerCase();
      if (!newName) throw new Error("名字不能为空");
      if (!/^[a-z][a-z0-9_-]{1,23}$/.test(newName)) {
        throw new Error("名字只能用小写字母、数字、- 和 _，2-24 个字符（它会被写进配置文件）");
      }
      const keyInput = readKey();
      const payload = {
        base_url: $("mBase").value.trim(),
        model: $("mModel").value.trim(),
        // 留空 = 不改动内存里已有的密钥；填了就覆盖；点"清空"则传空串
        api_key: keyInput || (keyCleared ? "" : null),
        kind: $("mKind").value,
        models: collectModelList(),
        // 仅当勾选状态与当前持久化策略不同才上报 remember，避免一个过期的
        // "仅本次会话"标记在每次保存时被静默写回——那样密钥永远存不下来。
        // 不报 remember 时后端不会改动 autosave 策略（见 app.py: upsert_model）。
        remember: ($("mRemember").checked) !== (d.key_autosave !== false)
          ? $("mRemember").checked : undefined,
      };
      if (!payload.model) throw new Error("还没填默认模型 —— 直接手打，或点「验证」后从候选里点选");
      if (!payload.base_url) {
        throw new Error("还没填 base_url（本地服务的地址，形如 http://127.0.0.1:1234/v1）");
      }
      await api(`/api/models/${encodeURIComponent(newName)}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      // 改名 = 新建一个 + 删掉旧的。顺序不能反：先删会让引用旧名的站点角色
      // 暂时悬空。没有"全局默认"要搬，新建完直接删旧项即可——站点角色若指
      // 向旧名，会在下一次保存站点配置时按新名更新。
      if (cur && cur.name !== newName) {
        await api(`/api/models/${encodeURIComponent(cur.name)}`, { method: "DELETE" })
          .catch((e) => toast(`旧名删除失败：${e.message}`));
      }
      state.models = null;
      // 从知识库页里点「＋新增模型」进来时，不能调 renderModels()——它会改写
      // 顶栏标题和按钮，而当前顶栏属于知识库页。只刷数据，不刷视图。
      if (state.view === "models") await renderModels();
      else await loadModels();
      await loadKb();
      if (state.view === "kb") await renderKbParamsPane();
      toast(`已保存 ${newName}：${payload.model}`);
    }, { okLabel: "保存", wide: true });

  const body = $("modalBody");
  // 名字实时校验：等点保存才报错，用户看到的是一条弹出框里的规则说明，
  // 而不是"我刚打的字哪里不对"。
  const checkName = () => {
    const v = ($("mName").value || "").trim().toLowerCase();
    const tip = $("mNameTip");
    if (!v) { tip.textContent = ""; return true; }
    const ok = /^[a-z][a-z0-9_-]{1,23}$/.test(v);
    const dup = ok && v !== cur?.name && d.providers.some((p) => p.name === v);
    tip.innerHTML = !ok
      ? `<span style="color:var(--danger,#ff6b6b)">只能用小写字母、数字、- 和 _，2-24 个字符</span>`
      : dup ? `<span style="color:var(--warn,#e0a34a)">「${esc(v)}」已存在，保存会覆盖它</span>` : "";
    return ok;
  };
  $("mName").oninput = checkName;

  // 密钥字段：现在填的是密钥本体。不做格式校验（各家前缀不一样），
  // 只在按"清空"时明确置一个标记，保存才能区分"不改"和"清掉"。
  const keyClearBtn = $("mKeyClear");
  if (keyClearBtn) {
    keyClearBtn.onclick = () => {
      $("mKey").value = "";
      keyCleared = true;
      keyClearBtn.disabled = true;
      keyClearBtn.textContent = "将清空";
      $("mKeyTip").innerHTML = "保存后会删掉内存里的密钥。";
    };
  }
  // 密钥里不可能有空白，也不可能是全角字符。从聊天记录、网页、PDF 复制时
  // 常带进末尾一整行换行或全角空格——发送时它们被当成密钥的一部分，
  // 服务端回 401，而用户看到的密钥"肉眼完全正确"。就地擦掉，比事后解释便宜。
  const readKey = () => {
    const raw = ($("mKey").value || "");
    const clean = raw.replace(/\s+/g, "").replace(/[^\x21-\x7e]/g, "");
    if (clean !== raw) {
      const tip = $("mKeyTip");
      if (tip) {
        tip.innerHTML = `已自动去掉密钥里 <b>${raw.length - clean.length}</b> 个空白或全角字符（复制时常带进来）。`;
      }
    }
    return clean;
  };
  $("mKey").oninput = () => { if ($("mKey").value.trim()) keyCleared = false; };

  /* 模型清单：抄 ccswitch 的做法——**清单是人维护的候选表**，探测只负责验证。
     以前把厂商 /models 的原始返回（含图像/语音/实时/向量）直接做成可点选项，
     等于让人在雷区里挑。

     行内改成**下拉框**而不是手打：模型 id 是这套界面最容易写错的东西——
     少个点、大小写差一位，保存能过，问答时才报 404。能选就别让人打。
     下拉框自带"这一个能不能用"的测试按钮，省得每次都整体验证一遍。 */
  const CUSTOM = "__custom__";
  // 候选池 = 上次探测回来的 + 已存清单里的 + 当前默认模型（它未必在清单里）。
  // 没探测过时至少已有条目还选得到，也不会把当前默认值显示成"未选"。
  // 排序是为了"找得到"：厂商 /models 的返回顺序没规律，几百条里肉眼翻等于没有。
  let pool = [...new Set([
    ...(((cur && cur.models) || []).map((m) => m.id)),
    ...(cur && cur.model ? [cur.model] : []),
  ].filter(Boolean))].sort((a, b) => a.localeCompare(b));
  // 候选过滤：几百个模型里找一个，原生下拉只能靠键盘跳转，用户不知道也不会用。
  // 一个小输入框按子串筛，行里已选的值不受过滤影响（见 optionsOf）。
  let poolFilter = "";
  const passFilter = (id, value) =>
    !poolFilter || id.toLowerCase().includes(poolFilter) || id === value;

  const optionsOf = (value, ph) => {
    const opts = [`<option value="">${esc(ph || "（选一个模型）")}</option>`];
    pool.forEach((id) => {
      if (!passFilter(id, value)) return;
      opts.push(
        `<option value="${esc(id)}"${id === value ? " selected" : ""}>${esc(id)}</option>`);
    });
    // 已存但不在候选里（探测失败、或换过后端）：保留原值，别让下拉框悄悄改掉数据
    if (value && !pool.includes(value)) {
      opts.push(`<option value="${esc(value)}" selected>${esc(value)}（不在候选里）</option>`);
    }
    opts.push(`<option value="${CUSTOM}">＋ 手动输入…</option>`);
    return opts.join("");
  };

  // 探测不出来的时候（自建服务、内网代理）不能把人堵死，所以留了手动输入的口子，
  // 但它不该是默认路径——选了才把下拉换成输入框。
  const toInput = (sel, keep) => {
    const inp = document.createElement("input");
    inp.className = sel.className;
    inp.id = sel.id;
    inp.value = keep || "";
    inp.placeholder = sel.id === "mModel" ? "手打模型 id，如 qwen-plus" : "手打模型 id";
    inp.autocomplete = "off";
    inp.spellcheck = false;
    sel.replaceWith(inp);
    return inp;
  };

  // 给"模型"字段赋值：值不在候选里就切成手输框，绝不让 value 静默变成空
  const setChoice = (el, v) => {
    if (!el) return;
    if (el.tagName === "SELECT" && v && ![...el.options].some((o) => o.value === v)) {
      toInput(el, v).focus();
      return;
    }
    el.value = v || "";
  };

  // 表头跟行共用同一套 grid 列，视觉上是一张表而不是一坨控件
  const listHead = () => `<div class="model-row model-head">
      <span>模型</span><span>上下文</span><span>最大输出</span><span></span><span></span>
    </div>`;
  const renderList = (entries) => {
    const box = $("mList");
    const rows = (entries && entries.length) ? entries : [];
    box.innerHTML = rows.length
      ? listHead() + rows.map((m) => listRow(m.id, m.context, m.max_output)).join("")
      : `<p class="hint" style="margin:4px 0">还没有条目。点上面「验证」探测出可用模型，再逐行从下拉里选。</p>`;
    refreshModelOptions();
  };
  const listRow = (id, ctx, out) => `<div class="model-row" data-row>
      <select class="mr-id">${optionsOf(id || "")}</select>
      <input class="mr-ctx" value="${ctx || ""}" placeholder="如 128000" inputmode="numeric">
      <input class="mr-out" value="${out || ""}" placeholder="如 8192" inputmode="numeric">
      <button class="mini icon" type="button" data-test title="只测这一个模型能不能对话">▶</button>
      <button class="mini icon danger" type="button" data-drop title="删除这一行">✕</button>
      <span class="mr-state"></span>
    </div>`;

  // 单模型测试：后端 probe-url 本来就能指定 model，不用新接口。
  // 复用的还是弹窗里当前的地址/密钥，所以"地址填错了"会在这里一起暴露。
  const testOne = async (row, btn) => {
    const field = row.querySelector(".mr-id");
    const st = row.querySelector(".mr-state");
    const id = (field.value || "").trim();
    if (!id) { st.innerHTML = `<span class="warn">先选一个模型</span>`; return; }
    btn.disabled = true;
    st.innerHTML = `<span class="hint">测试中…</span>`;
    try {
      const r = await jsonPost("/api/services/probe-url", {
        base_url: $("mBase").value.trim(),
        api_key: $("mKey").value.trim(),
        kind: $("mKind").value,
        model: id,
        name: cur ? cur.name : "",
      });
      st.innerHTML = `<i class="dot ${dotOf(r.status)}"></i> ${esc(r.message || statusText(r.status))}`
        + (r.latency_ms ? ` · ${r.latency_ms}ms` : "");
    } catch (err) {
      st.innerHTML = `<span class="warn">${esc(err.message)}</span>`;
    } finally {
      btn.disabled = false;
    }
  };

  const refreshAllSelects = () => {
    $("mList").querySelectorAll("select.mr-id").forEach((sel) => {
      const v = sel.value;
      sel.innerHTML = optionsOf(v);
      sel.value = v;
    });
  };
  // 默认模型的候选 = 清单里的名字；清单变化时两边的下拉一起刷新
  const refreshModelOptions = () => {
    collectModelList().map((m) => m.id).filter(Boolean)
      .forEach((i) => { if (!pool.includes(i)) pool.push(i); });
    pool.sort((a, b) => a.localeCompare(b));
    refreshAllSelects();
    const mm = $("mModel");
    if (mm && mm.tagName === "SELECT") {
      const v = mm.value;
      mm.innerHTML = optionsOf(v, "（先点验证探测，或从清单里选）");
      mm.value = v;
    }
  };
  // 清单行只编辑 id / 上下文 / 输出上限三样；`path` 和 `port` 是"这个模型
  // 本机能不能起"的唯一依据，界面上不编辑，但保存时必须原样带回去——
  // 丢一次，那个模型就再也点不了「启动」了。
  const keepLocal = new Map(((cur && cur.models) || [])
    .filter((m) => m.path).map((m) => [m.id, { path: m.path, port: m.port }]));
  const collectModelList = () => {
    const rows = [...$("mList").querySelectorAll("[data-row]")];
    return rows.map((r) => {
      const id = r.querySelector(".mr-id").value.trim();
      const extra = keepLocal.get(id) || {};
      return {
        id,
        context: r.querySelector(".mr-ctx").value.trim(),
        max_output: r.querySelector(".mr-out").value.trim(),
        path: extra.path,
        port: extra.port,
      };
    }).filter((m) => m.id);
  };
  // 从空态到第一行：表头要在这里补出来。点「添加模型」和点探测候选都是
  // 直接插行、不走 renderList，漏了表头第一行就成了没有列名的孤行。
  const ensureHead = () => {
    const box = $("mList");
    box.querySelector("p.hint")?.remove();
    if (!box.querySelector("[data-row]")) box.insertAdjacentHTML("beforeend", listHead());
  };
  const addToEntry = (id) => {
    if (!id) return;
    if (collectModelList().some((m) => m.id === id)) return;  // 已有就不重复加
    ensureHead();
    $("mList").insertAdjacentHTML("beforeend", listRow(id, "", ""));
    refreshModelOptions();
  };
  // select 元素自己不带 value 属性，初值只能在这里回填（先灌 options 再取值，
  // 否则空 select 会被 setChoice 误判成"值不在候选里"而切成手输框）。
  // 漏了这一步，编辑已有后端时默认模型会显示成"（未选）"，一保存原值就没了。
  $("mModel").innerHTML = optionsOf((cur && cur.model) || "", "（先点验证探测，或从清单里选）");
  renderList((cur && cur.models) || []);
  $("mListAdd").onclick = () => {
    ensureHead();
    $("mList").insertAdjacentHTML("beforeend", listRow("", "", ""));
    refreshModelOptions();
    const rows = $("mList").querySelectorAll("[data-row]");
    rows[rows.length - 1]?.querySelector(".mr-id")?.focus();
  };
  $("mList").onclick = (e) => {
    const drop = e.target.closest("[data-drop]");
    if (drop) {
      drop.closest("[data-row]").remove();
      if (!$("mList").querySelector("[data-row]")) renderList([]);
      refreshModelOptions();
      return;
    }
    const t = e.target.closest("[data-test]");
    if (t) testOne(t.closest("[data-row]"), t);
  };
  $("mList").onchange = (e) => {
    if (!e.target.classList.contains("mr-id")) return;
    if (e.target.value === CUSTOM) toInput(e.target, "").focus();
    refreshModelOptions();
  };
  $("mList").oninput = (e) => { if (e.target.classList.contains("mr-id")) refreshModelOptions(); };
  // 候选过滤框：只影响下拉里的选项，不动任何已存的值。
  $("mPoolFilter").oninput = (e) => {
    poolFilter = e.target.value.trim().toLowerCase();
    refreshAllSelects();
    const mm = $("mModel");
    if (mm && mm.tagName === "SELECT") {
      const v = mm.value;
      mm.innerHTML = optionsOf(v, "（先点验证探测，或从清单里选）");
      mm.value = v;
    }
  };

  // 记住"上一次自动填进去的默认值"：换类型时，只有当地址还是上一个类型的默认值
  // （说明用户没动过它）才跟着换。以前只判空，结果选了 DeepSeek 地址还停在
  // 上一个类型的 http://127.0.0.1:8080/v1，看着像没生效。
  let lastBase = null;
  let lastModel = null;
  let lastName = null;
  const syncKind = (fill) => {
    const k = kinds.find((x) => x.id === $("mKind").value) || kinds[0];
    const hintEl = $("mHint");
    if (hintEl) hintEl.textContent = k.hint || "";
    if (fill) {
      const b = $("mBase").value.trim();
      if (!b || b === lastBase) $("mBase").value = k.default_base || "";
      const m = $("mModel").value.trim();
      if (!m || m === lastModel) {
        // 类型自带的默认模型要进候选池：不然它不在 options 里，赋值会静默变空，
        // 或者把下拉框顶成手输框——看着像"选了 DeepSeek 但模型没跟着变"。
        const dm = k.default_model || "";
        if (dm && !pool.includes(dm)) pool.push(dm);
        refreshAllSelects();
        setChoice($("mModel"), dm);
      }
      // 换类型时顺手把名字也换成可用的：新增场景下用户不会去纠结这个字段，
      // 但同名冲突会让保存直接失败。只换掉"还是上一个建议值"的，别覆盖手打的名字。
      if (!cur) {
        const n = $("mName").value.trim();
        if (!n || n === lastName) $("mName").value = suggest(k.id);
      }
    }
    lastBase = k.default_base || "";
    lastModel = k.default_model || "";
    lastName = suggest(k.id);
    checkName();
  };
  syncKind(!cur);
  $("mKind").onchange = () => syncKind(true);

  const runProbe = async () => {
    const box = $("mProbeBox");
    const btns = [$("mProbe"), $("mKeyTest")].filter(Boolean);
    btns.forEach((b) => { b.disabled = true; });
    box.hidden = false;
    box.innerHTML = `<span class="hint">验证中…（地址 + 密钥 + 默认模型，真发一次对话）</span>`;
    try {
      const r = await jsonPost("/api/services/probe-url", {
        base_url: $("mBase").value.trim(),
        // 探测用的密钥就是刚敲进去的那份：留空时后端会退到已存的内存密钥
        api_key: readKey(),
        kind: $("mKind").value,
        model: $("mModel").value.trim(),
        name: cur ? cur.name : "",
      });
      const list = r.models || [];
      // 探测回来的模型全部进候选池：下面每一行的下拉框立刻都能选到，
      // 不用再手打一遍名字（这正是 404 的主要来源）。
      if (list.length) {
        pool = [...new Set([...pool, ...list])];
        refreshModelOptions();
      }
      const head = `<div class="probe-head">
          <i class="dot ${dotOf(r.status)}"></i>
          <span class="probe-msg">${esc(r.message || statusText(r.status))}${r.latency_ms ? ` · ${r.latency_ms}ms` : ""}</span>
        </div>`;
      // 候选只用来"加入清单"：点一下既填默认模型，也进清单。
      // 只列能做文本对话的那些——图像/语音/实时/向量点进去就是 403/500。
      const total = r.models_total || list.length;
      const hidden = r.models_hidden || 0;
      const chips = list.length
        ? `<div class="chip-row">${list.map((id) =>
            `<button class="chip${id === $("mModel").value.trim() ? " on" : ""}"
              data-pick="${esc(id)}" title="选用并加入清单">${esc(id)}</button>`).join("")}</div>
           <p class="hint u-mt6">点一个 = 设为默认模型并加入清单。${
             hidden ? `（共 ${total} 个，已隐藏 ${hidden} 个图像/语音/实时类）` : ""}</p>`
        : "";
      box.innerHTML = head + chips;
      box.querySelectorAll("[data-pick]").forEach((chip) => {
        chip.onclick = () => {
          setChoice($("mModel"), chip.dataset.pick);
          addToEntry(chip.dataset.pick);
          box.querySelectorAll("[data-pick]").forEach((c) => c.classList.toggle("on", c === chip));
        };
      });
    } catch (err) {
      box.innerHTML = `<span class="warn">${esc(err.message)}</span>`;
    } finally {
      btns.forEach((b) => { b.disabled = false; });
    }
  };
  $("mProbe").onclick = runProbe;
  // 密钥旁边也有一个「测试」：填完密钥顺手验一下，不用把眼睛挪到上面去找按钮
  $("mKeyTest").onclick = runProbe;
}

/* ============================================================ 新建知识库 */

async function newKbWizard() {
  if (!state.presets) {
    state.presets = await api("/api/kb/presets").catch(() => []);
  }
  const presets = state.presets || [];
  // 预设只在建库时作为**初始值**生效一次：建完之后每个库持有独立一套参数，
  // 参数页不再提供"套用预设"。这里记住选中的是哪个，提交时把 id 一起给后端
  // ——后端才能把预设里那几项（融合权重、候选数等）也一并落上。
  let picked = presets[0]?.id || null;

  showModal("新建知识库", `
    <div class="form-grid">
      <div class="form-row"><label>名称</label>
        <input id="kbName" placeholder="例如：技术文档" autofocus></div>
      <div class="form-row"><label>描述（可选）</label>
        <input id="kbDesc" placeholder="这个库里放什么"></div>
    </div>
    <p class="hint" style="margin:12px 0 6px">参数预设 · 建库时套一次，之后在「参数」页单独改</p>
    <div class="preset-row" id="presetRow">
      ${presets.map((p, i) => `<button class="mini${i === 0 ? " on" : ""}" data-preset="${p.id}"
        title="${esc(p.desc)}">${esc(p.label)}</button>`).join("")}
    </div>
    <div class="form-grid tight u-mt12">
      <div class="form-row"><label>分块策略</label>
        <select id="kbStrategy">
          <option value="size">按长度打包</option>
          <option value="heading">按章节切</option>
          <option value="paragraph">按段落切</option>
        </select></div>
      <div class="form-row"><label>分块长度（字）</label>
        <input type="number" id="kbSize" value="480" min="100" max="2000" step="20"></div>
      <div class="form-row"><label>重叠（字）</label>
        <input type="number" id="kbOverlap" value="80" min="0" max="1000" step="20"></div>
      <div class="form-row"><label>最终引用条数</label>
        <input type="number" id="kbTopK" value="5" min="1" max="20"></div>
    </div>
    <p class="hint u-mt8">
      分块类参数保存后会自动重建本库索引；改了全局也不会影响这个库。
    </p>`,
    async () => {
      const name = $("kbName").value.trim();
      if (!name) throw new Error("名称不能为空");
      const size = Number($("kbSize").value);
      const overlap = Number($("kbOverlap").value);
      if (!(overlap < size)) throw new Error("重叠必须小于分块长度");
      const config = {
        "chunking.strategy": $("kbStrategy").value,
        "chunking.size": size,
        "chunking.overlap": overlap,
        "retrieval.rerank.top_k": Number($("kbTopK").value),
      };
      const kb = await jsonPost("/api/kb", {
        name, description: $("kbDesc").value.trim(), preset: picked, config,
      });
      await loadKb();
      await selectKb(kb.id);
      state.kbCfgDraftFor = null;
      setView("kb", { tab: "params" });
      toast(`知识库「${name}」已创建`);
    },
    { okLabel: "创建", wide: true });

  const body = $("modalBody");
  body.querySelectorAll("[data-preset]").forEach((btn) => {
    btn.onclick = () => {
      body.querySelectorAll("[data-preset]").forEach((b) => b.classList.remove("on"));
      btn.classList.add("on");
      const preset = presets.find((p) => p.id === btn.dataset.preset);
      if (!preset) return;
      picked = preset.id;
      const v = preset.values || {};
      if (v["chunking.strategy"] && $("kbStrategy")) $("kbStrategy").value = v["chunking.strategy"];
      if (v["chunking.size"] !== undefined) $("kbSize").value = v["chunking.size"];
      if (v["chunking.overlap"] !== undefined) $("kbOverlap").value = v["chunking.overlap"];
      if (v["retrieval.rerank.top_k"] !== undefined) $("kbTopK").value = v["retrieval.rerank.top_k"];
    };
  });
}

/* ------------------------------------------------------------- 启动 */

(async function boot() {
  setupGlobalDrop();
  document.querySelectorAll(".nav-item").forEach((btn) => {
    btn.onclick = () => setView(btn.dataset.view);
  });
  $("btnKbConfigQuick").onclick = () => {
    toggleCfgPop(false);
    setView("kb", { tab: "params" });
  };

  // 首屏的每一步都必须能被单独容忍。以前整段只有一个 try：/api/kb 慢一拍
  // （例如服务刚重启、嵌入模型还在加载）就整块跳进 catch，侧栏知识库、会话、
  // 文档全空，用户看到的是一个"什么都没加载出来"的空壳，还以为程序坏了。
  const tolerate = async (label, fn, fallback = null) => {
    for (let i = 0; i < 3; i++) {
      try { return await fn(); }
      catch (err) {
        if (i === 2) {
          console.warn(`[boot] ${label} 失败：`, err.message);
          return fallback;
        }
        await new Promise((r) => setTimeout(r, 1200));
      }
    }
    return fallback;
  };

  try {
    const saved = localStorage.getItem("knowtune.kb");
    if (saved) state.kbId = Number(saved) || null;
    await tolerate("知识库列表", () => loadKb());
    if (state.kbId && !state.kbs.some((k) => k.id === state.kbId)) {
      state.kbId = state.kbs.length ? state.kbs[0].id : null;
    }
    if (state.kbId) localStorage.setItem("knowtune.kb", String(state.kbId));

    // 后端下拉必须在恢复 localStorage 里的选择之前填好，否则恢复不上去
    await tolerate("后端列表", () => loadProviders());
    const savedProvider = localStorage.getItem("knowtune.provider");
    if (savedProvider && [...$("providerSel").options].some((o) => o.value === savedProvider)) {
      $("providerSel").value = savedProvider;
    }
    restoreAdv();
    syncGenModelOptions();   // 选项依赖当前后端，必须在 restoreAdv 之后
    const savedK = localStorage.getItem("knowtune.topk");
    if (savedK && [...$("topKSel").options].some((o) => o.value === savedK)) {
      $("topKSel").value = savedK;
    }

    // 调试面板默认展开（这是这个项目最该被看见的部分）；收起过才记成 0。
    const dbgPref = localStorage.getItem("knowtune.debug") !== "0";
    $("dbgToggle").checked = dbgPref;
    document.querySelector(".app").classList.toggle("panel-hidden", !dbgPref);
    renderCfgSummary();

    // 深链：#home / #models / #models=new / #kb / #newkb。
    // 后台不再有会话 id，#s=<会话 id> 这类老链接进来就当普通聊天页，
    // 不做任何读取——那条会话本就不属于这一页了。
    const hash = location.hash;
    renderSessions();
    if (hash === "#home") {
      await setView("home");
    } else if (hash === "#models" || hash === "#models=new") {
      await setView("models");
      if (hash === "#models=new") openModelEditor(null, "openai");
    } else if (hash === "#kb") {
      await setView("kb");
    } else {
      setView("chat");
    }
    if (hash === "#newkb") await newKbWizard();
    // 老书签 #settings 指的是已退役的全局参数抽屉：落到当前库的「参数」栏目，
    // 顺手把 hash 清掉，免得每次重开又跳一次。
    if (hash === "#settings") {
      history.replaceState(null, "", location.pathname + location.search);
      await setView("kb", { tab: "params" });
    }

    // 首页默认就把服务状态探一遍，否则第一屏全是"未探测"，等于什么也没说
    if (state.view === "home") {
      const hasUnknown = (state.dash?.services || []).some((s) => s.status === "unknown");
      if (hasUnknown) probeServices().catch(() => {});
    }
  } catch (err) {
    $("chat").innerHTML = `<div class="empty-state"><h2>后端未就绪</h2>
      <p>${esc(err.message)}</p></div>`;
    setView("chat");
  }
})();


/* --------------------------------------------------------- 前台站点管理 */
/* 前台是给终端用户的那一层：一个知识库 + 一个端口 + 一套语言，界面上只有提问
   框和答案。这里只管"起几个、绑哪个库、占哪个端口"，界面本身在 frontsite/
   下另一套代码里，与后台的 CSS/JS 没有任何共享。 */

const SITE_LANG_NAME = { ug: "维吾尔语", zh: "中文", en: "English" };

function siteStatusBadge(st) {
  const map = {
    running: ["ok", "运行中"], stopped: ["", "已停止"], starting: ["warn", "启动中"],
    stopping: ["warn", "停止中"], error: ["err", "异常"],
    // 端口上是同一个站点，但跑在**另一个后台进程**里：链接能用，本页管不了。
    // 混进"异常"里就变成两件事共用一句"端口被占用"，用户会去改端口——改了就真错了。
    external: ["warn", "外部托管"],
  };
  const [cls, label] = map[st] || ["", st || "未知"];
  return `<span class="badge ${cls}">${label}</span>`;
}

function siteStatusWord(st) {
  return ({ running: "运行中", stopped: "已停止", starting: "启动中",
    stopping: "停止中", error: "异常", external: "外部托管" })[st] || (st || "未知");
}
function siteStatusDot(st) {
  return ({ running: "ok", starting: "warn", stopping: "warn", error: "err",
    external: "warn" })[st] || "";
}
function siteStatusTail(i) {
  if (i.status === "running" && i.uptime_s) return "已运行 " + uptime(i.uptime_s);
  if (i.status === "external") return i.managed_by && i.managed_by.pid
    ? "外部 PID " + i.managed_by.pid : "由别的后台托管";
  return "";
}

/* 预问诊的模型不在站点 roles 里，单独存 /api/previsit/config。这里按后端
 * 的同一套「后端::模型」拼法还原，供下面的生效条与矩阵共用。 */
function pvRolePair(pv) {
  const v = (pv && pv.values) || {};
  const pair = (p, m) => (p && m ? `${p}::${m}` : p || m || "");
  return {
    translate: pair(v.translator_provider, v.translator_model),
    generate: pair(v.provider, v.model),
  };
}

/* 「当前生效」条：回答“这个站点现在每个入口用谁译、用谁答”。
 *
 * 上一种做法是页面顶部摆一张独立的「当前运行」表，结果同一站点在页面上
 * 出现两次（表一行、卡一张），而且表里读的是 model-roles 的 resolved——
 * 那只是**站点默认**，三个板块自己的覆盖（section_resolved）压根没读，
 * 却写着“实际生效”。现在这张条挪进站点卡，状态在卡头说一次，这里只说
 * 模型，并且逐入口读 section_resolved / 预问诊配置；跟站点默认相同的
 * 标「继承」，不同才列真值。配置矩阵在下方负责“改”，这里负责“看”。
 *
 * 角色值是「后端::模型」，裸模型名是“按名路由”。显示沿用下拉那套短名
 * 规则（去厂商前缀 / -Instruct / 量化尾巴），完整值留在 title。 */
function siteModelName(v) {
  const full = String(v || "").trim();
  if (!full) return "未配置";
  const id = full.includes("::") ? full.slice(full.indexOf("::") + 2) : full;
  return trimQuant(shortModel(id)) || id;
}
function siteLiveStrip(i, plan, pv) {
  const site = (plan && plan.resolved) || i.roles || {};
  const sections = (plan && plan.section_resolved) || {};
  const secs = i.sections || {};
  const pvPair = pvRolePair(pv);
  const entries = [
    { label: "维医", r: sections.med || site },
    { label: "古籍", r: sections.classic || site },
    { label: "预问诊", r: pvPair.translate || pvPair.generate ? pvPair : site },
  ];
  const cells = entries.map((e) => {
    const tr = e.r.translate || "";
    const gen = e.r.generate || "";
    const inherited = (!tr || tr === site.translate) && (!gen || gen === site.generate);
    // 短名在不同后端之间会撞（local 的 7B 和 siliconflow 的 7B 同名），
    // 所以独立配置时把后端名直接写出来，而不是只丢一个"独立"。
    const ownBase = String(gen || tr).split("::")[0];
    return `<div class="sl-entry" title="译：${esc(tr || "未配置")}\n答：${esc(gen || "未配置")}">
      <span class="sl-entry-k">${esc(e.label)}</span>
      <span class="sl-entry-v"><em>译</em><b>${esc(siteModelName(tr))}</b></span>
      <span class="sl-entry-v"><em>答</em><b>${esc(siteModelName(gen))}</b></span>
      ${inherited ? `<span class="sl-inherit" title="与站点默认相同">继承</span>`
        : `<span class="sl-own" title="独立配置：${esc(ownBase)}">${esc(ownBase || "独立")}</span>`}
    </div>`;
  }).join("");
  return `<div class="sbk-strip${i.status === "running" ? "" : " is-off"}">
    <span class="sbk-strip-k">当前生效</span>
    <div class="sbk-strip-entries">${cells}</div>
    <span class="sbk-strip-note">改下方任一配置即时同步</span>
  </div>`;
}

/* --------------------------------------------------------- 站点卡（一卡一站） */
/*
 * 以前这块是「一张全局角色卡 + 一列只读站点卡 + 编辑弹窗」。三处都别扭：
 *
 * 1. 角色是**站点级**配置（spec.roles），却单独做成一张全局卡挂在站点卡上方，
 *    用户得先想"这个角色归谁"，再回头去站点里看"实际用了谁"。
 * 2. 参数全在弹窗里。改一个标题要：点编辑 → 找到第 9 个字段 → 改 → 保存 →
 *    等弹窗关。截图里那些参数一个都看不见，等于每次都在盲找。
 * 3. 弹窗一关，卡片上还是旧的只读摘要，改没改成功得靠记性。
 *
 * 现在一个站点一张大卡，卡内按「绑定 / 模型角色 / 答案」分区，
 * 所有字段就地可改，改完即存（后端热生效，只有端口和监听会重启）。
 */

/* 后端下拉。名字后面必须带"本地/云端 · 模型名 · 状态"：只给一个后端名字，
   用户不知道选了实际会发生什么——云端后端在缺密钥时会静默退回默认后端，
   看起来就是"配了没生效"。
 *
 * 而且"选后端"这件事本身就不够：翻译和推理经常落在同一个本地服务上，
 * 但用不同的模型（hy-mt2-1.8b 专翻，doctor-7b 专答）。只能选后端，等于
 * 逼用户为了换个翻译模型去改后端自己的 model，然后推理也跟着一起变。
 * 所以每个后端展开成一组，组里只列这个后端能用的模型，值写成「后端::模型」。
 * 不再有「默认」项：角色要么留空跟随上级，要么钉死到一个具体模型。
 * 旧配置里只写后端名的值会自动落到该后端的 model（仍是那个具体模型），
 * 保存一次就变成显式值，不会被浏览器静默换成别的第一项。
 */
function provSelect(role, cur, providers, meta, defProvider, headLabel) {
  const head = `<option value="">${esc(headLabel || "跟随上级")}</option>`;
  const ref = String(cur || "");
  const cut = ref.indexOf("::");
  const curBase = cut >= 0 ? ref.slice(0, cut) : ref;
  const curModel = cut >= 0 ? ref.slice(cut + 2) : "";

  const groups = (providers || []).map((name) => {
    const m = (meta || []).find((x) => x.name === name) || {};
    // 组名只写后端名。原来这里挂「云端 · 就绪」「本地 · 超时」，可探测状态
    // 跟"这个角色用谁"是两件事：本地显示超时往往只是模型没启动，用户却以为
    // 后端坏了；而且状态是上一次探测的快照，会在下拉里待到下次刷新。要看
    // 通不通去「模型服务」页，那里是实时的。悬停仍能看到完整说明。
    const label = name;

    // 只列「模型服务」页里已经加入的模型（config/models.yaml 的 models 清单）。
    // 探测回报的那批刻意不进来：siliconflow 61 个、aliyun 147 个，那是服务商的
    // 全量目录，不是"我要用哪些"。站点这一列回答的是"这个入口用谁"，候选必须
    // 是已经挑过的；想用一个没在清单里的模型，先去模型页把它加进来——那里的
    // 下拉本来就是挑模型的地方（选一下就会进清单）。
    const seen = new Set();
    const models = [];
    for (const it of (m.models || [])) {
      const id = String((it && it.id) || "").trim();
      if (id && !seen.has(id)) { seen.add(id); models.push(id); }
    }

    // 旧配置只有后端名、没有模型时，落到这个后端自己的 model 上——但它是
    // 作为一个**具体模型**被选中的，界面上不再出现"默认"这项。该模型若已
    // 不在清单里，补一条占位把 selected 留住，等用户保存时写成显式值。
    const legacyDefault = curBase === name && !curModel && Boolean(m.model);
    const missingModel = curBase === name
      ? (curModel && !seen.has(curModel) ? curModel
        : (legacyDefault && !seen.has(m.model) ? m.model : ""))
      : "";
    const missing = missingModel
      ? `<option value="${esc(`${name}::${missingModel}`)}" selected title="${esc(missingModel)}">${esc(shortModel(missingModel))}（未加入清单）</option>`
      : "";
    // 后端连 model 都没配的极端旧值：不能让它静默变成第一项，留个看得见的占位。
    const legacyNoModel = curBase === name && !curModel && !m.model
      ? `<option value="${esc(name)}" selected>${esc(name)}（未指定模型）</option>`
      : "";
    // 显示名去掉「厂商/」前缀和 -Instruct 尾巴；一组里人人都带的量化后缀
    // （本地那组五个都是 -Q4_K_M）也只留一份在 title，不重复读五遍。完整 id
    // 始终在 title。撞名才退回全名——不同厂商的同名模型挤在一个后端下的
    // 可能性小，但不能假设为零。
    const shorts = models.map((id) => shortModel(id));
    const common = commonSuffix(shorts);
    const labels = shorts.map((s) => shortModel(common && s.length > common.length
      ? s.slice(0, -common.length) : s));
    const counts = new Map();
    labels.forEach((s) => counts.set(s, (counts.get(s) || 0) + 1));
    // 本地模型一个模型一个进程：没启动的那个选了也调不通。以前这里不标状态，
    // 理由是"探测快照会过期"——但过期的代价是"选了才知道"，而现在后端已经
    // 会在请求时硬校验，UI 再瞒着就成了明知故问。只标未启动，不标其它状态：
    // 就绪是常态，一排「就绪」纯属噪音。
    const liveState = new Map(
      (m.models || []).map((x) => [String(x.id || ""), String(x.state || "")]));
    const isLocal = Boolean(m.local);
    const rest = models.map((id, i) => {
      const val = `${name}::${id}`;
      const sel = (curBase === name && curModel === id)
        || (legacyDefault && id === m.model) ? " selected" : "";
      const dup = counts.get(labels[i]) > 1;
      const text = dup ? id : labels[i];
      const st = isLocal ? liveState.get(id) || "" : "";
      const tag = !st || st === "running" ? ""
        : st === "starting" ? " · 启动中" : " · 未启动";
      const tip = st && st !== "running"
        ? (st === "starting" ? "正在加载权重" : "本地模型没在跑，选它会调不通，到「模型服务」页点启动")
        : id;
      return `<option value="${esc(val)}"${sel} title="${esc(tip)}">${
        esc(text + tag)}</option>`;
    }).join("");
    return `<optgroup label="${esc(label)}">${legacyNoModel}${missing}${rest}</optgroup>`;
  }).join("");

  // 当前值指向一个已删除/改名的后端时，它的 optgroup 根本不存在，浏览器会
  // 静默选中第一项——用户以为还配着，一保存就被改成别的后端，而且卡片上
  // 看不出发生过什么。补一条占位，让"已不存在"这件事写在脸上。
  //
  // 但「只写了模型名、没写后端」是**合法**配置（预问诊的维语翻译模型就是这样：
  // 交给网关切点按模型名路由）。这种值不加后缀——模型名本身就够长，再缀上
  // 「按模型名路由」在窄列里会被截成「（按…」，看着像渲染坏了。
  const knownModels = new Set();
  for (const m of (meta || [])) {
    for (const it of (m.models || [])) {
      const id = String((it && it.id) || "").trim();
      if (id) knownModels.add(id);
    }
    for (const id of (m.probed || [])) {
      const s = String(id || "").trim();
      if (s) knownModels.add(s);
    }
  }
  let orphan = "";
  if (ref && !(providers || []).includes(curBase)) {
    const bareModel = !curModel && knownModels.has(curBase);
    orphan = `<option value="${esc(ref)}" selected>${esc(ref)}${
      bareModel ? "" : "（已不存在）"}</option>`;
  }

  return head + orphan + groups;
}

/* 站点卡：和预问诊卡同一个形态——卡上只留"现在是什么样"和几个动作，
 * 全部字段收进「模型与参数」弹窗。
 *
 * 之前是十几个输入框平铺在卡上：一个站点就要滚两屏、两个站点三屏，而其中
 * 九成的字段一年改不到一次（示例问题、免责声明、另一语言那套文案）。天天
 * 要看的只有三件事：跑没跑、绑的哪个库、用的是哪套模型。
 */
/* 角色值存的是「后端::模型」（见 roles.SEP），那是配置语法，不该出现在界面上。
   显示时换成「后端 / 模型」，一眼能看出"哪个服务上的哪个模型"。 */
function roleText(v) {
  return String(v || "-").replace("::", " / ");
}

async function openPrevisitPrompt() {
  const cfg = state.pvCfg || await api("/api/previsit/config");
  state.pvCfg = cfg;
  const value = String((cfg.values || {})["prompts.chat"] || "");
  showModal("预问诊提示词", `<textarea id="pvPromptEditor" rows="18"
    style="width:100%;resize:vertical">${esc(value)}</textarea>`, async () => {
    const text = $("pvPromptEditor").value;
    state.pvCfg = await jsonSend("PUT", "/api/previsit/config", {
      values: { "prompts.chat": text },
    });
    toast("预问诊提示词已保存");
  }, { wide: true, okLabel: "保存" });
}

const LANG_NAME = { ug: "维吾尔语（RTL）", zh: "中文", en: "English" };

/* 站点页把常用配置直接铺在卡片下面，就地修改和保存。
 *
 * 下面的 siteInlinePanels() 复用原弹窗那套 scfg-* 控件：data-f 字段名一个没动，
 * readSiteBlock() 照旧全量读，所以后端接口零改动。 */
function siteBlock(i, meta, plan, defProv, pv, kbc) {
  const running = i.status === "running";
  const external = i.status === "external";
  const busy = i.status === "starting" || i.status === "stopping";
  const shown = i.content_lang && i.content_lang !== i.lang
    ? `<span class="sbk-dim warn" title="页面语言按文案内容判定，下拉只在判不出时兜底"
        >实际显示：${esc(LANG_NAME[i.content_lang] || i.content_lang)}</span>`
    : "";
  // 状态是卡片自己的事：一个站点一张卡，卡头把名字、状态、时长、端口、
  // URL 和操作一次说完。以前顶部另摆一张「当前运行」表，同一站点出现两遍。
  const metaBits = [
    `<span class="sbk-port">:${esc(String(i.port || ""))}</span>`,
    `<span class="sbk-state"><i class="dot ${siteStatusDot(i.status)}"></i><b${
      running ? " class=is-on" : ""}>${esc(siteStatusWord(i.status))}</b>${
      siteStatusTail(i) ? `<span>${esc(siteStatusTail(i))}</span>` : ""}</span>`,
    i.public ? `<span>局域网可访问</span>` : "",
    shown,
  ].filter(Boolean);

  /* 一段说明折成一行，点开看全文。自检/备注是"要看时才看"的信息，
   * 维吾尔文样本还带 RTL 混排，默认全展开会把一张卡撑成半屏。 */
  const foldNote = (label, text, cls = "", forceOpen = false) => {
    const full = String(text || "");
    if (!full) return "";
    const long = full.length > 78;
    const brief = long ? full.slice(0, 78) + "…" : full;
    return `<details class="sbk-note-line${cls ? " " + cls : ""}"${
      !long || forceOpen ? " open" : ""}>
        <summary><span class="sbk-note-tag">${esc(label)}</span>${esc(brief)}</summary>
        ${long ? `<div class="sbk-note-full">${esc(full)}</div>` : ""}
      </details>`;
  };
  /* 正常态的维吾尔语自检和模型区说的是同一件事，只有失败才占一行。 */
  const ugText = i.ug_capable === false && i.ug_note
    ? i.ug_note + (i.ug_sample ? `（模型返回：${i.ug_sample.slice(0, 40)}）` : "")
    : "";
  const notes = [
    foldNote("维吾尔语自检", ugText, "bad", true),
    foldNote("异常", i.error, "bad", true),
    foldNote("备注", i.note),
  ].filter(Boolean).join("");

  return `<section class="site-block${running || external ? "" : " is-off"}"
      data-name="${esc(i.name)}">
    <div class="sbk-top">
      <div class="sbk-idblock">
        <div class="sbk-id">
          <span class="sbk-name">${esc(i.name)}</span>
          ${metaBits.length
            ? `<span class="sbk-meta">${metaBits.join(" · ")}</span>` : ""}
        </div>
        <a class="sbk-url" href="${esc(i.url)}" target="_blank"
          rel="noreferrer">${esc(i.url)}</a>
      </div>
      <div class="sbk-acts">
        <div class="sbk-act-group is-ops">
          <button class="mini" data-sact="copy" data-url="${esc(i.url)}">复制链接</button>
          ${external ? "" : `<button class="mini${running ? " is-danger" : ""}"
            data-sact="${running ? "stop" : "start"}" ${busy ? "disabled" : ""
            }>${running ? "停止" : "启动"}</button>`}
          ${external ? "" : `<button class="mini" data-sact="restart"
            ${busy ? "disabled" : ""}>重启</button>`}
          <a class="mini loc" href="${esc(i.url)}" target="_blank" rel="noreferrer">打开</a>
        </div>
      </div>
    </div>

    ${siteLiveStrip(i, plan, pv)}
    <div class="site-inline" data-sinline data-kbid="${esc(String(i.kb_id || ""))}">
      ${siteInlinePanels(i, meta, plan, defProv, pv, kbc)}
      <div class="sif-savebar" data-save-state>
        <span class="sif-save-hint">回答行为按模块存（维医库 / 古籍库 / previsit.yaml），其余存 instances.yaml；改完即生效</span>
        <span class="sif-save-msg">就绪</span>
      </div>
    </div>

    ${notes ? `<div class="sbk-foot">${notes}</div>` : ""}
  </section>`;
}

/* 站点页的内联配置区（2026-11-13 打平）。
 *
 * 原来这三段是弹窗里的三个页签，一次只看得到一段；现在三段同时铺在页面上，
 * 谁都不用点开。模型仍是一张矩阵：行顺序就是角色的生效顺序
 * （板块 → 站点 → 系统 → 实例 → 默认），留空代表跟随上一级。
 *
 * 关键约束：**所有 data-f 字段名一个都不改**。readSiteBlock() 遍历站点卡内的
 * [data-f] 组 payload，三段都在同一个 DOM 里，保存一次全量提交，后端零改动。
 */
function siteInlinePanels(i, meta, plan, defProv, pv, kbc) {
  const d = state.sites || {};
  const roles = i.roles || {};
  const classicCur = i.classic_kb_id || 0;
  const lo = d.port_range ? d.port_range[0] : 8800;
  const hi = d.port_range ? d.port_range[1] : 8899;

  const sec = (key) => (i.sections && i.sections[key]) || {};
  const secOn = (key) => sec(key).enabled !== false;
  const sel = (field, role, cur, title, headLabel) =>
    `<select data-f="${field}" title="${esc(title)}">${
      provSelect(role, cur, d.providers, meta, defProv,
        headLabel || (field.startsWith("sec_") ? "跟随站点" : "跟随上级"))}</select>`;

  // 一行字段：标签在左、控件在右，说明放在控件下方，不再往控件行里塞小字。
  /* ---------- 01 入口与模型（2026-11-22 加提示词列）----------
   * 一行 = 一个入口：翻译模型 / 推理模型 / 提示词。提示词先前缩在表下一排
   * 小链接里（「角色提示词 · 维医板块 · 古籍板块 · 预问诊」），四件事挤成一条，
   * 想改古籍的提示词得先记住它对应上面第几行。现在按钮就在自己那一行的行尾，
   * 点哪行配哪行，不用在两张表之间对号。
   *
   * 入口开关、显示名、检索库仍然不在表里露面，但字段不能从 DOM 里凭空消失：
   * readSiteBlock 是全量提交，后端 sections 又是整份替换（coerce_sections 对缺的
   * enabled 回落成「全开」、name 回落成空串）。少一个 sec_*_enabled，用户改一次
   * 模型，被关掉的入口就自己回来了。所以把被移除的字段原样塞进 .et-keep 隐藏区
   * ——payload 一行不改，后端零改动。data-f 字段名一个没动。 */
  const pvPair = pvRolePair(pv);
  const row = (cells, cls = "") => `<tr class="${cls}">${cells}</tr>`;
  const keep = (f, v) => `<input type="hidden" data-f="${f}" value="${esc(v)}">`;
  const keepChk = (f, on) =>
    `<input type="checkbox" data-f="${f}"${on ? " checked" : ""} tabindex="-1">`;
  const keepFields = `<div class="et-keep" hidden aria-hidden="true">
      ${keep("kb_id", i.kb_id || 0)}
      ${keep("classic_kb_id", classicCur)}
      ${["med", "classic", "pv"].map((k) =>
        keepChk(`sec_${k}_enabled`, secOn(k)) + keep(`sec_${k}_name`, sec(k).name || "")
      ).join("")}
    </div>`;
  /* 行内只放名字 + 一行小字，和模型页的清单行（名字 / 元信息）一模一样：
     徽章、色块这类"另一套语言"不进这张表。 */
  const entryCell = (label, note = "留空跟随站点默认") => `<td class="et-name">
      <b>${esc(label)}</b><span>${esc(note)}</span></td>`;
  const defaultCell = entryCell("站点默认", "其它入口留空时继承这里");

  /* 行尾那格：提示词。按钮的锚点各不一样——站点默认行开的是「角色提示词」
   * （按后端存），三个板块行各开自己 scope 的抽屉，预问诊开它自己那份
   * previsit.yaml。文字统一写「配置」，列头已经说明了这一列是什么。 */
  const promptCell = (attr) =>
    `<td class="et-prompt"><button class="mini" type="button" ${attr}>配置</button></td>`;
  const cell = (label, note = "留空跟随站点默认") => entryCell(label, note);

  const entryPanel = `
    <div class="et-wrap">
      ${keepFields}
      <table class="et-table">
        <thead><tr>
          <th>入口</th><th>翻译模型</th><th>推理模型</th>
          <th class="et-th-act">提示词</th>
        </tr></thead>
        <tbody>
          ${row(`${defaultCell}
            <td>${sel("role_translate", "translate", roles.translate,
              "先把维吾尔语提问译成中文去检索，再把中文答案译回去")}</td>
            <td>${sel("role_generate", "generate", roles.generate, "读检索到的资料组织答案")}</td>
            ${promptCell("data-open-global-prompt")}`, "scfg-mrow-default")}
          ${row(`${cell("维医问答")}
            <td>${sel("sec_med_role_translate", "translate", (sec("med").roles || {}).translate,
              "维医问答的翻译模型；留空跟随站点默认")}</td>
            <td>${sel("sec_med_role_generate", "generate", (sec("med").roles || {}).generate,
              "维医问答的推理模型；留空跟随站点默认")}</td>
            ${promptCell('data-open-prompt="med"')}`)}
          ${row(`${cell("古籍问答")}
            <td>${sel("sec_classic_role_translate", "translate", (sec("classic").roles || {}).translate,
              "古籍问答的翻译模型；留空跟随站点默认")}</td>
            <td>${sel("sec_classic_role_generate", "generate", (sec("classic").roles || {}).generate,
              "古籍问答的推理模型；留空跟随站点默认")}</td>
            ${promptCell('data-open-prompt="classic"')}`)}
          ${row(`${cell("预问诊")}
            <td>${sel("pv_role_translate", "translate", pvPair.translate,
              "预问诊维语翻译模型", "跟随站点默认")}</td>
            <td>${sel("pv_role_generate", "generate", pvPair.generate,
              "预问诊推理模型", "跟随站点默认")}</td>
            ${promptCell("data-open-prompt=\"previsit\"")}`)}
        </tbody>
      </table>
    </div>
    <div class="et-links">
      <span class="et-links-k">模型自检</span>
      <button class="mini" type="button" data-trtest
        title="发一句真实翻译，验证翻译模型通不通、译得对不对">测试翻译</button>
    </div>
    <div class="tr-out scfg-tr-out" data-trout hidden></div>`;

  /* ---------- 回答行为：站点主体的问答旋钮 ----------
   * 拒答 / 生成温度 / 会话历史这七项，是"怎么答"而不是"这个库装了什么"。
   * 以前它们摆在后台「对话」页——那是检索调试台，不是用户真正提问的地方，
   * 于是出现主次错位：旋钮在测试台，主体网站却用写死的常量。现在回到站点：
   * 站点是主体，这七项就该跟站点走，改完 PATCH 到站点主库，前台网站立刻生效。
   *
   * 值来自 loadSiteKbConfigs 取回的参数快照（按主库 kb_id）。控件用 data-ab
   * 标记（不是 data-f）——它们写进 kb.config_json，不是 instances.yaml，
   * 保存时单独走 /api/kb/{id}/config，不能混进 readSiteBlock 的 payload。 */
  const abKeys = {
    temp: "generation.temperature",
    turns: "chat.history_turns", budget: "chat.history_token_budget",
    refuseOn: "refusal.enabled", refuseScore: "refusal.min_score",
    refuseMargin: "refusal.min_margin", refuseSoft: "refusal.soft_margin",
  };
  const valOf = (vals, key) => {
    const v = (vals || {})[key];
    return v === undefined || v === null ? "" : v;
  };
  /* 一行一个参数：左边名字（下面挂一行说明），右边控件。跟首页「运行状态」
   * 卡里的标签 / 值对齐是同一套，不再「卡里再分块、块里再两列」。 */
  const srow = (label, note, ctl) => `<div class="sif-row">
      <span class="sif-k"><b>${esc(label)}</b>${note ? `<em>${esc(note)}</em>` : ""}</span>
      <span class="sif-v">${ctl}</span>
    </div>`;
  const abInput = (vals, key, attrs) =>
    `<input data-ab="${esc(key)}" ${attrs} value="${esc(valOf(vals, key))}"
      placeholder="跟随默认">`;
  const sw = (attr, on) => `<label class="toggle">
      <input type="checkbox" ${attr}${on ? " checked" : ""}>
      <span class="sw" aria-hidden="true"></span></label>`;

  /* 三个模块各一套参数。维医写 kb_id、古籍写 classic_kb_id、预问诊写
   * previsit.yaml；哪个面板改了只 PATCH 自己那份，互不串值。
   *
   * 为什么非得分开：拒答阈值是"这一库的相似度分布"量出来的。维医标定的
   * 0.35 套到古籍上，要么满屏"资料不够硬"，要么一条都不拒——两个库的分数
   * 分布根本不是一个量级。以前只有一份全局值，等于逼用户二选一。 */
  const kbcfgs = kbc || {};
  const medKbId = Number(i.kb_id) || 0;
  const clsKbId = Number(i.classic_kb_id) || medKbId;
  const medCfg = kbcfgs[medKbId] || null;
  const clsCfg = kbcfgs[clsKbId] || null;
  const medVals = (medCfg && medCfg.values) || {};
  const clsVals = (clsCfg && clsCfg.values) || {};
  const pvCfgVals = (pv && pv.values) || {};
  const calibOf = (cfg) => Boolean(cfg && (cfg.calibrated_keys || []).includes(abKeys.refuseScore));

  const answerRows = (vals, calib) => `
      ${srow("启用拒答", "资料不够硬时说实话，而不是编",
        sw(`data-ab="${abKeys.refuseOn}"`, vals[abKeys.refuseOn] !== false))}
      ${srow("分数阈值", calib ? "已被标定覆盖，手改会覆盖标定值" : "低于它判为资料不够硬",
        abInput(vals, abKeys.refuseScore, 'type="number" min="0" max="1" step="0.005"'))}
      ${srow("区分度阈值", "低于它就算分不开，宁可不答",
        abInput(vals, abKeys.refuseMargin, 'type="number" min="0" max="0.5" step="0.005"'))}
      ${srow("低置信提醒", "命中这个区间只在答案上挂提示",
        abInput(vals, abKeys.refuseSoft, 'type="number" min="0" max="0.5" step="0.005"'))}
      ${srow("生成温度", "越高越发散，引用编号越易漂",
        abInput(vals, abKeys.temp, 'type="number" min="0" max="2" step="0.05"'))}
      ${srow("历史轮数", "多轮对话带几轮上文",
        abInput(vals, abKeys.turns, 'type="number" min="0" max="20" step="1"'))}
      ${srow("历史预算", "字符 / token 上限",
        abInput(vals, abKeys.budget, 'type="number" min="0" max="8000" step="100"'))}`;

  /* 预问诊不走检索：没有召回分，也就没有拒答阈值；会话状态由问诊流程自己
   * 管，不是聊天历史。它真正可调的是怎么生成，所以只给两项，别摆一排
   * 点了不起作用的旋钮。 */
  const pvRows = `
      ${srow("生成温度", "脚本化追问，温度高容易跑题",
        `<input data-ab="temperature" type="number" min="0" max="2" step="0.05"
          value="${esc(valOf(pvCfgVals, "temperature"))}" placeholder="跟随默认">`)}
      ${srow("单次上限", "一次回复的 token 上限，超出会被截断",
        `<input data-ab="max_tokens" type="number" min="64" max="4096" step="32"
          value="${esc(valOf(pvCfgVals, "max_tokens"))}" placeholder="跟随默认">`)}`;

  const abPanel = (panel, src, note, body) => `<div class="sif-flat ab-panel"
      data-abpanel="${panel}" data-absrc="${esc(src)}"${panel === "med" ? "" : " hidden"}>
      ${note ? `<div class="sif-note">${esc(note)}</div>` : ""}
      ${body}
    </div>`;

  /* 面板顶上那句灰字说清"这份参数存在哪、谁在用"——三个面板长得一模一样，
   * 不说清楚就只能靠猜。古籍没独立库时（classic_kb_id 为空）它跟维医共用
   * 同一份，这时得直说，否则用户改完维医、回头看古籍"也变了"会当成 bug。 */
  const classicShare = !Number(i.classic_kb_id) || clsKbId === medKbId;
  const answerPanel = `
    <div class="tabs-bar ab-tabs" data-abtabs>
      <button type="button" data-abtab="med" class="active">维医问答</button>
      <button type="button" data-abtab="classic">古籍问答</button>
      <button type="button" data-abtab="pv">预问诊</button>
    </div>
    ${abPanel("med", `kb:${medKbId}`,
      `存维医检索库（#${medKbId}）；前台维医问答与后台调试对话共用这一份。`,
      answerRows(medVals, calibOf(medCfg)))}
    ${abPanel("classic", `kb:${clsKbId}`,
      classicShare
        ? `古籍没有独立检索库，参数跟维医是同一份（#${medKbId}）。`
        : `存古籍检索库（#${clsKbId}）；古籍题按这里的阈值判资料够不够硬。`,
      answerRows(clsVals, calibOf(clsCfg)))}
    ${abPanel("pv", "pv",
      "预问诊是脚本化问诊，不走检索，所以只有生成参数；它的提示词在 01 段「预问诊」那行里改。",
      pvRows)}`;

  /* ---------- 页 3：站点设置 ---------- */
  /* 这一页只留「运行」三件事（端口 / 监听 / 开机）。
   *
   * 语言、语料、产出方式原本也摆在这里，问题不是"多了一张卡"，而是**同一份
   * 配置有两处落点**：真实值在 config/instances.yaml 的 lang / kb_lang /
   * answer_mode，这一页又摆一遍、保存时全量 PUT 回去。页面上改完、文件里没变
   * （或反过来），下次重启又翻回旧值，谁也不知道以哪个为准。这类部署时定一次
   * 的东西只留配置文件一个落点（instcfg 每次请求现读，改完不用重启）。
   */
  /* 运行三项也走同一套行：左边名字 + 说明，右边控件。之前并排三张小卡，
     和另外两段的"清单"不是一个东西，摆在一起就显出自造语言。 */
  const sitePanel = `
    <div class="sif-flat">
      ${srow("端口", "改这里会重启站点",
        `<input data-f="port" type="number" min="${lo}" max="${hi}" value="${i.port}">`)}
      ${srow("监听", "只监听回环时外部访问不到", sw('data-f="public"', i.public))}
      ${srow("开机", "关掉后每次都要手动启动", sw('data-f="autostart"', i.autostart))}
    </div>`;

  /* 段头就是全站通用的 .sec-head（h3 + hint），和模型页「本地模型 / 外部模型」
     一个字不差；不再有 01/02/03 大方块、左说明栏这些只有这一页才有的东西。 */
  const section = (key, title, sub, body) =>
    `<section class="sif-sec" data-sif="${key}">
      <header class="sec-head"><h3>${esc(title)}</h3><span class="hint">${esc(sub)}</span><span class="sp"></span></header>
      <div class="sif-body">${body}</div>
    </section>`;

  return `
    ${section("entries", "入口与模型", "每个入口的翻译 / 推理模型与提示词", entryPanel)}
    ${section("answer", "回答行为", "按模块各自一套：拒答、生成温度、会话历史", answerPanel)}
    ${section("site", "运行设置", "端口、监听、开机；语言与产出方式改 config/instances.yaml", sitePanel)}`;
}

/* 站点配置即时保存：回答行为按模块分别落盘（维医/古籍各 PATCH 自己的库，
 * 预问诊合并进 previsit.yaml），其余写 instances.yaml / previsit；几处各走
 * 各的接口，互不污染。
 * 没有保存按钮——控件 change 后由 queueSiteSave 防抖调用。
 * 成功只更新卡内状态条，不整页重绘（重绘会打断连续编辑、丢焦点）。
 * 只有端口改动触发重启时才重绘一次，那时卡片状态本来就变了。 */
async function saveSiteInline(name, block, dirtyPanels) {
  const state = block.querySelector("[data-save-state] .sif-save-msg");
  const payload = readSiteBlock(block);
  const inline = block.querySelector("[data-sinline]") || block;

  // 回答行为：每个模块一个面板（data-absrc = kb:<id> | pv），各扫各的
  // [data-ab] 组成一份 patch。不能像以前那样整卡扫一遍——同一组键在三个
  // 面板里各有一份，混成一份就等于把古籍的阈值写到维医库上。
  // 数字控件空着 = 没填，不当成 0（0 是合法值，但空是"不动"）。
  const abPatches = [];
  inline.querySelectorAll("[data-absrc]").forEach((panel) => {
    const src = String(panel.dataset.absrc || "");
    if (!src) return;
    // 只写这次真正改到的那个模块：一次改动顺手把另外两个库也覆盖一遍，
    // 既多余又会互相踩（预问诊的 config_save 会 reload_engine，无关的写
    // 会打断正在进行的问诊）。
    if (dirtyPanels && !dirtyPanels.has(src)) return;
    const vals = {};
    panel.querySelectorAll("[data-ab]").forEach((el) => {
      const key = el.dataset.ab;
      if (!key) return;
      if (el.type === "checkbox") { vals[key] = el.checked; return; }
      if (el.value === "" || el.value === null) return;
      const n = Number(el.value);
      vals[key] = Number.isFinite(n) ? n : el.value;
    });
    if (Object.keys(vals).length) abPatches.push({ src, vals });
  });

  const pvTranslate = payload.pv_role_translate;
  const pvGenerate = payload.pv_role_generate;
  delete payload.pv_role_translate;
  delete payload.pv_role_generate;
  const r = await jsonSend("PUT", `/api/instances/${encodeURIComponent(name)}`, payload);
  const values = {};
  if (pvTranslate !== undefined) {
    const [provider, model] = splitRoleValue(pvTranslate);
    values.translator_provider = provider;
    values.translator_model = model;
  }
  if (pvGenerate !== undefined) {
    const [provider, model] = splitRoleValue(pvGenerate);
    values.provider = provider;
    values.model = model;
  }
  // 预问诊面板的参数也进同一次 PUT，别为了一个温度多发一个请求。
  for (const p of abPatches) {
    if (p.src !== "pv") continue;
    Object.assign(values, p.vals);
  }
  if (Object.keys(values).length) await jsonSend("PUT", "/api/previsit/config", { values });
  for (const p of abPatches) {
    if (!p.src.startsWith("kb:")) continue;
    const id = Number(p.src.slice(3)) || 0;
    if (id) await jsonSend("PATCH", `/api/kb/${id}/config`, { values: p.vals });
  }
  if (state) {
    state.textContent = r.restarted ? "已重启" : "已生效";
    state.className = "sif-save-msg ok";
  }
  if (r.restarted) {
    await renderSites();
    toast(`${name} 已重启并生效`);
  }
  return r;
}

function splitRoleValue(value) {
  const raw = String(value || "").trim();
  const separator = raw.indexOf("::");
  if (separator < 0) return [raw, ""];
  return [raw.slice(0, separator), raw.slice(separator + 2)];
}

/* 站点页所有内联交互都委托给 #viewSite。容器元素本身不随重绘更换（只换
 * innerHTML），所以这里只绑一次；用 dataset 守卫挡住重复绑定。 */
function bindSiteInlineEvents() {
  const box = $("viewSite");
  if (box.dataset.inlineBound) return;
  box.dataset.inlineBound = "1";

  box.addEventListener("click", async (e) => {
    const card = e.target.closest(".site-block");

    // 回答行为的分模块页签：只切显示，不发请求——三个面板的值都已经在 DOM
    // 里，切回去还能看到刚才填的（切页签不等于保存，保存是控件 change）。
    const abtab = e.target.closest("[data-abtab]");
    if (abtab && card) {
      const key = abtab.dataset.abtab;
      card.querySelectorAll("[data-abtab]").forEach((b) =>
        b.classList.toggle("active", b === abtab));
      card.querySelectorAll("[data-abpanel]").forEach((p) => {
        p.hidden = p.dataset.abpanel !== key;
      });
      return;
    }

    // 长文本仍走提示词抽屉，但入口直接摆在页面上，且不用先关一层弹窗。
    if (e.target.closest("[data-open-global-prompt]")) {
      await openPromptDrawer("");
      return;
    }
    if (e.target.closest('[data-open-prompt="previsit"]')) {
      await openPrevisitPrompt();
      return;
    }
    const op = e.target.closest("[data-open-prompt]");
    if (op && card) {
      const key = op.dataset.openPrompt || "";
      const gen = card.querySelector(`[data-f="sec_${key}_role_generate"]`);
      const base = String(gen && gen.value ? gen.value.split("::")[0] : "");
      await openPromptDrawer(base, key);
      return;
    }

    // 翻译模型单测：输出框与下拉都限在本张卡内，多站点时互不串。
    const tr = e.target.closest("[data-trtest]");
    if (tr && card) {
      const sel = card.querySelector('[data-f="role_translate"]');
      const out = card.querySelector("[data-trout]");
      if (!out) return;
      tr.disabled = true;
      out.hidden = false;
      out.innerHTML = `<span class="hint">翻译中…（整段往返一次，云端可能要几秒）</span>`;
      try {
        const r = await jsonPost("/api/translate-test", { provider: sel ? sel.value : "" });
        // 必须写实际模型：本地后端遇到不存在的模型名不报错，只静默回落。
        const base = String(r.provider || "").split("::")[0];
        const meta = [base, r.model, `${r.src}→${r.dst}`, `${r.latency_ms}ms`]
          .filter(Boolean).join(" · ");
        if (r.warn) {
          out.innerHTML = `<div class="warn">${esc(r.warn)}</div>
            <div class="tr-ok" dir="auto">${esc(r.output || "（无输出）")}</div>
            <div class="hint">${esc(meta)}</div>`;
        } else if (r.ok) {
          out.innerHTML = `<div class="tr-ok" dir="auto">${esc(r.output)}</div>
            <div class="hint">${esc(meta)}</div>`;
        } else {
          out.innerHTML = `<div class="warn">翻译失败：${esc(r.error || "输出与原文相同或为空")}</div>
            <div class="tr-ok" dir="auto">${esc(r.output || "（无输出）")}</div>
            <div class="hint">${esc(meta)}</div>`;
        }
      } catch (err) {
        out.innerHTML = `<span class="warn">${esc(err.message)}</span>`;
      } finally {
        tr.disabled = false;
      }
    }
  });

  box.addEventListener("change", (e) => {
    const el = e.target;
    // data-f / data-ab 是站点配置控件：改动即写回，连续改动合并成一次请求。
    // 入口开关等值由 .et-keep 里的隐藏字段原样保活，不参与交互，也不会 change。
    if (el.matches("[data-f], [data-ab]")) {
      const card = el.closest(".site-block");
      if (!card) return;
      // 标脏：回答行为按模块存，改一个模块不该把另外两个库也写一遍。
      const panel = el.closest("[data-absrc]");
      if (panel) {
        card._abDirty = card._abDirty || new Set();
        card._abDirty.add(panel.dataset.absrc);
      }
      queueSiteSave(card);
    }
  });
}

/* 即时保存的防抖队列。change 已经是「这个值定了」的语义，260ms 只用来
 * 合并连点（比如一口气切三行下拉），不让半填的输入被当成最终值发出去。
 * 每个站点一条队列：多站点各自保存，互不取消。 */
const _siteSaveTimers = new Map();
function queueSiteSave(card) {
  const name = card.dataset.name;
  const state = card.querySelector("[data-save-state] .sif-save-msg");
  if (state) {
    state.textContent = "保存中…";
    state.className = "sif-save-msg busy";
  }
  clearTimeout(_siteSaveTimers.get(name));
  const timer = setTimeout(async () => {
    _siteSaveTimers.delete(name);
    // 先把脏集合取走再清空：保存期间又改了别的模块，那一份会重新标脏、
    // 重新排队，不会被这次一起吞掉。
    const dirty = card._abDirty ? new Set(card._abDirty) : new Set();
    card._abDirty = null;
    try {
      await saveSiteInline(name, card, dirty);
    } catch (err) {
      toast(err.message, "err");
      const s = card.querySelector("[data-save-state] .sif-save-msg");
      if (s) {
        s.textContent = "保存失败";
        s.className = "sif-save-msg err";
      }
    }
  }, 260);
  _siteSaveTimers.set(name, timer);
}

/* 把一张卡上的控件读成一份完整 payload。
   全量发而不是只发变化的那一个：PUT 的语义是"这份配置长这样"，增量补丁
   一旦漏掉某个字段，后端就会用 keep() 拿旧值，两边悄悄错开。字段总共十几个，
   一次请求几百字节，不值得为省这点带宽换来不一致的风险。 */
function readSiteBlock(block) {
  const out = { roles: {}, alt: {} };
  const split = (v) => v.split("\n").map((s) => s.trim()).filter(Boolean);
  block.querySelectorAll("[data-f]").forEach((el) => {
    const f = el.dataset.f;
    // 板块六个控件合成一份 sections，整份发。缺键会被后端当成"没配"而回落到
    // 全开——用户刚关掉的入口就自己回来了，这比不保存更莫名其妙。
    if (f.startsWith("sec_")) {
      const m = /^sec_(med|classic|pv)_(enabled|name)$/.exec(f);
      if (m) {
        out.sections = out.sections || { med: {}, classic: {}, pv: {} };
        out.sections[m[1]][m[2]] = el.type === "checkbox" ? el.checked : el.value.trim();
        return;
      }
      // 板块级角色：sec_<key>_role_<role>。空值 = 跟随站点，不写进 roles。
      const rm = /^sec_(med|classic)_role_(translate|generate)$/.exec(f);
      if (rm) {
        if (el.value) {
          out.sections = out.sections || { med: {}, classic: {}, pv: {} };
          out.sections[rm[1]].roles = out.sections[rm[1]].roles || {};
          out.sections[rm[1]].roles[rm[2]] = el.value;
        }
        return;
      }
      return;
    }
    if (el.type === "checkbox") { out[f] = el.checked; return; }
    if (f === "port" || f === "kb_id" || f === "classic_kb_id") {
      out[f] = Number(el.value) || 0; return;
    }
    if (f === "samples") { out[f] = split(el.value); return; }
    if (f.startsWith("alt_")) {
      // 整套 alt 一次发全：PUT 是"这份配置长这样"，只发改过的那个键，
      // coerce_alt 会把没发的键当成"没配"而丢掉，另一语言就只剩一项。
      const k = f.slice(4);
      if (k === "samples") { const v = split(el.value); if (v.length) out.alt.samples = v; return; }
      const v = el.value.trim();
      if (v) out.alt[k] = v;
      return;
    }
    if (f.startsWith("role_")) {
      // 空值 = 跟随系统，不要写进配置，否则 resolver 会拿一个空名字去查
      if (el.value) out.roles[f.slice(5)] = el.value;
      return;
    }
    out[f] = el.value;
  });
  if (!out.alt.lang) delete out.alt;
  return out;
}

/* 站点页要显示「回答行为」，而它按知识库存、不随实例下发；2026-11-22 起
 * 按模块分，所以**两个库都要取**：维医 = kb_id、古籍 = classic_kb_id。
 * 返回值按 kb_id 索引，一个库一份快照；失败降级成 null，界面显示占位。
 * 并发取，取不到不拖垮整页。 */
async function loadSiteKbConfigs(d) {
  const ids = new Set();
  for (const i of ((d && d.items) || [])) {
    const med = Number(i.kb_id) || 0;
    const cls = Number(i.classic_kb_id) || 0;
    if (med) ids.add(med);
    if (cls) ids.add(cls);
  }
  const out = {};
  await Promise.all([...ids].map(async (id) => {
    try { out[id] = await api(`/api/kb/${id}/config`); }
    catch { out[id] = null; }
  }));
  return out;
}

function paintSites(d, roles, meta, pv, kc) {
  const data = d || state.sites;
  if (!data) return;
  state.sites = data;
  state.rolePlan = roles;
  state.modelMeta = meta || [];
  state.pvCfg = pv || null;
  state.siteKbCfgs = kc || {};
  const items = data.items || [];
  const running = items.filter((i) => i.status === "running").length;
  // 外部托管的站点是**能访问**的，只是不归这个后台管。把它算成"没在跑"，
  // 页面顶部就会写着"0 个运行中"，而下面那张卡明明白白给了个能打开的链接。
  const external = items.filter((i) => i.status === "external").length;
  $("pageTitle").textContent = "前台站点";
  $("pageSub").textContent = items.length
    ? `${items.length} 个站点 · ${running} 个运行中`
      + (external ? ` · ${external} 个由别的后台托管` : "")
    : "还没有站点";
  $("pageActions").innerHTML =
    `<button class="primary-btn" id="btnNewSite">＋ 新建站点</button>`;

  const plans = (roles && roles.sites) || [];
  $("viewSite").innerHTML = `<div class="page">
    ${items.length
      ? items.map((i) => siteBlock(i, meta, plans.find((p) => p.name === i.name),
        "", pv, state.siteKbCfgs || {})).join("")
      : `<div class="card"><p class="hint" style="margin:6px 0 12px">还没有前台站点。</p></div>`}
  </div>`;

  $("btnNewSite").onclick = () => openSiteEditor(null);
  // datalist 里的模型候选要按当前 provider 填一次，否则下拉是空的。
  const box = $("viewSite");
}

/* 切页加速三件事：① 不撕掉上次的卡片（只有从没画过才给骨架）；
   ② 三份数据并发取，原来是 /instances → /model-roles → /models 串成一串；
   ③ seq 守卫，快速 A→B→A 时先发出的那次回来也不能盖掉后一次的内容。 */
let _siteRenderSeq = 0;
async function renderSites() {
  const box = $("viewSite");
  const seq = ++_siteRenderSeq;
  if (!box.dataset.ready) {
    box.innerHTML = `<div class="page"><div class="card">
      <div class="card-head"><h3>正在加载…</h3></div></div></div>`;
  }
  let d, r, meta, pv, kc;
  try {
    // 实例列表先取：回答行为七项按知识库存，要用列表里的 kb_id 才能取到
    // 每个站点主库的参数快照。其余四份并发，取不到各自降级、不拖垮整页。
    d = await api("/api/instances");
    [r, meta, pv, kc] = await Promise.all([
      api("/api/model-roles").catch(() => null),
      api("/api/models").then((x) => (x && x.providers) || []).catch(() => []),
      api("/api/previsit/config").catch(() => null),
      loadSiteKbConfigs(d),
    ]);
  } catch (err) {
    if (seq !== _siteRenderSeq) return;
    if (!box.dataset.ready) {
      box.innerHTML = `<div class="page"><div class="card">
        <div class="card-head"><h3>加载失败</h3></div>
        <p class="hint">${esc(err.message)}</p></div></div>`;
    }
    return;
  }
  if (seq !== _siteRenderSeq) return;
  paintSites(d, r, meta, pv, kc);
  bindSiteActions();
  box.dataset.ready = "1";
}

function bindSiteActions() {
  const box = $("viewSite");
  bindSiteInlineEvents();
  // 运行类按钮绑在容器上：卡片每次重绘都会换掉 DOM，绑在按钮上就得每次重绑。
  box.onclick = async (e) => {
    const btn = e.target.closest("[data-sact]");
    if (!btn) return;
    const act = btn.dataset.sact;
    if (act === "copy") {
      const url = btn.dataset.url;
      try {
        await navigator.clipboard.writeText(url);
        toast("已复制链接");
      } catch {
        toast(url, "warn");
      }
      return;
    }
    const block = btn.closest(".site-block");
    const name = block ? block.dataset.name : "";
    btn.disabled = true;
    try {
      const st = await jsonSend("POST", `/api/instances/${encodeURIComponent(name)}/${act}`, {});
      toast(`${name} ${act === "start" ? "已启动" : act === "stop" ? "已停止" : "已重启"}`
        + (st.status === "error" ? `（${st.error}）` : ""));
      await renderSites();
    } catch (err) {
      toast(err.message, "err");
      btn.disabled = false;
    }
  };
}

async function deleteSite(name) {
  // 站点已收敛成唯一一个，界面上不再提供删除入口；保留函数是为了脚本/调试
  // 直接调用时行为不变（真的删掉会连带停掉那个端口上的服务）。
  const spec = (state.sites.items || []).find((i) => i.name === name);
  await showModal(`删除站点「${name}」`, `
    <p>端口 <b>${spec ? spec.port : "?"}</b> 上的服务会被停止，配置从
      <code>config/instances.yaml</code> 移除。</p>
    <p class="hint">知识库本身不受影响——删站点只是撤掉这个入口。</p>`,
    async () => {
      await jsonSend("DELETE", `/api/instances/${encodeURIComponent(name)}`);
      await renderSites();
    }, { okLabel: "删除", danger: true });
}

/* 站点级角色分工。"跟随系统"是空值：不写就等于交给系统 roles 去定。 */
function roleOptions(cur, role, providers) {
  const picked = (cur && cur.roles ? cur.roles[role] : "") || "";
  return `<option value="">跟随系统角色配置</option>`
    + (providers || []).map((p) =>
      `<option value="${esc(p)}"${picked === p ? " selected" : ""}>${esc(p)}</option>`).join("");
}

async function openSiteEditor(cur) {
  const d = state.sites;
  if (!d) return;
  let suggested = cur ? cur.port : null;
  if (!suggested) {
    try {
      suggested = (await api("/api/instances/next-port")).port;
    } catch {
      suggested = 8801;
    }
  }
  const kbOptions = (d.kbs || []).map((k) =>
    `<option value="${k.id}" ${cur && cur.kb_id === k.id ? "selected" : ""
    }>${esc(k.name)}</option>`).join("");
  const classicOptions = `<option value="0">不单独分库（主库里按标题分）</option>`
    + (d.kbs || []).map((k) =>
      `<option value="${k.id}" ${cur && cur.classic_kb_id === k.id ? "selected" : ""
      }>${esc(k.name)}</option>`).join("");
  const provOptions = `<option value="">跟随后台默认</option>`
    + (d.providers || []).map((p) =>
      `<option value="${esc(p)}" ${cur && cur.provider === p ? "selected" : ""}>${esc(p)}</option>`).join("");

  showModal(cur ? `编辑站点 · ${cur.name}` : "新建站点", `
    <div class="form-grid">
      <div class="form-row"><label>名称（小写字母/数字/-，写进配置文件）</label>
        <input id="sName" value="${cur ? esc(cur.name) : ""}" placeholder="uyghur-med"
          ${cur ? "disabled" : ""}>
        <span class="hint" id="sNameTip"></span></div>
      <div class="form-row"><label>知识库（维医问答 / 预问诊的主库）</label>
        <select id="sKb">${kbOptions}</select></div>
      <div class="form-row"><label>古籍问答用的知识库（可选）</label>
        <select id="sClassicKb">${classicOptions}</select>
        <span class="hint">独立库时古籍问答只在这里检索；不分库则按主库标题是否含「古籍」分流。</span></div>
      <div class="form-row"><label>端口</label>
        <input id="sPort" type="number" value="${suggested}"></div>
      <div class="form-row"><label>界面与回答语言</label>
        <select id="sLang">
          <option value="ug" ${cur && cur.lang === "ug" ? "selected" : ""}>维吾尔语（RTL）</option>
          <option value="zh" ${cur && cur.lang === "zh" ? "selected" : ""}>中文</option>
          <option value="en" ${cur && cur.lang === "en" ? "selected" : ""}>English</option>
        </select></div>
      <div class="form-row"><label>知识库内容的语言（决定要不要翻译后再检索）</label>
        <select id="sKbLang">
          <option value="zh" ${cur && (cur.kb_lang || "zh") === "zh" ? "selected" : ""}>中文语料（维吾尔语提问会先译成中文）</option>
          <option value="ug" ${cur && cur.kb_lang === "ug" ? "selected" : ""}>维吾尔语语料（直接检索，不翻译）</option>
          <option value="en" ${cur && cur.kb_lang === "en" ? "selected" : ""}>英语语料</option>
        </select></div>
      <div class="form-row"><label>怎么产出维吾尔语答案</label>
        <select id="sMode">
          <option value="translate" ${!cur || (cur.answer_mode || "translate") === "translate" ? "selected" : ""}>先用中文组织答案，再整段译成维吾尔语（质量稳，慢一倍）</option>
          <option value="direct" ${cur && cur.answer_mode === "direct" ? "selected" : ""}>模型直接用维吾尔语回答（快，7B 级模型容易跑偏）</option>
        </select></div>
      <div class="form-row"><label>模型后端（不分工时的兜底）</label>
        <select id="sProv">${provOptions}</select></div>
      <div class="form-row"><label>翻译用哪个模型</label>
        <select id="sRoleTr">${roleOptions(cur, "translate", d.providers || [])}</select></div>
      <div class="form-row"><label>输出（作答）用哪个模型</label>
        <select id="sRoleGen">${roleOptions(cur, "generate", d.providers || [])}</select></div>
      <div class="form-row"><label>板块</label>
        <span class="hint">三个板块默认全开，建好后到「模型与参数」→ 板块里改名或关掉。</span></div>
      <div class="form-row"><label class="toggle">
        <input type="checkbox" id="sAuto" ${cur && cur.autostart ? "checked" : ""}>
        <span class="sw" aria-hidden="true"></span><span class="toggle-label">后台启动时自动拉起</span>
      </label></div>
      <div class="form-row"><label class="toggle">
        <input type="checkbox" id="sPublic" ${cur && cur.public ? "checked" : ""}>
        <span class="sw" aria-hidden="true"></span>
        <span class="toggle-label">监听 0.0.0.0（局域网可访问，仅内网使用）</span>
      </label></div>
    </div>`,
    async () => {
      const name = (cur ? cur.name : ($("sName").value || "").trim().toLowerCase());
      if (!name) throw new Error("名称不能为空");
      const payload = {
        name,
        kb_id: Number($("sKb").value),
        classic_kb_id: Number($("sClassicKb").value),
        port: Number($("sPort").value),
        lang: $("sLang").value,
        kb_lang: $("sKbLang").value,
        answer_mode: $("sMode").value,
        provider: $("sProv").value,
        roles: {
          translate: $("sRoleTr").value,
          generate: $("sRoleGen").value,
        },
        autostart: $("sAuto").checked,
        public: $("sPublic").checked,
      };
      const res = await jsonSend("PUT", `/api/instances/${encodeURIComponent(name)}`, payload);
      await renderSites();
      toast(res.restarted ? "已保存并重启生效" : "已保存");
    },
    { okLabel: "保存", wide: true });

  // 语言换了之后文案由提示词配置按语言取，不再需要在这里预填——
  // 那段 fill("sTitle", ...) 依赖的输入框已经随文案一起搬走了。
  $("sName").oninput = () => {
    const v = ($("sName").value || "").trim().toLowerCase();
    const tip = $("sNameTip");
    if (!v) { tip.textContent = ""; return; }
    const ok = /^[a-z][a-z0-9_-]{1,23}$/.test(v);
    const dup = ok && (state.sites.items || []).some((i) => i.name === v);
    tip.textContent = !ok ? "只能用小写字母、数字、- 和 _，2-24 个字符"
      : dup ? `「${v}」已存在，保存会覆盖它` : "";
  };
}
