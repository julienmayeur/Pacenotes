"use strict";

// Admin panel: clients, client sheet (filled by its web Auto-fill, claude -p Sonnet; double-click a
// cell to edit it), the interviewee profile cards (claude -p Sonnet), "prochain client",
// research + pre-plan (claude -p, Opus).
// Every string comes from the shared helper (static/i18n.js, catalogs in pacenote/locales/,
// keys under "admin."). Switching the screen language re-renders the texts in place: form fields
// being edited are left alone.
const t = (key, params) => i18n.t(key, params);
const when = (iso) => String(iso || "").replace("T", " ");
// languages offered for the interview: the UI languages plus common spoken ones (any code via "Other…")
const interviewLangs = () => [...new Set([...i18n.languages.map((l) => l.code),
  ...i18n.speechLocales.map((x) => x.split("-")[0])])].sort();

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
// a link found by a web agent: only http(s) ever becomes an href (never javascript:, data:…), escaped
const safeHref = (u) => esc(/^https?:\/\//i.test(String(u ?? "").trim()) ? String(u).trim() : "#");
const clock = (sec) => `${String(Math.floor(sec / 60)).padStart(2, "0")}:${String(Math.floor(sec % 60)).padStart(2, "0")}`;

let current = null;      // slug
let detail = null;       // last /admin/api/clients/<slug>
let poll = null;

async function api(path, method = "GET", body) {
  const res = await fetch(path, { method, headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || res.statusText);
  return data;
}

// --- client list ------------------------------------------------------------------------------
async function loadClients(select) {
  const { clients, next } = await api("/admin/api/clients");
  const ul = $("clients");
  ul.innerHTML = "";
  for (const c of clients) {
    const li = document.createElement("li");
    li.dataset.slug = c.slug;
    li.innerHTML = `<span class="n">${esc(c.name)}</span><span class="s">${esc(c.sector || c.slug)}</span>
      <span class="badges">${c.next ? `<span class="badge next">${esc(t("admin.badge.next"))}</span>` : ""}
      ${c.has_preplan ? `<span class="badge ok">${esc(t("admin.badge.preplan"))}</span>` : ""}
      ${c.researching ? `<span class="badge run">${esc(t("admin.badge.running"))}</span>` : ""}</span>`;
    li.classList.toggle("sel", c.slug === current);
    li.addEventListener("click", () => openClient(c.slug));
    ul.appendChild(li);
  }
  const want = select || current || new URLSearchParams(location.search).get("client") || next || (clients[0] && clients[0].slug);
  if (want && want !== current) await openClient(want);
}

$("new-client").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("new-error").textContent = "";
  try {
    const d = await api("/admin/api/clients", "POST", { name: $("new-name").value, language: $("new-lang").value || "fr" });
    $("new-name").value = "";
    await loadClients(d.slug);
    autofill.request({});
  } catch (err) { $("new-error").textContent = err.message; }
});

// --- one client --------------------------------------------------------------------------------
async function openClient(slug) {
  if (slug !== current) { autofill.reset(); people.reset(); }
  current = slug;
  history.replaceState(null, "", `/admin?client=${encodeURIComponent(slug)}`);
  [...$("clients").children].forEach((li) => li.classList.toggle("sel", li.dataset.slug === slug));
  detail = await api(`/admin/api/clients/${encodeURIComponent(slug)}`);
  $("empty").classList.add("hidden");
  $("client").classList.remove("hidden");
  fillInfo(detail);
  fillResearch(detail);
  watchResearch(detail.research);
  loadSessions(slug).catch(() => {});
  loadMetrics(slug).catch((e) => { $("metrics-error").textContent = e.message; });
}

// --- metric monitoring: one small chart per metric, baseline and target lines, add a measurement --
let metricsVersion = 0;  // the metrics.yaml version rendered: sent back so a stale page is refused (409)
async function loadMetrics(slug) {
  const data = await api(`/admin/api/clients/${encodeURIComponent(slug)}/metrics`);
  if (slug === current) renderMetrics(data);
}
function fmtNum(v) { return v == null ? "—" : String(Math.round(v * 100) / 100); }
function renderMetrics(data) {
  metricsVersion = data.version;
  $("metrics-error").textContent = "";
  $("metrics-empty").classList.toggle("hidden", (data.metrics || []).length > 0);
  $("metrics").innerHTML = (data.metrics || []).map((m) => {
    const s = m.summary || {};
    const progress = s.progress == null ? "" : ` · ${Math.round(s.progress * 100)} %`;
    const rows = (m.measurements || []).slice(0, 5).map((x) => `<li><span class="sid">${esc(x.date)}</span> <b>${esc(fmtNum(x.value))}</b>
      <span class="muted small">${esc(x.source)}${x.note ? ` · ${esc(x.note)}` : ""}</span></li>`).join("");
    return `<article class="metric" data-id="${esc(m.id)}">
      <div class="metric-head"><b>${esc(m.name)}</b> <span class="muted small">${esc(m.unit)}${m.recipe ? ` · ${esc(m.recipe)}` : ""}</span>
        <span class="state ${esc(s.state)}">${esc(t(`admin.metrics.state.${s.state}`))}${esc(progress)}</span></div>
      <div class="metric-body">
        <div class="metric-chart-box">${m.svg || `<p class="muted small">${esc(t("admin.metrics.noData"))}</p>`}
          <div class="legend small"><span class="lg baseline">${esc(t("admin.metrics.baseline"))} ${esc(fmtNum(m.baseline.value))}</span>
            <span class="lg target">${esc(t("admin.metrics.target"))} ${esc(fmtNum(m.target.value))}</span>
            <span class="lg last">${esc(t("admin.metrics.last"))} ${esc(fmtNum(s.last))}${s.last_date ? ` (${esc(s.last_date)})` : ""}</span></div></div>
        <ul class="measurements">${rows}</ul>
      </div>
      <form class="add-measure">
        <input name="value" type="number" step="any" required data-i18n-attr="placeholder:admin.metrics.value" placeholder="${esc(t("admin.metrics.value"))}">
        <input name="date" type="date">
        <input name="note" placeholder="${esc(t("admin.metrics.note"))}">
        <button type="submit">${esc(t("admin.metrics.add"))}</button>
      </form>
    </article>`;
  }).join("");
  $("metrics").querySelectorAll("form.add-measure").forEach((form) => form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const id = form.closest(".metric").dataset.id;
    const slug = current;
    try {
      const data = await api(`/admin/api/clients/${encodeURIComponent(slug)}/metrics/${encodeURIComponent(id)}/measurements`, "POST",
        { value: Number(form.value.value), date: form.date.value || null, note: form.note.value, version: metricsVersion });
      if (slug === current) renderMetrics(data);
    } catch (err) {
      await loadMetrics(slug).catch(() => {});  // stale (changed from the CLI or another tab): show the fresh file
      $("metrics-error").textContent = err.message;
    }
  }));
}

// --- saved interviews of this client, each with its Plan view (/admin/plan/<client>/<session>) --
let sessions = [];
async function loadSessions(slug) {
  sessions = (await api(`/admin/api/clients/${encodeURIComponent(slug)}/sessions`)).sessions || [];
  if (slug === current) renderSessions();
}
function renderSessions() {
  const sid = (id) => id.replace(/^(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})$/, "$1-$2-$3 $4:$5");
  $("sessions").innerHTML = sessions.map((s) => `<li><span class="sid">${esc(sid(s.id))}</span>
    <span class="muted small">${esc(t("admin.sessions.answered", { n: s.answered }))}</span>
    ${s.has_plan ? `<a href="/admin/plan/${encodeURIComponent(current)}/${encodeURIComponent(s.id)}" target="_blank" rel="noopener">${esc(t("admin.sessions.openPlan"))}</a>`
      : `<span class="muted small">${esc(t("admin.sessions.noPlan"))}</span>`}</li>`).join("");
  $("sessions-empty").classList.toggle("hidden", sessions.length > 0);
}

function fillInfo(d) {
  const i = d.info;
  $("c-name").textContent = i.name;
  $("c-slug").textContent = `clients/${d.slug}`;
  $("c-next").checked = d.next;
  $("f-name").value = i.name; $("f-language").value = i.language; $("f-notes").value = i.notes;
  $("f-use-preplan").checked = i.use_preplan;
  $("f-contacts").innerHTML = "";
  (i.contacts.length ? i.contacts : [{ name: "", role: "" }]).forEach(addContact);
  fillSheet(d);
  fillSaveState(d);
}
function fillSaveState(d) {
  $("save-state").textContent = d && d.info.updated_at ? t("admin.saved", { when: when(d.info.updated_at) }) : "";
}

function addContact(c = { name: "", role: "" }) {
  const li = document.createElement("li");
  li.innerHTML = `<input class="c-name" data-i18n-attr="placeholder:admin.contactName" value="${esc(c.name)}">` +
    `<input class="c-role" data-i18n-attr="placeholder:admin.contactRole" value="${esc(c.role)}">
    <button type="button" class="ghost icon c-research" data-i18n-attr="title:admin.person.research">🔎</button>
    <button type="button" class="ghost icon c-remove" data-i18n-attr="title:admin.remove">×</button>`;
  i18n.translatePage(li);
  li.querySelector(".c-remove").addEventListener("click", () => { li.remove(); renderProfiles(); });
  li.querySelector(".c-research").addEventListener("click", () => people.request(li, { force: true }));
  li.querySelector(".c-name").addEventListener("input", () => people.typed(li));
  li.querySelector(".c-name").addEventListener("blur", () => renderProfiles());
  $("f-contacts").appendChild(li);
}
$("add-contact").addEventListener("click", () => addContact());

// the whole sheet as the page holds it: the form inputs + the cells (company fields, linked
// entities, provenance) kept in `sheet`
function contactsNow() {
  return [...$("f-contacts").children].map((li) => ({ name: li.querySelector(".c-name").value, role: li.querySelector(".c-role").value }));
}
function currentInfo() {
  return { ...sheet, name: $("f-name").value, language: $("f-language").value, contacts: contactsNow(),
    notes: $("f-notes").value, use_preplan: $("f-use-preplan").checked };
}
async function saveSheet() {
  try {
    detail = await api(`/admin/api/clients/${encodeURIComponent(current)}`, "PUT", currentInfo());
    fillSheet(detail);
    fillSaveState(detail);
    $("c-name").textContent = detail.info.name;
    loadClients(current).catch(() => {});
  } catch (err) { $("save-state").textContent = t("admin.error", { error: err.message }); }
}
$("info-form").addEventListener("submit", (e) => { e.preventDefault(); saveSheet(); });

$("c-next").addEventListener("change", async () => {
  await api("/admin/api/next", "POST", { slug: $("c-next").checked ? current : null });
  await loadClients(current);
});

// --- research + pre-plan ---------------------------------------------------------------------------
function fillResearch(d, keepEdits) {
  $("gen-info").textContent = d.generated_at ? t("admin.generated", { when: when(d.generated_at), model: d.model || "" }) : "";
  $("summary").textContent = d.summary || "";
  $("preplan-view").innerHTML = d.preplan_html || `<p class="empty">${esc(t("admin.noPreplan"))}</p>`;
  $("research-view").innerHTML = d.research_html || `<p class="empty">${esc(t("admin.noResearch"))}</p>`;
  if (!keepEdits) { $("preplan-text").value = d.preplan_md; $("research-text").value = d.research_md; }
  $("run-research").textContent = d.preplan_md ? t("admin.rerun") : t("admin.run");
  const lang = d.info && d.info.language;
  $("seed").innerHTML = (d.seed_questions || []).map((q) => `<li><div class="q">${esc(q[lang] || q[i18n.language] || q.fr || q.en)}</div>
    <div class="uc">${esc(q.use_case || "")} · ${esc(t("admin.priority", { p: q.priority }))}</div>
    <div class="rungs">${["business", "workflow", "implementation"].map((r) => (q.expect && q.expect[r] || []).length
      ? `<span class="rung"><b>${esc(t(`focus.rungs.${r}`))}</b>${esc(q.expect[r].join(" · "))}</span>` : "").join("")}</div></li>`).join("")
    || `<li class="muted">${esc(t("admin.noQuestions"))}</li>`;
  $("cases").innerHTML = (d.use_cases || []).map((u) => `<tr><td><b>${esc(u.title)}</b><br><span class="muted small">${esc(u.description)}</span></td>
    <td class="num">${esc(u.value)}/5</td><td class="num">${esc(u.feasibility)}/5</td><td>${esc(u.kind === "quick_win" ? t("admin.quickWin") : t("admin.bigBet"))}${u.already_done ? ` · ${esc(t("admin.alreadyDone"))}` : ""}</td></tr>`).join("");
  $("history").innerHTML = (d.history || []).map((h) => `<li><span class="v">${esc(h.version)}</span>
    <button class="ghost" data-view="${esc(h.version)}">${esc(t("admin.view"))}</button><button class="ghost" data-restore="${esc(h.version)}">${esc(t("admin.restore"))}</button></li>`).join("")
    || `<li class="muted">${esc(t("admin.noHistory"))}</li>`;
}

function watchResearch(r) {
  clearInterval(poll);
  showResearch(r);
  if (r && r.status === "running") {
    poll = setInterval(async () => {
      const s = await api(`/admin/api/clients/${encodeURIComponent(current)}/research`);
      showResearch(s);
      if (s.status !== "running") {
        clearInterval(poll);
        detail = await api(`/admin/api/clients/${encodeURIComponent(current)}`);
        fillResearch(detail);
        loadClients(current);
      }
    }, 1500);
  }
}

let lastResearch = null;
function showResearch(r) {
  lastResearch = r;
  const running = r && r.status === "running";
  $("run-research").disabled = running;
  $("stop-research").classList.toggle("hidden", !running);
  const st = $("research-status");
  st.className = "research-status" + (running ? " running" : r && r.status === "error" ? " err" : "");
  if (!r || r.status === "idle") st.textContent = "";
  else if (running) st.textContent = t("admin.status.running", { time: clock(r.seconds), searches: r.searches, pages: r.pages });
  else if (r.status === "done") st.textContent = t("admin.status.done", { time: clock(r.seconds) }) + (r.archived ? ` · ${t("admin.status.archived", { v: r.archived })}` : "");
  else if (r.status === "stopped") st.textContent = t("admin.status.stopped");
  else st.textContent = t("admin.status.error", { error: r.error || t("admin.status.unknownError") });
  renderSteps($("steps"), (r && r.steps) || []);
}

function renderSteps(ol, steps) {
  if (steps.length < ol.children.length) ol.innerHTML = "";
  ol.classList.toggle("hidden", steps.length === 0);
  // append only the new steps (no re-render, the list just grows)
  for (let i = ol.children.length; i < steps.length; i++) {
    const s = steps[i];
    const li = document.createElement("li");
    li.className = s.kind;
    // steps carry a catalog key + params: shown in the screen language (text = server-side fallback)
    li.innerHTML = `<span class="t">${clock(s.t)}</span>${esc(s.key ? t(s.key, s.params) : s.text)}`;
    ol.appendChild(li);
  }
  ol.scrollTop = ol.scrollHeight;
}

$("run-research").addEventListener("click", async () => {
  if (detail && detail.preplan_md && !confirm(t("admin.confirmRerun"))) return;
  $("steps").innerHTML = "";
  const r = await api(`/admin/api/clients/${encodeURIComponent(current)}/research`, "POST");
  watchResearch(r);
  loadClients(current);
});
$("stop-research").addEventListener("click", () => api(`/admin/api/clients/${encodeURIComponent(current)}/research/stop`, "POST"));

// --- the company cells: filled by Auto-fill, double-click to edit ---------------------------------------
// Auto-fill (pacenote/autofill.py) runs after the company name is typed (debounced, and on leaving
// the field), right after a client is created, and from the button. Its answer lands straight in the
// cells and is saved, with its provenance (source, confidence). A value the FDE edits becomes
// "manual" and is never overwritten by a later run, even after a reload (provenance is in client.yaml).
// To edit: double-click a cell (or Enter on it); Enter or leaving saves, Esc cancels.
const GROUPS = {
  "cells-registered": [["legal_name", "admin.legalName"], ["registration_number", "admin.registrationNumber"],
    ["registered_address", "admin.registeredAddress", "wide"]],
  "cells-company": [["website", "admin.website"], ["linkedin_url", "admin.linkedin"], ["sector", "admin.sector"],
    ["country", "admin.country"], ["hq_city", "admin.hqCity"], ["headcount", "admin.headcount"],
    ["working_languages", "admin.workingLanguages"], ["description", "admin.description", "wide long"],
    ["products", "admin.products", "wide"], ["leadership", "admin.leadership", "wide"]],
};
const SHEET_FIELDS = Object.values(GROUPS).flat().map(([k]) => k);
const AUTOFILL_DEBOUNCE = 1200;
const fold = (x) => String(x || "").normalize("NFKD").replace(/[̀-ͯ]/g, "").toLowerCase().replace(/\s+/g, " ").trim();
const host = (url) => { try { return new URL(url).hostname.replace(/^www\./, ""); } catch { return url; } };
let sheet = {};          // the saved sheet (company fields, linked_entities, provenance, removed_entities)
let relations = ["distributed_brand", "brand_entity", "parent", "subsidiary", "sister", "other"];

function isManual(k) {  // an empty field is nobody's: cleared, it goes back to the agent
  if (!String(sheet[k] || "").trim()) return false;
  const p = (sheet.provenance || {})[k];
  return p ? p.by === "manual" : true;
}

// a discreet source mark: a tiny link with its host and confidence as a tooltip
function sourceMark(src, conf, low, dead) {
  if (!src) return "";
  const pct = Math.round((conf ?? 0) * 100);
  const tip = dead ? t("admin.cell.deadSource", { host: host(src) }) : low ? t("admin.cell.low", { host: host(src), pct })
    : t("admin.cell.source", { host: host(src), pct });
  return `<a class="src" href="${safeHref(src)}" target="_blank" rel="noopener" title="${esc(tip)}" aria-label="${esc(t("admin.cell.open"))}">↗</a>`;
}

function fillSheet(d) {
  sheet = JSON.parse(JSON.stringify(d.info));
  sheet.provenance = sheet.provenance || {};
  sheet.linked_entities = sheet.linked_entities || [];
  sheet.removed_entities = sheet.removed_entities || [];
  renderCells();
  renderLinked();
  people.load(d.profiles || {});
}

function renderCells() {
  for (const [box, fields] of Object.entries(GROUPS)) {
    $(box).innerHTML = fields.filter(([k]) => k in sheet).map(([k, label, cls]) => {
      const v = String(sheet[k] || ""), p = sheet.provenance[k] || {};
      const agent = p.by === "agent", low = agent && (p.low || p.dead_source);
      const tip = isManual(k) && v ? t("admin.cell.manual") : agent ? t("admin.cell.agent") : t("admin.cell.editHint");
      return `<div class="cell ${cls || ""}${v ? "" : " empty"}${low ? " low" : ""}${agent ? " agent" : ""}" data-field="${k}" tabindex="0" title="${esc(tip)}">
        <span class="cl">${esc(t(label))}</span><span class="cv">${esc(v)}</span>${agent ? sourceMark(p.source_url, p.confidence, p.low, p.dead_source) : ""}</div>`;
    }).join("");
  }
}

// inline editing: one helper for cells, linked-entity cells and profile facts
function editInline(el, value, { long = false, options = null, onSave }) {
  if (el.querySelector(".inline")) return;
  const old = el.innerHTML;
  const input = document.createElement(options ? "select" : long ? "textarea" : "input");
  input.className = "inline";
  if (options) input.innerHTML = options.map(([v, label]) => `<option value="${esc(v)}"${v === value ? " selected" : ""}>${esc(label)}</option>`).join("");
  else input.value = value;
  if (long) input.rows = 3;
  el.innerHTML = "";
  el.appendChild(input);
  input.focus();
  if (input.select) input.select();
  let done = false;
  const finish = async (save) => {
    if (done) return;
    done = true;
    const v = input.value.trim();
    el.innerHTML = old;
    if (save && v !== String(value || "").trim()) await onSave(v);
  };
  input.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { e.preventDefault(); finish(false); }
    else if (e.key === "Enter" && !(long && e.shiftKey)) { e.preventDefault(); finish(true); }
  });
  input.addEventListener("blur", () => finish(true));
  if (options) input.addEventListener("change", () => finish(true));
}

function startCellEdit(cell) {
  const k = cell.dataset.field;
  editInline(cell.querySelector(".cv"), sheet[k] || "", { long: cell.classList.contains("long"), onSave: async (v) => {
    sheet[k] = v;
    if (v) sheet.provenance[k] = { by: "manual" };  // the FDE's value: Auto-fill never overwrites it
    else delete sheet.provenance[k];  // cleared: no longer his, the next run fills it
    renderCells();
    await saveSheet();
  } });
}
for (const box of Object.keys(GROUPS)) {
  $(box).addEventListener("dblclick", (e) => { const c = e.target.closest(".cell"); if (c && !e.target.closest("a")) startCellEdit(c); });
  $(box).addEventListener("keydown", (e) => {
    const c = e.target.closest(".cell");
    if (c && e.target === c && e.key === "Enter") { e.preventDefault(); startCellEdit(c); }
  });
}

// --- linked brands / entities: same cells, a discreet × on hover -------------------------------------------
const entityKey = (e) => fold(e.name) || host(e.website || "");
function renderLinked() {
  const rows = sheet.linked_entities || [];
  $("linked").classList.toggle("hidden", rows.length === 0);
  $("linked-empty").classList.toggle("hidden", rows.length > 0);
  $("linked-rows").innerHTML = rows.map((e, i) => {
    const low = e.by === "agent" && e.confidence != null && e.confidence < 0.6;
    const link = (u) => `<span class="cv">${esc(String(u || "").replace(/^https?:\/\/(www\.)?/, "").replace(/\/$/, ""))}</span>`;
    return `<tr data-i="${i}" class="${low ? "low" : ""}${e.by === "agent" ? " agent" : ""}" title="${esc(e.by === "manual" ? t("admin.cell.manual") : t("admin.cell.editHint"))}">
      <td class="cell" data-k="name" tabindex="0"><span class="cv">${esc(e.name)}</span>${e.by === "agent" ? sourceMark(e.source_url, e.confidence, low, e.dead_source) : ""}</td>
      <td class="cell" data-k="relation" tabindex="0"><span class="cv">${esc(t(`admin.linked.relation.${e.relation || "other"}`))}</span></td>
      <td class="cell" data-k="website" tabindex="0">${link(e.website)}${e.website ? `<a class="src" href="${safeHref(e.website)}" target="_blank" rel="noopener" title="${esc(e.website)}">↗</a>` : ""}</td>
      <td class="cell" data-k="linkedin_url" tabindex="0">${link(e.linkedin_url)}${e.linkedin_url ? `<a class="src" href="${safeHref(e.linkedin_url)}" target="_blank" rel="noopener" title="${esc(e.linkedin_url)}">↗</a>` : ""}</td>
      <td class="rm"><button type="button" class="ghost icon" data-rm="${i}" title="${esc(t("admin.cell.remove"))}">×</button></td></tr>`;
  }).join("");
}
function startEntityEdit(td) {
  const i = Number(td.closest("tr").dataset.i), k = td.dataset.k, e = sheet.linked_entities[i];
  const opts = k === "relation" ? relations.map((r) => [r, t(`admin.linked.relation.${r}`)]) : null;
  editInline(td.querySelector(".cv"), e[k] || "", { options: opts, onSave: async (v) => {
    if (k === "name" && !v) return;
    sheet.linked_entities[i] = { ...e, [k]: v, by: "manual" };
    renderLinked();
    await saveSheet();
  } });
}
$("linked-rows").addEventListener("dblclick", (e) => { const td = e.target.closest("td.cell"); if (td && !e.target.closest("a")) startEntityEdit(td); });
$("linked-rows").addEventListener("keydown", (e) => {
  const td = e.target.closest("td.cell");
  if (td && e.target === td && e.key === "Enter") { e.preventDefault(); startEntityEdit(td); }
});
$("linked-rows").addEventListener("click", async (e) => {
  const b = e.target.closest("button[data-rm]");
  if (!b) return;
  const [gone] = sheet.linked_entities.splice(Number(b.dataset.rm), 1);
  if (gone.by === "agent") sheet.removed_entities = [...new Set([...sheet.removed_entities, entityKey(gone)])];  // not brought back
  renderLinked();
  await saveSheet();
});

// --- Auto-fill: the run, then its answer straight into the sheet ---------------------------------------------
const autofill = {
  enabled: false,
  job: null,          // last view of the current run
  timer: null, poll: null,
  lastKey: null,      // name|lang|candidate of the last request: the same one is not sent again
  nameTyped: false,   // the FDE typed in the name since the sheet was opened
  applied: new Set(), // runs already written into the sheet
  written: null,      // what the last apply wrote: {fields, linked}

  async probe() {
    try {
      const res = await fetch("/admin/api/autofill");
      const info = res.ok ? await res.json() : {};
      this.enabled = info.enabled === true;
      if (Array.isArray(info.relations)) relations = info.relations;
      people.enabled = this.enabled && !!info.people;
    } catch { this.enabled = false; }
    $("autofill").classList.toggle("hidden", !this.enabled);
    renderProfiles();
  },

  reset() {
    clearTimeout(this.timer); clearInterval(this.poll);
    if (this.job && this.job.status === "running") this.stop();
    this.job = null; this.lastKey = null; this.nameTyped = false; this.written = null;
    $("autofill-steps").innerHTML = "";
    this.show();
  },

  fillable() { return SHEET_FIELDS.filter((k) => k in sheet && !isManual(k)); },

  async request({ force = false, candidate = null }) {
    const name = $("f-name").value.trim();
    if (!this.enabled || !current || name.length < 2) return;
    if (!candidate && !force && this.fillable().length === 0) return;  // every field is the FDE's
    const lang = $("f-language").value || "fr";
    const key = `${fold(name)}|${lang}|${fold(candidate && (candidate.website || candidate.name))}`;
    if (!force && key === this.lastKey) return;  // same name again: no new run
    this.lastKey = key;
    clearTimeout(this.timer);
    if (this.job && this.job.status === "running") this.stop();
    const forSlug = current;
    // what the FDE typed is trusted and not searched again
    const hints = Object.fromEntries(SHEET_FIELDS.filter((k) => k in sheet && isManual(k) && String(sheet[k] || "").trim()).map((k) => [k, sheet[k]]));
    try {
      const job = await api("/admin/api/autofill", "POST", { name, lang, force, candidate, hints });
      if (forSlug !== current) return;
      this.applied.delete(job.id);  // a cached answer asked again is written again (manual fields stay)
      this.watch(job);
    } catch (err) {
      this.job = { status: "error", error: err.message, steps: [] };
      this.show();
    }
  },

  stop() {
    if (this.job && this.job.id) api(`/admin/api/autofill/${encodeURIComponent(this.job.id)}/stop`, "POST").catch(() => {});
  },

  watch(job) {
    clearInterval(this.poll);
    $("autofill-steps").innerHTML = "";
    this.update(job);
    if (job.status !== "running") return;
    const id = job.id;
    this.poll = setInterval(async () => {
      let j;
      try { j = await api(`/admin/api/autofill/${encodeURIComponent(id)}`); } catch { clearInterval(this.poll); return; }
      if (!this.job || this.job.id !== id) { clearInterval(this.poll); return; }  // superseded
      this.update(j);
      if (j.status !== "running") clearInterval(this.poll);
    }, 1000);
  },

  async update(job) {
    this.job = job;
    const r = job.result;
    if (job.status === "done" && r && r.status === "fields" && !this.applied.has(job.id) && current) {
      this.applied.add(job.id);
      const forSlug = current;
      try {
        const d = await api(`/admin/api/clients/${encodeURIComponent(forSlug)}/autofill/${encodeURIComponent(job.id)}/apply`,
          "POST", { info: currentInfo() });
        if (forSlug === current) {
          detail = d;
          fillSheet(d); fillSaveState(d);
          const w = d.written || [];
          this.written = { fields: w.filter((x) => !x.startsWith("linked:")).length, linked: w.filter((x) => x.startsWith("linked:")).length };
          loadClients(current).catch(() => {});
        }
      } catch (err) { this.job = { ...job, status: "error", error: err.message }; }
    }
    this.show();
  },

  show() {
    const j = this.job;
    const running = !!(j && j.status === "running");
    $("autofill-run").disabled = running;
    $("autofill-stop").classList.toggle("hidden", !running);
    const st = $("autofill-status");
    st.className = "research-status" + (running ? " running" : j && j.status === "error" ? " err" : "");
    const r = j && j.result;
    const w = this.written;
    if (!j) st.textContent = "";
    else if (running) st.textContent = t("admin.autofill.running", { name: j.name, time: clock(j.seconds || 0), searches: j.searches || 0, pages: j.pages || 0 });
    else if (j.status === "error") st.textContent = t("admin.autofill.failed", { error: j.error || t("admin.status.unknownError") });
    else if (j.status === "stopped") st.textContent = t("admin.autofill.stopped");
    else if (r && r.status === "candidates") st.textContent = "";
    else if (r && r.status === "not_found") st.textContent = t("admin.autofill.notFound");
    else st.textContent = w && (w.fields || w.linked) ? t("admin.autofill.filled", { n: w.fields, linked: w.linked }) : t("admin.autofill.nothingNew");
    renderSteps($("autofill-steps"), (j && j.steps) || []);
    // candidates: pick one, then it fills from that company
    const cands = j && j.status === "done" && r && r.status === "candidates" ? r.candidates : [];
    $("autofill-candidates").classList.toggle("hidden", cands.length === 0);
    $("autofill-candidate-list").innerHTML = cands.map((c, i) => `<li>
      <div class="cn"><b>${esc(c.name)}</b>${c.country ? ` · ${esc(c.country)}` : ""}${c.website
        ? ` · <a href="${safeHref(c.website)}" target="_blank" rel="noopener">${esc(host(c.website))}</a>` : ""}</div>
      <div class="muted small">${esc(c.description)}</div>
      <button type="button" class="ghost" data-pick="${i}">${esc(t("admin.autofill.pickOne"))}</button></li>`).join("");
  },
};

$("autofill-candidate-list").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-pick]");
  const r = autofill.job && autofill.job.result;
  if (b && r && r.candidates) autofill.request({ candidate: r.candidates[Number(b.dataset.pick)] });
});
$("autofill-run").addEventListener("click", () => autofill.request({ force: true }));
$("autofill-stop").addEventListener("click", () => autofill.stop());
$("f-name").addEventListener("input", () => {
  autofill.nameTyped = true;
  clearTimeout(autofill.timer);
  autofill.timer = setTimeout(() => autofill.request({}), AUTOFILL_DEBOUNCE);
});
$("f-name").addEventListener("blur", () => { if (autofill.nameTyped) autofill.request({}); });

// --- interviewee research: a profile card per contact (pacenote/people.py) -----------------------------
// "🔎" next to a contact (or typing a full name: debounced, like Auto-fill) runs a web agent; its card
// is saved straight away and shown under "Who we meet". Facts are cells too: double-click to edit
// (the fact becomes manual, kept across runs), × on hover to remove (not brought back). Homonyms: pick one.
const PERSON_DEBOUNCE = 1500;
const CATS = ["role", "tenure", "location", "career", "education", "publication", "interest", "language", "mutual_context", "icebreaker"];
const cslug = (name) => fold(name).replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 60);
const people = {
  enabled: false,
  profiles: {},   // contact slug -> card (as saved)
  jobs: {},       // contact slug -> last job view
  polls: {}, timers: {},

  load(profiles) {
    this.profiles = {};
    for (const p of Object.values(profiles || {})) if (p) this.profiles[p.contact || cslug(p.name)] = p;
    renderProfiles();
  },

  typed(li) {
    const name = li.querySelector(".c-name").value.trim();
    const k = cslug(name);
    clearTimeout(this.timers.typing);
    if (!this.enabled || name.split(/\s+/).length < 2 || name.length < 5 || this.profiles[k] || this.jobs[k]) return;
    this.timers.typing = setTimeout(() => this.request(li, {}), PERSON_DEBOUNCE);
  },

  async request(li, { force = false, candidate = null }) {
    const name = li.querySelector(".c-name").value.trim(), role = li.querySelector(".c-role").value.trim();
    if (!this.enabled || !current || name.length < 3) return;
    const k = cslug(name);
    const forSlug = current;
    await saveSheet();  // the contact is kept like a normal edit
    try {
      const job = await api(`/admin/api/clients/${encodeURIComponent(forSlug)}/people`, "POST",
        { name, role, candidate, force, lang: $("f-language").value || "fr" });
      if (forSlug === current) this.watch(k, job);
    } catch (err) {
      this.jobs[k] = { status: "error", error: err.message, steps: [], name };
      renderProfiles();
    }
  },

  stop(k) {
    const j = this.jobs[k];
    if (j && j.id) api(`/admin/api/people/${encodeURIComponent(j.id)}/stop`, "POST").catch(() => {});
  },

  watch(k, job) {
    clearInterval(this.polls[k]);
    this.update(k, job);
    if (job.status !== "running") return;
    const id = job.id;
    this.polls[k] = setInterval(async () => {
      let j;
      try { j = await api(`/admin/api/people/${encodeURIComponent(id)}`); } catch { clearInterval(this.polls[k]); return; }
      if (!this.jobs[k] || this.jobs[k].id !== id) { clearInterval(this.polls[k]); return; }
      this.update(k, j);
      if (j.status !== "running") clearInterval(this.polls[k]);
    }, 1000);
  },

  update(k, job) {
    this.jobs[k] = job;
    if (job.profile) this.profiles[k] = job.profile;  // saved by the server: shown as is
    renderProfiles();
  },

  async saveFacts(k, facts, removed = []) {
    const p = this.profiles[k];
    try {
      this.profiles[k] = await api(`/admin/api/clients/${encodeURIComponent(current)}/people/${encodeURIComponent(p.name)}`,
        "PUT", { facts, removed });
    } catch (err) { this.jobs[k] = { status: "error", error: err.message, steps: [], name: p.name }; }
    renderProfiles();
  },

  reset() {
    Object.values(this.polls).forEach(clearInterval);
    clearTimeout(this.timers.typing);
    this.jobs = {}; this.polls = {};
  },
};

function renderProfiles() {
  const box = $("profiles");
  if (!box) return;
  const names = contactsNow().map((c) => c.name.trim()).filter(Boolean);
  const keys = [...new Set(names.map(cslug))];
  if (!people.enabled) { box.innerHTML = ""; return; }
  box.innerHTML = keys.filter((k) => people.profiles[k] || people.jobs[k]).map((k) => {
    const p = people.profiles[k] || {}, j = people.jobs[k];
    const running = j && j.status === "running";
    const name = p.name || (j && j.name) || "";
    let status = "";
    if (running) status = t("admin.person.running", { name, time: clock(j.seconds || 0), searches: j.searches || 0, pages: j.pages || 0 });
    else if (j && j.status === "error") status = t("admin.person.failed", { error: j.error || t("admin.status.unknownError") });
    else if (j && j.status === "stopped") status = t("admin.person.stopped");
    else if (p.status === "not_found") status = t("admin.person.notFound");
    const cands = !running && p.status === "candidates" ? p.candidates || [] : [];
    const facts = p.facts || [];
    const dropped = Object.values(p.dropped || {}).reduce((a, b) => a + b, 0);
    const groups = CATS.map((c) => [c, facts.filter((f) => f.category === c)]).filter(([, fs]) => fs.length);
    return `<article class="pcard" data-k="${esc(k)}">
      <header><b>${esc(name)}</b>${p.role ? ` <span class="muted">· ${esc(p.role)}</span>` : ""}
        <span class="research-status${running ? " running" : j && j.status === "error" ? " err" : ""}">${esc(status)}</span>
        <span class="pacts">${running ? `<button type="button" class="ghost" data-pstop="${esc(k)}">${esc(t("admin.stop"))}</button>`
          : `<button type="button" class="ghost icon" data-prerun="${esc(k)}" title="${esc(t("admin.person.rerun"))}">↻</button>`}</span></header>
      ${running && j.steps && j.steps.length ? `<div class="muted small pstep">${esc(j.steps[j.steps.length - 1].key ? t(j.steps[j.steps.length - 1].key, j.steps[j.steps.length - 1].params) : j.steps[j.steps.length - 1].text)}</div>` : ""}
      ${cands.length ? `<div class="candidates"><div class="field-label">${esc(t("admin.person.pick"))}</div><ul>${cands.map((c, i) => `<li>
        <div class="cn"><b>${esc(c.name)}</b>${c.company ? ` · ${esc(c.company)}` : ""}${c.location ? ` · ${esc(c.location)}` : ""}${c.url
          ? ` · <a href="${safeHref(c.url)}" target="_blank" rel="noopener">${esc(host(c.url))}</a>` : ""}</div>
        <div class="muted small">${esc(c.headline)}</div>
        <button type="button" class="ghost" data-ppick="${i}">${esc(t("admin.person.pickOne"))}</button></li>`).join("")}</ul></div>` : ""}
      ${groups.length ? `<dl class="facts">${groups.map(([c, fs]) => `<dt>${esc(t(`admin.person.cat.${c}`))}</dt>${fs.map((f) => `<dd
        class="fact${f.by === "agent" && f.low ? " low" : ""}" data-id="${esc(f.id)}" tabindex="0" title="${esc(f.by === "manual" ? t("admin.cell.manual") : t("admin.cell.editHint"))}"><span class="cv">${esc(f.value)}</span>${f.source_url ? sourceMark(f.source_url, f.confidence ?? 1, f.low, f.dead_source) : ""}<button
        type="button" class="ghost icon rm" data-frm="${esc(f.id)}" title="${esc(t("admin.cell.remove"))}">×</button></dd>`).join("")}`).join("")}</dl>` : ""}
      <p class="privacy muted small">${esc(t("admin.person.privacy"))}${dropped ? ` · ${esc(t("admin.person.filtered", { n: dropped }))}` : ""}${p.researched_at ? ` · ${esc(t("admin.person.researched", { when: when(p.researched_at) }))}` : ""}</p>
    </article>`;
  }).join("");
}

function contactLi(k) { return [...$("f-contacts").children].find((li) => cslug(li.querySelector(".c-name").value) === k); }
function startFactEdit(dd) {
  const k = dd.closest(".pcard").dataset.k, id = dd.dataset.id, p = people.profiles[k];
  const f = (p.facts || []).find((x) => x.id === id);
  if (!f) return;
  // cleared (empty value): dropped, not remembered as removed (× does that), so a re-run may fill it
  editInline(dd.querySelector(".cv"), f.value, { onSave: (v) => people.saveFacts(k,
    p.facts.map((x) => (x.id === id ? { ...x, value: v } : x))) });
}
$("profiles").addEventListener("dblclick", (e) => { const dd = e.target.closest("dd.fact"); if (dd && !e.target.closest("a,button")) startFactEdit(dd); });
$("profiles").addEventListener("keydown", (e) => {
  const dd = e.target.closest("dd.fact");
  if (dd && e.target === dd && e.key === "Enter") { e.preventDefault(); startFactEdit(dd); }
});
$("profiles").addEventListener("click", (e) => {
  const card = e.target.closest(".pcard");
  if (!card) return;
  const k = card.dataset.k, b = e.target.closest("button");
  if (!b) return;
  if (b.dataset.pstop) people.stop(k);
  else if (b.dataset.prerun) { const li = contactLi(k); if (li) people.request(li, { force: true }); }
  else if (b.dataset.ppick) {
    const li = contactLi(k), c = (people.profiles[k].candidates || [])[Number(b.dataset.ppick)];
    if (li && c) people.request(li, { candidate: c });
  } else if (b.dataset.frm) {
    const p = people.profiles[k];
    people.saveFacts(k, p.facts.filter((x) => x.id !== b.dataset.frm), [b.dataset.frm]);
  }
});

// tabs and editing
document.querySelectorAll("#tabs button").forEach((b) => b.addEventListener("click", () => {
  document.querySelectorAll("#tabs button").forEach((x) => x.classList.toggle("on", x === b));
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("hidden", t.dataset.tab !== b.dataset.tab));
}));
document.querySelectorAll("[data-edit]").forEach((b) => b.addEventListener("click", () => {
  $(`${b.dataset.edit}-view`).classList.add("hidden"); $(`${b.dataset.edit}-edit`).classList.remove("hidden");
}));
document.querySelectorAll("[data-cancel]").forEach((b) => b.addEventListener("click", () => {
  const k = b.dataset.cancel;
  $(`${k}-text`).value = detail[`${k}_md`];
  $(`${k}-view`).classList.remove("hidden"); $(`${k}-edit`).classList.add("hidden");
}));
document.querySelectorAll("[data-save]").forEach((b) => b.addEventListener("click", async () => {
  const k = b.dataset.save;
  const path = k === "preplan" ? "preplan" : "research-md";
  const r = await api(`/admin/api/clients/${encodeURIComponent(current)}/${path}`, "PUT", { markdown: $(`${k}-text`).value });
  detail[`${k}_md`] = $(`${k}-text`).value;
  $(`${k}-view`).innerHTML = r.html;
  $(`${k}-view`).classList.remove("hidden"); $(`${k}-edit`).classList.add("hidden");
}));
$("history").addEventListener("click", async (e) => {
  const v = e.target.dataset.view, rs = e.target.dataset.restore;
  if (v) {
    const h = await api(`/admin/api/clients/${encodeURIComponent(current)}/history/${encodeURIComponent(v)}`);
    const box = $("history-view");
    box.innerHTML = `<h3>${esc(t("admin.version", { v }))}</h3>${h.preplan_html}<hr>${h.research_html}`;
    box.classList.remove("hidden");
  }
  if (rs && confirm(t("admin.confirmRestore", { v: rs }))) {
    detail = await api(`/admin/api/clients/${encodeURIComponent(current)}/history/${encodeURIComponent(rs)}/restore`, "POST");
    fillResearch(detail);
  }
});

// screen language: re-render the texts in place (never the form fields being edited)
i18n.onChange((lang, info) => {
  document.title = t("admin.pageTitle");
  if (info.kind !== "change" && info.kind !== "loaded") return;
  for (const id of ["new-lang", "f-language"]) $(id).options = interviewLangs();
  if (!detail) return;
  loadClients(current).catch(() => {});
  fillSaveState(detail);
  fillResearch(detail, true);
  renderSessions();
  if (lastResearch) { $("steps").innerHTML = ""; showResearch(lastResearch); }
  $("autofill-steps").innerHTML = ""; autofill.show();
  renderCells(); renderLinked(); renderProfiles();
});

i18n.init().then(() => {
  document.title = t("admin.pageTitle");
  for (const id of ["new-lang", "f-language"]) $(id).options = interviewLangs();
  return Promise.all([autofill.probe(), loadClients()]);
});
