"use strict";

// The Plan view (static/plan.html): plan.md of one session, rendered in the app, with Export PDF.
//   /plan                          -> this interview's plan   (data: /api/plan)
//   /admin/plan/<client>/<session> -> any saved session's plan (data: /admin/api/plan/<client>/<session>)
// Every visible string comes from the shared helper (static/i18n.js, keys under "planView.").
const t = (key, params) => i18n.t(key, params);
const $ = (id) => document.getElementById(id);

const parts = location.pathname.split("/").filter(Boolean).map(decodeURIComponent);
const inAdmin = parts[0] === "admin" && parts[1] === "plan" && parts.length >= 4;
const enc = encodeURIComponent;
const API = inAdmin ? `/admin/api/plan/${enc(parts[2])}/${enc(parts[3])}` : "/api/plan";

let plan = null;              // the last /api/plan payload
let status = null;            // {key, params, kind} shown next to the buttons
let busy = false;
let retry = null;

function setStatus(key, params, kind) {
  status = key ? { key, params: params || {}, kind: kind || "" } : null;
  render();
}

function render() {
  document.title = plan ? `${plan.title} — ${t("planView.title")}` : t("planView.title");
  const back = $("back");
  back.href = inAdmin ? `/admin?client=${enc(parts[2])}` : "/";
  back.textContent = t(inAdmin ? "planView.backAdmin" : "planView.backInterview");
  $("meta").textContent = plan ? t("planView.meta", { client: plan.client, session: plan.session,
    when: String(plan.generated_at || "").replace("T", " ") }) : "";
  const s = $("status");
  s.textContent = status ? t(status.key, status.params) : "";
  s.className = `plan-status ${status ? status.kind : ""}`;
  $("export").disabled = !plan || busy;
  const cap = plan && plan.capabilities;  // the capability brief the plan was written with is old
  $("cap-warning").textContent = cap && cap.stale ? t("planView.capabilitiesStale", { date: cap.as_of }) : "";
}

async function load() {
  clearTimeout(retry);
  try {
    const res = await fetch(API, { headers: { Accept: "application/json" } });
    if (res.status === 404) {  // still being written (end of the interview): look again shortly
      setStatus("planView.notReady");
      retry = setTimeout(load, 3000);
      return;
    }
    if (!res.ok) throw new Error(res.statusText);
    plan = await res.json();
    const doc = $("plan-doc");
    doc.innerHTML = plan.html;   // rendered server-side from plan.md
    doc.lang = plan.language;    // the plan's language, whatever the screen language
    doc.classList.remove("hidden");
    $("md-link").href = `${API}/md`;
    $("md-link").classList.remove("hidden");
    setStatus(null);
  } catch (e) {
    setStatus("planView.error", { error: e.message }, "err");
  }
}

async function exportPdf() {
  if (!plan || busy) return;
  busy = true;
  setStatus("planView.exporting");
  try {
    const res = await fetch(`${API}/pdf`);
    if (res.status === 501) {  // no Chrome-family browser on this Mac: the print dialog saves a PDF
      setStatus("planView.pdfFallback");
      window.print();
      return;
    }
    if (!res.ok) {
      const d = await res.json().catch(() => ({}));
      throw new Error(d.detail || res.statusText);
    }
    const blob = await res.blob();
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `${plan.slug}-${plan.session}-plan.pdf`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 10000);
    setStatus("planView.exported", { size: Math.max(1, Math.round(blob.size / 1024)) }, "ok");
  } catch (e) {
    setStatus("planView.pdfError", { error: e.message }, "err");
  } finally {
    busy = false;
    render();
  }
}

$("export").addEventListener("click", exportPdf);
i18n.onChange(() => render());
i18n.init().then(() => { render(); return load(); });
