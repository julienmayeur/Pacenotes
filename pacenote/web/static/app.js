"use strict";

// Everything on this page updates IN PLACE: lists are keyed and only changed items are touched,
// new items fade in, nothing is re-ordered while people talk (the server freezes the upcoming
// list), and the current question only changes when the interviewer moves on.

// Every string comes from the shared helper (static/i18n.js, catalogs in pacenote/locales/).
// ONE display language at a time, everywhere: the selector top right (<language-select bind="ui">)
// sets the labels here AND, on the server, the language of the question texts, follow-ups, ladder
// and missing items in the state (S.ui_language). It never touches the interview language
// (prompts, generated follow-ups, plan) nor the spoken languages (speech capture). The switch is
// instant (the cached state is re-rendered), then the server sends the texts in the new language.
const t = (key, params, opts) => i18n.t(key, params, opts);

const $ = (id) => document.getElementById(id);
let S = null;
let lastCurrentId = null;
// The interviewee sees this screen too: interviewer details (timings, transcript, coverage by theme, follow-ups
// already asked...) are hidden until the "Détails" toggle (d) shows them, styled apart. Both choices are kept
// on this browser (localStorage). The transcript alone also toggles with t.
const stored = (k) => { try { return localStorage.getItem(k); } catch (_) { return null; } };
const store = (k, v) => { try { localStorage.setItem(k, v); } catch (_) {} };
let showDetails = stored("pacenote.details") === "1";
let showTail = stored("pacenote.tail") === "1";
let busy = false;
let lastSeq = null;  // last transcript line seen (drives the level meter)
let level = 0;       // 0..1, decays

const rungName = (r) => t(`focus.rungs.${r}`);
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function clock(sec) {
  sec = Math.max(0, Math.floor(sec));
  return `${String(Math.floor(sec / 60)).padStart(2, "0")}:${String(sec % 60).padStart(2, "0")}`;
}
function setText(el, text) { if (el.textContent !== text) el.textContent = text; }
function setClass(el, name, on) { if (el.classList.contains(name) !== !!on) el.classList.toggle(name, !!on); }

/** Keyed list update: reuse nodes by key, update only what changed, fade new ones in. */
function syncList(ul, items, key, build, update, leaveMs = 0) {
  const existing = new Map([...ul.children].filter((c) => !c.classList.contains("leaving")).map((c) => [c.dataset.key, c]));
  const keep = new Set();
  let prev = null;
  for (const item of items) {
    const k = String(key(item));
    keep.add(k);
    let node = existing.get(k);
    if (!node) {
      node = build(item);
      node.dataset.key = k;
      node.classList.add("enter");
      requestAnimationFrame(() => requestAnimationFrame(() => node.classList.remove("enter")));
    } else if (update) {
      update(node, item);
    }
    let want = prev ? prev.nextSibling : ul.firstChild;
    while (want && want !== node && want.classList.contains("leaving")) want = want.nextSibling;  // leave them in place
    if (node !== want) ul.insertBefore(node, want);
    prev = node;
  }
  for (const [k, node] of existing) {
    if (keep.has(k)) continue;
    if (!leaveMs) { node.remove(); continue; }
    node.classList.add("leaving");  // fade/slide out, then remove
    setTimeout(() => node.remove(), leaveMs);
  }
}

// "Ce qui manque": remember when an item turned from missing to heard, so it can show as
// filled (green tick) for a moment before it glides out to "Déjà capté".
const FILL_MS = 1400;
const filledAt = new Map();   // key -> ms timestamp
const seenMissing = new Map(); // key -> was it missing last time?
const ghosts = new Map();      // key -> {key, item}: was missing, vanished with a rung change -> show it filled
const itemText = new Map();    // key -> last shown text (the item in the interview language)
let fillTimer = null;

/** Types a new thread entry in, instead of popping the whole text at once. */
function typeInto(el, text, from = 0) {
  clearInterval(el._typer);
  let n = from;
  const caret = document.createElement("span");
  caret.className = "caret";
  el._typer = setInterval(() => {
    n = Math.min(text.length, n + 2);
    el.textContent = text.slice(0, n);
    if (n < text.length) el.appendChild(caret);
    else clearInterval(el._typer);
  }, 16);
}

function render(state) {
  // a POST response can reach us after a newer SSE state (e.g. the display-language switch answers
  // before its translations land): never step back to an older version
  if (S && state !== S && state.version != null && S.version != null && state.version < S.version) state = S;
  S = state;
  const L = S.languages || {};
  if (L.overlay) i18n.extend(S.language, L.overlay);  // Claude's translations for the interview language
  if (S.ui_language && S.ui_language !== i18n.language && !uiPending) i18n.setLanguage(S.ui_language, { source: "server" });
  setText($("client"), S.client);
  document.title = t("app.title", { client: S.client });

  // consent: in the display language only
  setClass($("consent"), "hidden", S.mode !== "consent");
  setText($("consent-client"), S.client);
  setText($("consent-primary"), (S.consent && S.consent.primary) || "");
  setText($("consent-error"), (S.capture_problems || []).join("\n\n"));
  setText($("consent-progress"), S.mode === "consent" && starting ? (S.capture_progress || "") : "");
  renderLanguages();
  renderWho();

  renderInterview();
  renderWhere();
  renderStrip();
  renderListening();
  renderChrome();
}

// --- the interview panel ------------------------------------------------------------------------
// The card shows `shownId`, NOT whatever the last poll said: polls only update it in place. When the
// server moves the focus (answered + silence, or an interviewer control), the card collapses into the thread
// with its summary and the next question slides in. Polls arriving meanwhile are applied at the end.
const COLLAPSE_MS = 320, SLIDE_MS = 330;
let shownId;                 // undefined until the first render; null = no question left
let transitioning = false;
let follow = true;           // keep the current card in view; false once the user scrolled up to read
let programmaticUntil = 0;   // scroll events before this come from us
const expanded = new Set();  // history items opened by the interviewer

function renderInterview() {
  const cur = S.current;
  const id = cur ? cur.id : null;
  if (transitioning) return;  // the end of the transition renders the latest state
  if (shownId !== undefined && id !== shownId) { transition(); return; }
  keepAnchor(() => {
    renderHistory();
    renderCurrent();
    renderUpcoming();
  });
  if (shownId === undefined) {  // first paint: open on the current question
    shownId = id;
    requestAnimationFrame(() => scrollToCurrent(false));
  }
}

/** Keep the current card where it is on screen while things above it change (no jumps). */
function keepAnchor(fn) {
  const panel = $("interview"), card = $("current-card");
  const before = card.getBoundingClientRect().top;
  fn();
  const delta = card.getBoundingClientRect().top - before;
  if (Math.abs(delta) > 0.5 && panel.scrollTop > 0) { programmaticUntil = Date.now() + 100; panel.scrollTop += delta; }
}

function cardOffset() { return $("current-card").offsetTop; }  // layout position (ignores the slide-in transform)
function scrollToCurrent(smooth) {
  const panel = $("interview");
  const last = $("history").lastElementChild;
  const peek = last ? Math.min(last.offsetHeight + 14, 120) : 0;  // let the last answered question peek in
  programmaticUntil = Date.now() + (smooth ? 900 : 120);
  panel.scrollTo({ top: Math.max(0, cardOffset() - peek - 6), behavior: smooth ? "smooth" : "auto" });
  follow = true;
  setClass($("jump"), "hidden", true);
}
function onPanelScroll() {
  if (Date.now() < programmaticUntil) return;
  const panel = $("interview");
  const top = $("current-card").getBoundingClientRect().top - panel.getBoundingClientRect().top;
  follow = top < panel.clientHeight * 0.45 && top > -$("current-card").offsetHeight + 80;
  setClass($("jump"), "hidden", follow);
}

function transition() {
  transitioning = true;
  const card = $("current-card"), body = $("card-body"), ghost = $("card-ghost");
  const old = (S.history || []).find((h) => h.id === shownId);
  const wasFollowing = follow;
  // 1. the card collapses into a compact thread row (same look as the history items)
  ghost.innerHTML = "";
  if (old) ghost.appendChild(buildHistory(old));
  const from = card.offsetHeight;
  const spacer = document.querySelector(".interview-end");
  spacer.style.height = `calc(38vh + ${from}px)`;  // the panel never gets shorter mid-animation: no scroll jump
  card.style.height = `${from}px`;
  card.classList.add("collapsing");
  void card.offsetHeight;
  const to = old ? ghost.firstChild.offsetHeight : 0;
  card.style.height = `${to}px`;
  setTimeout(() => {
    // 2. the row joins the thread for real, exactly where the ghost row was (nothing moves);
    //    the card turns into the next question and slides in under it
    renderHistory();
    $("history").querySelectorAll(".enter").forEach((n) => n.classList.remove("enter"));
    card.classList.remove("collapsing");
    card.classList.add("incoming");
    card.style.height = "";
    ghost.innerHTML = "";
    shownId = S.current ? S.current.id : null;
    renderCurrent(true);
    renderUpcoming();
    void card.offsetHeight;
    if (wasFollowing) scrollToCurrent(true);
    requestAnimationFrame(() => card.classList.remove("incoming"));
    setTimeout(() => {
      spacer.style.height = "";
      transitioning = false;
      if (S) render(S);  // whatever arrived during the animation
    }, SLIDE_MS);
  }, COLLAPSE_MS);
}

function renderHistory() {
  const items = S.history || [];
  setClass($("history-head"), "hidden", !items.length);
  syncList($("history"), items, (h) => h.id, buildHistory, fillHistory);
}
function buildHistory(h) {
  const li = document.createElement("li");
  li.innerHTML = `<button class="h-head" type="button"><span class="h-mark"></span><span class="h-text">` +
    `<span class="h-q"></span><span class="h-sum"></span></span><span class="h-chev">›</span></button>` +
    `<div class="h-body"><ol class="h-thread"></ol></div>`;
  li.querySelector(".h-head").addEventListener("click", () => {
    const k = li.dataset.key || h.id;
    if (expanded.has(k)) expanded.delete(k); else expanded.add(k);
    setClass(li, "open", expanded.has(k));
    li.querySelector(".h-head").setAttribute("aria-expanded", String(expanded.has(k)));
  });
  fillHistory(li, h);
  return li;
}
function fillHistory(li, h) {
  const state = h.skipped ? "skipped" : h.status;
  const cls = `h-item ${state}` + (expanded.has(h.id) ? " open" : "") + (li.classList.contains("enter") ? " enter" : "");
  if (li.className !== cls) li.className = cls;
  li.querySelector(".h-head").title = t("thread.toggle");
  setText(li.querySelector(".h-mark"), h.skipped ? "⤼" : h.status === "answered" ? "✓" : h.summary ? "◐" : "○");
  setText(li.querySelector(".h-q"), h.text);
  setText(li.querySelector(".h-sum"), h.skipped ? t("thread.skipped") : h.summary || t("thread.noAnswer"));
  const names = rungNames();
  syncList(li.querySelector(".h-thread"), h.thread || [], (e) => e.i,
    (e) => { const x = document.createElement("li"); fillEntry(x, e, names); return x; },
    (x, e) => fillEntry(x, e, names));
}
function fillEntry(li, e, names) {
  li.className = e.kind;
  li.innerHTML = `<span></span>` + (e.kind === "followup" ? `<span class="rung"></span>` : "");
  li.firstChild.textContent = e.text;
  if (e.kind === "followup") li.lastChild.textContent = names[e.rung] || e.rung;
}
function rungNames() {
  return { business: rungName("business"), workflow: rungName("workflow"),
           implementation: rungName("implementation"), done: rungName("done") };
}

let lastSummary = "";
let summaryFlash = null;

function renderCurrent(swap = false) {
  const cur = S.current;
  const changed = swap || (cur && cur.id) !== lastCurrentId;
  const curEl = $("current");
  if (changed) lastCurrentId = cur ? cur.id : "";
  setText(curEl, cur ? cur.text : t("interview.noQuestion"));
  setText($("current-origin"), cur && cur.origin === "reworded" ? t("focus.reworded") : cur && cur.origin === "added" ? t("focus.added")
    : cur && cur.translated ? t("focus.translated") : "");
  setClass($("current-card"), "empty", !cur);

  // "analyse…" while a pass runs on the new lines (deep = full sync)
  const an = $("analysing");
  const on = S.mode === "interview" && (S.analysing || S.sync_running);
  setClass(an, "on", on);
  setText(an, on ? (S.analysing ? t("focus.analysing") : t("focus.deep")) : "");

  // inline controls (same endpoints and keys as the console)
  const open = !!(cur && (cur.status === "pending" || cur.status === "partial"));
  setClass($("controls"), "hidden", !cur || S.mode !== "interview");
  document.querySelectorAll('#controls [data-action="answered"]').forEach((b) => { b.disabled = !open; });
  const skip = $("ctl-skip");
  skip.disabled = !(cur && cur.status === "pending");
  skip.title = skip.disabled ? t("controls.skipDisabled") : t("controls.skipTitle");

  // the ladder: Business > Workflow > Implementation (current rung highlighted)
  const rungs = cur ? cur.rungs || [] : [];
  const names = rungNames();
  const ladderItems = [];
  rungs.forEach((r, i) => { if (i) ladderItems.push({ sep: i }); ladderItems.push(r); });
  setClass($("ladder"), "hidden", !cur);
  syncList($("ladder"), ladderItems, (r) => (r.sep ? `sep${r.sep}` : r.name),
    (r) => { const li = document.createElement("li"); if (r.sep) { li.className = "sep"; li.textContent = "›"; } else fillRung(li, r, names); return li; },
    (li, r) => { if (!r.sep) fillRung(li, r, names); });

  renderAsk(cur, changed);
  renderAmbassadorAsk(cur);
  renderMissing(cur, changed, names);
  renderSummary(cur, changed, names);

  const hint = $("move-hint");
  const ready = cur && cur.status === "answered";  // ladder done
  const ph = (S.phases || {}).current;
  setClass(hint, "ready", ready);
  // the phase hands over at the next move, never mid-answer: say where n / Enter goes
  const phaseHint = ph && ph.next ? (ph.over ? t("phases.overHint", { next: ph.next })
    : ph.done ? t("phases.doneHint", { next: ph.next }) : "") : "";
  setText(hint, cur && S.mode === "interview" ? (phaseHint || (ready ? t("focus.moveReady") : t("focus.moveHint"))) : "");
  setText($("phase-label"), ph ? `${t("phases.step", { n: ph.index, m: ph.count })} · ${ph.title}` : "");
  setClass($("theme-word"), "hidden", !!ph);
  // the phase's hidden checklist, quiet: what its maps still miss, to aim the next follow-up
  setText($("phase-gaps"), ph && ph.gaps && ph.gaps.length && cur && S.mode === "interview"
    ? `${t("phases.toCapture")} : ${ph.gaps.slice(0, 3).join(" · ")}` : "");
}

/** "À demander maintenant", THE focal point of the screen: what the interviewee is answering. It stays put
 *  until the answer is over (the server decides: the interviewer reads a queued question, the interviewee
 *  answers the next one, or a real pause after a complete answer); a new follow-up never takes its place,
 *  it is queued underneath (renderAskNext). With no follow-up asked yet, the theme question itself takes the
 *  focal spot (the theme header then keeps only its label and ladder, so it is never shown twice). */
let lastAsk = "";
let lastAskKey = "";
let askFlash = null;
function renderAsk(cur, changed) {
  const ask = cur ? cur.ask || { key: `q:${cur.id}`, kind: "question", text: cur.text } : null;
  const fallback = !!ask && ask.kind === "question";
  const box = $("ask-now");
  setClass(box, "hidden-soft", !cur);
  setClass(box, "fallback", fallback);
  setClass(box, "answered", !!cur && cur.status === "answered");  // same text, a quiet green rule
  setClass($("theme-head"), "echoed", fallback);
  setText($("ask-label"), fallback ? t("focus.askTheme") : t("focus.ask"));
  const text = ask ? ask.text : "";
  setText($("ask-text"), text);
  // with several attendees: who to address it to (the model's pick, else the rung's rule)
  const to = cur && S.mode === "interview" && cur.status !== "answered" ? cur.ask_to : null;
  setClass($("ask-to"), "hidden", !to);
  setText($("ask-to"), to ? `→ ${t("focus.askTo", { who: to.reason ? t("focus.askToWhy", { who: to.who, why: to.reason }) : to.who })}` : "");
  // a queued question became the one asked (not a language switch, not a new card): a short highlight
  const key = ask ? ask.key : "";
  if (key !== lastAskKey && !changed && text && lastAskKey) {
    box.classList.add("fresh");
    clearTimeout(askFlash);
    askFlash = setTimeout(() => box.classList.remove("fresh"), 1100);
  }
  lastAsk = text;
  lastAskKey = key;
  renderAskNext(cur);
}

/** Under the question being asked: what to ask next, as understanding grows. Items are only appended
 *  (a calm fade-in, nothing above moves); one leaves when it is asked. Clicking one asks it now. */
function renderAskNext(cur) {
  const items = cur && S.mode === "interview" ? cur.ask_next || [] : [];
  const ol = $("ask-next");
  setClass(ol, "hidden", !items.length);
  syncList(ol, items, (i) => i.key, buildAskNext, fillAskNext);
}
function buildAskNext(i) {
  const li = document.createElement("li");
  li.innerHTML = `<button type="button" class="an-item"><span class="an-label"></span><span class="an-text"></span></button>`;
  li.querySelector("button").addEventListener("click", () => askNow(li.dataset.key));
  fillAskNext(li, i);
  return li;
}
function fillAskNext(li, i) {
  setClass(li, "question", i.kind === "question");
  li.querySelector("button").title = t("focus.askNowTitle");
  setText(li.querySelector(".an-label"), i.kind === "question" ? t("focus.nextQuestion") : t("focus.queued"));
  setText(li.querySelector(".an-text"), i.text);
}
async function askNow(key) {
  if (!S || busy || transitioning || !key) return;
  busy = true;
  try { render(await post("/api/ask", { key })); } catch (_) { /* no longer queued: the next poll shows why */ }
  finally { busy = false; }
}

/** Under the focal follow-up, quiet, tied to the current workflow: a talking point to say about its manual
 *  work (lens.py), then the questions also worth asking: its missing baseline or success metric (lens.py)
 *  and the indirect people question (pilots.py). Only what to say and the workflow's name; why stays
 *  in the console. */
function renderAmbassadorAsk(cur) {
  const live = cur && S.mode === "interview";
  const f = live ? cur.framing : null;
  setClass($("framing-say"), "hidden", !f);
  setText($("framing-uc"), f && f.use_case ? ` · ${f.use_case}` : "");
  setText($("framing-text"), f ? f.point : "");
  const m = live ? cur.metric_ask : null, a = live ? cur.ambassador : null;
  // the sponsor question (no workflow) is for the leader: say so when several people attend
  const forLead = a && a.kind === "sponsor" && (S.attendees || []).length > 1 ? a.person : "";
  const uc = (m && m.use_case) || (a && a.use_case) || forLead;
  setClass($("ambassador-ask"), "hidden", !m && !a);
  setText($("ambassador-uc"), uc ? ` · ${uc}` : "");
  setText($("metric-text"), m ? m.question : "");
  setText($("ambassador-text"), a ? a.question : "");
  // what this interviewee could not answer here: who would know (personas.py), never pushed
  const tc = live ? cur.to_check || [] : [];
  setClass($("to-check"), "hidden", !tc.length);
  setText($("to-check-text"), tc.map((x) => t("focus.toCheck", { who: x.ask, question: x.question })).join("\n"));
}

/** The live answer: grows as the fast pass appends pieces, tightens when a full sync rewrites it. */
function renderSummary(cur, changed, names) {
  setClass($("summary-card"), "hidden", !cur);
  const text = cur ? cur.summary || "" : "";
  const el = $("summary-text");
  if (changed) {
    clearInterval(el._typer);
    el.textContent = text;
  } else if (text !== lastSummary) {
    if (text.startsWith(lastSummary) && lastSummary) {  // richer: type only the new part
      typeInto(el, text, lastSummary.length);
    } else {
      clearInterval(el._typer);
      el.textContent = text;
    }
    const box = $("summary-card");
    box.classList.add("grew");
    clearTimeout(summaryFlash);
    summaryFlash = setTimeout(() => box.classList.remove("grew"), 1100);
  }
  lastSummary = text;
  setText($("thread-empty"), cur && !text ? t("focus.waiting") : "");
  const by = cur && cur.summary_by ? t(`focus.summaryBy.${cur.summary_by}`) : "";
  const age = cur && cur.summary_at ? Math.max(0, Math.round(Date.now() / 1000 - cur.summary_at)) : null;
  setText($("summary-meta"), by && age !== null ? summaryMeta(cur, by, age) : "");

  const hasAnswer = !!(cur && cur.summary);
  const pill = $("current-pill");
  setText(pill, hasAnswer ? (cur.status === "answered" ? t("focus.answered") : t("focus.partial")) : "");
  setClass(pill, "answered", cur && cur.status === "answered");
  setText($("vague"), cur && cur.vague && cur.status !== "answered" ? t("focus.vague") : "");

  // follow-ups stack under the question like a thread (business → workflow → implementation). The
  // answer pieces are in the summary above and the follow-up to ask now is the focal "ask now", so
  // only the follow-ups already asked are listed here (the full thread stays in the history item).
  const all = cur ? cur.thread || [] : [];
  const pinned = cur && cur.ask && cur.ask.kind === "followup" ? cur.ask.text : null;
  const thread = all.filter((e) => e.kind === "followup" && e.text !== pinned);
  const tlist = $("thread");
  setClass($("followups"), "hidden", !thread.length);
  syncList(tlist, thread, (e) => `${cur.id}|${e.i}`,
    (e) => {
      const li = document.createElement("li");
      fillEntry(li, e, names);
      return li;
    },
    (li, e) => fillEntry(li, e, names));  // in place: the display language may have changed
}

function renderMissing(cur, changed, names) {
  // Ce qui manque, for the current rung: big chips; filled ones tick green then move to "Déjà capté"
  const checklist = cur ? cur.checklist || [] : [];
  const now = Date.now();
  const keyOf = (c) => `${cur.id}|${c.key || c.item}`;
  const present = new Set(checklist.map(keyOf));
  for (const c of checklist) {
    const k = keyOf(c);
    itemText.set(k, c.item);
    if (c.done && seenMissing.get(k) === true && !filledAt.has(k) && !changed) filledAt.set(k, now);
    seenMissing.set(k, !c.done);
  }
  // an item that was missing and vanished because the rung moved on was just filled: give it its
  // green moment too (the fast pass often fills the last item and climbs the ladder at once)
  if (cur && !changed) {
    for (const [k, wasMissing] of seenMissing) {
      if (!wasMissing || present.has(k) || !k.startsWith(`${cur.id}|`)) continue;
      ghosts.set(k, { key: k.slice(cur.id.length + 1), item: itemText.get(k) || k.slice(cur.id.length + 1) });
      filledAt.set(k, now);
      seenMissing.set(k, false);
    }
  }
  if (changed) ghosts.clear();
  const ghostItems = [...ghosts].filter(([k]) => now - (filledAt.get(k) || 0) < FILL_MS).map(([, g]) => ({ ...g, done: true }));
  const showing = cur ? [...ghostItems, ...checklist.filter((c) => !c.done || (filledAt.has(keyOf(c)) && now - filledAt.get(keyOf(c)) < FILL_MS))] : [];
  const showingKeys = new Set(showing.map(keyOf));
  // "Déjà capté": every detail of this question heard so far (all rungs), once its moment is over
  const coveredKeys = cur ? cur.covered_keys || cur.covered || [] : [];
  const got = (cur ? cur.covered || [] : []).map((item, i) => ({ item, key: coveredKeys[i], done: true }))
    .filter((c) => !showingKeys.has(keyOf(c)));
  const ul = $("missing");
  setClass($("missing-card"), "hidden", !cur);
  setText($("missing-rung"), cur ? `· ${names[cur.rung === "done" ? "implementation" : cur.rung]}` : "");
  // a question drilling a use case: its integration slots are what is missing
  setText($("missing-usecase"), cur && cur.use_case ? `· ${cur.use_case.title}` : "");
  const left = checklist.filter((c) => !c.done).length;  // on the current rung
  setText($("missing-count"), cur && checklist.length ? t("focus.countOf", { n: left, m: checklist.length }) : "");
  syncList(ul, showing, keyOf,
    (c) => {
      const li = document.createElement("li");
      li.textContent = c.item;
      if (!changed) { li.classList.add("fresh"); setTimeout(() => li.classList.remove("fresh"), 1500); }
      setClass(li, "filled", c.done);
      return li;
    },
    (li, c) => { setClass(li, "filled", c.done); setText(li, c.item); }, changed ? 0 : 450);
  setText($("missing-empty"), cur && !showing.length ? (checklist.length ? t("focus.rungDone") : t("focus.nothingMissing")) : "");
  syncList($("got"), got, keyOf, (c) => { const li = document.createElement("li"); li.textContent = c.item; return li; },
    (li, c) => setText(li, c.item));
  for (const [k] of ghosts) if (now - (filledAt.get(k) || 0) > FILL_MS * 3) ghosts.delete(k);
  setClass($("got-row"), "empty", got.length === 0);
  if (filledAt.size) {  // re-render once the filled chips have had their moment
    clearTimeout(fillTimer);
    fillTimer = setTimeout(() => { if (S && !transitioning) renderMissing(S.current, false, rungNames()); }, FILL_MS + 50);
    for (const [k, t] of filledAt) if (now - t > FILL_MS * 3) filledAt.delete(k);
  }

}

// "fast pass · updated 3 s ago · analyse à la lecture 42": which transcript line the analysis that
// wrote this summary had read (versioned analyses, see state.py)
function summaryMeta(cur, by, age) {
  return `${by} · ${t("focus.updated", { s: age })}` + (cur.read ? ` · ${t("focus.readAt", { n: cur.read })}` : "");
}

function fillRung(li, r, names) {
  li.className = r.state;
  li.innerHTML = `${esc(names[r.name] || r.name)}` + (r.total ? `<span class="count">${r.covered}/${r.total}</span>` : "");
}

function renderUpcoming() {
  syncList($("next"), S.next || [], (q) => q.id,
    (q) => { const li = document.createElement("li"); li.innerHTML = `<span class="q"></span><span class="draft debug"></span>`; fillUpcoming(li, q); return li; },
    fillUpcoming);
}
function fillUpcoming(li, q) {
  setText(li.querySelector(".q"), q.text);
  setText(li.querySelector(".draft"), q.answer ? `◐ ${q.answer}` : "");
}

function timeLeft() {
  return S.started_at ? Math.max(0, S.target_minutes * 60 - (Date.now() / 1000 - S.started_at)) : S.target_minutes * 60;
}

function renderWhere() {
  const st = S.status || {};
  const left = timeLeft();
  setText($("time-left"), clock(left));
  setClass($("time-left").parentElement, "low", left < 180);
  setText($("coverage-pct"), `${Math.round((st.coverage || 0) * 100)} %`);
  // short "where we are" next to the upcoming questions
  setText($("where-line"), t("focus.whereLine", { answered: st.answered || 0, open: st.open || 0,
                                                  pct: Math.round((st.coverage || 0) * 100), left: clock(left) }));
  const must = st.must_open || [];
  $("must").innerHTML = st.must_open_count
    ? `<b>${st.must_open_count}</b> ${esc(t("where.mustOpen"))} · <b>${st.missing_details || 0}</b> ${esc(t("where.details"))}`
      + (must.length ? `<br>${must.slice(0, 3).map((t) => `· ${esc(t.length > 64 ? t.slice(0, 62) + "…" : t)}`).join("<br>")}` : "")
    : "";
  syncList($("themes"), S.themes, (th) => th.id,
    (th) => {
      const li = document.createElement("li");
      li.className = "theme";
      li.innerHTML = `<div class="row"><span class="name"></span><span class="pct"></span></div><div class="bar"><span></span></div>`;
      fillTheme(li, th);
      return li;
    }, fillTheme);
  renderPhases();
  // the "updated N s ago" under the live summary ticks too
  const cur = S.current;
  if (cur && cur.summary_by && cur.summary_at && !transitioning) {
    const age = Math.max(0, Math.round(Date.now() / 1000 - cur.summary_at));
    setText($("summary-meta"), summaryMeta(cur, t(`focus.summaryBy.${cur.summary_by}`), age));
  }
}
function renderPhases() {
  const P = S.phases || { items: [], maps: {} };
  syncList($("phases"), P.items || [], (p) => p.id,
    (p) => { const li = document.createElement("li"); li.innerHTML = `<span class="mark" aria-hidden="true"></span><span class="name"></span><span class="time"></span>`; fillPhase(li, p); return li; },
    fillPhase);
  const m = P.maps || {};
  setText($("maps-line"), Object.keys(m).length ? t("phases.maps", { org: m.org || 0, flows: m.flows || 0,
    stores: m.stores || 0, decisions: m.decisions || 0, catalog: m.catalog || 0 }) : "");
}
function fillPhase(li, p) {
  li.className = `phase ${p.state}`;
  setText(li.querySelector(".mark"), p.state === "done" ? "✓" : p.state === "current" ? "▸" : "");
  setText(li.querySelector(".name"), p.title);
  const over = p.state === "current" && p.budget && p.spent >= p.budget;
  // time stays discreet (small, faint): the planned minutes ahead, time spent / planned once a step has started
  setText(li.querySelector(".time"), p.state === "todo" ? clock(p.budget) : `${clock(p.spent)} / ${clock(p.budget)}`);
  setClass(li, "over", over);
  li.setAttribute("aria-current", p.state === "current" ? "step" : "false");
}

function fillTheme(li, th) {
  const pct = Math.round((th.coverage || 0) * 100);
  setText(li.querySelector(".name"), th.title);
  setText(li.querySelector(".pct"), `${pct}%`);
  li.querySelector(".bar > span").style.width = `${pct}%`;
  setClass(li, "done", pct >= 80);
  li.title = th.note || "";
}

function renderStrip() {
  setClass($("live-wrap"), "hidden", !(showTail || showDetails));
  setClass($("live-wrap"), "on", S.mode === "interview");
  syncList($("tail"), S.tail || [], (l) => l.seq,
    (l) => {
      const div = document.createElement("div");
      div.innerHTML = `<span class="t">${esc(l.t)}</span>` + (l.stream ? `<span class="s">${esc(l.stream.toUpperCase())}</span>` : "") + `<span class="x"></span>`;
      setText(div.querySelector(".x"), l.text);
      setClass(div, "pending", l.pending);
      return div;
    },
    (div, l) => setClass(div, "pending", l.pending));
  setText($("noise"), S.noise_ignored ? `${S.noise_ignored} ${t("live.noise")}` : "");
}

function renderListening() {
  const L = S.listening || { streams: [] };
  const interviewing = S.mode === "interview";
  setClass($("listen"), "off", !interviewing);
  setText($("listen-label"), interviewing ? t("live.listening") : "");
  syncList($("langs"), interviewing ? L.streams : [], (x) => x.stream,
    (x) => { const s = document.createElement("span"); fillChip(s, x); return s; }, fillChip);
  if (lastSeq !== null && L.line_seq > lastSeq) bump(Math.min(1, 0.45 + 0.15 * (L.line_seq - lastSeq)));
  lastSeq = L.line_seq;

  // streams stopped on purpose (interview ended, shutdown) are not a problem; a language that was
  // not ready at Start is a calm banner (the others listen), not an outage
  const off = interviewing ? L.streams.filter((x) => x.unavailable && !x.stopped) : [];
  const dead = interviewing ? L.streams.filter((x) => !x.alive && !x.stopped && !x.unavailable && !(x.problem || "").includes("restarting")) : [];
  const alerts = off.length ? [`<div class="alert degraded">⚠ <b>${esc(t("live.unavailable", {
    langs: off.map((x) => i18n.name(x.locale)).join(", "),
    ready: L.streams.filter((x) => x.alive).map((x) => i18n.name(x.locale)).join(", ") }))}</b>` +
    `<small>${esc(off.map((x) => x.problem || x.locale).join(" · "))}</small></div>`] : [];
  // the primary speech backend failed its health check: the backup one listens (speech.py)
  if (interviewing && L.fallback) alerts.unshift(`<div class="alert degraded">⚠ <b>${esc(t("live.fallback", { engine: L.backend || "" }))}</b><small>${esc(L.fallback)}</small></div>`);
  dead.forEach((x) => {
    alerts.push(`<div class="alert">⚠ <b>${esc(t("live.stopped", { lang: i18n.name(x.locale) }))}</b> (${esc(x.locale)})<small>${esc(x.problem || "")}</small></div>`);
  });
  if (!L.streams.some((x) => !x.alive && !x.unavailable) && S.mode !== "consent" && !L.stopped) {
    (S.capture_problems || []).forEach((p) => alerts.push(`<div class="alert">⚠ ${esc(p)}</div>`));
  }
  const html = alerts.join("");
  if ($("alerts").innerHTML !== html) $("alerts").innerHTML = html;
}
function fillChip(s, x) {
  const restarting = !x.alive && (x.problem || "").includes("restarting");
  s.className = `chip ${x.alive ? "ok" : x.unavailable ? "off" : restarting ? "restarting" : "dead"}`;
  s.title = x.problem || `${i18n.name(x.locale)} (${x.locale})`;
  setText(s, x.stream.toUpperCase() + (x.alive ? "" : x.unavailable ? " –" : restarting ? ` ${t("live.restarting")}…` : " ✕"));
}

function renderChrome() {
  const F = S.fast || {};
  // what the two timings are: the fast pass's speech -> screen delay, and how long the last full analysis took
  const parts = [];
  if (F.enabled && F.latency != null) parts.push(t("details.fast", { s: F.latency.toFixed(1) }));
  if (S.last_sync_seconds != null) parts.push(t("details.sync", { s: S.last_sync_seconds.toFixed(1) }));
  setText($("latency"), parts.join(" · "));
  $("latency").title = t("console.latency", { model: F.model || "-", latency: F.latency ?? "-", call: F.seconds ?? "-",
                                              sync: S.last_sync_seconds ?? "-" });

  const dot = $("sync-dot");
  const cls = "dot " + (S.sync_running ? "busy" : S.last_sync_error ? "err" : S.sync_count ? "ok" : "");
  if (dot.className !== cls) dot.className = cls;
  dot.title = S.last_sync_error || (S.last_sync_at ? t("console.syncNo", { n: S.sync_count }) : t("console.waitingSync"));

  const ending = S.enough_data || ["finalize", "generating", "done"].includes(S.mode);
  setClass($("banner"), "hidden", !ending);
  setText($("banner-primary"), t("interview.bannerDone"));  // display language only
  setText($("banner-status"), S.mode === "generating" ? t("end.generating") : S.mode === "done" ? t("end.done") : "");
  // the plan opens in the app's Plan view (/plan: read it, Export PDF); plan.md stays the source
  const links = S.plan_ready ? `<a href="/plan" target="_blank">${esc(t("planView.open"))}</a><a href="/plan.md" target="_blank">plan.md</a>` : "";
  if ($("banner-links").innerHTML !== links) $("banner-links").innerHTML = links;
  if ($("console-links").innerHTML !== links) $("console-links").innerHTML = links;
  const P = S.plan_files || {};
  const paths = (P.md ? `<span class="path-label">${esc(t("end.planSaved"))}</span><code>${esc(P.md)}</code>` : "")
    + (S.session_dir ? `<span class="path-label">${esc(t("end.sessionDir"))}</span><code>${esc(S.session_dir)}</code>` : "");
  for (const id of ["banner-paths", "console-paths"]) if ($(id).innerHTML !== paths) $(id).innerHTML = paths;

  const err = S.last_sync_error ? ` · <span class="err">${esc(S.last_sync_error)}</span>` : "";
  const cap = (S.capture_problems || []).length ? ` · <span class="err">${esc(S.capture_problems.join("; "))}</span>` : "";
  const last = S.last_sync_seconds != null ? ` · ${esc(t("console.lastCall"))} <b>${S.last_sync_seconds.toFixed(1)}s</b>` : "";
  const skipped = S.skipped_ticks ? ` · ${esc(t("console.skippedTicks"))} ${S.skipped_ticks}` : "";
  const fastInfo = F.enabled ? ` · ${esc(t("console.fast"))} <b>${F.count}</b>` +
    (F.seconds != null ? ` (${esc(t("console.fastCall", { s: F.seconds.toFixed(1) }))})` : "") +
    (F.error ? ` <span class="err">${esc(F.error)}</span>` : "") : ` · ${esc(t("console.fastOff"))}`;
  const A = S.analysis || {};
  const reading = A.transcript ? ` · ${esc(t("console.readAt", { read: A.read || 0, total: A.transcript }))}` : "";
  const html = `${esc(t("console.mode"))} <b>${esc(S.mode)}</b> · ${esc(t("console.engine"))} <b>${esc(S.engine)}</b> · ` +
    `${esc(t("console.syncs"))} <b>${S.sync_count}</b> ${esc(t("console.every", { s: S.interval }))}` +
    last + (S.sync_running ? ` · <b>${esc(t("console.syncing"))}</b>` : "") + skipped + fastInfo +
    reading + (S.enough_data_reason ? ` · ${esc(S.enough_data_reason)}` : "") + err + cap;
  if ($("console-status").innerHTML !== html) $("console-status").innerHTML = html;
  setText($("wispr-status"), S.wispr_merged ? t("console.wisprMerged") : "");
  renderRecipes();
}

// "Possible recipes" in the interviewer console: title, fit, what is still missing (quiet: hidden when none)
function renderRecipes() {
  const list = S.recipes || [];
  setClass($("console-recipes"), "hidden", !list.length);
  const html = list.map((r) => `<li class="fit-${esc(r.fit)}"><b>${esc(r.title)}</b>`
    + ` <span class="fit">${esc(t(`console.recipeFit.${r.fit}`))}</span>`
    + (r.use_case ? ` <span class="muted">· ${esc(r.use_case)}</span>` : "")
    + (r.missing && r.missing.length ? `<br><small>${esc(t("console.recipeMissing"))} : ${esc(r.missing.slice(0, 3).join(" · "))}</small>` : "")
    + `</li>`).join("");
  if ($("console-recipe-list").innerHTML !== html) $("console-recipe-list").innerHTML = html;
  renderConsoleAmbassadors();
}

// "Potential ambassadors" in the interviewer console: who, for which use case, the interviewee's words
function renderConsoleAmbassadors() {
  const list = S.ambassadors || [];
  setClass($("console-ambassadors"), "hidden", !list.length);
  const html = list.map((a) => `<li><b>${esc(a.person)}</b>${a.role ? ` <span class="muted">· ${esc(a.role)}</span>` : ""}`
    + (a.use_case ? ` <span class="fit">${esc(t("console.ambassadorUseCase", { useCase: a.use_case }))}</span>` : "")
    + (a.quote ? `<br><small>« ${esc(a.quote)} »</small>` : "") + `</li>`).join("");
  if ($("console-ambassador-list").innerHTML !== html) $("console-ambassador-list").innerHTML = html;
  renderConsoleProbes();
}

// The people questions per workflow in the interviewer console: the question, why (the ambassador angle),
// held for later or asked; above them, one line on how to ask this interviewee.
function renderConsoleProbes() {
  const list = S.probes || [];
  const cur = S.current && S.current.ambassador;
  const shown = cur && !list.some((p) => p.question === cur.question) ? [{ ...cur, timing: "now", asked: false }] : [];
  const all = [...shown, ...list];
  const read = S.read;
  setClass($("console-probes"), "hidden", !all.length && !read);
  setClass($("console-read"), "hidden", !read);
  setText($("console-read"), read ? t("console.readLine", { tone: read.tone || "—", approach: read.approach || "—" }) : "");
  // the sponsor question (no pilot named so far: would the leader interviewed open the way) has no workflow
  const sponsor = (p) => p.kind === "sponsor";
  const html = all.map((p) => `<li><b>${esc(sponsor(p) ? t("console.sponsorProbe") : p.use_case)}</b> <span class="fit">${esc(t(`console.probeState.${p.asked ? "asked" : p.timing === "now" ? "now" : "later"}`))}</span>`
    + `<br>${esc(p.question)}`
    + `<br><small>${esc(t("console.probeWhy"))} : ${esc(p.reason || t(sponsor(p) ? "console.sponsorTemplateWhy" : "console.probeTemplateWhy", { person: p.person || "—" }))}</small></li>`).join("");
  if ($("console-probe-list").innerHTML !== html) $("console-probe-list").innerHTML = html;
  renderConsoleFramings();
}

// The talking points per workflow in the interviewer console: the point, the decision the person moves up
// to, why now, held for later or said.
function renderConsoleFramings() {
  const list = S.framings || [];
  setClass($("console-framings"), "hidden", !list.length);
  const html = list.map((f) => `<li><b>${esc(f.use_case)}</b> <span class="fit">${esc(t(`console.framingState.${f.asked ? "asked" : f.timing === "now" ? "now" : "later"}`))}</span>`
    + `<br>${esc(f.point)}`
    + (f.moves_up_to ? `<br><small>${esc(t("console.framingMoves", { moves: f.moves_up_to }))}</small>` : "")
    + (f.reason ? `<br><small>${esc(t("console.probeWhy"))} : ${esc(f.reason)}</small>` : "") + `</li>`).join("");
  if ($("console-framing-list").innerHTML !== html) $("console-framing-list").innerHTML = html;
  renderConsoleToCheck();
}

// What the interviewee could not answer, by who would know (it goes to the plan's "To check with" section).
function renderConsoleToCheck() {
  const list = S.to_check || [];
  setClass($("console-to-check"), "hidden", !list.length);
  const html = list.map((x) => `<li><b>${esc(x.ask)}</b>${x.use_case ? ` <span class="muted">· ${esc(x.use_case)}</span>` : ""}`
    + `<br>${esc(x.question)}` + (x.evidence ? `<br><small>« ${esc(x.evidence)} »</small>` : "") + `</li>`).join("");
  if ($("console-to-check-list").innerHTML !== html) $("console-to-check-list").innerHTML = html;
}

// --- who is in the meeting: the client sheet's people with their personas (attendees.py), listed on the
// consent screen; a quiet chip in the header afterwards; the views heard in the console only. ---
function personLabel(p) {
  return [p.role, p.name].filter(Boolean).join(" · ") || t(`persona.${p.persona}`);
}
function renderWho() {
  const people = S.attendees || [];
  const main = people.find((p) => p.lead) || people[0];
  const chip = $("who-chip");
  setClass(chip, "hidden", S.mode === "consent");
  const who = main ? [main.role, t(`persona.${main.persona}`)].filter(Boolean).join(" · ") : t(`persona.${S.persona || "executive"}`);
  setText(chip, t("console.interviewee", { who }) + (people.length > 1 ? ` ${t("console.others", { n: people.length - 1 })}` : ""));
  renderConsoleAttendees();
  if (S.mode !== "consent") return;
  const html = people.map((p) => `<li><b>${esc(personLabel(p))}</b> <span class="muted">· ${esc(t(`persona.${p.persona}`))}</span></li>`).join("");
  if ($("who-list").innerHTML !== html) $("who-list").innerHTML = html;
  setClass($("who-list"), "hidden", !people.length);
  setText($("who-why"), people.length ? "" : t("consent.noContact"));
}

// Who is in the meeting, with persona and prep note (interviewer console only).
function renderConsoleAttendees() {
  const people = S.attendees || [];
  setClass($("console-attendees"), "hidden", !people.length);
  const html = people.map((p) => `<li><b>${esc(personLabel(p))}</b>`
    + ` <span class="muted">· ${esc(t(`persona.${p.persona}`))}${p.lead ? ` · ${esc(t("console.lead"))}` : ""}</span>`
    + (p.prep ? `<br><small class="muted">${esc(p.prep)}</small>` : "") + `</li>`).join("");
  if ($("console-attendee-list").innerHTML !== html) $("console-attendee-list").innerHTML = html;
  renderConsoleViewpoints();
}

// The views heard on AI, by statement, not by person: kind, position, topic, verbatim quotes. A quote names
// someone only when the statement did, or when a labelled transcript mapped its voice after the meeting.
function renderConsoleViewpoints() {
  const V = S.viewpoints || { items: [], speakers: {} };
  const items = V.items || [];
  setClass($("console-viewpoints"), "hidden", !items.length && !(S.attendees || []).length);
  const quote = (q) => `<br><small>« ${esc(q.text)} »`
    + (q.speaker ? ` <span class="muted">— ${esc(q.speaker)}</span>` : "")
    + (q.voice && (q.voice_who || q.voice) !== q.speaker
      ? ` <span class="muted">· ${esc(t("console.voice", { who: q.voice_who || q.voice }))}</span>` : "") + `</small>`;
  const html = items.length ? items.map((v) => `<li class="vp-${esc(v.kind)}">`
    + `<span class="fit vp-kind">${esc(t(`viewpoints.kind.${v.kind}`))}</span> <b>${esc(v.position)}</b>`
    + (v.topic ? ` <span class="muted">· ${esc(v.topic)}</span>` : "")
    + (v.quotes || []).map(quote).join("") + `</li>`).join("")
    : `<li class="muted">${esc(t("console.viewpointsEmpty"))}</li>`;
  if ($("console-viewpoint-list").innerHTML !== html) $("console-viewpoint-list").innerHTML = html;
  const sp = V.speakers || {};
  setText($("console-speakers"), sp.status === "mapped" ? t("console.speakersMapped", { n: sp.mapped || 0 })
    : sp.status === "unavailable" ? t("console.speakersUnavailable") : "");
}

// --- languages: the same <language-select> component on the consent screen, header and console.
// Display language: the header toggle (and the consent screen's, before the interview). Interview
// and spoken languages: the consent screen and the console. ---
let uiPending = false;  // a screen-language change of ours is on its way to the server
let starting = false;

function renderLanguages() {
  const L = S.languages || {};
  for (const id of ["consent-interview", "console-interview"]) {
    const el = $(id);
    el.options = L.interview_options || [S.language];
    el.value = L.interview || S.language;
  }
  for (const id of ["consent-speech", "console-speech"]) {
    const el = $(id);
    el.options = L.speech_options || L.speech || [];
    el.value = L.speech || [];
  }
  $("console-speech").toggleAttribute("disabled", !L.speech_editable);
  const tx = L.translation || {};
  const name = tx.lang ? i18n.name(tx.lang) : "";
  setText($("translation-status"), tx.status === "running" ? t("console.translating", { lang: name })
    : tx.status === "error" ? t("console.translateFailed", { lang: name, error: tx.error || "" })
    : tx.status === "done" && tx.count ? t("console.translated", { count: tx.count, lang: name }) : "");
}

async function setLanguages(body) {
  try { render(await post("/api/language", body)); }
  catch (e) { $("console-status").insertAdjacentHTML("beforeend", ` · <span class="err">${esc(e.message)}</span>`); if (S) render(S); }
}

// display language: instant (re-render the cached state), then remembered by the session (state.json),
// which re-sends the question texts / follow-ups / items in that language
i18n.onChange((lang, info) => {
  if (!S) return;
  if (info.kind === "change" && info.source === "user" && lang !== S.ui_language) {
    S = { ...S, ui_language: lang };  // optimistic: the switch is instant, the server catches up
    uiPending = true;
    post("/api/language", { ui: lang }).then(render).catch(() => {}).finally(() => { uiPending = false; });
  }
  render(S);
});
document.addEventListener("language-change", (e) => {
  const id = e.target.id || "";
  if (id.endsWith("-interview")) setLanguages({ interview: e.detail.value });
  else if (id.endsWith("-speech")) setLanguages({ speech: e.detail.value });
});
function cycleUiLanguage() {
  const codes = i18n.languages.map((l) => l.code);
  if (codes.length < 2) return;
  i18n.setLanguage(codes[(codes.indexOf(i18n.language) + 1) % codes.length], { source: "user" });
}

function bump(v) { level = Math.max(level, v); }
function animateMeter() {
  const bars = document.querySelectorAll("#listen .meter i");
  const active = level > 0.04 && S && S.mode === "interview";
  $("listen").classList.toggle("idle", !active && !!(S && S.mode === "interview"));
  bars.forEach((b, i) => {
    const shape = [0.55, 0.85, 1, 0.75, 0.5][i];
    b.style.height = active ? `${4 + 12 * level * shape * (0.65 + Math.random() * 0.35)}px` : "";
  });
  level *= 0.9;
  requestAnimationFrame(() => setTimeout(animateMeter, 70));
}
animateMeter();
setInterval(() => { if (S) renderWhere(); }, 1000);  // time left ticks

async function post(path, body) {
  const res = await fetch(path, {
    method: "POST",
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    let msg = res.statusText;
    try { msg = (await res.json()).detail || msg; } catch (_) {}
    throw new Error(msg);
  }
  return res.json();
}

async function act(action) {
  if (!S || busy) return;
  // the question the interviewer is looking at (not the newest state): a key pressed during a transition
  // never acts on the question that is only sliding in
  if (transitioning && ["next", "answered", "skip", "phase"].includes(action)) return;
  busy = true;
  try {
    const cur = shownId || (S.current && S.current.id);
    if (action === "next") render(await post("/api/next", { from: cur }));
    if (action === "phase") render(await post("/api/next-phase", { from: cur }));
    if (action === "sync") { await post("/api/sync"); $("sync-dot").className = "dot busy"; }
    if (action === "answered" && cur) render(await post(`/api/questions/${encodeURIComponent(cur)}/answered`));
    if (action === "skip" && cur) render(await post(`/api/questions/${encodeURIComponent(cur)}/skip`));
    if (action === "enough") await post("/api/enough");
    if (action === "wispr") await post("/api/wispr", { text: $("wispr").value });
    if (action === "finalize") {
      if (confirm(t("console.confirmFinalize"))) await post("/api/finalize");
    }
  } catch (e) {
    $("console-status").insertAdjacentHTML("beforeend", ` · <span class="err">${esc(e.message)}</span>`);
  } finally {
    busy = false;
  }
}

$("start").addEventListener("click", async () => {
  setText($("consent-error"), "");
  starting = true;
  $("start").disabled = true;
  try { render(await post("/api/start")); }
  catch (e) { setText($("consent-error"), e.message); }
  finally { starting = false; $("start").disabled = false; }
});
document.querySelectorAll("[data-action]").forEach((b) => b.addEventListener("click", () => act(b.dataset.action)));
$("console-toggle").addEventListener("click", () => $("console").classList.toggle("hidden"));
$("details-toggle").addEventListener("click", () => toggleDetails());

function applyDetails() {
  setClass(document.body, "details-open", showDetails);
  $("details-toggle").setAttribute("aria-pressed", String(showDetails));
  if (S) renderStrip();
}
function toggleDetails() {
  showDetails = !showDetails;
  store("pacenote.details", showDetails ? "1" : "0");
  applyDetails();
}
applyDetails();
$("interview").addEventListener("scroll", onPanelScroll, { passive: true });
$("jump").addEventListener("click", () => scrollToCurrent(true));

document.addEventListener("keydown", (e) => {
  if (["TEXTAREA", "SELECT", "INPUT"].includes(e.target.tagName) || e.metaKey || e.ctrlKey || e.altKey) return;
  const k = e.key.toLowerCase();
  if (k === "n" || k === "enter") { e.preventDefault(); act("next"); }
  else if (k === "c") $("console").classList.toggle("hidden");
  else if (k === "s") act("sync");
  else if (k === "a") act("answered");
  else if (k === "k") act("skip");
  else if (k === "p") act("phase");
  else if (k === "d") toggleDetails();
  else if (k === "t") { showTail = !showTail; store("pacenote.tail", showTail ? "1" : "0"); if (S) renderStrip(); }
  else if (k === "l") cycleUiLanguage();
  else if (k === "f") {
    if (document.fullscreenElement) document.exitFullscreen(); else document.documentElement.requestFullscreen?.();
  }
});

function connect() {
  const es = new EventSource("/api/events");
  es.addEventListener("state", (ev) => render(JSON.parse(ev.data)));
  es.onerror = () => { $("sync-dot").className = "dot err"; $("sync-dot").title = t("console.disconnected"); };
}

// The screen opens in the session's screen language (default: the client's language).
fetch("/api/state").then((r) => r.json())
  .then(async (st) => { await i18n.init({ lang: st.ui_language, preferStored: false }); render(st); })
  .catch(() => i18n.init())
  .finally(connect);
