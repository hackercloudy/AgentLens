/* AgentLens front-end core: DOM helper, API client, shared settings, reusable widgets, trace renderer. */
(function () {
  "use strict";
  const AL = (window.AL = { views: {}, state: {} });

  /* ---------------------------------------------------------------- dom */
  AL.h = function h(tag, attrs, ...kids) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === undefined || v === null || v === false) continue;
      if (k === "class") el.className = v;
      else if (k === "text") el.textContent = v;
      else if (k === "html") el.innerHTML = v;
      else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
      else if (k === "style" && typeof v === "object") Object.assign(el.style, v);
      else if (k === "value") el.value = v;
      else el.setAttribute(k, v === true ? "" : v);
    }
    for (const kid of kids.flat()) {
      if (kid === null || kid === undefined || kid === false) continue;
      el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
    }
    return el;
  };
  const h = AL.h;
  AL.$ = (s, el = document) => el.querySelector(s);
  AL.$$ = (s, el = document) => Array.from(el.querySelectorAll(s));
  AL.esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  AL.num = (n) => (n === null || n === undefined ? "" : Number(n).toLocaleString("en-US"));
  AL.pct = (n, d = 1) => `${Number(n).toFixed(d)}%`;
  AL.sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  AL.debounce = (fn, ms) => {
    let t;
    return (...a) => {
      clearTimeout(t);
      t = setTimeout(() => fn(...a), ms);
    };
  };

  const ICONS = {
    chat: '<path d="M4 5h16v11H9l-5 4V5z"/>',
    compare: '<path d="M4 5h6v14H4zM14 5h6v14h-6z"/>',
    knowledge: '<path d="M5 4h4v16H5zM11 4h4v16h-4zM17.5 5l3 .8-3.4 14.4-3-.8z"/>',
    playground: '<path d="M4 7h10M18 7h2M4 17h2M10 17h10"/><circle cx="16" cy="7" r="2"/><circle cx="8" cy="17" r="2"/>',
    evals: '<path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/>',
    redteam: '<path d="M12 3l8 3v6c0 4.5-3.3 7.8-8 9-4.7-1.2-8-4.5-8-9V6l8-3z"/><path d="M9 12l2.2 2.2L15.5 10"/>',
    learn: '<path d="M3 8l9-4 9 4-9 4-9-4z"/><path d="M7 10.5V16c0 1.2 2.2 2.5 5 2.5s5-1.3 5-2.5v-5.5"/>',
    send: '<path d="M4 12l16-8-6 16-2.5-6.5L4 12z"/>',
    plus: '<path d="M12 5v14M5 12h14"/>',
    trash: '<path d="M5 7h14M10 7V4h4v3M7 7l1 13h8l1-13"/>',
    play: '<path d="M7 4l13 8-13 8V4z"/>',
    stop: '<rect x="6" y="6" width="12" height="12" rx="1"/>',
    refresh: '<path d="M20 11a8 8 0 10-2.3 6M20 4v7h-7"/>',
    theme: '<path d="M20 14.5A8 8 0 019.5 4 8 8 0 1020 14.5z"/>',
  };
  AL.icon = (name) => {
    const s = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    s.setAttribute("viewBox", "0 0 24 24");
    s.setAttribute("aria-hidden", "true");
    s.innerHTML = ICONS[name] || "";
    return s;
  };

  /* -------------------------------------------------------------- theme */
  AL.THEMES = [["emerald", "Emerald theme"], ["light", "Aurora theme"], ["dark", "Midnight theme"]];
  const applyTheme = (t) => { document.documentElement.dataset.theme = t; document.documentElement.dataset.mode = t === "light" ? "light" : "dark"; };
  AL.theme = (function () { try { return localStorage.getItem("agentlens.theme2") || "emerald"; } catch (_) { return "emerald"; } })();
  if (!AL.THEMES.some(([id]) => id === AL.theme)) AL.theme = "emerald";
  applyTheme(AL.theme);
  AL.themeLabel = () => (AL.THEMES.find(([id]) => id === AL.theme) || AL.THEMES[0])[1];
  AL.setTheme = function (t) {
    AL.theme = t; applyTheme(t);
    try { localStorage.setItem("agentlens.theme2", t); } catch (_) {}
    const lbl = document.querySelector(".theme-btn .tl"); if (lbl) lbl.textContent = AL.themeLabel();
    if (AL.current === "knowledge" && AL.views.knowledge) AL.go("knowledge");
  };
  AL.nextTheme = () => { const i = AL.THEMES.findIndex(([id]) => id === AL.theme); AL.setTheme(AL.THEMES[(i + 1) % AL.THEMES.length][0]); };

  /* ---------------------------------------------------------------- api */
  async function request(method, url, body, isForm) {
    const opts = { method, headers: {} };
    if (body !== undefined) {
      if (isForm) opts.body = body;
      else {
        opts.headers["Content-Type"] = "application/json";
        opts.body = JSON.stringify(body);
      }
    }
    let res;
    try {
      res = await fetch(url, opts);
    } catch (e) {
      throw new Error("Cannot reach the AgentLens server. Is it still running?");
    }
    if (!res.ok) {
      let detail = res.statusText;
      try {
        const j = await res.json();
        detail = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail);
      } catch (_) {}
      throw new Error(detail);
    }
    return res.json();
  }
  AL.api = {
    get: (u) => request("GET", u),
    post: (u, b) => request("POST", u, b || {}),
    del: (u) => request("DELETE", u),
    upload: (u, form) => request("POST", u, form, true),
  };

  /** Poll a background job until it finishes. onTick receives the job status. */
  AL.pollJob = async function (job, onTick) {
    for (;;) {
      const st = await AL.api.get(`/api/jobs/${job.id}`);
      onTick && onTick(st);
      if (st.status !== "running") {
        if (st.status === "error") throw new Error(st.error || "Job failed");
        return st;
      }
      await AL.sleep(350);
    }
  };

  /* ------------------------------------------------------------ settings */
  const DEFAULTS = {
    model: "mock:offline-demo", temperature: 0.2, top_p: 0.95, top_k: 4, retrieval: "hybrid", prompt_style: "zero-shot",
    language: "auto", use_docs: true, use_sql: true, use_calc: true, guardrails: true, general_knowledge: true, use_web: true, show_cites: true, max_steps: 5, context_window: 8192,
  };
  let saved = {};
  try { saved = JSON.parse(localStorage.getItem("agentlens.settings") || "{}"); } catch (_) {}
  AL.settings = Object.assign({}, DEFAULTS, saved);
  AL.applyCites = () => document.body && document.body.classList.toggle("no-cites", !AL.settings.show_cites);
  AL.saveSettings = () => { AL.applyCites(); try { localStorage.setItem("agentlens.settings", JSON.stringify(AL.settings)); } catch (_) {} };
  AL.resetSettings = () => { Object.assign(AL.settings, DEFAULTS); AL.saveSettings(); };
  AL.models = { providers: [] };

  AL.loadModels = async function (refresh) {
    try {
      AL.models = await AL.api.get("/api/models" + (refresh ? "?refresh=true" : ""));
    } catch (_) { /* keep previous */ }
    const flat = AL.models.providers.flatMap((p) => p.models.map((m) => m.id));
    if (!flat.includes(AL.settings.model)) { AL.settings.model = "mock:offline-demo"; AL.saveSettings(); }
    // First time a real local model shows up, switch to it (a real chatbot beats the simulator).
    try {
      const ollama = AL.models.providers.find((p) => p.id === "ollama");
      if (ollama && ollama.available && ollama.models.length && AL.settings.model.startsWith("mock:") && !localStorage.getItem("agentlens.autopicked")) {
        const best = ollama.models.find((m) => /qwen|llama|gemma|mistral|phi/i.test(m.name)) || ollama.models[0];
        AL.settings.model = best.id; AL.saveSettings(); localStorage.setItem("agentlens.autopicked", "1");
        setTimeout(() => AL.toast(`Using ${best.name} from Ollama. You can switch models in the top bar.`), 400);
      }
      const groq = AL.models.providers.find((p) => p.id === "groq");
      if (groq && groq.models.length && !(ollama && ollama.available && ollama.models.length) && AL.settings.model.startsWith("mock:") && !localStorage.getItem("agentlens.autopicked")) {
        const best = groq.models.find((m) => m.name === "llama-3.1-8b-instant") || groq.models[0];
        AL.settings.model = best.id; AL.saveSettings(); localStorage.setItem("agentlens.autopicked", "1");
      }
    } catch (_) {}
    AL.updateStatus();
    return AL.models;
  };

  AL.updateStatus = function () {
    const dot = AL.$("#status-dot"), txt = AL.$("#status-text"), sub = AL.$("#status-sub");
    if (!dot) return;
    const [prov, ...rest] = AL.settings.model.split(":");
    const name = rest.join(":");
    dot.className = "dot " + (prov === "mock" ? "sim" : "on");
    txt.textContent = prov === "mock" ? "Offline simulator" : name;
    const ollama = AL.models.providers.find((p) => p.id === "ollama");
    sub.textContent = prov === "mock"
      ? (ollama && ollama.available ? "Ollama detected. Pick a model above." : "No local model needed.")
      : `via ${prov}`;
  };

  AL.modelSelect = function (value, onChange, opts = {}) {
    const sel = h("select", { "aria-label": opts.label || "Model", onchange: (e) => onChange(e.target.value) });
    for (const p of AL.models.providers) {
      const og = h("optgroup", { label: p.label + (p.available ? "" : " (not detected)") });
      if (!p.models.length) og.append(h("option", { value: "", disabled: true, text: p.note || "no models" }));
      for (const m of p.models) og.append(h("option", { value: m.id, text: m.name, selected: m.id === value }));
      sel.append(og);
    }
    sel.value = value;
    return sel;
  };

  /* ------------------------------------------------------------- widgets */
  AL.toast = function (msg, bad) {
    const t = h("div", { class: "toast" + (bad ? " bad" : ""), role: "status", text: msg });
    document.body.append(t);
    setTimeout(() => t.remove(), bad ? 6000 : 3200);
  };

  AL.modal = function (title, body) {
    const back = h("div", { class: "modal-back", onclick: (e) => e.target === back && close() });
    const close = () => { back.remove(); document.removeEventListener("keydown", onKey); };
    const onKey = (e) => e.key === "Escape" && close();
    document.addEventListener("keydown", onKey);
    back.append(h("div", { class: "modal", role: "dialog", "aria-label": title },
      h("header", {}, h("h3", { text: title }), h("button", { class: "btn ghost sm", onclick: close, text: "Close" })),
      h("div", { class: "body" }, body)));
    document.body.append(back);
    return close;
  };

  AL.switchEl = function (label, checked, onChange, title) {
    const input = h("input", { type: "checkbox", onchange: (e) => onChange(e.target.checked) });
    input.checked = !!checked;
    return h("label", { class: "switch", title: title || "" }, input, h("span", { class: "track" }), h("span", { text: label }));
  };

  AL.rangeEl = function ({ label, min, max, step, value, onInput, format, hint }) {
    const out = h("output", { text: (format || String)(value) });
    const input = h("input", { type: "range", min, max, step, value, "aria-label": label,
      oninput: (e) => { const v = parseFloat(e.target.value); out.textContent = (format || String)(v); onInput(v); } });
    return h("div", { class: "field" }, h("label", { text: label }), h("div", { class: "rangerow" }, input, out), hint ? h("div", { class: "hint", text: hint }) : null);
  };

  AL.segEl = function (options, value, onChange) {
    const wrap = h("div", { class: "seg", role: "group" });
    const draw = (v) => {
      wrap.innerHTML = "";
      for (const [val, label] of options) {
        wrap.append(h("button", { type: "button", "aria-pressed": String(v === val), text: label, onclick: () => { draw(val); onChange(val); } }));
      }
    };
    draw(value);
    return wrap;
  };

  AL.chip = (text, cls = "") => h("span", { class: "chip " + cls, text });


  /* ------------------------------------------------------ animated numbers */
  AL.countUp = function (el, to, { decimals = 0, duration = 800, suffix = "" } = {}) {
    if (window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches) { el.textContent = to.toFixed(decimals) + suffix; return; }
    const t0 = performance.now();
    const step = (t) => {
      const k = Math.min(1, (t - t0) / duration), e = 1 - Math.pow(1 - k, 3);
      el.textContent = (to * e).toFixed(decimals) + suffix;
      if (k < 1) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  };

  /* ------------------------------------------------------------- markdown */
  // Small, safe markdown renderer: everything is HTML-escaped first, then a limited syntax is re-introduced.
  AL.md = function (text, onCite, reveal) {
    const stash = [];
    const hold = (html) => { stash.push(html); return `\u0000${stash.length - 1}\u0000`; };
    let t = String(text ?? "");
    t = t.replace(/```(\w*)\n?([\s\S]*?)```/g, (_, lang, code) => hold(`<pre class="codeblock md-code"><code>${AL.esc(code.replace(/\n$/, ""))}</code></pre>`));
    t = t.replace(/`([^`\n]+)`/g, (_, code) => hold(`<code class="md-inline">${AL.esc(code)}</code>`));
    t = AL.esc(t);
    t = t.replace(/\[(\d+)\]/g, (_, n) => hold(`<button class="cite" type="button" data-n="${n}" title="Open source ${n}">${n}</button>`));
    t = t.replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, (_, label, url) => `<a href="${url}" target="_blank" rel="noopener noreferrer">${label}</a>`);
    t = t.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>").replace(/(^|[\s(])\*([^*\n]+)\*(?=[\s).,!?:;]|$)/g, "$1<em>$2</em>");
    const blocks = t.split(/\n{2,}/).map((block) => {
      const lines = block.split("\n");
      if (lines.every((l) => /^\s*[-*\u2022]\s+/.test(l))) return "<ul>" + lines.map((l) => `<li>${l.replace(/^\s*[-*\u2022]\s+/, "")}</li>`).join("") + "</ul>";
      if (lines.every((l) => /^\s*\d+[.)]\s+/.test(l))) return "<ol>" + lines.map((l) => `<li>${l.replace(/^\s*\d+[.)]\s+/, "")}</li>`).join("") + "</ol>";
      const head = /^(#{1,4})\s+(.*)$/.exec(lines[0]);
      if (head && lines.length === 1) return `<h4 class="md-h">${head[2]}</h4>`;
      return `<p>${lines.join("<br>")}</p>`;
    });
    let html = blocks.join("");
    html = html.replace(/\u0000(\d+)\u0000/g, (_, i) => stash[+i]);
    const box = h("div", { class: "md", html });
    if (onCite) box.addEventListener("click", (e) => { const b = e.target.closest(".cite"); if (b) onCite(parseInt(b.dataset.n, 10)); });
    if (reveal && !(window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches)) {
      const walker = document.createTreeWalker(box, NodeFilter.SHOW_TEXT, { acceptNode: (n) => (n.parentElement.closest("pre, button") || !n.nodeValue.trim() ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT) });
      const nodes = []; while (walker.nextNode()) nodes.push(walker.currentNode);
      const total = nodes.reduce((a, n) => a + n.nodeValue.split(/(\s+)/).filter((w) => w.trim()).length, 0) || 1;
      const per = Math.min(38, 1500 / total);
      let i = 0;
      nodes.forEach((n) => {
        const frag = document.createDocumentFragment();
        n.nodeValue.split(/(\s+)/).forEach((w) => {
          if (!w.trim()) return frag.append(w);
          const sp = h("span", { class: "rv", text: w }); sp.style.animationDelay = `${Math.round(i++ * per)}ms`; frag.append(sp);
        });
        n.replaceWith(frag);
      });
    }
    return box;
  };

  /* ------------------------------------------------------- citations text */
  AL.richAnswer = function (text, onCite, reveal) { return AL.md(text, onCite, reveal); };

  /* --------------------------------------------------------- trace render */
  const GUARD_LABEL = {
    "input-guard": "Input guard", "context-sanitiser": "Context sanitiser", "sql-guard": "SQL guard",
    "db-readonly": "Read-only database", "output-guard": "Output guard",
  };
  AL.guardLabel = (g) => GUARD_LABEL[g] || g;

  function sqlTable(data) {
    if (!data || !data.columns) return null;
    const table = h("table", { class: "mini-table" },
      h("thead", {}, h("tr", {}, data.columns.map((c) => h("th", { text: c })))),
      h("tbody", {}, data.rows.slice(0, 8).map((r) => h("tr", {}, r.map((v) => h("td", { text: v === null ? "" : typeof v === "number" ? AL.num(v) : String(v) }))))));
    return h("div", { class: "tbl-wrap" }, table);
  }

  AL.stepNode = function (s) {
    const cls = ["node", s.kind];
    if (s.kind === "observation" && s.ok === false) cls.push("bad");
    const head = h("div", { class: "node-h" }, h("b", { text: s.title }),
      s.tool && s.kind === "tool" ? AL.chip(s.tool, "beam") : null,
      h("span", { class: "t", text: s.ms !== undefined && s.kind !== "tool" ? `${s.ms} ms` : s.t !== undefined ? `${(s.t / 1000).toFixed(2)} s` : "" }));
    const node = h("div", { class: cls.join(" ") }, h("span", { class: "pin" }), head);

    if (s.kind === "tool") {
      node.append(h("div", { class: "codeblock" + (s.tool === "run_sql" ? " sql" : ""), text: s.input ?? s.text }));
    } else if (s.kind === "observation") {
      const d = s.data || {};
      if (s.tool === "search_docs" && d.hits) {
        const shown = d.hits.slice(0, d.k || 4);
        node.append(h("div", { class: "node-t", text: shown.length ? `Retrieved ${shown.length} passage${shown.length > 1 ? "s" : ""} (${d.mode})` : "Nothing retrieved" }));
        for (const hit of shown) {
          node.append(h("div", { class: "hitbar", title: hit.text },
            h("div", {}, h("div", { class: "nm", text: `${hit.doc}${hit.heading ? " / " + hit.heading : ""}` }),
              h("div", { class: "bar " + (hit.score >= (d.min_score || 0) ? "" : "beam") }, h("i", { style: { width: `${Math.min(100, Math.round(hit.score * 100))}%` } }))),
            h("span", { class: "small muted", text: hit.score.toFixed(2) })));
        }
        if (s.text.startsWith("No relevant")) node.append(h("div", { class: "node-t", text: s.text }));
        if (s.guards && s.guards.length) node.append(h("div", { class: "node-t", style: { color: "var(--bad)" }, text: "A retrieved passage contained instruction-like text and was cleaned before the model saw it." }));
      } else if (s.tool === "web_search") {
        const res = d.results || [];
        node.append(h("div", { class: "node-t", text: res.length ? `Found ${res.length} web result${res.length > 1 ? "s" : ""} via ${d.provider}` : s.text }));
        res.forEach((r) => node.append(h("div", { class: "web-hit" },
          h("a", { href: r.url, target: "_blank", rel: "noopener noreferrer", text: r.title || r.domain }), h("span", { class: "small muted", text: r.domain }))));
        if (s.guards && s.guards.length) node.append(h("div", { class: "node-t", style: { color: "var(--bad)" }, text: "A result contained instruction-like text and was cleaned before the model saw it." }));
      } else if (s.tool === "run_sql" && d.columns) {
        node.append(h("div", { class: "node-t", text: `${d.rows.length} row${d.rows.length === 1 ? "" : "s"} returned` }), sqlTable(d));
      } else {
        node.append(h("div", { class: "node-t", text: s.text.length > 360 ? s.text.slice(0, 360) + "..." : s.text }));
      }
    } else if (s.kind === "guard") {
      node.append(h("div", { class: "node-t", style: { color: "var(--bad)" }, text: s.text }));
    } else if (s.kind === "answer") {
      node.append(h("div", { class: "node-t", text: s.text.length > 200 ? s.text.slice(0, 200) + "..." : s.text }));
    } else if (s.text) {
      node.append(h("div", { class: "node-t", text: s.text }));
    }
    if (s.prompt_tokens) node.append(h("div", { class: "small muted", style: { marginTop: "3px" }, text: `${AL.num(s.prompt_tokens)} tokens in, ${AL.num(s.completion_tokens)} out` }));
    return node;
  };

  AL.renderTrace = function (steps, opts) {
    const wrap = h("div", { class: "trace" + (opts && opts.animate === false ? " static" : "") });
    steps.forEach((s) => wrap.append(AL.stepNode(s)));
    return wrap;
  };

  /* ---------------------------------------------------------- budget view */
  const BUDGET_PARTS = [
    ["system", "System prompt and tools", "#164766"],
    ["history", "Earlier chat turns", "#6d8899"],
    ["question", "Your question", "#2760a8"],
    ["retrieved", "Retrieved text and tool output", "#f0ae2e"],
    ["steps", "Agent steps so far", "#148468"],
  ];
  AL.renderBudget = function (res) {
    const b = res.budget;
    if (!b) return h("div", { class: "empty", text: "No model call was made for this message." });
    const bar = h("div", { class: "budget-bar", role: "img", "aria-label": `Context window ${b.percent}% used` });
    const legend = h("div", { class: "legend" });
    for (const [key, label, color] of BUDGET_PARTS) {
      bar.append(h("i", { style: { width: `${(100 * b[key]) / b.window}%`, background: color }, title: `${label}: ${b[key]} tokens` }));
      legend.append(h("span", { class: "k" }, h("i", { class: "sw", style: { background: color } }), label), h("span", { class: "small", text: AL.num(b[key]) }));
    }
    const u = res.usage;
    return h("div", {},
      h("div", { class: "row", style: { alignItems: "baseline", marginBottom: "10px" } },
        h("div", { class: "bigstat", text: AL.pct(b.percent) }), h("span", { class: "muted", text: `of a ${AL.num(b.window)}-token context window (last model call)` })),
      bar, legend, h("hr", { class: "divider" }),
      h("div", { class: "stat-row" },
        h("div", { class: "stat" }, h("b", { text: AL.num(u.prompt_tokens) }), h("span", { text: "prompt tokens, all calls" })),
        h("div", { class: "stat" }, h("b", { text: AL.num(u.completion_tokens) }), h("span", { text: "generated tokens" })),
        h("div", { class: "stat" }, h("b", { text: u.llm_calls }), h("span", { text: "model calls" }))),
      h("p", { class: "small muted", style: { marginTop: "12px" }, text: u.estimated
        ? "Token counts here are estimates from a built-in approximate tokenizer. Providers such as Ollama report exact counts."
        : "Token counts are as reported by the model provider." }));
  };
})();
