import { VoiceInput, speak } from "./voice.js";

const $ = (sel) => document.querySelector(sel);
const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

// ------------------------------------------------------------ Markdown（仅用于导购回复）
function mdInline(text) {
  let s = esc(text);
  s = s.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
  s = s.replace(/`([^`]+)`/g, "<code>$1</code>");
  // Markdown 链接（下载地址是站内相对路径）
  s = s.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
  s = s.replace(/(https?:\/\/[^\s<]+)/g, (u) => `<a href="${u}" target="_blank" rel="noopener noreferrer">${u}</a>`);
  return s;
}

function splitRow(line) {
  return String(line)
    .trim()
    .replace(/^\|/, "")
    .replace(/\|$/, "")
    .split("|")
    .map((c) => c.trim());
}

function renderMarkdown(text) {
  const lines = String(text || "").split(/\r?\n/);
  const html = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];

    // 表格：表头 + |---| 分隔行 + 数据行
    if (/^\s*\|/.test(line) && i + 1 < lines.length && /^\s*\|[\s:|\-]+\|\s*$/.test(lines[i + 1])) {
      const head = splitRow(line);
      i += 2;
      const rows = [];
      while (i < lines.length && /^\s*\|/.test(lines[i])) {
        rows.push(splitRow(lines[i]));
        i += 1;
      }
      html.push(
        `<div class="md-table-wrap"><table class="md-table"><thead><tr>${head
          .map((h) => `<th>${mdInline(h)}</th>`)
          .join("")}</tr></thead><tbody>${rows
          .map((r) => `<tr>${r.map((c) => `<td>${mdInline(c)}</td>`).join("")}</tr>`)
          .join("")}</tbody></table></div>`
      );
      continue;
    }

    if (/^\s*>\s?/.test(line)) {
      const buf = [];
      while (i < lines.length && /^\s*>\s?/.test(lines[i])) {
        buf.push(lines[i].replace(/^\s*>\s?/, ""));
        i += 1;
      }
      html.push(`<div class="md-quote">${buf.map(mdInline).join("<br>")}</div>`);
      continue;
    }

    if (/^\s*(?:[-*]|\d+\.)\s+/.test(line)) {
      const ordered = /^\s*\d+\./.test(line);
      const buf = [];
      while (i < lines.length && /^\s*(?:[-*]|\d+\.)\s+/.test(lines[i])) {
        buf.push(lines[i].replace(/^\s*(?:[-*]|\d+\.)\s+/, ""));
        i += 1;
      }
      const tag = ordered ? "ol" : "ul";
      html.push(`<${tag} class="md-list">${buf.map((b) => `<li>${mdInline(b)}</li>`).join("")}</${tag}>`);
      continue;
    }

    if (!line.trim()) {
      i += 1;
      continue;
    }
    html.push(`<p class="md-p">${mdInline(line)}</p>`);
    i += 1;
  }
  return html.join("");
}

function uid() {
  const saved = localStorage.getItem("sg_uid");
  if (saved) return saved;
  const id = `u-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 7)}`;
  localStorage.setItem("sg_uid", id);
  return id;
}

const state = {
  userId: uid(),
  config: null,
  profile: null,
  feed: [],
  compare: null,
  selection: [],
  dialect: { lang: "zh-CN", code: "" },
  tts: false,
  busy: false,
  lastParsed: null,
  productMap: {},
  modalProduct: null,
};

async function api(path, opts = {}) {
  const init = { headers: { "Content-Type": "application/json" }, cache: "no-store", ...opts };
  if (opts.body) init.body = JSON.stringify(opts.body);
  const res = await fetch(path, init);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `请求失败（${res.status}）`);
  return data;
}

let toastTimer = null;
function toast(message) {
  const el = $("#toast");
  el.textContent = message;
  el.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("show"), 3200);
}

// ------------------------------------------------------------ 商品视觉
function phoneEl(p) {
  const d = document.createElement("div");
  d.className = "phone";
  d.style.setProperty("--c1", p.color || "#ff6900");
  d.style.setProperty("--c2", p.color2 || "#ffb26b");
  const lens = document.createElement("span");
  lens.className = "lens";
  d.appendChild(lens);
  return d;
}

function thumbEl(p, cls) {
  if (p.image) {
    const img = document.createElement("img");
    img.className = cls;
    img.src = p.image;
    img.alt = p.name || "";
    img.loading = "lazy";
    return img;
  }
  return phoneEl(p);
}

// ------------------------------------------------------------ 商品卡
function productCardEl(p) {
  state.productMap[p.id] = p;
  const el = document.createElement("div");
  el.className = "pcard";
  el.dataset.id = p.id;

  const media = document.createElement("div");
  media.className = "media";
  media.appendChild(thumbEl(p, "pimg"));

  const info = document.createElement("div");
  info.className = "info";
  info.innerHTML = `
    <div class="row1">
      <span class="brand-tag">${esc(p.brand)}</span>
      ${p.category ? `<span class="cat-tag">${esc(p.category)}</span>` : ""}
      ${p.web ? `<span class="web-tag">联网</span>` : ""}
      ${p.match ? `<span class="match">匹配 ${p.match}%</span>` : ""}
    </div>
    <div class="pname">${esc(p.name)}</div>
    <div class="price-line">
      <span class="price">¥${p.price}${p.web ? `<small> 参考价</small>` : ""}</span>
    </div>
    <div class="meta">
      <span class="rate">好评 ${esc(p.ratingText)}</span>
      <span>销量 ${esc(p.salesText)}</span>
    </div>
    <div class="tags">${p.tags.slice(0, 4).map((t) => `<span>${esc(t)}</span>`).join("")}</div>
    ${p.reasons?.length ? `<div class="reasons">${p.reasons.map((r) => `<span>· ${esc(r)}</span>`).join("")}</div>` : ""}
  `;

  const actions = document.createElement("div");
  actions.className = "actions";
  const btnOpen = document.createElement("button");
  btnOpen.textContent = "详情";
  btnOpen.dataset.act = "open";
  const btnCompare = document.createElement("button");
  btnCompare.textContent = "加入对比";
  btnCompare.dataset.act = "compare";
  const btnAsk = document.createElement("button");
  btnAsk.textContent = "问这款";
  btnAsk.dataset.act = "ask";
  actions.append(btnOpen, btnCompare, btnAsk);
  info.appendChild(actions);

  el.append(media, info);
  syncCardButton(el);
  return el;
}

function syncCardButton(el) {
  const id = el.dataset.id;
  const btn = el.querySelector('[data-act="compare"]');
  if (!btn) return;
  const on = state.selection.includes(id);
  btn.classList.toggle("on", on);
  btn.textContent = on ? "已加入对比" : "加入对比";
}

function syncAllCardButtons() {
  document.querySelectorAll(".pcard").forEach(syncCardButton);
  syncModalCompare();
  updateTray();
}

function updateTray() {
  const tray = $("#tray");
  const count = state.selection.length;
  tray.classList.toggle("hidden", count === 0);
  $("#trayText").textContent = `已选 ${count}/3 款：${count ? state.selection.map((id) => nameOf(id)).join("、") : ""}`;
  $("#trayCompare").disabled = count < 2;
}

function nameOf(id) {
  const found = state.productMap[id];
  return found ? found.name.replace(/\s+/g, "") : id;
}

async function toggleSelection(id) {
  if (state.selection.includes(id)) {
    state.selection = state.selection.filter((x) => x !== id);
  } else {
    if (state.selection.length >= 3) {
      toast("最多同时对比 3 款，先移除一款吧");
      return;
    }
    state.selection.push(id);
  }
  syncAllCardButtons();
  if (state.selection.length >= 2) {
    await generateCompare();
  }
}

async function generateCompare(ids = null) {
  const target = ids || state.selection;
  if (target.length < 2) return;
  try {
    const res = await api("/api/compare", {
      method: "POST",
      body: { userId: state.userId, ids: target, scenes: state.lastParsed?.scenes || [] },
    });
    state.compare = res.compare;
    renderCompare(res.compare);
    switchTab("compare");
    if (!ids) toast("对比表已生成，见右侧");
  } catch (err) {
    toast("生成对比表失败：" + err.message);
  }
}

// ------------------------------------------------------------ 对比表渲染
function renderCompare(cmp) {
  const pane = $("#comparePane");
  pane.innerHTML = "";
  if (!cmp) {
    pane.innerHTML = '<div class="empty">选择 2-3 款商品后，这里会生成「价格 / 好评 / 销量」对比表</div>';
    return;
  }

  const cols = cmp.products;
  const cell = (row, idx) => {
    const isBest = cmp.best[row.key] === idx;
    return `<td class="${isBest ? "best" : ""}">${esc(row.values[idx])}</td>`;
  };

  const coreRows = cmp.rows.filter((r) => r.group === "core");
  const specRows = cmp.rows.filter((r) => r.group === "spec");

  let html = `<div class="cmp-title">价格 · 好评率 · 销量 对比</div><div class="cmp-scroll"><table class="cmp-table">
    <thead><tr><th></th>${cols
      .map((p) => `<th><div class="th-name">${esc(p.name)}</div><div class="th-brand">${esc(p.brand)}</div></th>`)
      .join("")}</tr></thead><tbody>`;
  coreRows.forEach((row) => {
    html += `<tr class="core"><th>${esc(row.label)}</th>${cols.map((_, i) => cell(row, i)).join("")}</tr>`;
  });
  html += `</tbody></table></div><div class="cmp-section-label">核心参数</div><div class="cmp-scroll"><table class="cmp-table"><tbody>`;
  specRows.forEach((row) => {
    html += `<tr><th>${esc(row.label)}</th>${cols.map((_, i) => `<td>${esc(row.values[i])}</td>`).join("")}</tr>`;
  });
  html += `</tbody></table></div>`;

  const advice = cmp.advice
    .map((a) => `<div class="advice-item"><b>${esc(a.title)}</b><p>${esc(a.text)}</p></div>`)
    .join("");
  html += `<div class="cmp-section-label">选购结论</div><div class="advice">${advice}</div>`;

  pane.innerHTML = html;
}

// ------------------------------------------------------------ 推荐流
function renderFeed(data) {
  const pane = $("#feedPane");
  pane.innerHTML = "";
  if (!data.products?.length) {
    pane.innerHTML = '<div class="empty">暂无推荐，先在左侧对话里描述一下需求吧</div>';
    return;
  }
  const plan = planCardEl(data.plan, true);
  if (plan) pane.appendChild(plan);
  const label = document.createElement("div");
  label.className = "feed-label";
  label.textContent = `候选清单 · ${data.products.length} 款`;
  pane.appendChild(label);
  data.products.forEach((p) => pane.appendChild(productCardEl(p)));
  $("#feedNote").textContent = data.note || "按你的实时需求排序";
  if (data.profile) renderProfile(data.profile);
}

async function refreshFeed() {
  try {
    const data = await api(`/api/recommend?userId=${state.userId}&limit=6`);
    state.feed = data.products;
    renderFeed(data);
  } catch (err) {
    console.warn(err);
  }
}

// ------------------------------------------------------------ 画像
function renderProfile(profile) {
  if (!profile) return;
  state.profile = profile;
  $("#profileSummary").textContent = profile.summary || "还没有形成明显偏好";
  $("#statSearch").textContent = profile.searchCount ?? 0;
  $("#statView").textContent = profile.viewedCount ?? 0;
  $("#statUntouched").textContent = profile.untouched ?? 0;

  const chips = [];
  (profile.scenes || []).forEach((s) => chips.push({ text: s, cls: "hot" }));
  (profile.brands || []).forEach((b) => chips.push({ text: b, cls: "blue" }));
  if (profile.budget) chips.push({ text: `预算 ${profile.budget.min}-${profile.budget.max}`, cls: "" });
  (profile.tags || []).forEach((t) => chips.push({ text: t, cls: "" }));
  $("#profileChips").innerHTML = chips.length
    ? chips.map((c) => `<span class="chip ${c.cls}">${esc(c.text)}</span>`).join("")
    : '<span class="chip empty">多聊几句，我就能记住你</span>';

  const recent = $("#recentSearches");
  recent.innerHTML = profile.recentSearches?.length
    ? profile.recentSearches.map((s) => `<span class="chip">${esc(s)}</span>`).join("")
    : '<span class="chip empty">还没有搜索记录</span>';

  $("#avoidToggle").checked = profile.avoidNew !== false;
}

$("#avoidToggle").addEventListener("change", async (e) => {
  try {
    const res = await api("/api/settings", {
      method: "POST",
      body: { userId: state.userId, avoidNew: e.target.checked },
    });
    renderProfile(res.profile);
    refreshFeed();
    toast(e.target.checked ? "已开启：优先推荐看过的商品" : "已关闭：允许推荐从未看过的商品");
  } catch (err) {
    toast("设置失败：" + err.message);
  }
});

// ------------------------------------------------------------ 消息
function scrollBottom() {
  const box = $("#messages");
  box.scrollTop = box.scrollHeight;
}

function addUserMessage(text) {
  const msg = document.createElement("div");
  msg.className = "msg user";
  const body = document.createElement("div");
  body.className = "body";
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  bubble.textContent = text;
  body.appendChild(bubble);
  msg.appendChild(body);
  $("#messages").appendChild(msg);
  scrollBottom();
}

function addBotMessage(res) {
  const msg = document.createElement("div");
  msg.className = "msg bot";
  const avatar = document.createElement("div");
  avatar.className = "avatar";
  avatar.textContent = "智";
  const body = document.createElement("div");
  body.className = "body";

  const bubble = document.createElement("div");
  bubble.className = "bubble";
  if (res.markdown) {
    bubble.classList.add("md");
    body.classList.add("wide");
    bubble.innerHTML = renderMarkdown(res.reply || "");
  } else {
    bubble.textContent = res.reply || "";
  }
  body.appendChild(bubble);

  if (res.download?.url) {
    const link = document.createElement("a");
    link.className = "dl-btn";
    link.href = res.download.url;
    link.download = res.download.name || "";
    link.rel = "noopener";
    link.textContent = `📥 下载${res.download.kind || "表格"}（${res.download.name || "文件"}）`;
    body.appendChild(link);
  }

  if (res.dialect) {
    const tag = document.createElement("span");
    tag.className = "chip blue";
    tag.style.marginTop = "7px";
    tag.style.display = "inline-block";
    tag.textContent = `识别为 ${res.dialect.name}`;
    body.appendChild(tag);
  }

  const plan = planCardEl(res.plan, false);
  if (plan) body.appendChild(plan);

  if (res.compare) {
    const btn = document.createElement("button");
    btn.className = "quick-reply";
    btn.style.marginTop = "10px";
    btn.textContent = "📊 打开对比表";
    btn.addEventListener("click", () => switchTab("compare"));
    body.appendChild(btn);
  }

  if (res.products?.length) {
    const strip = document.createElement("div");
    strip.className = "msg-cards";
    res.products.forEach((p) => strip.appendChild(miniCardEl(p)));
    body.appendChild(strip);
  }

  if (res.quickReplies?.length) {
    const quick = document.createElement("div");
    quick.className = "quick-replies";
    res.quickReplies.forEach((q) => {
      const chip = document.createElement("button");
      chip.className = "quick-reply";
      chip.textContent = q;
      chip.addEventListener("click", () => handleQuick(q));
      quick.appendChild(chip);
    });
    body.appendChild(quick);
  }

  msg.append(avatar, body);
  $("#messages").appendChild(msg);
  scrollBottom();
}

// ------------------------------------------------------------ 购买方案卡
function planCardEl(plan, compact = false) {
  if (!plan) return null;
  const el = document.createElement("div");
  el.className = "plan-card" + (compact ? " compact" : "");

  const riskItems = plan.risk?.items?.length
    ? plan.risk.items.map((r) => `<li>${esc(r)}</li>`).join("")
    : "<li>按平台常规保修政策执行</li>";
  const alts = plan.alternatives?.length
    ? plan.alternatives
        .map(
          (a) => `<div class="plan-alt">
            ${a.image ? `<img src="${esc(a.image)}" alt="" loading="lazy">` : ""}
            <div><b>${esc(a.name)}</b><span>¥${a.price} · ${esc(a.role)}</span></div>
          </div>`
        )
        .join("")
    : '<div class="plan-empty">暂时没有更合适的备选</div>';

  el.innerHTML = `
    <div class="plan-head">
      <span class="plan-badge">购买方案</span>
      <span class="plan-title">${esc(plan.title)}</span>
    </div>
    <div class="plan-facts">
      <div class="plan-fact"><b>预算</b><span>${esc(plan.budget.text)}</span></div>
      <div class="plan-fact"><b>场景</b><span>${esc(plan.scenario.text)}</span></div>
    </div>
    <div class="plan-primary">
      ${plan.primary.image ? `<img src="${esc(plan.primary.image)}" alt="" loading="lazy">` : ""}
      <div class="plan-primary-info">
        <div class="plan-pname">${esc(plan.primary.name)}<span class="plan-price">¥${plan.primary.price}</span></div>
        <div class="plan-sub">好评 ${esc(plan.primary.ratingText)} · 销量 ${esc(plan.primary.salesText)}</div>
      </div>
    </div>
    <div class="plan-block">
      <h5>为什么适合你</h5>
      <ul class="plan-list why">${plan.primary.why.map((r) => `<li>${esc(r)}</li>`).join("")}</ul>
    </div>
    <div class="plan-block">
      <h5>评价与售后风险<span class="risk-tag ${plan.risk.level === "低" ? "low" : "mid"}">${esc(plan.risk.level)}风险</span></h5>
      <p class="plan-review">${esc(plan.reviews.text)}</p>
      <ul class="plan-list risk">${riskItems}</ul>
    </div>
    ${
      compact
        ? ""
        : `<div class="plan-block"><h5>备选方案</h5><div class="plan-alts">${alts}</div></div>`
    }
    <div class="plan-verdict"><b>结论</b>${esc(plan.verdict)}</div>
    ${
      compact
        ? ""
        : `<div class="plan-block"><h5>下单前三步</h5><ol class="plan-steps">${plan.steps
            .map((s) => `<li>${esc(s)}</li>`)
            .join("")}</ol></div>`
    }
  `;
  return el;
}

function miniCardEl(p) {
  state.productMap[p.id] = p;
  const el = document.createElement("div");
  el.className = "msg-card";
  const media = thumbEl(p, "mc-img");
  if (media.classList.contains("mc-img")) {
    media.style.width = "46px";
    media.style.height = "46px";
  } else {
    media.style.width = "34px";
    media.style.height = "66px";
    media.style.borderRadius = "9px";
  }
  const top = document.createElement("div");
  top.className = "mc-top";
  top.append(media);
  const t = document.createElement("div");
  t.innerHTML = `<div class="mc-name">${esc(p.name)}</div><div class="mc-price">¥${p.price}</div><div class="mc-meta">好评 ${esc(p.ratingText)} · ${esc(p.salesText)}${p.web ? " · 联网参考价" : ""}</div>`;
  top.appendChild(t);
  el.appendChild(top);

  const acts = document.createElement("div");
  acts.className = "mc-actions";
  const b1 = document.createElement("button");
  b1.textContent = "对比";
  b1.addEventListener("click", () => {
    toggleSelection(p.id);
  });
  const b2 = document.createElement("button");
  b2.textContent = "详情";
  b2.addEventListener("click", () => openProduct(p.id));
  acts.append(b1, b2);
  el.appendChild(acts);
  return el;
}

let typingEl = null;
function addTyping() {
  const msg = document.createElement("div");
  msg.className = "msg bot typing";
  const avatar = document.createElement("div");
  avatar.className = "avatar";
  avatar.textContent = "智";
  const body = document.createElement("div");
  body.className = "body";
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  bubble.innerHTML = "<i></i><i></i><i></i>";
  body.appendChild(bubble);
  msg.append(avatar, body);
  $("#messages").appendChild(msg);
  typingEl = msg;
  scrollBottom();
  return msg;
}

function handleQuick(q) {
  const text = q.replace(/^对比这 \d+ 款$/, "__COMPARE_SEL__");
  if (text === "__COMPARE_SEL__") {
    generateCompare();
    return;
  }
  if (q.includes("清空我的记忆")) {
    resetMemory();
    return;
  }
  if (q === "直接买哪款更稳") {
    send("帮我推荐一款口碑最好的");
    return;
  }
  send(q);
}

// ------------------------------------------------------------ 发送
async function send(text) {
  const message = (text || "").trim();
  if (!message || state.busy) return;

  if (/清空.*记忆|清除.*记忆/.test(message)) {
    resetMemory();
    return;
  }

  addUserMessage(message);
  $("#input").value = "";
  state.busy = true;
  $("#sendBtn").disabled = true;
  addTyping();

  try {
    const res = await api("/api/chat", {
      method: "POST",
      body: { userId: state.userId, message, dialect: state.dialect.code || undefined },
    });
    typingEl?.remove();
    typingEl = null;

    addBotMessage(res);
    state.lastParsed = res.parsed;
    renderProfile(res.profile);

    if (res.compare) {
      state.compare = res.compare;
      renderCompare(res.compare);
      switchTab("compare");
    }
    if (res.products?.length) {
      res.products.forEach((p) => (state.productMap[p.id] = p));
    }
    refreshFeed();
    if (state.tts) speak(res.markdown ? String(res.reply || "").replace(/[#>*`|]/g, " ").replace(/\s+/g, " ") : res.reply);
  } catch (err) {
    typingEl?.remove();
    typingEl = null;
    addBotMessage({ reply: "抱歉，服务出了点问题：" + err.message, quickReplies: ["再试一次", "换个说法"] });
  } finally {
    state.busy = false;
    $("#sendBtn").disabled = false;
    $("#input").focus();
  }
}

// ------------------------------------------------------------ 语音
const voice = new VoiceInput({
  onInterim: (t) => {
    $("#input").value = t;
  },
  onFinal: (t) => {
    $("#input").value = t;
    send(t);
  },
  onState: (s) => {
    const mic = $("#micBtn");
    mic.classList.toggle("rec", s === "recording");
    $("#voiceHint").textContent =
      s === "recording" ? "正在聆听…" : s === "unsupported" ? "当前浏览器不支持语音识别，可点击麦克风使用模拟输入" : "支持粤语 / 四川话 / 东北话等方言";
  },
  onError: (msg) => {
    toast(msg);
    $("#micBtn").classList.remove("rec");
  },
});

$("#micBtn").addEventListener("click", () => {
  if (voice.active) {
    voice.stop();
    return;
  }
  const started = voice.start(state.dialect.lang);
  if (!started) {
    const sample = voice.mockSample(state.dialect.code);
    toast(`已切换到模拟语音输入：${sample}`);
    $("#input").value = sample;
    setTimeout(() => send(sample), 400);
  }
});

$("#dialectSelect").addEventListener("change", (e) => {
  const [lang, code] = e.target.value.split("|");
  state.dialect = { lang, code: code || "" };
  const label = e.target.options[e.target.selectedIndex].text;
  toast(`语音输入语言已切换为：${label}${code ? "（会自动识别方言词）" : ""}`);
});

function buildDialectOptions() {
  const sel = $("#dialectSelect");
  const langs = state.config?.speechLanguages || [{ lang: "zh-CN", label: "普通话" }];
  sel.innerHTML = langs
    .map((l) => `<option value="${l.lang}|${l.dialect || ""}">${esc(l.label)}</option>`)
    .join("");
}

$("#ttsBtn").addEventListener("click", () => {
  state.tts = !state.tts;
  $("#ttsBtn").classList.toggle("active", state.tts);
  toast(state.tts ? "已开启语音播报" : "已关闭语音播报");
  if (!state.tts && "speechSynthesis" in window) window.speechSynthesis.cancel();
});

// ------------------------------------------------------------ 记忆库弹窗
async function openMemory() {
  $("#memoryModal").classList.remove("hidden");
  $("#memoryBody").innerHTML = '<div class="empty">读取中…</div>';
  try {
    const data = await api(`/api/memory?userId=${state.userId}`);
    const raw = data.raw || {};
    const prefs = raw.preferences || {};
    const blocks = [];
    const barBlock = (title, map, fixed) => {
      const items = fixed
        ? fixed.map(([label, key]) => [label, (map || {})[key] || 0])
        : Object.entries(map || {})
            .sort((a, b) => b[1] - a[1])
            .slice(0, 5)
            .filter(([, v]) => v > 0);
      if (!items.length) return `<div class="pref-block"><h4>${title}</h4><div class="empty" style="padding:12px">暂无记录</div></div>`;
      const max = Math.max(1, ...items.map(([, v]) => v));
      return `<div class="pref-block"><h4>${title}</h4>${items
        .map(
          ([k, v]) =>
            `<div class="bar-row"><span class="k">${esc(k)}</span><div class="bar-track"><div class="bar-fill" style="width:${Math.round(
              (v / max) * 100
            )}%"></div></div><span class="v">${v}</span></div>`
        )
        .join("")}</div>`;
    };
    blocks.push(barBlock("品类偏好", prefs.category));
    blocks.push(
      barBlock("场景偏好 · 室内 / 室外", prefs.environment, [
        ["室内", "indoor"],
        ["室外", "outdoor"],
      ])
    );
    blocks.push(
      barBlock("品牌偏好 · 高奢 / 热门 / 经济 / 小众", prefs.brandTier, [
        ["高奢", "luxury"],
        ["热门", "popular"],
        ["经济", "economy"],
        ["小众", "niche"],
      ])
    );
    blocks.push(barBlock("功能场景偏好（搜索/浏览累计）", prefs.scene));
    blocks.push(barBlock("标签偏好", prefs.tag));

    const searches = (raw.searches || []).slice(-6).reverse();
    blocks.push(
      `<div class="pref-block"><h4>历史搜索（最近 6 条）</h4><div class="log-list">${
        searches.length
          ? searches
              .map(
                (s) =>
                  `<div class="log-item"><b>${esc(s.text)}</b>${
                    s.dialect ? ` <span>[${esc(s.dialect)}]</span>` : ""
                  }<br><span>${esc(s.ts)} · 意图：${esc(s.intent)}${s.budget ? ` · 预算 ${s.budget.min}-${s.budget.max}` : ""}</span></div>`
              )
              .join("")
          : '<div class="empty" style="padding:10px">暂无搜索记录</div>'
      }</div></div>`
    );

    const persona = data.profile || {};
    blocks.push(
      `<div class="pref-block"><h4>当前画像</h4><div class="log-item"><b>${esc(persona.summary)}</b><br><span>搜索 ${
        persona.searchCount
      } 次 · 浏览 ${persona.viewedCount} 款 · 未接触 ${persona.untouched} 款</span></div></div>`
    );

    $("#memoryBody").innerHTML = blocks.join("");
  } catch (err) {
    $("#memoryBody").innerHTML = `<div class="empty">读取失败：${esc(err.message)}</div>`;
  }
}

async function resetMemory() {
  try {
    await api(`/api/memory?userId=${state.userId}`, { method: "DELETE" });
    state.selection = [];
    state.compare = null;
    renderCompare(null);
    $("#messages").innerHTML = "";
    toast("记忆已清空，我们重新认识一下吧");
    welcome();
    refreshFeed();
  } catch (err) {
    toast("清空失败：" + err.message);
  }
}

// ------------------------------------------------------------ 面板切换
function switchTab(name) {
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
  $("#feedPane").classList.toggle("hidden", name !== "feed");
  $("#searchPane").classList.toggle("hidden", name !== "search");
  $("#comparePane").classList.toggle("hidden", name !== "compare");
  $("#feedNote").style.display = name === "feed" ? "" : "none";
  if (name === "search") {
    const box = $("#searchInput");
    if (box && !box.value) setTimeout(() => box.focus(), 60);
  }
  if (window.innerWidth <= 820) {
    $("#panel").classList.add("open");
    $("#panelToggle").textContent = "收起";
  }
}

// ------------------------------------------------------------ 站内检索智能体
function buyLinksEl(p) {
  const kw = encodeURIComponent((p.name || "").replace(/\s+/g, ""));
  const box = document.createElement("div");
  box.className = "buy-links";
  box.innerHTML =
    `<a href="https://search.jd.com/Search?keyword=${kw}&enc=utf-8" target="_blank" rel="noopener noreferrer">京东比价 ↗</a>` +
    `<a href="https://s.taobao.com/search?q=${kw}" target="_blank" rel="noopener noreferrer">淘宝比价 ↗</a>`;
  return box;
}

// 浏览器内直连大模型（Key 只在本机 localStorage，不经过任何服务器）
async function llmCall(messages, { maxTokens = 320, temperature = 0.2, json = false } = {}) {
  const conf = readLlmConf();
  if (!conf.apiKey) throw new Error("还没配置大模型 Key");
  const base = (conf.apiBase || "https://api.deepseek.com/v1").replace(/\/+$/, "");
  const body = { model: conf.model || "deepseek-chat", temperature, max_tokens: maxTokens, messages };
  if (json) body.response_format = { type: "json_object" };
  const res = await fetch(`${base}/chat/completions`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${conf.apiKey}` },
    body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error((data && data.error && data.error.message) || `大模型接口返回 ${res.status}`);
  const choice = (data.choices && data.choices[0]) || {};
  return (choice.message && choice.message.content) || "";
}

const HINT_SYSTEM = [
  "你是电商导购的检索解析器。把用户的中文购物需求解析成 JSON，只输出 JSON，不要解释。",
  "字段：budgetMin（整数元，未知填 null）、budgetMax（整数元，未知填 null）、",
  "brands（明确要求的品牌数组）、categories（只能是：手机/笔记本/平板/耳机/智能穿戴/家电/美妆/食品/服饰/家居）、",
  "needs（场景词，只能取：拍照/游戏/续航/快充/小屏/大屏/轻薄/性价比/商务/折叠/系统/自拍/信号/办公/性能/音乐/通勤/健康/居家/护肤/运动/户外/零食）、",
  "keywords（商品名或特征词数组）、exclude（明确不要的品牌或属性数组）。",
  "价格换算：一千出头≈1000-1500；两千以内≈0-2000；三千左右≈2500-3500；四千档≈3500-4500。",
].join("");

async function llmHints(query) {
  const raw = await llmCall(
    [{ role: "system", content: HINT_SYSTEM }, { role: "user", content: query }],
    { maxTokens: 200, json: true }
  );
  return JSON.parse(raw);
}

async function renderAiInsight(data, hints) {
  const pane = $("#searchResults");
  if (!pane) return;
  const box = document.createElement("div");
  box.className = "ai-note";
  box.textContent = "DeepSeek 正在解读这批结果…";
  pane.prepend(box);
  const facts = {
    query: data.query,
    conditions: (data.conditions || []).map((c) => `${c.label}=${c.value}`),
    products: (data.cards || []).slice(0, 4).map((c) => ({
      name: c.name,
      price: c.price,
      brand: c.brand,
      rating: c.ratingText,
      tags: (c.tags || []).slice(0, 3),
      reasons: c.reasons,
    })),
  };
  try {
    const text = await llmCall(
      [
        {
          role: "system",
          content:
            "你是「小函」，说话干脆的中文导购。只输出一段不超过 70 字的口语解读：先说这批结果适合谁，再点出首选与备选的差别。" +
            "只能用给定事实，严禁编造参数、价格、销量或评价；不要开场白，不要 markdown。",
        },
        { role: "user", content: JSON.stringify(facts) },
      ],
      { maxTokens: 140, temperature: 0.5 }
    );
    if (!text.trim()) throw new Error("空回复");
    box.textContent = text.trim();
  } catch (err) {
    box.remove();
  }
}

async function runSearch(rawQuery) {
  const box = $("#searchInput");
  const query = String(rawQuery ?? box.value ?? "").trim();
  if (!query) {
    box.focus();
    toast("先输入要搜的内容，例如「3000 以内拍照手机」");
    return;
  }
  box.value = query;
  const pane = $("#searchResults");
  pane.innerHTML = '<div class="empty">检索中…</div>';
  $("#searchChips").innerHTML = "";

  // 配了 Key 就先用大模型把口语需求翻成结构化条件，再交给站内检索引擎
  let hints = null;
  if (readLlmConf().apiKey) {
    try {
      hints = await llmHints(query);
    } catch (err) {
      console.warn("大模型解析失败，回落规则解析", err);
    }
  }

  try {
    let url = `/api/search?q=${encodeURIComponent(query)}&userId=${encodeURIComponent(state.userId)}&limit=10`;
    if (hints) url += `&hints=${encodeURIComponent(JSON.stringify(hints))}`;
    const data = await api(url);
    renderSearchResults(data);
    if (hints) renderAiInsight(data, hints);
  } catch (err) {
    pane.innerHTML = `<div class="empty">检索失败：${esc(err.message)}</div>`;
  }
}

function renderSearchResults(data) {
  const pane = $("#searchResults");
  const chipsBox = $("#searchChips");
  pane.innerHTML = "";
  chipsBox.innerHTML = "";

  (data.conditions || []).forEach((c) => {
    const chip = document.createElement("span");
    chip.className = "chip hot";
    chip.textContent = `${c.label}：${c.value}`;
    chipsBox.appendChild(chip);
  });
  if (data.llm) {
    const chip = document.createElement("span");
    chip.className = "chip blue";
    chip.textContent = "DeepSeek 已参与理解";
    chipsBox.appendChild(chip);
  }

  if (!data.cards || !data.cards.length) {
    const sug = (data.suggestions || [])
      .map((s) => `<button class="chip" data-sug="${esc(s)}">${esc(s)}</button>`)
      .join("");
    pane.innerHTML = `<div class="empty">${esc(data.note || "没有找到匹配的商品")}</div>${
      sug ? `<div class="search-sug"><span>试试这些：</span>${sug}</div>` : ""
    }`;
    return;
  }

  const label = document.createElement("div");
  label.className = "feed-label";
  label.textContent = `命中 ${data.cards.length} 款 · 按匹配度排序`;
  pane.appendChild(label);
  data.cards.forEach((p) => {
    const card = productCardEl(p);
    const info = card.querySelector(".info");
    if (info) info.appendChild(buyLinksEl(p));
    pane.appendChild(card);
  });
  if (data.note) {
    const note = document.createElement("div");
    note.className = "search-note";
    note.textContent = data.note;
    pane.appendChild(note);
  }
}

$("#searchBtn").addEventListener("click", () => runSearch());
$("#searchInput").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.isComposing) runSearch();
});
$("#searchPane").addEventListener("click", (e) => {
  const sug = e.target.closest("[data-sug]");
  if (sug) {
    runSearch(sug.dataset.sug);
    return;
  }
  onCardAction(e);
});

// ------------------------------------------------------------ 对话引擎（可选大模型 Key）
const LLM_STORAGE = "xiaohan_llm_conf";

function readLlmConf() {
  try {
    return JSON.parse(localStorage.getItem(LLM_STORAGE) || "{}") || {};
  } catch {
    return {};
  }
}

function pillText(status, config) {
  if (status && status.enabled) {
    return `对话引擎 · ${status.model || "大模型"}`;
  }
  const n = config?.products || 61;
  return `对话引擎 · 本地规则（${n} 款机型）`;
}

async function applyLlmConf(conf) {
  const payload = { apiKey: conf.apiKey || "", apiBase: conf.apiBase || "", model: conf.model || "" };
  const status = await api("/api/llm", { method: "POST", body: payload });
  $("#llmPill").textContent = pillText(status, state.config);
  $("#llmStatus").textContent = status.enabled
    ? `当前：已启用 ${status.model}（${status.maskedKey}），本机浏览器内直连`
    : "当前：本地规则引擎（61 款机型，无需 Key）";
  return status;
}

async function openLlmModal() {
  const conf = readLlmConf();
  $("#llmKey").value = conf.apiKey || "";
  $("#llmBase").value = conf.apiBase || "https://api.deepseek.com/v1";
  $("#llmModel").value = conf.model || "deepseek-chat";
  $("#llmModal").classList.remove("hidden");
  try {
    const status = await api("/api/llm");
    if (status.enabled) $("#llmStatus").textContent = `当前：已启用 ${status.model}（${status.maskedKey}）`;
  } catch {
    /* 忽略 */
  }
}

$("#llmPill").addEventListener("click", openLlmModal);
$("#llmClose").addEventListener("click", () => $("#llmModal").classList.add("hidden"));
$("#llmModal").addEventListener("click", (e) => {
  if (e.target.id === "llmModal") $("#llmModal").classList.add("hidden");
});
$("#llmSave").addEventListener("click", async () => {
  const conf = {
    apiKey: $("#llmKey").value.trim(),
    apiBase: $("#llmBase").value.trim() || "https://api.deepseek.com/v1",
    model: $("#llmModel").value.trim() || "deepseek-chat",
  };
  if (!conf.apiKey) {
    toast("请先填入 API Key");
    return;
  }
  localStorage.setItem(LLM_STORAGE, JSON.stringify(conf));
  try {
    const status = await applyLlmConf(conf);
    toast(status.enabled ? `已启用 ${status.model}，下一次对话生效` : "已保存，但引擎未识别到 Key");
    $("#llmModal").classList.add("hidden");
  } catch (err) {
    toast("启用失败：" + err.message);
  }
});
$("#llmClear").addEventListener("click", async () => {
  localStorage.removeItem(LLM_STORAGE);
  $("#llmKey").value = "";
  try {
    await applyLlmConf({});
    toast("已清除 Key，回到本地规则引擎");
  } catch (err) {
    toast("清除失败：" + err.message);
  }
  $("#llmModal").classList.add("hidden");
});

document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => switchTab(tab.dataset.tab));
});

$("#panelToggle").addEventListener("click", () => {
  const panel = $("#panel");
  panel.classList.toggle("open");
  $("#panelToggle").textContent = panel.classList.contains("open") ? "收起" : "推荐";
});

$("#sideToggle").addEventListener("click", () => {
  const side = document.querySelector(".side");
  side.classList.toggle("open");
  $("#sideToggle").textContent = side.classList.contains("open") ? "收起画像" : "我的画像";
});

$("#memoryBtn").addEventListener("click", openMemory);
$("#memoryClose").addEventListener("click", () => $("#memoryModal").classList.add("hidden"));
$("#memoryOk").addEventListener("click", () => $("#memoryModal").classList.add("hidden"));
$("#memoryReset").addEventListener("click", async () => {
  $("#memoryModal").classList.add("hidden");
  await resetMemory();
});
$("#memoryModal").addEventListener("click", (e) => {
  if (e.target.id === "memoryModal") $("#memoryModal").classList.add("hidden");
});

// ------------------------------------------------------------ 快捷需求
const QUICK_NEEDS = [
  ["3000-4000 拍照好", "3000到4000元，拍照好一点的手机"],
  ["通勤降噪耳机", "室外通勤用，想要降噪好一点的耳机"],
  ["轻薄笔记本", "室内办公用，想要轻一点的笔记本"],
  ["小屏旗舰", "想要小屏旗舰，手感要好"],
  ["家里扫地机", "家里用，想买个扫地机器人"],
  ["看看我的画像", "你还记得我的喜好吗"],
];

function buildQuickGrid() {
  const grid = $("#quickGrid");
  QUICK_NEEDS.forEach(([label, text]) => {
    const btn = document.createElement("button");
    btn.className = "quick";
    btn.textContent = label;
    btn.addEventListener("click", () => send(text));
    grid.appendChild(btn);
  });
}

// ------------------------------------------------------------ 事件绑定
$("#sendBtn").addEventListener("click", () => send($("#input").value));
$("#input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.isComposing) send($("#input").value);
});
$("#trayCompare").addEventListener("click", () => generateCompare());
$("#trayClear").addEventListener("click", () => {
  state.selection = [];
  syncAllCardButtons();
});

// 商品卡按钮（事件委托）
function onCardAction(e) {
  const btn = e.target.closest("button[data-act]");
  if (btn) {
    const card = btn.closest(".pcard");
    const id = card.dataset.id;
    if (btn.dataset.act === "compare") toggleSelection(id);
    else if (btn.dataset.act === "ask") send(`介绍一下${nameOf(id)}`);
    else if (btn.dataset.act === "open") openProduct(id);
    return;
  }
  const card = e.target.closest(".pcard");
  if (card && e.target.closest(".media, .pname")) openProduct(card.dataset.id);
}
$("#feedPane").addEventListener("click", onCardAction);

// ------------------------------------------------------------ 商品详情（2D / 3D 展示）
async function openProduct(id) {
  const cached = state.productMap[id] || {};
  $("#productModal").classList.remove("hidden");
  $("#pmTitle").textContent = cached.name || "商品详情";
  $("#pmBrand").textContent = [cached.brand, cached.category].filter(Boolean).join(" · ");
  $("#pmInfo").innerHTML = '<div class="pm-loading">正在读取商品资料…</div>';
  setProductView("2d");
  try {
    const d = await api(`/api/product?id=${encodeURIComponent(id)}&userId=${encodeURIComponent(state.userId)}`);
    renderProductModal(d);
  } catch (err) {
    toast("详情加载失败：" + err.message);
  }
}

function renderProductModal(d) {
  state.modalProduct = d;
  $("#pmTitle").textContent = d.name;
  $("#pmBrand").textContent = [d.brand, d.category, d.web ? "联网参考价" : ""].filter(Boolean).join(" · ");

  const makeMedia = () => {
    if (!d.image) {
      const ph = phoneEl(d);
      ph.style.width = "150px";
      ph.style.height = "150px";
      return ph;
    }
    const img = document.createElement("img");
    img.src = d.image;
    img.alt = d.name || "";
    return img;
  };

  // 2D 平面大图
  const stage = $("#pmStage2d");
  stage.innerHTML = "";
  stage.appendChild(makeMedia());

  // 3D 正反面
  const front = $("#pmFaceFront");
  const back = $("#pmFaceBack");
  front.innerHTML = "";
  back.innerHTML = "";
  front.appendChild(makeMedia());
  back.appendChild(makeMedia());

  // 有真实 GLB 模型时优先用 <model-viewer>，否则回退到图片立体卡片
  const hasModel = !!d.model3d;
  $("#viewer3d").classList.toggle("hidden", hasModel);
  const modelWrap = $("#pmModelViewer");
  modelWrap.classList.toggle("hidden", !hasModel);
  // 延迟到「3D 展示」真正可见时再创建，避免在 display:none 容器里初始化导致不渲染
  modelWrap.innerHTML = "";
  $("#pm3dTip").textContent = hasModel
    ? "3D 模型 · 拖动旋转 / 滚轮缩放 / 双指缩放"
    : "3D 交互视图 · 按住拖动可 360° 旋转";

  const specs = (d.specs || [])
    .map((s) => `<div class="pm-spec"><b>${esc(s.label)}</b><span>${esc(s.value)}</span></div>`)
    .join("");
  const highlights = (d.highlights || []).map((h) => `<li>${esc(h)}</li>`).join("");
  $("#pmInfo").innerHTML = `
    <div class="pm-price-row">
      <span class="pm-big-price">¥${d.price}${d.web ? "<small> 参考价</small>" : ""}</span>
      <span class="pm-stat">好评 ${esc(d.ratingText)} · 销量 ${esc(d.salesText)}</span>
    </div>
    <div class="pm-tags">${(d.tags || []).map((t) => `<span>${esc(t)}</span>`).join("")}</div>
    ${highlights ? `<div class="pm-block"><h5>核心亮点</h5><ul class="pm-points">${highlights}</ul></div>` : ""}
    ${specs ? `<div class="pm-block"><h5>关键参数</h5><div class="pm-spec-grid">${specs}</div></div>` : ""}
    <div class="pm-block"><h5>适合人群</h5><p class="pm-audience">${esc(d.audience || "通用场景")}</p></div>
    ${d.note ? `<div class="pm-note">${esc(d.note)}</div>` : ""}
  `;
  syncModalCompare();
}

function syncModalCompare() {
  const btn = $("#pmCompare");
  if (!btn) return;
  const d = state.modalProduct;
  const on = !!d && state.selection.includes(d.id);
  btn.classList.toggle("on", on);
  btn.textContent = on ? "已加入对比" : "加入对比";
}

function closeProduct() {
  $("#productModal").classList.add("hidden");
  stop3d();
}

function ensureModelViewer() {
  const wrap = $("#pmModelViewer");
  const src = state.modalProduct?.model3d;
  if (!wrap || !src) return null;
  const existing = wrap.querySelector("model-viewer");
  if (existing && existing.getAttribute("src") === src) return existing;
  wrap.innerHTML = `<model-viewer src="${esc(src)}" camera-controls auto-rotate auto-rotate-delay="2500"
      rotation-per-second="14deg" shadow-intensity="1" exposure="1" interaction-prompt="none"></model-viewer>`;
  return wrap.querySelector("model-viewer");
}

function setProductView(view) {
  document.querySelectorAll(".pm-tab").forEach((t) => t.classList.toggle("active", t.dataset.view === view));
  $("#pmView2d").classList.toggle("hidden", view !== "2d");
  $("#pmView3d").classList.toggle("hidden", view !== "3d");
  if (view === "3d") {
    if (state.modalProduct?.model3d) {
      stop3d();
      ensureModelViewer();
    } else {
      reset3d();
      start3d();
    }
  } else {
    stop3d();
  }
}

// ---- 3D 交互视图：拖动旋转 / 滚轮缩放 / 双击复位
const view3d = { rotX: 10, rotY: -18, zoom: 1, drag: false, x: 0, y: 0, idleAt: 0, raf: 0, bound: false };

function apply3d() {
  const stage = $("#stage3d");
  if (stage) stage.style.transform = `rotateX(${view3d.rotX}deg) rotateY(${view3d.rotY}deg) scale(${view3d.zoom})`;
}

function reset3d() {
  view3d.rotX = 10;
  view3d.rotY = -18;
  view3d.zoom = 1;
  view3d.idleAt = performance.now();
  apply3d();
}

function bind3d() {
  if (view3d.bound) return;
  view3d.bound = true;
  const box = $("#viewer3d");

  box.addEventListener("pointerdown", (e) => {
    if (e.target.closest(".viewer3d-reset")) return;
    view3d.drag = true;
    view3d.x = e.clientX;
    view3d.y = e.clientY;
    view3d.idleAt = performance.now();
    box.setPointerCapture?.(e.pointerId);
  });

  box.addEventListener("pointermove", (e) => {
    if (!view3d.drag) return;
    view3d.rotY += (e.clientX - view3d.x) * 0.5;
    view3d.rotX = Math.max(-40, Math.min(40, view3d.rotX - (e.clientY - view3d.y) * 0.3));
    view3d.x = e.clientX;
    view3d.y = e.clientY;
    view3d.idleAt = performance.now();
    apply3d();
  });

  const release = () => {
    view3d.drag = false;
    view3d.idleAt = performance.now();
  };
  box.addEventListener("pointerup", release);
  box.addEventListener("pointercancel", release);
  box.addEventListener("pointerleave", release);

  box.addEventListener(
    "wheel",
    (e) => {
      e.preventDefault();
      view3d.zoom = Math.max(0.7, Math.min(1.7, view3d.zoom + (e.deltaY > 0 ? -0.06 : 0.06)));
      view3d.idleAt = performance.now();
      apply3d();
    },
    { passive: false }
  );

  box.addEventListener("dblclick", reset3d);
  $("#pm3dReset").addEventListener("click", (e) => {
    e.stopPropagation();
    reset3d();
  });
}

function start3d() {
  bind3d();
  if (view3d.raf) return;
  view3d.idleAt = performance.now();
  apply3d();
  const loop = () => {
    if ($("#pmView3d").classList.contains("hidden")) {
      view3d.raf = 0;
      return;
    }
    // 松手 2 秒后恢复缓慢自转
    if (view3d.drag) view3d.idleAt = performance.now();
    else if (performance.now() - view3d.idleAt > 2000) {
      view3d.rotY += 0.4;
      apply3d();
    }
    view3d.raf = requestAnimationFrame(loop);
  };
  view3d.raf = requestAnimationFrame(loop);
}

function stop3d() {
  if (view3d.raf) cancelAnimationFrame(view3d.raf);
  view3d.raf = 0;
  view3d.drag = false;
}

// 2D 悬停放大时跟随鼠标定位放大中心
$("#pmStage2d").addEventListener("mousemove", (e) => {
  const img = $("#pmStage2d img");
  if (!img) return;
  const r = e.currentTarget.getBoundingClientRect();
  img.style.transformOrigin = `${((e.clientX - r.left) / r.width) * 100}% ${((e.clientY - r.top) / r.height) * 100}%`;
});

document.querySelectorAll(".pm-tab").forEach((tab) => {
  tab.addEventListener("click", () => setProductView(tab.dataset.view));
});
$("#pmClose").addEventListener("click", closeProduct);
$("#productModal").addEventListener("click", (e) => {
  if (e.target.id === "productModal") closeProduct();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("#productModal").classList.contains("hidden")) closeProduct();
});
$("#pmCompare").addEventListener("click", () => {
  if (state.modalProduct) toggleSelection(state.modalProduct.id);
});
$("#pmAsk").addEventListener("click", () => {
  const d = state.modalProduct;
  if (!d) return;
  closeProduct();
  send(`介绍一下${d.name}`);
});

// ------------------------------------------------------------ 启动
function welcome() {
  $("#messages").innerHTML = "";
  const p = state.profile;
  const memoryLine =
    p && p.searchCount
      ? `已加载你的偏好：${p.summary}`
      : "还没有历史记录，从下面任意一句开始就行。";
  addBotMessage({
    markdown: true,
    reply:
      "我是小函，你的智能导购助手。\n" +
      "\n**我能做什么**\n" +
      "- 按预算 / 场景挑手机、电脑、耳机、家电、美妆、食品\n" +
      "- 发淘宝、京东链接 → 自动解析参数，并对比 3-5 款同价位竞品\n" +
      "- 任意两款商品对比，可导出一份 Excel 表格\n" +
      "\n**怎么输入（三种都行）**\n" +
      "- 说需求：`3000-4000 元，拍照好一点的手机`\n" +
      "- 说对比：`小米 15 和 iPhone 16 哪个好`\n" +
      "\n**还支持**：方言语音输入、记住你的偏好、联网查库里没有的新机型。\n" +
      memoryLine,
    quickReplies: ["3000-4000 元拍照手机", "小米 15 和 iPhone 16 哪个好", "通勤用的降噪耳机"],
  });
}

async function init() {
  buildQuickGrid();
  try {
    state.config = await api("/api/config");
    buildDialectOptions();
    const llm = state.config.llm;
    $("#llmPill").textContent = llm.enabled
      ? `对话引擎 · ${llm.model}`
      : `对话引擎 · 本地规则（${state.config.products} 款机型）`;
  } catch (err) {
    toast("配置加载失败：" + err.message);
  }

  // 本机保存过的 Key：启动时重新注入引擎（只在本浏览器内生效）
  const saved = readLlmConf();
  if (saved.apiKey) {
    try {
      await applyLlmConf(saved);
    } catch (err) {
      console.warn("Key 注入失败", err);
    }
  }

  try {
    const mem = await api(`/api/memory?userId=${state.userId}`);
    renderProfile(mem.profile);
  } catch (err) {
    console.warn(err);
  }

  welcome();
  await refreshFeed();
}

init();
