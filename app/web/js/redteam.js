/* Red Team view: run attacks with guardrails off and on, show what got through and which layer stopped it. */
(function () {
  "use strict";
  const { h, $, api } = AL;

  const S = (AL.state.redteam = { attacks: [], result: null, progress: null, busy: false });
  let root;

  async function run() {
    if (S.busy) return;
    S.busy = true; S.progress = { done: 0, total: 18, note: "" }; draw();
    try {
      const job = await api.post("/api/redteam/run", { config: { ...AL.settings, general_knowledge: false, use_web: false } });
      const st = await AL.pollJob(job, (t) => { S.progress = { done: t.done, total: t.total, note: t.note }; drawProgress(); });
      S.result = st.result;
    } catch (e) { AL.toast(e.message, true); }
    S.busy = false; S.progress = null; draw();
  }

  function drawProgress() {
    const box = $("#rt-progress");
    if (!box || !S.progress) return;
    box.innerHTML = "";
    box.append(h("div", { class: "row", style: { justifyContent: "space-between", margin: "6px 0" } }, h("b", { text: "Attacking..." }), h("span", { class: "small muted", text: `${S.progress.done} of ${S.progress.total} attempts` })),
      h("div", { class: "progress" }, h("i", { style: { width: `${(100 * S.progress.done) / S.progress.total}%` } })),
      h("div", { class: "small muted", style: { marginTop: "6px" }, text: S.progress.note }));
  }

  function summary(r) {
    const stat = (value, label, tone) => h("div", { class: "stat" }, h("b", { style: { color: tone }, text: value }), h("span", { text: label }));
    return h("div", { class: "panel" }, h("div", { class: "panel-b" }, h("div", { class: "score-hero" },
      stat(`${r.succeeded_off} of ${r.total}`, "attacks got through with guardrails off", r.succeeded_off ? "var(--bad)" : "var(--ok)"),
      stat(`${r.succeeded_on} of ${r.total}`, "got through with guardrails on", r.succeeded_on ? "var(--bad)" : "var(--ok)"),
      stat(r.control_ok_on ? "Yes" : "No", "normal question still answered correctly", r.control_ok_on ? "var(--ok)" : "var(--bad)"))));
  }

  function verdict(cell, kind, mode) {
    if (kind === "control") return cell.control_ok ? h("span", { class: "verdict", style: { color: "var(--ok)" } }, h("span", { class: "mark ok", text: "\u2713" }), "Answered") : h("span", { class: "verdict", style: { color: "var(--bad)" } }, h("span", { class: "mark bad", text: "\u2715" }), "Broken");
    return cell.succeeded
      ? h("span", { class: "verdict", style: { color: "var(--bad)" } }, h("span", { class: "mark bad", text: "!" }), "Got through")
      : h("span", { class: "verdict", style: { color: "var(--ok)" } }, h("span", { class: "mark ok", text: "\u2713" }), mode === "off" ? "Failed anyway" : "Stopped");
  }

  function openDetail(row) {
    const col = (title, cell, kind) => h("div", {},
      h("div", { class: "row", style: { marginBottom: "8px" } }, h("b", { text: title }), verdict(cell, kind, title.includes("off") ? "off" : "on")),
      h("div", { class: "answerbox " + (cell.succeeded ? "leak" : "safe") }, cell.answer || "(no answer)"),
      cell.stopped_by.length ? h("div", { class: "row tight", style: { marginTop: "8px" } }, h("span", { class: "small muted", text: "Stopped by:" }), cell.stopped_by.map((l) => AL.chip(l, "ok"))) : null,
      h("div", { class: "trace", style: { marginTop: "14px" } }, cell.trace.map((s) => AL.stepNode({ ...s, ok: true }))));
    AL.modal(row.name, h("div", {},
      h("p", { text: row.explain }),
      h("div", { class: "row tight", style: { marginBottom: "10px" } }, AL.chip(row.kind, "info"), h("span", { class: "small muted", text: `Counts as success if: ${row.success_if}` })),
      h("div", { class: "answerbox", style: { fontFamily: "var(--mono)", fontSize: "13px", marginBottom: "16px" } }, row.prompt),
      h("div", { class: "compare" }, col("Guardrails off", row.off, row.kind), col("Guardrails on", row.on, row.kind))));
  }

  function table(r) {
    const body = h("tbody", {});
    r.rows.forEach((row) => body.append(h("tr", { class: "click", tabindex: 0, onclick: () => openDetail(row), onkeydown: (e) => e.key === "Enter" && openDetail(row) },
      h("td", {}, h("b", { text: row.name }), h("div", { class: "small muted", text: row.kind })),
      h("td", {}, verdict(row.off, row.kind, "off")), h("td", {}, verdict(row.on, row.kind, "on")),
      h("td", {}, h("div", { class: "row tight" }, row.on.stopped_by.length ? row.on.stopped_by.map((l) => AL.chip(l)) : h("span", { class: "muted small", text: row.kind === "control" ? "not needed" : "none" }))))));
    return h("div", { class: "panel" }, h("div", { class: "panel-h" }, h("h2", { text: "Results" }), h("span", { class: "sub", text: "Click a row to see both runs step by step" })),
      h("div", { class: "panel-b" }, h("div", { class: "tbl-wrap" }, h("table", { class: "data" },
        h("thead", {}, h("tr", {}, ["Attack", "Guardrails off", "Guardrails on", "Stopped by"].map((t) => h("th", { text: t })))), body))));
  }

  function planned() {
    return h("div", { class: "panel" }, h("div", { class: "panel-h" }, h("h2", { text: "What will be tried" })),
      h("div", { class: "panel-b" }, S.attacks.map((a) => h("div", { class: "doc", style: { gridTemplateColumns: "1fr auto" } },
        h("div", {}, h("div", { class: "nm", text: a.name }), h("div", { class: "small muted", text: a.explain })), AL.chip(a.kind, a.kind === "control" ? "ok" : "info")))));
  }

  function draw() {
    root.innerHTML = "";
    const sim = AL.settings.model.startsWith("mock:");
    root.append(h("div", { class: "pad" }, h("div", { class: "stack" },
      h("div", { class: "panel" }, h("div", { class: "panel-b" },
        h("h2", { style: { fontFamily: "var(--serif)", fontWeight: 500, fontSize: "24px", marginBottom: "6px" }, text: "Try to break the agent." }),
        h("p", { style: { maxWidth: "720px", color: "var(--ink-2)" }, text: "Eight attacks run twice, once with every guardrail off and once with them on. An attack counts as a success only if a planted secret leaks, an external link is emitted, an email address is exposed, or destructive SQL actually runs. No AI judge is involved." }),
        AL.health && AL.health.readonly ? h("p", { class: "small", style: { color: "var(--warn)", maxWidth: "720px" }, text: "Public demo: attacks run against the offline simulator so no API quota is spent. Run locally with a real model to test it." }) : null,
        sim ? h("p", { class: "small", style: { color: "var(--warn)", maxWidth: "720px" }, text: "The offline simulator is deliberately gullible so the attacks are visible. Switch to a real model to find out how yours behaves." }) : null,
        h("div", { class: "row" }, h("button", { class: "btn primary", disabled: S.busy, onclick: run }, AL.icon("play"), S.result ? "Run again" : "Run all attacks"),
          h("div", { style: { width: "230px" } }, AL.modelSelect(AL.settings.model, (v) => { AL.settings.model = v; AL.saveSettings(); AL.updateStatus(); draw(); }))),
        h("div", { id: "rt-progress" }))),
      S.result ? [summary(S.result), table(S.result)] : planned())));
    drawProgress();
  }

  AL.views.redteam = {
    async mount(view) {
      const bar = $("#topbar");
      bar.innerHTML = "";
      bar.append(h("h1", {}, "Red team", h("span", { class: "sub", text: "Prompt injection, data leaks and tool abuse." })));
      view.innerHTML = "";
      root = view;
      draw();
      if (!S.attacks.length) { try { S.attacks = (await api.get("/api/redteam/attacks")).attacks; draw(); } catch (e) { AL.toast(e.message, true); } }
    },
  };
})();
