/* Evals view: score the agent on 29 test questions, compare setups, find failures. */
(function () {
  "use strict";
  const { h, $, api } = AL;

  const CHECK_LABELS = {
    contains_expected: "States the expected fact", matches_database: "Matches the database", cites_source: "Cites a source",
    used_right_tool: "Used the right tool", used_second_tool: "Used both tools", abstains: "Says it doesn't know", no_hallucinated_number: "Invents no numbers",
  };

  const S = (AL.state.evals = {
    cfg: { use_docs: true, chunk_size: null, retrieval: "hybrid", prompt_style: "zero-shot", top_k: 4 },
    runs: [], current: null, filter: "all", progress: null, busy: false, open: {},
  });
  let root;

  async function loadRuns(selectLast) {
    S.runs = (await api.get("/api/evals/runs")).runs;
    if (selectLast && S.runs.length) S.current = await api.get("/api/evals/runs/" + S.runs[S.runs.length - 1].id);
  }

  async function runOne(extra) {
    const config = { ...AL.settings, ...S.cfg, ...(extra || {}), general_knowledge: false, use_web: false };
    const job = await api.post("/api/evals/run", { config });
    const st = await AL.pollJob(job, (t) => { S.progress = { ...S.progress, done: t.done, total: t.total, note: t.note }; drawProgress(); });
    return st.result;
  }

  async function runEval() {
    if (S.busy) return;
    S.busy = true; S.progress = { done: 0, total: 29, note: "", prefix: "" }; draw();
    try { const r = await runOne(); await loadRuns(false); S.current = r; S.animateScore = true; AL.toast(`Accuracy ${AL.pct(r.accuracy)} (${r.passed} of ${r.total})`); }
    catch (e) { AL.toast(e.message, true); }
    S.busy = false; S.progress = null; draw();
  }

  async function runSweep() {
    if (S.busy) return;
    const steps = [["Documents off", { use_docs: false }], ["Baseline", {}], ["Small chunks (200)", { chunk_size: 200 }], ["Large chunks (1500)", { chunk_size: 1500 }], ["Keyword search only", { retrieval: "bm25" }], ["Embedding search only", { retrieval: "dense" }], ["Only 1 passage", { top_k: 1 }]];
    const real = !AL.settings.model.startsWith("mock:");
    if (real && !confirm(`This runs ${steps.length} full evaluations with ${AL.settings.model}. With a local model that can take a while. Continue?`)) return;
    S.busy = true; draw();
    try {
      let last = null;
      for (let i = 0; i < steps.length; i++) {
        S.progress = { done: 0, total: 29, note: "", prefix: `Run ${i + 1} of ${steps.length}: ${steps[i][0]}` }; drawProgress();
        last = await runOne({ use_docs: true, chunk_size: null, retrieval: "hybrid", top_k: 4, ...steps[i][1] });
      }
      await loadRuns(false); S.current = last; S.animateScore = true; AL.toast("Ablation sweep finished. Compare the bars below.");
    } catch (e) { AL.toast(e.message, true); }
    S.busy = false; S.progress = null; draw();
  }

  function drawProgress() {
    const box = $("#progress");
    if (!box || !S.progress) return;
    const p = S.progress;
    box.innerHTML = "";
    box.append(h("div", { class: "row", style: { justifyContent: "space-between", marginBottom: "6px" } },
      h("b", { text: p.prefix || "Running evaluation" }), h("span", { class: "small muted", text: `${p.done} of ${p.total} questions` })),
      h("div", { class: "progress" }, h("i", { style: { width: `${p.total ? (100 * p.done) / p.total : 0}%` } })),
      h("div", { class: "small muted", style: { marginTop: "6px", whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }, text: p.note }));
  }

  /* ----------------------------------------------------------------- config */
  function configPanel() {
    const sel = (label, key, opts) => h("div", { class: "field" }, h("label", { text: label }),
      h("select", { onchange: (e) => { const v = e.target.value; S.cfg[key] = key === "chunk_size" || key === "top_k" ? (v === "" ? null : parseInt(v, 10)) : v; } },
        opts.map(([v, l]) => h("option", { value: v, text: l, selected: String(S.cfg[key] ?? "") === String(v) }))));
    return h("div", { class: "panel" },
      h("div", { class: "panel-h" }, h("h2", { text: "Run a test" }), h("span", { class: "sub", text: "29 questions: documents, data, both, and unanswerable" })),
      h("div", { class: "panel-b stack" },
        h("div", { class: "grid cols-3", style: { gap: "14px" } },
          h("div", { class: "field" }, h("label", { text: "Model" }), AL.modelSelect(AL.settings.model, (v) => { AL.settings.model = v; AL.saveSettings(); AL.updateStatus(); })),
          sel("Prompt style", "prompt_style", [["zero-shot", "Zero-shot"], ["few-shot", "Few-shot"], ["cot", "Step by step"]]),
          sel("Retrieval", "retrieval", [["hybrid", "Hybrid"], ["dense", "Embeddings only"], ["bm25", "Keyword only"]]),
          sel("Chunk size", "chunk_size", [["", "Index default"], [200, "200 characters"], [400, "400"], [800, "800"], [1500, "1500"]]),
          sel("Passages (top-k)", "top_k", [[1, "1"], [2, "2"], [4, "4"], [6, "6"]]),
          h("div", { class: "field" }, h("label", { text: "Retrieval" }), AL.switchEl("Use documents", S.cfg.use_docs, (v) => { S.cfg.use_docs = v; }))),
        h("div", { class: "row" },
          h("button", { class: "btn primary", disabled: S.busy, onclick: runEval }, AL.icon("play"), "Run evaluation"),
          h("button", { class: "btn", disabled: S.busy, onclick: runSweep, title: "Runs 7 setups back to back so you can see what each choice is worth" }, "Run ablation sweep"),
          h("span", { class: "small muted", text: AL.settings.model.startsWith("mock:") ? "Using the offline simulator: fast, but a rule-based stand-in. Use a real model for real numbers." : "" })),
        AL.health && AL.health.readonly ? h("p", { class: "small", style: { color: "var(--warn)", margin: 0 }, text: "Public demo: batch runs use the offline simulator so no API quota is spent. Run AgentLens locally with Ollama for real-model results." }) : null,
        h("div", { id: "progress" })));
  }

  /* ---------------------------------------------------------------- history */
  function historyPanel() {
    const runs = S.runs.slice(-12);
    const body = h("div", { class: "panel-b" });
    if (!runs.length) body.append(h("div", { class: "empty", text: "No runs yet. Run an evaluation, or the ablation sweep to compare several setups." }));
    else {
      body.append(h("p", { class: "small muted", style: { marginTop: 0 }, text: "Accuracy of each run. Click a bar to open it." }));
      runs.forEach((r) => body.append(h("div", { class: "hbar", role: "button", tabindex: 0, "aria-current": String(S.current && S.current.id === r.id),
        onclick: async () => { S.current = await api.get("/api/evals/runs/" + r.id); S.animateScore = true; draw(); }, onkeydown: (e) => e.key === "Enter" && e.target.click() },
        h("span", { class: "lbl", title: r.label, text: r.label }), h("div", { class: "bar" }, h("i", { style: { width: `${r.accuracy}%` } })), h("b", { text: AL.pct(r.accuracy, 0) }))));
      body.append(h("div", { class: "row", style: { marginTop: "10px" } }, h("button", { class: "btn ghost sm danger", onclick: async () => {
        if (!S.current || !confirm("Delete the open run?")) return;
        S.runs = (await api.del("/api/evals/runs/" + S.current.id)).runs; S.current = null; draw();
      } }, AL.icon("trash"), "Delete open run")));
    }
    return h("div", { class: "panel" }, h("div", { class: "panel-h" }, h("h2", { text: "Run history" }), h("span", { class: "sub", text: "Which choice moved the score?" })), body);
  }

  /* ----------------------------------------------------------------- detail */
  function detail() {
    const r = S.current;
    if (!r) return h("div", { class: "panel" }, h("div", { class: "panel-b" }, h("div", { class: "empty", text: "Results appear here after a run." })));
    const cats = Object.values(r.by_category);
    const failed = r.results.filter((x) => !x.passed);
    const rows = r.results.filter((x) => S.filter === "all" || !x.passed);
    return h("div", { class: "panel" },
      h("div", { class: "panel-h" }, h("h2", { text: r.label }), h("span", { class: "sub", text: new Date(r.created).toLocaleString() })),
      h("div", { class: "panel-b stack" },
        h("div", { class: "score-hero" },
          h("div", {}, h("div", { class: "score-big" }, h("span", { class: "score-num", text: S.animateScore ? "0" : `${Math.round(r.accuracy)}` }), h("small", { text: "%" })), h("div", { class: "muted", text: `${r.passed} of ${r.total} questions passed` })),
          h("div", { class: "stat-row" },
            h("div", { class: "stat" }, h("b", { text: AL.num(r.avg_latency_ms) }), h("span", { text: "ms per question" })),
            h("div", { class: "stat" }, h("b", { text: AL.num(r.avg_tokens) }), h("span", { text: "tokens per question" })),
            h("div", { class: "stat" }, h("b", { text: failed.length }), h("span", { text: "failed" })))),
        h("div", {}, h("div", { class: "lab", style: { marginBottom: "4px" }, text: "By type of question" }),
          cats.map((c) => { const p = (100 * c.passed) / c.total; return h("div", { class: "catbar" }, h("span", { text: c.label }),
            h("div", { class: "bar" }, h("i", { class: p >= 85 ? "good" : p >= 60 ? "mid" : "low", style: { width: `${p}%` } })), h("span", { class: "small", text: `${c.passed} / ${c.total}` })); })),
        h("div", {}, h("div", { class: "lab", style: { marginBottom: "6px" }, text: "What was checked" }),
          h("div", { class: "row tight" }, Object.entries(r.checks).map(([k, v]) => AL.chip(`${CHECK_LABELS[k] || k}: ${v.passed}/${v.total}`, v.passed === v.total ? "ok" : "warn")))),
        h("div", { class: "row" }, h("b", { text: "Questions" }), AL.segEl([["all", `All ${r.total}`], ["failed", `Failed ${failed.length}`]], S.filter, (v) => { S.filter = v; draw(); })),
        rows.length ? casesTable(rows) : h("div", { class: "empty", text: "Nothing failed in this run." })));
  }

  function casesTable(rows) {
    const body = h("tbody", {});
    rows.forEach((x) => {
      const open = S.open[x.id];
      body.append(h("tr", { class: "click", onclick: () => { S.open[x.id] = !S.open[x.id]; draw(); } },
        h("td", {}, h("span", { class: "mark " + (x.passed ? "ok" : "bad"), text: x.passed ? "\u2713" : "\u2715", "aria-label": x.passed ? "passed" : "failed" })),
        h("td", { text: x.question }), h("td", {}, AL.chip(r2label(x.category))), h("td", { class: "num", text: AL.num(x.tokens) }), h("td", { class: "num", text: x.latency_ms })));
      if (open) body.append(h("tr", {}, h("td", {}), h("td", { colspan: 4 },
        h("div", { class: "answerbox " + (x.passed ? "safe" : "leak") }, x.answer),
        h("div", { class: "row tight", style: { marginTop: "8px" } }, Object.entries(x.checks).map(([k, v]) => AL.chip(CHECK_LABELS[k] || k, v ? "ok" : "bad"))),
        x.gold ? h("div", { class: "small muted", style: { marginTop: "6px" }, text: `Database says: ${x.gold.join(", ")}` }) : null)));
    });
    return h("div", { class: "tbl-wrap", style: { maxHeight: "560px" } }, h("table", { class: "data" },
      h("thead", {}, h("tr", {}, h("th", { style: { width: "34px" } }), h("th", { text: "Question" }), h("th", { text: "Type" }), h("th", { class: "num", text: "Tokens" }), h("th", { class: "num", text: "ms" }))), body));
  }
  const r2label = (c) => ({ doc_qa: "documents", sql: "data", multi: "both", unanswerable: "unanswerable" }[c] || c);

  function draw() {
    root.innerHTML = "";
    root.append(h("div", { class: "pad" }, h("div", { class: "grid evals-grid", style: { alignItems: "start" } },
      h("div", { class: "stack" }, configPanel(), detail()), historyPanel())));
    drawProgress();
    if (S.animateScore && S.current) { const n = $(".score-num"); if (n) AL.countUp(n, S.current.accuracy, { decimals: 0, duration: 900 }); S.animateScore = false; }
  }

  AL.views.evals = {
    async mount(view) {
      const bar = $("#topbar");
      bar.innerHTML = "";
      bar.append(h("h1", {}, "Evals", h("span", { class: "sub", text: "Measure, then improve." })));
      view.innerHTML = "";
      root = view;
      draw();
      try { await loadRuns(!S.current); } catch (e) { AL.toast(e.message, true); }
      draw();
    },
  };
})();
