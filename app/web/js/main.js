/* Router and bootstrap. */
(function () {
  "use strict";
  const { h, $ } = AL;

  const NAV = [
    ["chat", "Chat", "chat"],
    ["compare", "Compare", "compare"],
    ["knowledge", "Knowledge", "knowledge"],
    ["playground", "Playground", "playground"],
    ["evals", "Evals", "evals"],
    ["redteam", "Red team", "redteam"],
    ["learn", "Learn", "learn"],
  ];

  function drawNav(current) {
    const nav = $("#nav");
    nav.innerHTML = "";
    NAV.forEach(([id, label, icon]) => nav.append(h("button", {
      type: "button", "aria-current": id === current ? "page" : "false", onclick: () => AL.go(id),
    }, AL.icon(icon), h("span", { text: label }))));
  }

  AL.go = function (view) {
    if (!AL.views[view]) view = "chat";
    AL.current = view;
    drawNav(view);
    const container = $("#view");
    container.scrollTop = 0;
    AL.views[view].mount(container);
    if (location.hash.slice(1) !== view) history.replaceState(null, "", "#" + view);
    document.title = `${NAV.find((n) => n[0] === view)[1]} | AgentLens`;
  };

  window.addEventListener("hashchange", () => {
    const v = location.hash.slice(1);
    if (v && v !== AL.current && AL.views[v]) AL.go(v);
  });


  /* ------------------------------------------------------ command palette */
  function commands() {
    const go = NAV.map(([id, label]) => ({ kind: "Go to", label: label, run: () => AL.go(id) }));
    const ask = (AL.starters || []).map(([q]) => ({ kind: "Ask", label: q, run: () => AL.sendChat(q) }));
    const toggle = (key, label) => ({ kind: "Toggle", label: `${AL.settings[key] ? "Turn off" : "Turn on"}: ${label}`, run: () => { AL.settings[key] = !AL.settings[key]; AL.saveSettings(); AL.toast(`${label} is now ${AL.settings[key] ? "on" : "off"}`); if (AL.current === "chat") AL.go("chat"); } });
    return [
      ...AL.THEMES.filter(([id]) => id !== AL.theme).map(([id, label]) => ({ kind: "Theme", label: `Switch to ${label}`, run: () => AL.setTheme(id) })),
      { kind: "Action", label: "New chat", run: () => { AL.go("chat"); AL.newChat && AL.newChat(); } },
      ...go, ...ask,
      toggle("guardrails", "Guardrails"), toggle("use_web", "Web search"), toggle("show_cites", "Citation numbers"), toggle("general_knowledge", "General knowledge"), toggle("use_docs", "Use documents"),
      { kind: "Action", label: "Refresh model list (after starting Ollama)", run: () => AL.loadModels(true).then(() => { AL.toast("Model list refreshed"); if (AL.current === "chat") AL.go("chat"); }) },
    ];
  }

  function openPalette() {
    if (document.querySelector(".palette-back")) return;
    const all = commands();
    let shown = all, idx = 0;
    const list = h("ul", { role: "listbox" });
    const input = h("input", { type: "text", placeholder: "Type a command or a question to ask...", "aria-label": "Command palette" });
    const close = () => { back.remove(); document.removeEventListener("keydown", onKey, true); };
    const draw = () => {
      list.innerHTML = "";
      shown.forEach((c, i) => list.append(h("li", { role: "option", "aria-selected": String(i === idx), onclick: () => { close(); c.run(); }, onmousemove: () => { if (idx !== i) { idx = i; draw(); } } },
        h("span", { text: c.label }), AL.chip(c.kind, c.kind === "Ask" ? "beam" : "line"))));
      const sel = list.children[idx]; if (sel && sel.scrollIntoView) sel.scrollIntoView({ block: "nearest" });
    };
    const filter = () => {
      const q = input.value.trim().toLowerCase();
      shown = q ? all.filter((c) => c.label.toLowerCase().includes(q) || c.kind.toLowerCase().includes(q)) : all;
      if (q && !shown.some((c) => c.kind === "Ask" && c.label.toLowerCase() === q) && q.length > 2) shown = [...shown, { kind: "Ask", label: input.value.trim(), run: () => AL.sendChat(input.value.trim()) }];
      idx = 0; draw();
    };
    const onKey = (e) => {
      if (e.key === "Escape") { e.preventDefault(); close(); }
      else if (e.key === "ArrowDown") { e.preventDefault(); idx = Math.min(shown.length - 1, idx + 1); draw(); }
      else if (e.key === "ArrowUp") { e.preventDefault(); idx = Math.max(0, idx - 1); draw(); }
      else if (e.key === "Enter") { e.preventDefault(); const c = shown[idx]; if (c) { close(); c.run(); } }
    };
    const back = h("div", { class: "palette-back", onclick: (e) => e.target === back && close() },
      h("div", { class: "palette", role: "dialog", "aria-label": "Command palette" }, input, list,
        h("div", { class: "hint-row" }, h("kbd", { text: "\u2191" }), " ", h("kbd", { text: "\u2193" }), " to move, ", h("kbd", { text: "Enter" }), " to run, ", h("kbd", { text: "Esc" }), " to close")));
    input.addEventListener("input", filter);
    document.addEventListener("keydown", onKey, true);
    document.body.append(back); draw(); input.focus();
  }

  document.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") { e.preventDefault(); openPalette(); return; }
    const tag = (document.activeElement || {}).tagName;
    if (e.key === "/" && !["INPUT", "TEXTAREA", "SELECT"].includes(tag)) { const inp = document.getElementById("chat-input"); if (inp) { e.preventDefault(); inp.focus(); } }
  });

  function mountThemeButton() {
    const foot = $(".rail-foot");
    if (!foot || foot.querySelector(".theme-btn")) return;
    foot.append(h("button", { class: "theme-btn", type: "button", "aria-label": "Switch between light and dark theme", onclick: () => AL.nextTheme() },
      AL.icon("theme"), h("span", { class: "tl", text: AL.themeLabel() }), h("span", { class: "swatch" })));
  }

  (async function boot() {
    mountThemeButton();
    AL.applyCites();
    drawNav("chat");
    try { AL.health = await AL.api.get("/api/health"); } catch (_) { AL.health = {}; }
    await AL.loadModels();
    AL.go(location.hash.slice(1) || "chat");
    // keep the model list fresh (e.g. user starts Ollama after opening the page)
    setInterval(() => AL.loadModels(true).then(() => AL.updateStatus()), 30000);
  })();
})();
