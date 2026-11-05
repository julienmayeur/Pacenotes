"use strict";
/*
 * i18n.js — the one front-end i18n helper, shared by every screen (interview, admin…).
 *
 *   <script src="/static/i18n.js"></script>
 *   await i18n.init({ lang: "fr" });          // loads /api/i18n + the catalog (fallbacks filled in)
 *   i18n.t("focus.countOf", { n: 2, m: 5 });  // "2 sur 5"  ({{name}} interpolation, i18next format)
 *   i18n.t("interview.bannerDone", {}, { lang: "de" });  // another language (loaded on demand)
 *   <span data-i18n="focus.now"></span>       // static text, re-translated on every switch
 *   <input data-i18n-attr="placeholder:console.wisprPlaceholder;title:app.console">
 *   <language-select bind="ui"></language-select>   // the screen-language switcher
 *   i18n.onChange((lang, info) => render());  // re-render dynamic parts, no page reload
 *
 * Catalogs are the JSON files in pacenote/locales/ (the same ones Python uses), served by
 * pacenote.i18n.api_router(). Adding a language = adding <lang>.json there.
 */
(function () {
  const state = {
    base: "/api/i18n",
    lang: null,
    fallback: "en",
    languages: [],      // [{code, name, native, label}] UI languages with a locale file
    speech: [],         // common speech locales offered by the server
    catalogs: {},       // lang -> flat catalog (server already merged the fallbacks)
    hasFile: {},        // lang -> does it have its own locale file?
    overlays: {},       // lang -> machine-translated strings (interview language without a file)
    loading: {},        // lang -> Promise
    listeners: new Set(),
    storageKey: "pacenote.ui-language",
  };

  const PARAM = /\{\{\s*([\w.]+)\s*\}\}/g;
  const base = (code) => String(code || "").split(/[-_]/)[0].toLowerCase();

  function interpolate(text, params) {
    if (!params) return text;
    return text.replace(PARAM, (m, k) => (params[k] !== undefined && params[k] !== null ? String(params[k]) : m));
  }

  async function fetchJson(url) {
    const res = await fetch(url, { headers: { Accept: "application/json" } });
    if (!res.ok) throw new Error(`${url}: ${res.status}`);
    return res.json();
  }

  function load(lang) {
    if (!lang) return Promise.resolve();
    if (state.catalogs[lang]) return Promise.resolve(state.catalogs[lang]);
    if (!state.loading[lang]) {
      state.loading[lang] = fetchJson(`${state.base}/${encodeURIComponent(lang)}`)
        .then((d) => { state.catalogs[lang] = d.catalog || {}; state.hasFile[lang] = !!d.has_file; return state.catalogs[lang]; })
        .catch((e) => {  // keep the fallback; never retry in a loop (t() calls load() on every render)
          console.warn("i18n:", e.message);
          state.catalogs[lang] = state.catalogs[lang] || {};
          return state.catalogs[lang];
        });
    }
    return state.loading[lang];
  }

  /** Best available UI language for a wish: exact, then base language, then the fallback. */
  function resolve(code) {
    const codes = state.languages.map((l) => l.code);
    if (!codes.length) return code || state.fallback;
    if (codes.includes(code)) return code;
    if (codes.includes(base(code))) return base(code);
    return codes.includes(state.fallback) ? state.fallback : codes[0];
  }

  function stored() {
    try { return localStorage.getItem(state.storageKey); } catch (_) { return null; }
  }
  function store(lang) {
    try { localStorage.setItem(state.storageKey, lang); } catch (_) {}
  }

  /** t(key, params, {lang}) — translation with fallback; returns the key when nothing has it. */
  function t(key, params, opts) {
    if (!state.lang && !(opts && opts.lang)) return "";  // before init(): nothing rather than a flash
    const lang = (opts && opts.lang) || state.lang || state.fallback;
    const cat = state.catalogs[lang];
    if (!cat && !state.loading[lang]) {
      load(lang).then((c) => { if (c && Object.keys(c).length) emit("loaded"); });  // callers re-render on "loaded"
    }
    const own = cat && cat[key];
    const over = state.overlays[lang] && state.overlays[lang][key];
    let value = state.hasFile[lang] ? (own ?? over) : (over ?? own);
    if (value === undefined || value === null) {
      const fb = state.catalogs[state.fallback];
      value = fb && fb[key];
    }
    if (value === undefined || value === null) return key;
    return typeof value === "string" ? interpolate(value, params) : value;
  }

  /** Translate static markup: [data-i18n] text, [data-i18n-attr="attr:key;attr2:key2"]. */
  function translatePage(root) {
    const scope = root || document;
    scope.querySelectorAll("[data-i18n]").forEach((el) => {
      const v = t(el.dataset.i18n);
      if (el.textContent !== v) el.textContent = v;
    });
    scope.querySelectorAll("[data-i18n-attr]").forEach((el) => {
      el.dataset.i18nAttr.split(";").forEach((pair) => {
        const [attr, key] = pair.split(":").map((x) => x && x.trim());
        if (attr && key) el.setAttribute(attr, t(key));
      });
    });
  }

  function emit(kind, extra) {
    if (state.lang && (kind === "change" || kind === "loaded")) translatePage();
    for (const fn of state.listeners) {
      try { fn(state.lang, { kind, ...(extra || {}) }); } catch (e) { console.error(e); }
    }
  }

  /** Switch the UI language instantly (no reload). info.source = "user" when a person picked it. */
  async function setLanguage(lang, info) {
    const next = resolve(lang);
    await Promise.all([load(next), load(state.fallback)]);
    const changed = next !== state.lang;
    state.lang = next;
    document.documentElement.lang = next;
    if (info && info.source === "user") store(next);
    if (changed || (info && info.force)) emit("change", info);
    return next;
  }

  async function init(opts) {
    opts = opts || {};
    if (opts.base) state.base = opts.base;
    if (opts.storageKey) state.storageKey = opts.storageKey;
    try {
      const meta = await fetchJson(state.base);
      state.languages = meta.languages || [];
      state.fallback = meta.fallback || state.fallback;
      state.speech = meta.speech || [];
    } catch (e) {
      console.warn("i18n:", e.message);
    }
    const wish = opts.lang || (opts.preferStored !== false && stored()) || document.documentElement.lang || navigator.language;
    return setLanguage(wish, { source: "init", force: true });
  }

  /** Machine-translated strings for a language without a locale file (sent by the server). */
  function extend(lang, entries) {
    if (!lang || !entries) return;
    const before = JSON.stringify(state.overlays[lang] || {});
    state.overlays[lang] = { ...(state.overlays[lang] || {}), ...entries };
    if (JSON.stringify(state.overlays[lang]) !== before) load(lang);
  }

  /** Display name of a language or locale ("de" -> "allemand", "de-DE" -> "allemand (Allemagne)"). */
  function name(code, inLang) {
    try {
      const dn = new Intl.DisplayNames([inLang || state.lang || "en"], { type: "language" });
      const n = dn.of(code);
      return n ? n.charAt(0).toLocaleUpperCase(inLang || state.lang || "en") + n.slice(1) : code;
    } catch (_) {
      return code;
    }
  }

  function onChange(fn) { state.listeners.add(fn); return () => state.listeners.delete(fn); }

  window.i18n = {
    init, t, setLanguage, translatePage, extend, name, onChange, load, resolve, interpolate,
    get language() { return state.lang; },
    get languages() { return state.languages.slice(); },
    get speechLocales() { return state.speech.slice(); },
    get fallback() { return state.fallback; },
  };

  /*
   * <language-select> — the one language picker, for every screen.
   *   bind="ui"            switches the screen language itself (i18n.setLanguage)
   *   options="fr,en,de"   or the .options property; default: the UI languages
   *   value="fr"           or .value (an array with `multiple`)
   *   multiple             toggle chips instead of a <select> (e.g. spoken languages)
   *   allow-other          adds "Other…" to type any language code
   *   label="i18n.key"     optional label, translated
   *   compact              no label, short codes
   *   segmented            one button per option (FR | EN) instead of a <select>; `label` is then
   *                        the group's accessible name
   * Fires `language-change` ({detail: {value}}) on user changes.
   */
  class LanguageSelect extends HTMLElement {
    constructor() {
      super();
      this._options = null;
      this._value = null;
      this._off = null;
    }
    connectedCallback() {
      this.classList.add("language-select");
      this._off = onChange(() => this.render());
      this.render();
    }
    disconnectedCallback() { if (this._off) this._off(); }
    static get observedAttributes() { return ["options", "value", "label", "disabled"]; }
    attributeChangedCallback() { if (this.isConnected) this.render(); }

    get multiple() { return this.hasAttribute("multiple"); }
    get options() {
      if (this._options) return this._options;
      const attr = this.getAttribute("options");
      if (attr) return attr.split(",").map((x) => x.trim()).filter(Boolean);
      return state.languages.map((l) => l.code);
    }
    set options(list) {
      const next = Array.from(list || []);
      if (JSON.stringify(next) === JSON.stringify(this._options)) return;
      this._options = next;
      this.render();
    }
    get value() {
      if (this._value !== null) return this._value;
      if (this.getAttribute("bind") === "ui") return state.lang;
      const v = this.getAttribute("value") || "";
      return this.multiple ? v.split(",").filter(Boolean) : v;
    }
    set value(v) {
      const next = this.multiple ? Array.from(v || []) : v;
      if (JSON.stringify(next) === JSON.stringify(this._value)) return;
      this._value = next;
      this.render();
    }

    labelFor(code) {
      const known = state.languages.find((l) => l.code === code);
      if (this.getAttribute("bind") === "ui" && known) return known.label || known.native || name(code, code);
      return name(code);
    }

    pick(value) {
      this._value = value;
      if (this.getAttribute("bind") === "ui") setLanguage(value, { source: "user" });
      this.dispatchEvent(new CustomEvent("language-change", { detail: { value }, bubbles: true }));
      this.render();
    }

    render() {
      const disabled = this.hasAttribute("disabled");
      const compact = this.hasAttribute("compact");
      const labelKey = this.getAttribute("label");
      const value = this.value;
      const opts = [...this.options];
      for (const v of this.multiple ? value : [value]) if (v && !opts.includes(v)) opts.push(v);
      const sig = JSON.stringify([state.lang, opts, value, disabled, compact, labelKey]);
      if (sig === this._sig) return;
      this._sig = sig;
      this.textContent = "";
      if (labelKey && !compact) {
        const lab = document.createElement("span");
        lab.className = "ls-label";
        lab.textContent = t(labelKey);
        this.appendChild(lab);
      }
      if (this.multiple) {
        // the picked values as chips (click to remove, one must stay) + a menu to add one
        const box = document.createElement("span");
        box.className = "ls-chips";
        for (const code of value) {
          const b = document.createElement("button");
          b.type = "button";
          b.className = "ls-chip on";
          b.textContent = (compact ? code : `${this.labelFor(code)} · ${code}`) + (disabled || value.length < 2 ? "" : " ×");
          b.title = this.labelFor(code);
          b.disabled = disabled || value.length < 2;
          b.addEventListener("click", () => this.pick(value.filter((x) => x !== code)));
          box.appendChild(b);
        }
        const rest = opts.filter((c) => !value.includes(c));
        if (!disabled && rest.length) {
          const add = document.createElement("select");
          add.className = "ls-add";
          add.setAttribute("aria-label", t("lang.add"));
          const first = document.createElement("option");
          first.value = "";
          first.textContent = t("lang.add");
          add.appendChild(first);
          for (const code of rest.sort((a, b) => this.labelFor(a).localeCompare(this.labelFor(b)))) {
            const o = document.createElement("option");
            o.value = code;
            o.textContent = `${this.labelFor(code)} · ${code}`;
            add.appendChild(o);
          }
          add.addEventListener("change", () => { if (add.value) this.pick([...value, add.value]); });
          box.appendChild(add);
        }
        this.appendChild(box);
        return;
      }
      if (this.hasAttribute("segmented")) {
        const seg = document.createElement("span");
        seg.className = "ls-seg";
        seg.setAttribute("role", "group");
        if (labelKey) seg.setAttribute("aria-label", t(labelKey));
        for (const code of opts) {
          const b = document.createElement("button");
          b.type = "button";
          b.dataset.lang = code;
          b.textContent = code.toUpperCase();
          b.title = this.labelFor(code);
          b.disabled = disabled;
          b.className = code === value ? "on" : "";
          b.setAttribute("aria-pressed", String(code === value));
          b.addEventListener("click", () => { if (code !== this.value) this.pick(code); });
          seg.appendChild(b);
        }
        this.appendChild(seg);
        return;
      }
      const sel = document.createElement("select");
      sel.disabled = disabled;
      if (labelKey) sel.setAttribute("aria-label", t(labelKey));
      for (const code of opts) {
        const o = document.createElement("option");
        o.value = code;
        o.textContent = compact ? code.toUpperCase() : this.labelFor(code);
        o.title = this.labelFor(code);
        if (code === value) o.selected = true;
        sel.appendChild(o);
      }
      if (this.hasAttribute("allow-other")) {
        const o = document.createElement("option");
        o.value = "__other";
        o.textContent = t("lang.other");
        sel.appendChild(o);
      }
      sel.addEventListener("change", () => {
        let v = sel.value;
        if (v === "__other") {
          v = (window.prompt(t("lang.otherPrompt")) || "").trim();
          if (!/^[A-Za-z]{2,3}([-_][A-Za-z0-9]{2,8})*$/.test(v)) { this._sig = null; this.render(); return; }
        }
        this.pick(v);
      });
      this.appendChild(sel);
    }
  }
  if (!customElements.get("language-select")) customElements.define("language-select", LanguageSelect);
})();
