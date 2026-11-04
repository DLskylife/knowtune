/* 预问诊前端：纯原生 JS，零框架。和主项目的前端保持同一套作风。
 *
 * 三块：① 基本信息表单（配置驱动渲染）② 多轮问诊 ③ 结构化病历。
 * 所有请求走**相对路径**，所以这份页面挂到 /previsit/ 下也照样能用。
 */
(() => {
  const $ = (id) => document.getElementById(id);
  const thread = $("thread");
  const input = $("input");
  const sendBtn = $("send");
  const grid = $("formGrid");

  const state = {
    sessionId: null,
    started: false,
    busy: false,
    meta: null,
    welcome: "",
    record: null,
    shown: 0,          // 已经画进 thread 的 turns 数（增量渲染的游标）
    greeted: false,    // 欢迎语是不是已经画过
    typeTimer: null,   // 打字机定时器：新消息来了要能立刻打断
    typeEl: null,      // 正在出字的那个气泡：被打断时要把它身上的光标去掉
    lastStamp: "",     // 上一条 AI 消息的时间戳：同一分钟内不重复写
    editProfile: false,    // 表单此刻是「补档案」，不是「开新会话」
    supplementing: false,  // 结果页点了继续补充：下一句走 supplement 端点
    lastView: null,        // 最近一次响应，补档案时回填表单用
  };

  // 系统开了"减少动态效果"就不逐字、不淡入了：前庭功能敏感的人会被这类
  // 动画弄晕，而"逐字出现"对他们是纯粹的干扰，不是节奏感。
  const reduceMotion = Boolean(window.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches);

  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));

  async function getJSON(path) {
    const r = await fetch(path);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    return r.json();
  }

  async function sendJSON(method, path, body) {
    const r = await fetch(path, {
      method,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) {
      const d = data.detail;
      throw new Error(typeof d === "string" ? d : `HTTP ${r.status}`);
    }
    return data;
  }
  const postJSON = (path, body) => sendJSON("POST", path, body);
  const patchJSON = (path, body) => sendJSON("PATCH", path, body);

  /* ================================================================ 表单 */
  function fieldHTML(f) {
    const req = f.required ? '<span class="req">*</span>' : "";
    const ph = esc(f.placeholder || "");
    const hint = f.unit ? `<span class="unit">${esc(f.unit)}</span>` : "";
    let control = "";
    if (f.type === "textarea") {
      control = `<textarea id="pf_${esc(f.key)}" rows="2" placeholder="${ph}" maxlength="500"></textarea>`;
    } else if (f.type === "select") {
      const opts = (f.options || []).map(
        (o) => `<option value="${esc(o)}">${esc(o)}</option>`).join("");
      control = `<select id="pf_${esc(f.key)}"><option value="">请选择</option>${opts}</select>`;
    } else if (f.type === "number") {
      const lo = f.min != null ? ` min="${f.min}"` : "";
      const hi = f.max != null ? ` max="${f.max}"` : "";
      control = `<span class="with-unit"><input id="pf_${esc(f.key)}" type="number" inputmode="numeric"${lo}${hi} placeholder="${ph}"><i>${esc(f.unit || "")}</i></span>`;
    } else {
      control = `<input id="pf_${esc(f.key)}" type="text" placeholder="${ph}" maxlength="120" autocomplete="off">`;
    }
    const wide = f.type === "textarea" ? " full" : "";
    return `<div class="field${wide}" data-field="${esc(f.key)}">
      <label for="pf_${esc(f.key)}">${esc(f.label)}${req}</label>
      ${control}${hint}
    </div>`;
  }

  function renderForm(fields) {
    grid.innerHTML = (fields || []).map(fieldHTML).join("");
    grid.addEventListener("input", clearError);
    grid.addEventListener("change", syncVisibility);
    syncVisibility();
  }

  /* show_if：只在依赖字段等于某值时显示（例如选了"女"才问孕育状态）。 */
  function syncVisibility() {
    for (const f of state.meta?.profile_fields || []) {
      if (!f.show_if) continue;
      const wrap = grid.querySelector(`[data-field="${f.key}"]`);
      const dep = grid.querySelector(`#pf_${f.show_if.field}`);
      if (!wrap || !dep) continue;
      const show = String(dep.value || "") === String(f.show_if.value);
      wrap.hidden = !show;
      if (!show) {
        const own = wrap.querySelector("input,select,textarea");
        if (own) own.value = "";
      }
    }
  }

  function clearError() {
    const box = $("formError");
    box.hidden = true;
    box.textContent = "";
  }

  function showError(msg) {
    const box = $("formError");
    box.hidden = false;
    box.textContent = msg;
    box.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  function collectProfile() {
    const out = {};
    for (const f of state.meta?.profile_fields || []) {
      const wrap = grid.querySelector(`[data-field="${f.key}"]`);
      if (!wrap || wrap.hidden) continue;
      const el = document.getElementById(`pf_${f.key}`);
      const v = (el?.value || "").trim();
      if (v) out[f.key] = v;
    }
    return out;
  }

  function validate() {
    for (const f of state.meta?.profile_fields || []) {
      const wrap = grid.querySelector(`[data-field="${f.key}"]`);
      if (wrap?.hidden) continue;
      const el = document.getElementById(`pf_${f.key}`);
      if (f.required && !(el?.value || "").trim()) {
        return `请填写「${f.label}」`;
      }
      if (f.type === "number" && (el?.value || "").trim()) {
        const n = Number(el.value);
        if (Number.isNaN(n)) return `「${f.label}」请填数字`;
        if (f.min != null && n < f.min) return `「${f.label}」不能小于 ${f.min}`;
        if (f.max != null && n > f.max) return `「${f.label}」不能大于 ${f.max}`;
      }
    }
    return "";
  }

  /* 表单上已经没有主诉输入框了：哪里不舒服由第一句追问来问。原来那个必填
   * 文本域是整个门禁——"用一句话描述自己的病"恰恰是最难写的一步，写不出来
   * 就进不去。现在空着也能开始，主诉在对话里落成第一句用户消息。 */
  function focusFirst() {
    const el = [...grid.querySelectorAll("input, select, textarea")]
      .find((x) => x.offsetParent !== null);
    (el || input).focus();
  }

  /* ================================================================ 渲染 */

  const hhmm = () => {
    const d = new Date();
    return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
  };

  /* 只在用户本来就贴着底部时才自动跟到底。
   * 之前每次响应都无条件 scrollTop = scrollHeight：患者往回翻上一句的工夫，
   * 新回答一到就把人拽回底部，读到哪里全忘。 */
  function followBottom(force = false) {
    const near = thread.scrollHeight - thread.scrollTop - thread.clientHeight < 140;
    if (force || near) thread.scrollTop = thread.scrollHeight;
  }

  /* 逐字吐字。医疗问答的句子很短，一个字 18ms ≈ 0.4~1.2 秒落完，
   * 比"整块闪现"多了一点"对面正在说"的感觉，又不至于让人等。
   * 长文本（病历、长段解释）按块加速，否则 300 字要五秒。 */
  function typeOut(el, text, onDone) {
    clearInterval(state.typeTimer);
    // 上一句还没打完就被新消息插进来：定时器清掉了，但它身上的光标类还在，
    // 屏幕上会留下一个一直在闪的竖线，像卡住了。
    state.typeEl?.classList.remove("typing-out");
    state.typeEl = null;
    if (reduceMotion || !text) {
      el.textContent = text;
      onDone?.();
      return;
    }
    const step = text.length > 140 ? 7 : text.length > 60 ? 3 : 1;
    let i = 0;
    el.classList.add("typing-out");
    state.typeEl = el;
    state.typeTimer = setInterval(() => {
      i = Math.min(text.length, i + step);
      el.textContent = text.slice(0, i);
      followBottom();
      if (i >= text.length) {
        clearInterval(state.typeTimer);
        state.typeTimer = null;
        el.classList.remove("typing-out");
        state.typeEl = null;
        onDone?.();
      }
    }, 18);
  }

  function addMsg(role, text, opts = {}) {
    const wrap = document.createElement("div");
    wrap.className = `msg ${role}${opts.report ? " report" : ""}`;
    const av = document.createElement("div");
    av.className = "avatar";
    av.textContent = role === "user" ? "我" : "AI";
    const body = document.createElement("div");
    body.className = "body";
    const bubble = document.createElement("div");
    bubble.className = "bubble";
    body.appendChild(bubble);
    /* 时间戳只挂在 AI 那侧，而且**同一分钟内不重复**：
     * 一问一答隔十几秒，每条都标 15:23 / 15:23 / 15:24，那不是信息，是杂点。
     * 隔了一分钟以上才出现——它标记的是"这段对话的节奏"，不是每条消息。 */
    if (role === "assistant") {
      const now = hhmm();
      if (now !== state.lastStamp) {
        state.lastStamp = now;
        const stamp = document.createElement("div");
        stamp.className = "stamp";
        stamp.textContent = now;
        body.appendChild(stamp);
      }
    }
    wrap.appendChild(av);
    wrap.appendChild(body);
    thread.appendChild(wrap);
    if (opts.flow) typeOut(bubble, text, opts.onDone);
    else bubble.textContent = text;
    return bubble;
  }

  /* 增量渲染：新回答只追加新那一条。
   *
   * 原来每次响应都把整条 thread 清空重画——视觉上所有气泡同时淡入并跳一下，
   * 患者正在读的那句会"闪"一下重排；观感就是一张被反复刷新的表格。
   * 现在按 turns 游标追加，只有真正新增的那条会入场。
   *
   * 游标（state.shown）在**发送的那一刻就 +1**：用户气泡是本地抢先画上去的，
   * 服务端随后把它作为 turns 的下一项返回。不提前加，第一次请求回来时游标
   * 还停在 0，整条 thread 会被清掉重画，刚发出去的那句话"闪"一下。
   */
  function syncThread(turns, report, onTyped) {
    const list = turns || [];
    if (list.length < state.shown || !state.greeted) {
      thread.innerHTML = "";
      state.shown = 0;
      state.greeted = true;
      // 空会话：开场白自己长出来，说完再给示例。
      // 有历史：欢迎语只是背景，一口气画完，别让回填的气泡一条条排队。
      const empty = list.length === 0;
      addMsg("assistant", state.welcome, {
        flow: empty && !reduceMotion,
        onDone: empty ? onTyped : undefined,
      });
      if (empty) {
        // 减少动态效果时不逐字，addMsg 走的是"直接填文本"那一支，
        // onDone 不会被调用——示例按钮就永远不出现。这里补一次。
        if (reduceMotion) onTyped?.();
        followBottom();
        return;
      }
    }
    const fresh = list.slice(state.shown);
    // 只有最后一条 AI 的话逐字出现；有逐字时，"后续动作"（候选答案）
    // 等它打完再加，否则按钮会在句子说到一半时先冒出来。
    let flow = false;
    fresh.forEach((t, i) => {
      const isReport = t.role === "assistant" && report && t.content === report;
      const last = i === fresh.length - 1;
      const typeNow = last && t.role === "assistant" && !isReport && !reduceMotion;
      addMsg(t.role, t.content, {
        report: isReport,
        flow: typeNow,
        onDone: typeNow ? onTyped : undefined,
      });
      if (typeNow) flow = true;
    });
    if (!flow) onTyped?.();
    state.shown = list.length;
    followBottom();
  }

  function renderThread(turns, report, onTyped) {
    syncThread(turns, report, onTyped);
  }

  /* ---- 候选回答 ----
   * 从问题本身反推"能怎么答"：二选一的拆成两个选项，是非的问给有/没有/
   * 说不清，问时长的给三个粗档。猜不出来就一条都不给——硬塞几个不着边的
   * 选项，比不给更让人无从下手。 */
  function guessReplies(q) {
    const s = String(q || "").trim();
    if (!s) return [];

    /* 把一句问话削成选项。中文口语里的引导词（您是觉得…、请问…）和语气
     * 尾巴（…呢、…吗）都得剥掉，否则按钮上写着"您是觉得上腹痛"——
     * 那是把问题的一半抄上去，不是答案。 */
    const trim = (x) => {
      let out = String(x || "").trim();
      out = out.replace(/^[\s，,。；;：:、]+|[\s，,。；;：:、？?！!]+$/g, "");
      out = out.replace(/^(请问一下|请问|那么|那|您是|您|你是|你|觉得|认为)+/g, "");
      out = out.replace(/(呢|吗|啊|呀|吧)$/g, "");
      return out.trim();
    };
    const cutHead = (x) => {
      // 取最后一段非空：句尾的逗号会切出一个空串，用 parts[last] 会拿到 ""，
      // 再回退到整句——于是就出现"您觉得是上腹疼"这种把问题抄一半的按钮。
      const parts = String(x || "")
        .split(/[，,。；;：:？！?!]|觉得|请问|您是|你是|那么|是/)
        .map((p) => p.trim()).filter(Boolean);
      return parts[parts.length - 1] || x;
    };

    if (s.includes("还是")) {
      const [a0, b0] = s.split("还是");
      const a = trim(cutHead(a0));
      const b = trim(b0);
      // 选项必须干净、短、不重复；否则宁可不出——错选项会把人往错误答案上带
      const ok = (x) => x.length >= 1 && x.length <= 12 && !/[，,。？?]/.test(x);
      if (ok(a) && ok(b) && a !== b) return [a, b, "说不清"];
    }
    if (/有没有|是否|会不会|有没有过|能不能/.test(s)) return ["有", "没有", "说不清"];
    if (/多久|多长时间|几天|几周|什么时候开始/.test(s)) return ["今天", "两三天", "一周以上"];
    if (/[吗么][？?]?\s*$/.test(s)) return ["是", "不是", "说不清"];
    return [];
  }

  function clearChips() {
    thread.querySelectorAll(".chips").forEach((el) => el.remove());
  }

  function addChips(items, opts = {}) {
    clearChips();
    if (!items.length) return;
    const box = document.createElement("div");
    box.className = "chips";
    box.innerHTML = items.map((t) => (
      `<button type="button" class="chip${opts.sample ? " sample" : ""}" `
      + `data-${opts.sample ? "fill" : "say"}="${esc(t)}">${esc(t)}</button>`
    )).join("");
    thread.appendChild(box);
    box.addEventListener("click", (e) => {
      const b = e.target.closest(".chip");
      if (!b) return;
      if (opts.sample) {
        input.value = b.dataset.fill || "";
        autoGrow();
        input.focus();
        return;
      }
      const say = b.dataset.say || "";
      if (say) submit(say);
    });
    followBottom();
  }

  // 第一次开口前给一句示例：空输入框 + "说说哪里不舒服"，很多人会卡在这里。
  const SAMPLE_REPLIES = ["肚子疼，一阵一阵的，两天了", "孩子发烧 38.5 度，有点咳嗽", "头晕，站起来更明显"];

  function renderList(target, list, keyLabel = "label") {
    const src = (list && list.length) ? list : [];
    $(target).innerHTML = src.map((s) => `
      <div class="row ${s.filled ? "ok" : ""}">
        <span class="dot"></span>
        <span class="k">${esc(s[keyLabel])}</span>
        <span class="v" title="${esc(s.display)}">${esc(s.display)}</span>
      </div>`).join("");
  }

  function renderRecord(list) {
    // JS 里空数组是 truthy，不能用 list || fallback —— 那样传 [] 时兜底永远不跑。
    const src = (list && list.length) ? list : (state.meta?.slots || []).map((s) => (
      { key: s.key, label: s.label, filled: false, display: "—" }
    ));
    renderList("record", src);
    const done = src.filter((s) => s.filled).length;
    $("slotCount").textContent = src.length ? `${done}/${src.length}` : "";
    // 顶部那根线跟着档案走。问诊是"问一句填一格"，看得见进度才不像在原地打转。
    const bar = $("pvProgress");
    if (bar) bar.style.width = `${src.length ? Math.round((done / src.length) * 100) : 0}%`;
  }

  // 患者看不到"系统把他判成了哪一类"，就会疑惑为什么咳嗽的人被问痰的颜色。
  // 这个标签不参与逻辑，只负责把域识别结果摊开给人看。
  function renderDomain(label) {
    const tag = $("domainTag");
    if (!tag) return;
    if (label) {
      tag.textContent = label;
      tag.hidden = false;
    } else {
      tag.hidden = true;
    }
  }

  /* 跳过表单的患者要能随时看到「还差什么」。完整度取后端算的那一套，
   * 不在前端另算一遍——两处口径不一致时，进度条和病历会对不上。 */
  function renderCoverage(resp) {
    const cov = resp.coverage || { filled: 0, total: 0, percent: 0 };
    $("coverageText").textContent = cov.total
      ? `档案采集 ${cov.filled}/${cov.total} 项 · ${cov.percent}%`
      : "等待主诉";
    const miss = resp.profile_missing || [];
    const box = $("profileMissing");
    box.hidden = !miss.length;
    box.textContent = miss.length
      ? `还差基本信息：${miss.map((m) => m.label).join("、")}（对话里直接说也行）`
      : "";
  }

  function renderProfile(list) {
    const filled = (list || []).filter((p) => p.filled);
    $("profileCard").hidden = !filled.length;
    renderList("profileList", filled);
  }

  const DEG_LABEL = {
    extract_rules: "规则抽取",
    ask_template: "模板追问",
    report_template: "模板报告",
    danger_gate: "安全闸门",
  };

  function setHint(resp) {
    const box = $("hint");
    if (resp && resp.degraded && resp.degraded.length) {
      const labels = resp.degraded.map((x) => DEG_LABEL[x] || x).join("、");
      const done = resp.state === "done" || resp.state === "emergency";
      box.textContent = done
        ? `本次问诊已结束 · 使用本地规则兜底（${labels}）`
        : `本步未调用大模型，使用本地规则兜底（${labels}）`;
      return;
    }
    box.textContent = "按 Enter 发送，Shift + Enter 换行";
  }

  /* ---------------------------------------------------------------- 病历工具 */
  async function copyRecord(rec, btn) {
    const text = rec?.text || "";
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      // 非 https / 老浏览器没有 clipboard API：退回隐藏 textarea
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand("copy"); } catch { /* 无能为力，提示手动选 */ }
      ta.remove();
    }
    if (btn) {
      const old = btn.textContent;
      btn.textContent = "已复制";
      setTimeout(() => { btn.textContent = old; }, 1400);
    }
  }

  function showResult(resp) {
    const card = $("resultCard");
    const danger = resp.state === "emergency";
    card.hidden = false;
    card.classList.toggle("danger", danger);
    $("result").innerHTML = danger
      ? `<div class="dept">请立即就医</div>
         <div class="urgency">急诊科</div>
         <p class="report">${esc(resp.danger_message || "")}</p>`
      : `<div class="dept">${esc(resp.dept || "—")}</div>
         <div class="urgency">${esc(resp.urgency || "")}</div>
         <p class="report">完整病历已在结果页，可编辑、复制或下载。</p>`;
  }

  /* ---------------------------------------------------------------- 结果页 */
  function triageHTML(resp) {
    const danger = resp.state === "emergency";
    return danger
      ? `<span class="rv-dept">请立即就医</span><span class="rv-urgency">急诊科</span>
         <p class="rv-danger">${esc(resp.danger_message || "")}</p>`
      : `<span class="rv-dept">${esc(resp.dept || "—")}</span>
         <span class="rv-urgency">${esc(resp.urgency || "")}</span>`;
  }

  function recordHTML(rec) {
    return (rec.sections || []).map((s) => `
      <div class="rv-sec"><h4>${esc(s.label)}</h4><p>${esc(s.text)}</p></div>`).join("")
      || '<div class="rv-sec"><p>病历暂不可用</p></div>';
  }

  function downloadAs(text, sid, ext, mime) {
    const blob = new Blob([text], { type: mime });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `${(state.record?.title || "预问诊病历")}_${sid || ""}.${ext}`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 3000);
  }

  function markdownRecord(rec) {
    const lines = [`# ${rec.title || "预问诊病历"}`, "",
      `> 生成时间：${rec.created_at || ""} · 编号：${state.sessionId || ""}`, ""];
    (rec.sections || []).forEach((s) => {
      lines.push(`## ${s.label}`, "", String(s.text || ""), "");
    });
    if (rec.note) lines.push("---", "", rec.note);
    return lines.join("\n");
  }

  function showResultPage(resp) {
    state.record = resp.record || null;
    state.supplementing = false;
    $("chatView").hidden = true;
    $("intakeView").hidden = true;
    $("resultView").hidden = false;
    $("btnBack").hidden = true;
    const danger = resp.state === "emergency";
    $("rvTitle").textContent = danger ? "请尽快就医" : "问诊已完成";
    $("rvSub").textContent = danger
      ? "系统识别到需要立即处理的情况，下面的信息可以带去急诊。"
      : "下面是整理好的就诊摘要，可编辑、复制或下载带去给医生。";
    const triage = $("rvTriage");
    triage.classList.toggle("danger", danger);
    triage.innerHTML = triageHTML(resp);
    $("btnSupplement").hidden = Boolean(danger || !resp.can_supplement);

    const cov = resp.coverage || {};
    const rec = resp.record || {};
    const userTurns = (resp.turns || []).filter((t) => t.role === "user").length;
    $("rvMeta").innerHTML = [
      cov.total ? `档案采集 <b>${cov.filled}/${cov.total}</b> 项` : "",
      (rec.missing || []).length
        ? `未采集 <b>${rec.missing.length}</b> 项：${esc(rec.missing.join("、"))}` : "",
      userTurns ? `患者发言 <b>${userTurns}</b> 轮` : "",
    ].filter(Boolean).join(" · ");

    $("rvRecordTitle").textContent = rec.title || "预问诊病历";
    $("rvBody").innerHTML = recordHTML(rec);
    $("rvBody").hidden = false;
    $("recordEdit").hidden = true;
    $("rvEditFoot").hidden = true;
    $("recordSaved").textContent = rec.edited ? "已保存过修改" : "";
    $("rvNote").textContent = rec.note || "";
    $("rvTurns").innerHTML = (rec.patient_turns || []).map((t) => `
      <div class="rv-turn-row"><i>${t.index}.</i><span>${esc(t.text)}</span></div>`).join("")
      || '<div class="rv-turn-row"><span>暂无记录</span></div>';
    $("rvDisclaimer").textContent = state.meta?.disclaimer || "";
    $("pvProgress").style.width = "100%";
  }

  function editRecord(on) {
    if (!state.record) return;
    $("recordEdit").hidden = !on;
    $("rvBody").hidden = on;
    $("rvEditFoot").hidden = !on;
    if (on) {
      $("recordEdit").value = state.record.text || "";
      $("recordEdit").focus();
    }
  }

  async function saveRecord() {
    if (!state.record || !state.sessionId) return;
    const text = $("recordEdit").value;
    if (!text.trim()) return;
    try {
      await patchJSON(`api/previsit/record/${encodeURIComponent(state.sessionId)}`, { text });
      state.record = { ...state.record, text, edited: true };
      $("recordSaved").textContent = "已保存";
      editRecord(false);
    } catch (e) {
      $("recordSaved").textContent = `保存失败：${e.message}`;
    }
  }

  /* 结束后想起一件事：回到对话，下一句走 supplement 端点重新出病历。 */
  function continueChat() {
    $("resultView").hidden = true;
    $("chatView").hidden = false;
    $("btnBack").hidden = false;
    state.supplementing = true;
    unlock();
    input.placeholder = "还想补充什么？例如用药、症状变化；病历会重新生成";
    input.focus();
    addMsg("assistant", "还有想补充的吗？说出来我会重新整理病历。");
    followBottom(true);
  }

  function render(resp) {
    state.lastView = resp;
    state.sessionId = resp.session_id;
    state.started = true;
    const ended = resp.state === "done" || resp.state === "emergency";
    renderDomain(resp.domain_label);
    renderRecord(resp.slot_status);
    renderProfile(resp.profile_status);
    renderCoverage(resp);
    // 候选答案跟着"说完最后一句"再出现，不抢在打字前面
    renderThread(resp.turns, resp.report, () => {
      if (!ended) offerReplies(resp);
    });
    setHint(resp);
    if (ended) {
      clearChips();
      showResult(resp);       // 侧栏那张小卡仍给「科室 / 紧急度」
      lock();                 // 结果页接管，对话输入框先锁上
      showResultPage(resp);
    } else {
      $("resultCard").hidden = true;
      $("resultView").hidden = true;
      unlock();
    }
  }

  /* 问诊还在继续：把这一句的候选答案摆出来。
   * 一条都猜不出来时给示例（只在开场）——开场那个空输入框是最容易劝退人的地方。 */
  function offerReplies(resp) {
    const turns = resp.turns || [];
    const lastAI = [...turns].reverse().find((t) => t.role === "assistant");
    const guess = guessReplies(lastAI?.content || state.welcome);
    if (guess.length) { addChips(guess); return; }
    if (!turns.length) { addChips(SAMPLE_REPLIES, { sample: true }); return; }
    // 问句猜不出选项时，至少给「答不上来」的出口。患者说「不知道」和沉默
    // 不是一回事：引擎会把明确不知道记账并跳过，不再重复追问同一格。
    addChips(["没有", "不清楚", "想不起来了"]);
  }

  /* ================================================================ 交互 */
  function lock() {
    sendBtn.disabled = true;
    input.disabled = true;
    input.placeholder = "本次问诊已结束";
  }
  function unlock() {
    sendBtn.disabled = false;
    input.disabled = false;
    input.placeholder = state.meta?.placeholder || "描述一下哪里不舒服";
    input.focus();
    syncComposerIdle();
  }
  function setBusy(on) {
    state.busy = on;
    sendBtn.disabled = on;
  }
  // 输入框空着时让发送键后退半步。它一直是满亮的，反而像在催人。
  function syncComposerIdle() {
    document.querySelector(".composer")?.classList.toggle("idle", !input.value.trim());
  }

  function showTyping() {
    clearChips();          // 已经在答了，候选答案就是过期信息
    const wrap = document.createElement("div");
    wrap.className = "msg assistant";
    wrap.id = "typing";
    wrap.innerHTML = `<div class="avatar">AI</div>
      <div class="body"><div class="bubble"><div class="typing"><i></i><i></i><i></i>
        <span class="typing-label">正在整理</span></div></div></div>`;
    thread.appendChild(wrap);
    followBottom();
  }
  function hideTyping() {
    $("typing")?.remove();
  }

  function errorBubble(msg) {
    addMsg("assistant", `出错了：${msg || "请稍后重试"}`);
    followBottom();
  }

  /* text 参数：候选回答点一下就提交，不用先塞进输入框再假装敲了一遍回车。 */
  async function submit(text) {
    if (state.busy) return;
    const say = (typeof text === "string" ? text : input.value).trim();
    if (!say) return;
    setBusy(true);
    if (typeof text !== "string") {
      input.value = "";
      autoGrow();
    }
    addMsg("user", say);
    // 抢先画出去的这一句，就是服务端 turns 里的下一项：游标同时前进一格，
    // 否则第一次响应回来时会把整条 thread 清空重画。
    state.shown += 1;
    showTyping();
    followBottom(true);
    try {
      const path = state.supplementing ? "api/previsit/supplement" : "api/previsit/reply";
      const resp = await postJSON(path,
        { session_id: state.sessionId, message: say });
      state.supplementing = false;
      hideTyping();
      render(resp);
    } catch (e) {
      hideTyping();
      // 请求失败时服务端记没记下这一句，客户端无从知道：把游标退回未对齐
      // 状态，下一次成功的响应会按服务端的真实历史重画一遍。
      state.shown = 0;
      state.greeted = false;
      errorBubble(e.message);
      unlock();
    } finally {
      setBusy(false);
      syncComposerIdle();
    }
  }

  function formMode(editing) {
    state.editProfile = editing;
    $("btnStart").textContent = editing ? "保存并返回问诊" : "开始问诊";
    $("btnSkip").hidden = editing;
    $("formNote").innerHTML = editing
      ? "修改会立即写进当前这场问诊，不会新开会话。"
      : '带 <span class="req">*</span> 的必填。信息只存在本机。';
  }

  function fillForm(profile) {
    for (const f of state.meta?.profile_fields || []) {
      const el = document.getElementById(`pf_${f.key}`);
      if (el) el.value = profile?.[f.key] || "";
    }
    syncVisibility();
  }

  /* 首屏表单不再是门禁：点「跳过」照样开诊，缺的性别 / 年龄在对话里补，
   * 也能随时从「补充基本信息」回来填。 */
  async function begin(profile) {
    state.busy = true;
    $("btnStart").disabled = true;
    $("btnStart").textContent = "正在整理…";
    try {
      const resp = await postJSON("api/previsit/start", { profile });
      $("intakeView").hidden = true;
      $("chatView").hidden = false;
      $("resultView").hidden = true;
      $("btnBack").hidden = false;
      state.shown = 0;
      state.greeted = false;
      state.supplementing = false;
      clearChips();
      render(resp);
      syncComposerIdle();
    } catch (e) {
      showError(`无法开始问诊：${e.message}`);
    } finally {
      state.busy = false;
      $("btnStart").disabled = false;
      formMode(false);
    }
  }

  async function saveProfile(profile) {
    if (!state.sessionId) return;
    state.busy = true;
    $("btnStart").disabled = true;
    $("btnStart").textContent = "正在保存…";
    try {
      const resp = await patchJSON(
        `api/previsit/profile/${encodeURIComponent(state.sessionId)}`, { profile });
      formMode(false);
      $("intakeView").hidden = true;
      $("chatView").hidden = false;
      $("btnBack").hidden = false;
      render(resp);
    } catch (e) {
      showError(`保存失败：${e.message}`);
    } finally {
      state.busy = false;
      $("btnStart").disabled = false;
      $("btnStart").textContent = state.editProfile ? "保存并返回问诊" : "开始问诊";
    }
  }

  function openProfileEditor() {
    if (!state.sessionId) return;
    formMode(true);
    fillForm(state.lastView?.profile || {});
    $("chatView").hidden = true;
    $("resultView").hidden = true;
    $("intakeView").hidden = false;
    $("btnBack").hidden = true;
    clearError();
    focusFirst();
  }

  async function startIntake(ev) {
    ev.preventDefault();
    if (state.busy) return;
    const bad = validate();
    if (bad) { showError(bad); return; }
    clearError();
    const profile = collectProfile();
    if (state.editProfile) await saveProfile(profile);
    else await begin(profile);
  }

  function autoGrow() {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 140) + "px";
  }

  function toIntake() {
    $("chatView").hidden = true;
    $("resultView").hidden = true;
    $("intakeView").hidden = false;
    $("btnBack").hidden = true;
    $("resultCard").hidden = true;
    $("profileCard").hidden = true;
    formMode(false);
  }

  function restart() {
    state.sessionId = null;
    state.started = false;
    state.record = null;
    state.lastView = null;
    state.editProfile = false;
    state.supplementing = false;
    state.shown = 0;
    state.greeted = false;
    clearInterval(state.typeTimer);
    state.typeTimer = null;
    clearChips();
    thread.innerHTML = "";
    grid.querySelectorAll("input,select,textarea").forEach((el) => { el.value = ""; });
    syncVisibility();
    clearError();
    renderRecord([]);
    $("slotCount").textContent = "";
    $("intakeForm").reset();
    toIntake();
    focusFirst();
  }

  /* ================================================================ 启动 */
  async function init() {
    try {
      const meta = await getJSON("api/previsit/meta");
      state.meta = meta;
      state.welcome = meta.welcome || "您好，先说说您哪里不舒服？";
      if (meta.title) { $("pvTitle").textContent = meta.title; document.title = `${meta.title} · KnowTune`; }
      if (meta.subtitle) $("pvSubtitle").textContent = meta.subtitle;
      if (meta.disclaimer) {
        $("disclaimer").textContent = meta.disclaimer;
        $("disclaimerTop").textContent = meta.disclaimer;
      }
      if (meta.placeholder) input.placeholder = meta.placeholder;
      renderForm(meta.profile_fields || []);
      renderRecord([]);
      focusFirst();
    } catch (e) {
      $("intakeForm").hidden = true;
      showError(`无法连接预问诊服务：${e.message}。请确认服务已在运行。`);
    }
  }

  input.addEventListener("input", autoGrow);
  input.addEventListener("input", syncComposerIdle);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      submit();
    }
  });
  sendBtn.addEventListener("click", submit);
  $("intakeForm").addEventListener("submit", startIntake);
  $("btnSkip").addEventListener("click", () => {
    if (!state.busy && !state.editProfile) begin({});
  });
  $("btnRestart").addEventListener("click", restart);
  $("btnRestart2").addEventListener("click", restart);
  $("btnBack").addEventListener("click", openProfileEditor);
  $("btnEditProfile").addEventListener("click", openProfileEditor);
  $("btnSupplement").addEventListener("click", continueChat);
  $("resultView").querySelector(".rv-tools").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-act]");
    if (!b || !state.record) return;
    const act = b.dataset.act;
    if (act === "edit") editRecord(true);
    else if (act === "copy") copyRecord(state.record, b);
    else if (act === "txt") downloadAs(state.record.text, state.sessionId, "txt", "text/plain;charset=utf-8");
    else if (act === "md") downloadAs(markdownRecord(state.record), state.sessionId, "md", "text/markdown;charset=utf-8");
  });
  $("btnSaveRecord").addEventListener("click", saveRecord);
  $("btnCancelRecord").addEventListener("click", () => editRecord(false));
  syncComposerIdle();
  init();
})();
