/* Debtscope — onboarding → projects → dashboard; vanilla JS, no build step. */
"use strict";

const state = {
  boot: null,
  projectId: null,
  overview: null,
  ruleMap: {},
  findings: [],
  filters: { rule: "", severity: "", confidence: "", q: "",
             status: "open,confirmed,wontfix" },
  expanded: new Set(),
  editingRule: null,
  initialScanStarted: false,
};

const SEV_COLORS = { high: "#f85149", medium: "#d29922", low: "#58a6ff" };
const SEV_LABEL = { high: "高", medium: "中", low: "低" };
const STATUS_LABEL = {
  open: "待处理", confirmed: "已确认", false_positive: "误报",
  wontfix: "暂不处理", resolved: "已消除",
};

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function api(path, opts) {
  const resp = await fetch(path, opts);
  if (!resp.ok) throw new Error(await resp.text());
  return resp.json();
}
const P = (rel) => "/api/projects/" + state.projectId + rel;

function toast(msg, ms) {
  const t = $("toast");
  t.textContent = msg; t.hidden = false;
  clearTimeout(t._timer);
  t._timer = setTimeout(() => { t.hidden = true; }, ms || 3200);
}

function showView(name) {
  ["onboarding", "setup", "app"].forEach((v) => {
    $("view-" + v).hidden = (v !== name);
  });
}

// ===========================================================================
// boot / view routing
// ===========================================================================

async function boot() {
  state.boot = await api("/api/boot");
  if (!state.boot.configured) {
    fillProviderSelect("ob");
    showView("onboarding");
    return;
  }
  renderModelBadges();
  if (!state.boot.projects.length) {
    showSetup();
    return;
  }
  let pid = state.boot.focus_pid || localStorage.getItem("debtscope_pid");
  if (!state.boot.projects.some((p) => p.id === pid)) pid = state.boot.projects[0].id;
  await enterProject(pid);
  if (location.search.includes("rules=1")) openRules();
  if (location.search.includes("ruleedit=1")) {
    $("rules-project-name").textContent = "· " + state.overview.repo;
    $("rules-modal").hidden = false;
    openRuleEditor(null);
  }
}

function renderModelBadges(degraded, error) {
  const label = "AI 精判 · " + (state.boot.model || "model");
  ["llm-badge", "setup-model-badge"].forEach((id) => {
    const el = $(id);
    if (!el) return;
    el.classList.remove("ok", "warn");
    if (degraded) {
      el.textContent = "AI 精判降级（点此检查模型）";
      el.classList.add("warn");
      el.title = "本次扫描未能完成 AI 精判，疑似废弃函数仅做静态判定。\n原因：" +
        (error || "模型未返回可用判定") + "\n点击检查/修改模型配置后重新扫描。";
    } else {
      el.textContent = label;
      el.classList.add("ok");
      el.title = "点击修改模型配置";
    }
  });
}

// ===========================================================================
// model configuration forms (onboarding prefix ob-, modal prefix cfg-)
// ===========================================================================

function fillProviderSelect(prefix, selected) {
  const sel = $(prefix + "-provider");
  sel.innerHTML = Object.keys(state.boot.providers).map((k) =>
    '<option value="' + k + '">' + esc(state.boot.providers[k].label) + "</option>").join("");
  const current = selected ||
    (state.boot.config_exists ? state.boot.provider : "") || "doubao";
  sel.value = current;
  applyProviderPreset(prefix);
}

function applyProviderPreset(prefix) {
  const p = state.boot.providers[$(prefix + "-provider").value];
  if (!p) return;
  const base = $(prefix + "-base"), model = $(prefix + "-model");
  if (!base.value && p.api_base) base.value = p.api_base;
  if (!model.value && p.model) model.value = p.model;
  const urlEl = $(prefix + "-keyurl");
  if (urlEl) {
    urlEl.innerHTML = p.key_url
      ? '获取 API Key：<a href="' + p.key_url + '" target="_blank" rel="noopener">' + esc(p.key_url) + "</a>"
      : "本地服务无需 API Key";
  }
}

function formValues(prefix) {
  return {
    provider: $(prefix + "-provider").value,
    api_base: $(prefix + "-base").value.trim(),
    model: $(prefix + "-model").value.trim(),
    api_key: $(prefix + "-key").value.trim(),
  };
}

async function testConfig(prefix) {
  const body = formValues(prefix);
  if (!body.api_base) { toast("请填写 API Base"); return false; }
  if ($(prefix + "-provider").value !== "ollama" && !body.api_key
      && !(prefix === "cfg" && state.boot.key_masked)) {
    toast("请填写 API Key"); return false;
  }
  const out = $(prefix + "-result");
  out.className = "cfg-result"; out.textContent = "正在测试连接…";
  try {
    const res = await api("/api/config/test", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    out.textContent = "连接正常：" + res.msg + "（" + res.model + "）";
    out.className = "cfg-result ok";
    return true;
  } catch (e) {
    out.textContent = "连接失败：" + errText(e);
    out.className = "cfg-result fail";
    return false;
  }
}

async function saveConfig(prefix) {
  const body = formValues(prefix);
  if (!body.api_base) { toast("请填写 API Base"); return false; }
  if ($(prefix + "-provider").value !== "ollama" && !body.api_key
      && !(prefix === "cfg" && state.boot.key_masked)) {
    toast("请填写 API Key"); return false;
  }
  await api("/api/config", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return true;
}

function errText(e) {
  try { return JSON.parse(e.message).error || e.message; } catch { return e.message; }
}

// onboarding
$("ob-provider").addEventListener("change", () => {
  $("ob-base").value = ""; $("ob-model").value = ""; applyProviderPreset("ob");
});
$("ob-test").addEventListener("click", () => testConfig("ob"));
$("ob-save").addEventListener("click", async () => {
  const ok = await testConfig("ob");
  if (!ok) {
    if (!confirm("连接测试未通过，仍要保存并继续吗？")) return;
  }
  if (!(await saveConfig("ob"))) return;
  toast("模型已连接");
  state.boot = await api("/api/boot");
  renderModelBadges();
  showSetup();
});

// dashboard modal
$("llm-badge").addEventListener("click", openConfigModal);
$("setup-model-badge").addEventListener("click", openConfigModal);
function openConfigModal() {
  fillProviderSelect("cfg", state.boot.provider);
  $("cfg-base").value = state.boot.api_base || "";
  $("cfg-model").value = state.boot.model || "";
  $("cfg-key").value = "";
  $("cfg-keyhint").textContent = state.boot.key_masked ? "（已保存 " + state.boot.key_masked + "，留空不修改）" : "";
  $("cfg-result").textContent = ""; $("cfg-result").className = "cfg-result";
  $("config-modal").hidden = false;
}
$("cfg-provider").addEventListener("change", () => {
  $("cfg-base").value = ""; $("cfg-model").value = ""; applyProviderPreset("cfg");
});
$("cfg-test").addEventListener("click", () => testConfig("cfg"));
$("cfg-close").addEventListener("click", () => { $("config-modal").hidden = true; });
$("cfg-cancel").addEventListener("click", () => { $("config-modal").hidden = true; });
$("config-modal").addEventListener("click", (e) => {
  if (e.target.id === "config-modal") $("config-modal").hidden = true;
});
$("cfg-save").addEventListener("click", async () => {
  if (!(await saveConfig("cfg"))) return;
  $("config-modal").hidden = true;
  state.boot = await api("/api/boot");
  renderModelBadges();
  toast("配置已保存，重新扫描后生效");
});

// ===========================================================================
// project setup
// ===========================================================================

function showSetup() {
  renderProjectList();
  showView("setup");
}

function renderProjectList() {
  const projects = state.boot.projects;
  $("ap-list-title").hidden = !projects.length;
  $("ap-list").innerHTML = projects.map((p) =>
    '<div class="project-card" data-pid="' + p.id + '">' +
      '<div class="pc-main">' +
        '<div class="pc-name">' + esc(p.name) +
          (p.last_commit ? ' <span class="muted">@ ' + esc(p.last_commit) + "</span>" : "") + "</div>" +
        '<div class="pc-path" title="' + esc(p.path) + '">' + esc(p.path) + "</div>" +
        '<div class="pc-meta">' +
          (p.files ? p.files + " 文件 · " + p.loc + " 行 · " : "") +
          (p.last_scan ? "上次扫描 " + p.last_scan.replace("T", " ") : "尚未扫描") + "</div>" +
      "</div>" +
      '<div class="pc-score ' + scoreClass(p.last_score) + '">' +
        (p.last_score == null ? "—" : p.last_score) +
        "<small>" + (p.last_open == null ? "" : p.last_open + " 问题") + "</small></div>" +
      '<div class="pc-actions">' +
        '<button class="btn primary tiny" data-act="open">进入看板</button>' +
        '<button class="btn tiny ghost" data-act="remove">移除</button>' +
      "</div></div>").join("");
  document.querySelectorAll("#ap-list .project-card").forEach((card) => {
    const pid = card.dataset.pid;
    card.querySelector('[data-act="open"]').addEventListener("click", () => enterProject(pid));
    card.querySelector('[data-act="remove"]').addEventListener("click", async () => {
      if (!confirm("从监控中移除该项目？可选择同时删除历史数据。")) return;
      const del = confirm("是否同时删除该项目的扫描历史数据库？\n（确定=删除数据，取消=仅移除，数据保留）");
      await api("/api/projects/" + pid + "?delete_data=" + (del ? "1" : "0"), { method: "DELETE" });
      state.boot = await api("/api/boot");
      renderProjectList();
    });
  });
}

function scoreClass(score) {
  if (score == null) return "";
  if (score >= 90) return "good";
  if (score >= 75) return "ok";
  if (score >= 60) return "warn";
  return "bad";
}

$("ap-add").addEventListener("click", async () => {
  const path = $("ap-path").value.trim();
  const name = $("ap-name").value.trim();
  const out = $("ap-result");
  if (!path) { out.className = "cfg-result fail"; out.textContent = "请填写项目路径"; return; }
  const btn = $("ap-add");
  btn.disabled = true; btn.textContent = "正在初始化扫描…";
  out.className = "cfg-result"; out.textContent = "首次扫描中，仓库较大时请耐心等待…";
  try {
    const res = await api("/api/projects", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path, name }),
    });
    state.boot = await api("/api/boot");
    enterProject(res.project.id);
  } catch (e) {
    out.className = "cfg-result fail"; out.textContent = "初始化失败：" + errText(e);
  } finally {
    btn.disabled = false; btn.textContent = "初始化监控";
  }
});

// project switcher
function renderSwitch() {
  const sel = $("project-switch");
  sel.innerHTML = state.boot.projects.map((p) =>
    '<option value="' + p.id + '"' + (p.id === state.projectId ? " selected" : "") + ">" +
    esc(p.name) + "</option>").join("") +
    '<option value="__add__">＋ 添加项目…</option>';
}
$("project-switch").addEventListener("change", (e) => {
  if (e.target.value === "__add__") { showSetup(); return; }
  enterProject(e.target.value);
});

async function enterProject(pid) {
  state.projectId = pid;
  localStorage.setItem("debtscope_pid", pid);
  state.expanded.clear();
  renderSwitch();
  showView("app");
  await loadAll();
}

// ===========================================================================
// dashboard data
// ===========================================================================

async function loadAll() {
  const params = new URLSearchParams();
  const f = state.filters;
  if (f.rule) params.set("rule", f.rule);
  if (f.severity) params.set("severity", f.severity);
  if (f.confidence) params.set("confidence", f.confidence);
  if (f.q) params.set("q", f.q);
  params.set("status", f.status);

  const [overview, rulesResp, findingsResp] = await Promise.all([
    api(P("/overview")),
    api(P("/rules")),
    api(P("/findings?" + params.toString())),
  ]);
  state.overview = overview;
  state.ruleMap = {};
  rulesResp.rules.forEach((r) => { state.ruleMap[r.id] = r; });
  state.findings = findingsResp.findings;
  render();
  const hm = location.hash.match(/^#finding-(\d+)$/);
  if (hm && state.findings.some((x) => x.id === Number(hm[1]))) {
    const id = Number(hm[1]);
    await toggleDetail(id);
    const row = document.querySelector('[data-detail="' + id + '"]');
    if (row) row.scrollIntoView({ block: "center" });
  }

  // A project registered via `debtscope serve <path>` has no snapshot yet —
  // run its initialization scan automatically instead of showing an empty board.
  if (!overview.last_scan && !state.initialScanStarted) {
    state.initialScanStarted = true;
    rescan();
  }
}

function scoreColor(score) {
  if (score >= 90) return "#3fb950";
  if (score >= 75) return "#39c5cf";
  if (score >= 60) return "#d29922";
  return "#f85149";
}

function renderRing(score) {
  const r = 46, c = 2 * Math.PI * r;
  const color = scoreColor(score);
  $("score-ring").innerHTML =
    '<circle cx="54" cy="54" r="' + r + '" fill="none" stroke="#21262d" stroke-width="9"/>' +
    '<circle cx="54" cy="54" r="' + r + '" fill="none" stroke="' + color + '" stroke-width="9"' +
    ' stroke-linecap="round" stroke-dasharray="' + c + '"' +
    ' stroke-dashoffset="' + c * (1 - score / 100) + '"' +
    ' transform="rotate(-90 54 54)" style="transition:stroke-dashoffset .6s"/>';
  $("score-num").textContent = score;
  $("score-num").style.color = color;
  $("score-grade").textContent = "等级 " + state.overview.grade;
  $("score-hint").textContent =
    score >= 90 ? "代码健康，继续保持" :
    score >= 75 ? "存在可控技术债" :
    score >= 60 ? "技术债偏高，建议安排治理" : "技术债堆积，需要优先治理";
}

function renderKPI() {
  const o = state.overview;
  $("repo-name").textContent = o.repo + (o.commit ? "  @ " + o.commit : "");
  $("scan-time").textContent = o.last_scan ? "上次扫描 " + o.last_scan.replace("T", " ") : "尚未扫描";
  renderRing(o.score);
  $("kpi-total").textContent = o.total_open;
  $("kpi-new").textContent = "+" + (o.delta.new || 0);
  $("kpi-resolved").textContent = o.delta.resolved || 0;
  $("kpi-confirm").textContent = o.aggregate.by_confidence ? (o.aggregate.by_confidence.medium || 0) : 0;
  const stats = o.scan_stats || {};
  renderModelBadges(stats.llm_degraded, stats.llm_error);
  $("foot-stats").textContent = stats.loc
    ? stats.files + " 文件 · " + stats.loc + " 行 · " + stats.symbols + " 符号 · 启用指标 " +
      (stats.rules_enabled ?? "-") + "（自定义 " + (stats.rules_custom ?? 0) + "）"
    : "";
}

function donut(containerId, data) {
  const total = data.reduce((s, d) => s + d.value, 0);
  const el = $(containerId);
  if (!total) { el.innerHTML = '<div class="trend-hint">暂无数据</div>'; return; }
  const cx = 90, cy = 90, r = 62, w = 26;
  let angle = -Math.PI / 2;
  const arcs = data.filter((d) => d.value > 0).map((d) => {
    const frac = d.value / total;
    const a0 = angle, a1 = angle + frac * Math.PI * 2;
    angle = a1;
    const large = frac > 0.5 ? 1 : 0;
    const p = (a, rr) => [cx + rr * Math.cos(a), cy + rr * Math.sin(a)];
    const x0y0 = p(a0, r), x1y1 = p(a1, r), x2y2 = p(a1, r - w), x3y3 = p(a0, r - w);
    return '<path d="M' + x0y0[0] + "," + x0y0[1] + " A" + r + "," + r + " 0 " + large + " 1 " +
      x1y1[0] + "," + x1y1[1] + " L" + x2y2[0] + "," + x2y2[1] + " A" + (r - w) + "," + (r - w) +
      " 0 " + large + " 0 " + x3y3[0] + "," + x3y3[1] + ' Z" fill="' + d.color +
      '"><title>' + esc(d.label) + ": " + d.value + "</title></path>";
  }).join("");
  el.innerHTML =
    '<svg viewBox="0 0 180 200" style="max-height:210px">' + arcs +
    '<text x="' + cx + '" y="' + (cy - 4) + '" text-anchor="middle" fill="#e6edf3" font-size="26" font-weight="700">' +
    total + '</text><text x="' + cx + '" y="' + (cy + 16) +
    '" text-anchor="middle" fill="#8b949e" font-size="11">未解决问题</text></svg>' +
    '<div class="donut-legend">' + data.map((d) =>
      '<span><i style="background:' + d.color + '"></i>' + d.label + " " + d.value + "</span>").join("") + "</div>";
}

function hbars(containerId, data, colorFor) {
  const el = $(containerId);
  if (!data.length) { el.innerHTML = '<div class="trend-hint">暂无数据</div>'; return; }
  const max = Math.max.apply(null, data.map((d) => d.value));
  el.innerHTML = data.map((d) =>
    '<div class="bar-row"><div class="bar-label" title="' + esc(d.label) + '">' + esc(d.label) +
    '</div><div class="bar-track"><div class="bar-fill" style="width:' +
    Math.max(3, d.value / max * 100) + '%;background:' + colorFor(d) + '"></div></div>' +
    '<div class="bar-val">' + d.value + "</div></div>").join("");
}

function trendChart() {
  const el = $("chart-trend");
  const pts = state.overview.trend || [];
  if (pts.length < 2) {
    el.innerHTML = '<div class="trend-hint">完成第二次扫描后<br>这里会展示健康分趋势</div>';
    return;
  }
  const W = 320, H = 190, pad = { l: 30, r: 12, t: 14, b: 26 };
  const xs = pts.map((_, i) => pad.l + i * (W - pad.l - pad.r) / Math.max(1, pts.length - 1));
  const ys = pts.map((p) => pad.t + (100 - p.score) / 100 * (H - pad.t - pad.b));
  const line = xs.map((x, i) => (i ? "L" : "M") + x + "," + ys[i]).join(" ");
  const area = line + " L" + xs[xs.length - 1] + "," + (H - pad.b) + " L" + xs[0] + "," + (H - pad.b) + " Z";
  const grid = [0, 25, 50, 75, 100].map((v) => {
    const y = pad.t + (100 - v) / 100 * (H - pad.t - pad.b);
    return '<line x1="' + pad.l + '" y1="' + y + '" x2="' + (W - pad.r) + '" y2="' + y + '" stroke="#20262f"/>' +
      '<text x="' + (pad.l - 5) + '" y="' + (y + 3) + '" text-anchor="end" fill="#5c6672" font-size="9">' + v + "</text>";
  }).join("");
  const dots = xs.map((x, i) =>
    '<circle cx="' + x + '" cy="' + ys[i] + '" r="3" fill="#39c5cf"><title>#' + (i + 1) +
    " " + pts[i].scanned_at + " · " + pts[i].score + "分</title></circle>").join("");
  el.innerHTML = '<svg viewBox="0 0 ' + W + " " + H + '">' + grid +
    '<path d="' + area + '" fill="rgba(57,197,207,.08)"/>' +
    '<path d="' + line + '" fill="none" stroke="#39c5cf" stroke-width="2"/>' + dots +
    '<text x="' + W / 2 + '" y="' + (H - 6) + '" text-anchor="middle" fill="#5c6672" font-size="10">扫描次数 →</text></svg>';
}

function renderChips() {
  const counts = state.overview.aggregate.by_rule || {};
  const chips = ['<span class="chip ' + (!state.filters.rule ? "active" : "") +
    '" data-rule="">全部<b>' + state.overview.total_open + "</b></span>"];
  for (const rid in counts) {
    const rule = state.ruleMap[rid] || { name: rid };
    chips.push('<span class="chip ' + (state.filters.rule === rid ? "active" : "") +
      '" data-rule="' + rid + '">' + esc(rule.name || rid) + "<b>" + counts[rid] + "</b></span>");
  }
  $("rule-chips").innerHTML = chips.join("");
  document.querySelectorAll(".chip").forEach((c) =>
    c.addEventListener("click", () => {
      state.filters.rule = c.dataset.rule;
      state.expanded.clear();
      loadAll();
    }));
}

function renderTable() {
  $("findings-count").textContent = "（" + state.findings.length + "）";
  if (!state.findings.length) {
    $("findings-table").innerHTML = '<div class="empty">没有符合条件的记录</div>';
    return;
  }
  const confLabel = { high: "高置信", medium: "中置信", low: "低置信" };
  const rows = state.findings.map((f) => {
    const rule = state.ruleMap[f.rule_id] || { name: f.rule_id };
    const open = state.expanded.has(f.id);
    return (
      '<tr class="finding-row" data-id="' + f.id + '">' +
      '<td style="width:26px"><span class="sev ' + f.severity + '"></span></td>' +
      '<td style="width:150px"><span class="rule-name">' + esc(rule.name || f.rule_id) + '</span>' +
      '<span class="conf-tag ' + f.confidence + '">' + (confLabel[f.confidence] || f.confidence) + '</span></td>' +
      '<td><div class="msg">' + esc(f.message) + "</div></td>" +
      '<td style="width:210px"><span class="loc">' + esc(f.file) + ":" + f.line + '</span></td>' +
      '<td style="width:84px"><span class="status-tag status-' + f.status + '">' +
      (STATUS_LABEL[f.status] || f.status) + "</span></td></tr>" +
      '<tr class="detail-row" data-detail="' + f.id + '" style="' + (open ? "" : "display:none") + '">' +
      '<td colspan="5"><div class="detail-box" id="detail-' + f.id + '"></div></td></tr>');
  }).join("");
  $("findings-table").innerHTML =
    "<table><tr><th></th><th>类型</th><th>问题</th><th>位置</th><th>状态</th></tr>" + rows + "</table>";
  document.querySelectorAll("tr.finding-row").forEach((tr) =>
    tr.addEventListener("click", () => toggleDetail(Number(tr.dataset.id))));
}

async function toggleDetail(id) {
  const tr = document.querySelector('[data-detail="' + id + '"]');
  const box = $("detail-" + id);
  if (tr.style.display === "none") {
    tr.style.display = "";
    state.expanded.add(id);
    const f = state.findings.find((x) => x.id === id);
    if (!box.dataset.loaded) {
      box.innerHTML = '<div class="muted">加载代码…</div>';
      const code = await api(P("/code?file=" + encodeURIComponent(f.file) + "&around=" + f.line));
      box.dataset.loaded = "1";
      box.innerHTML = renderDetail(f, code);
      box.querySelectorAll(".detail-actions button").forEach((b) =>
        b.addEventListener("click", () => review(Number(b.dataset.id), b.dataset.status)));
    }
  } else {
    tr.style.display = "none";
    state.expanded.delete(id);
  }
}

function renderDetail(f, code) {
  const lines = code.lines.map((l) =>
    '<div class="code-line ' + (l.n === f.line ? "hl" : "") + '"><span class="ln">' + l.n +
    '</span><span class="ct">' + (esc(l.text) || " ") + "</span></div>").join("");
  const meta = [];
  if (f.evidence && f.evidence.decorators && f.evidence.decorators.length)
    meta.push("装饰器: " + f.evidence.decorators.join(", "));
  if (f.evidence && f.evidence.duplicate_of) meta.push("重复来源: " + f.evidence.duplicate_of);
  if (f.evidence && f.evidence.review) meta.push("研判: " + f.evidence.review);
  const todos = ((f.evidence && f.evidence.todos) || []).map((t) =>
    '<div class="code-line"><span class="ln">' + t.line + '</span><span class="ct"># ' +
    t.kind + ": " + esc(t.text) + "</span></div>").join("");
  return (
    (meta.length ? '<div class="evidence-meta">' + esc(meta.join(" · ")) + "</div>" : "") +
    (f.symbol ? '<div class="evidence-meta">符号: ' + esc(f.symbol) + "</div>" : "") +
    "<pre>" + ((f.evidence && f.evidence.todos) ? todos : lines) + "</pre>" +
    (f.suggestion ? '<div class="suggestion"><b>修复建议：</b>' + esc(f.suggestion) + "</div>" : "") +
    (f.note ? '<div class="suggestion">备注：' + esc(f.note) + "</div>" : "") +
    '<div class="detail-actions">' +
    '<button class="btn tiny good" data-id="' + f.id + '" data-status="confirmed">确认问题</button>' +
    '<button class="btn tiny ghost" data-id="' + f.id + '" data-status="false_positive">误报</button>' +
    '<button class="btn tiny amber" data-id="' + f.id + '" data-status="wontfix">暂不处理</button>' +
    "</div>");
}

async function review(id, status) {
  await api(P("/findings/" + id + "/review"), {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ status }),
  });
  await loadAll();
}

function render() {
  renderKPI();
  const sev = state.overview.aggregate.by_severity || {};
  donut("chart-severity", [
    { label: "高", value: sev.high || 0, color: SEV_COLORS.high },
    { label: "中", value: sev.medium || 0, color: SEV_COLORS.medium },
    { label: "低", value: sev.low || 0, color: SEV_COLORS.low },
  ]);
  const ruleCounts = state.overview.aggregate.by_rule || {};
  hbars("chart-rules",
    Object.keys(ruleCounts).map((rid) => ({
      label: (state.ruleMap[rid] && state.ruleMap[rid].name) || rid,
      value: ruleCounts[rid],
      color: SEV_COLORS[(state.ruleMap[rid] && state.ruleMap[rid].severity)] || "#8b949e",
    })),
    (d) => d.color);
  trendChart();
  renderChips();
  renderTable();
}

let searchTimer;
$("search-input").addEventListener("input", (e) => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => { state.filters.q = e.target.value.trim(); loadAll(); }, 250);
});
$("severity-filter").addEventListener("change", (e) => { state.filters.severity = e.target.value; loadAll(); });
$("confidence-filter").addEventListener("change", (e) => { state.filters.confidence = e.target.value; loadAll(); });
$("status-filter").addEventListener("change", (e) => { state.filters.status = e.target.value; loadAll(); });
$("rescan-btn").addEventListener("click", rescan);

async function rescan() {
  const btn = $("rescan-btn");
  btn.disabled = true; btn.textContent = "扫描中…";
  try {
    await api(P("/scan"), { method: "POST" });
    state.boot = await api("/api/boot");
    renderSwitch();
    state.expanded.clear();
    await loadAll();
    toast("扫描完成");
  } catch (e) {
    alert("扫描失败: " + errText(e));
  } finally {
    btn.disabled = false; btn.textContent = "重新扫描";
  }
}

// ===========================================================================
// rules manager
// ===========================================================================

$("rules-btn").addEventListener("click", openRules);
$("rm-close").addEventListener("click", () => { $("rules-modal").hidden = true; });
$("rules-modal").addEventListener("click", (e) => {
  if (e.target.id === "rules-modal") $("rules-modal").hidden = true;
});
$("rm-new").addEventListener("click", () => openRuleEditor(null));
$("rm-back").addEventListener("click", () => {
  $("rm-edit-view").hidden = true; $("rm-list-view").hidden = false;
});

async function openRules() {
  $("rules-project-name").textContent = "· " + state.overview.repo;
  $("rules-modal").hidden = false;
  $("rm-edit-view").hidden = true; $("rm-list-view").hidden = false;
  await refreshRuleList();
}

async function refreshRuleList() {
  const resp = await api(P("/rules"));
  $("rm-list").innerHTML = resp.rules.map((r) =>
    '<div class="rm-row' + (r.enabled ? "" : " off") + '">' +
      '<label class="rm-switch"><input type="checkbox" data-act="enable" ' +
        (r.enabled ? "checked" : "") + '> 启用</label>' +
      '<div class="rm-main"><div class="rm-name">' + esc(r.name) +
        (r.builtin ? '<span class="tag builtin">内置</span>' : '<span class="tag custom">自定义</span>') +
        '<span class="tag kind">' + esc(r.kind_label) + "</span></div>" +
        '<div class="rm-desc">' + esc(r.description) + "</div></div>" +
      '<select class="rm-sev" data-act="severity">' +
        ["high", "medium", "low"].map((s) =>
          '<option value="' + s + '"' + (r.severity === s ? " selected" : "") + ">" + SEV_LABEL[s] + "</option>").join("") +
      "</select>" +
      '<div class="rm-row-actions">' +
        '<button class="btn tiny" data-act="edit">编辑</button>' +
        (r.builtin
          ? '<button class="btn tiny ghost" data-act="reset">重置</button>'
          : '<button class="btn tiny ghost danger" data-act="delete">删除</button>') +
      "</div></div>").join("");

  document.querySelectorAll("#rm-list .rm-row").forEach((rowEl, i) => {
    const r = resp.rules[i];
    rowEl.querySelector('[data-act="enable"]').addEventListener("change", async (e) => {
      await api(P("/rules/" + r.id), {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: e.target.checked }),
      });
      await refreshRuleList();
    });
    rowEl.querySelector('[data-act="severity"]').addEventListener("change", async (e) => {
      await api(P("/rules/" + r.id), {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ severity: e.target.value }),
      });
    });
    rowEl.querySelector('[data-act="edit"]').addEventListener("click", () => openRuleEditor(r));
    const del = rowEl.querySelector('[data-act="delete"]');
    if (del) del.addEventListener("click", async () => {
      if (!confirm("删除该自定义指标？删除并重新扫描后，相关历史记录将不再更新。")) return;
      await api(P("/rules/" + r.id), { method: "DELETE" });
      await refreshRuleList();
      toast("已删除，重新扫描后生效");
    });
    const reset = rowEl.querySelector('[data-act="reset"]');
    if (reset) reset.addEventListener("click", async () => {
      await api(P("/rules/" + r.id + "/reset"), { method: "POST" });
      await refreshRuleList();
      toast("已重置为默认");
    });
  });
}

function creatableKinds() {
  return Object.keys(state.boot.rule_kinds)
    .filter((k) => state.boot.rule_kinds[k].creatable);
}

function openRuleEditor(rule) {
  state.editingRule = rule;
  $("rm-list-view").hidden = true; $("rm-edit-view").hidden = false;
  $("rm-preview").textContent = ""; $("rm-preview").className = "cfg-result";
  const kindSel = $("rm-kind");
  const kinds = creatableKinds();
  kindSel.innerHTML = kinds.map((k) =>
    '<option value="' + k + '">' + esc(state.boot.rule_kinds[k].label) + "（" + k + "）</option>").join("");
  kindSel.disabled = !!rule;  // kind is fixed when editing
  document.querySelector(".rm-ai").style.display = rule ? "none" : "block";
  $("rm-nl").value = "";

  if (rule) {
    $("rm-name").value = rule.name;
    kindSel.value = rule.kind;
    $("rm-severity").value = rule.severity;
    renderParams(rule.kind, rule.params || {});
  } else {
    $("rm-name").value = "";
    kindSel.value = kinds[0];
    $("rm-severity").value = state.boot.rule_kinds[kinds[0]].default_severity;
    renderParams(kinds[0], state.boot.rule_kinds[kinds[0]].default_params);
  }
}

$("rm-kind").addEventListener("change", () => {
  const k = $("rm-kind").value;
  renderParams(k, state.boot.rule_kinds[k].default_params);
});

function renderParams(kind, values) {
  const schema = state.boot.rule_kinds[kind].params_schema || {};
  const labels = {
    max_lines: "行数阈值", max_count: "数量阈值", min_lines: "最小行数",
    min_stmts: "最小语句数", max_args: "参数个数阈值", max_depth: "嵌套层数阈值",
    patterns: "禁用调用名（逗号分隔，如 print,eval,os.system）",
    target: "检查对象", regex: "命名正则（完整匹配）", message: "不合规提示语",
  };
  $("rm-params").innerHTML = Object.keys(schema).map((key) => {
    const val = values[key] ?? "";
    let input;
    if (key === "target") {
      input = '<select data-param="' + key + '"><option value="function"' +
        (val === "function" ? " selected" : "") + ">函数/方法</option><option value=\"class\"" +
        (val === "class" ? " selected" : "") + ">类</option></select>";
    } else if (schema[key] === "int") {
      input = '<input type="number" data-param="' + key + '" value="' + esc(String(val)) + '">';
    } else {
      input = '<input type="text" data-param="' + key + '" value="' + esc(String(val)) + '" spellcheck="false">';
    }
    return "<label>" + (labels[key] || key) + "</label>" + input;
  }).join("");
}

function collectRule() {
  const kind = $("rm-kind").value;
  const schema = state.boot.rule_kinds[kind].params_schema || {};
  const params = {};
  document.querySelectorAll("#rm-params [data-param]").forEach((el) => {
    let v = el.value.trim();
    if (schema[el.dataset.param] === "int") v = Number(v) || 0;
    params[el.dataset.param] = v;
  });
  return {
    name: $("rm-name").value.trim() || state.boot.rule_kinds[kind].label,
    kind,
    severity: $("rm-severity").value,
    params,
  };
}

$("rm-ai-btn").addEventListener("click", async () => {
  const desc = $("rm-nl").value.trim();
  if (!desc) { toast("请先描述你想要的指标"); return; }
  const btn = $("rm-ai-btn");
  btn.disabled = true; btn.textContent = "生成中…";
  try {
    const res = await api(P("/rules/generate"), {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ description: desc }),
    });
    const r = res.rule;
    $("rm-name").value = r.name;
    $("rm-kind").value = r.kind;
    $("rm-severity").value = r.severity;
    renderParams(r.kind, r.params);
    toast("已生成，请试跑预览确认");
  } catch (e) {
    alert("生成失败：" + errText(e));
  } finally {
    btn.disabled = false; btn.textContent = "生成指标";
  }
});

$("rm-preview-btn").addEventListener("click", async () => {
  const rule = collectRule();
  const out = $("rm-preview");
  out.className = "cfg-result"; out.textContent = "正在当前项目上试跑…";
  try {
    const res = await api(P("/rules/preview"), {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(rule),
    });
    let text = "试跑结果：命中 " + res.count + " 条";
    if (res.samples.length) {
      text += "\n样例：\n" + res.samples.map((s) =>
        "· " + s.file + ":" + s.line + " " + (s.symbol ? s.symbol + " — " : "") + s.message).join("\n");
    }
    out.className = "cfg-result " + (res.count ? "ok" : "");
    out.style.whiteSpace = "pre-wrap";
    out.textContent = text + (res.count ? "" : "（阈值可能过严，或当前仓库确实没有此类问题）");
  } catch (e) {
    out.className = "cfg-result fail";
    out.textContent = "试跑失败：" + errText(e);
  }
});

$("rm-save").addEventListener("click", async () => {
  const rule = collectRule();
  const editing = state.editingRule;
  try {
    if (editing) {
      await api(P("/rules/" + editing.id), {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(rule),
      });
    } else {
      await api(P("/rules"), {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(rule),
      });
    }
    $("rules-modal").hidden = true;
    toast("指标已保存，正在重新扫描…");
    await rescan();
    await openRules();
  } catch (e) {
    alert("保存失败：" + errText(e));
  }
});

// ===========================================================================
// start
// ===========================================================================

boot().catch((e) => {
  document.body.innerHTML =
    '<div class="fatal">启动失败：' + esc(e.message) + "<br>请确认 debtscope 服务正在运行。</div>";
});
