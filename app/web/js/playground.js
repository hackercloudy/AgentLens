/* Playground: see tokens, feel temperature/top-p, and sample the model directly (no agent, no retrieval). */
(function () {
  "use strict";
  const { h, $, api } = AL;

  const SAMPLES = [
    ["English", "The recommended dose of Zentrovax depends on body weight, not age."],
    ["Hindi", "ज़ेंट्रोवैक्स की खुराक उम्र नहीं, बल्कि वज़न पर निर्भर करती है।"],
    ["Code", "SELECT region, SUM(revenue) FROM sales WHERE year = 2026 GROUP BY region;"],
    ["Numbers", "Order 2026-09-14: 1,250 units at 42.50 with a 12.5% discount."],
  ];
  const WORDS = [["food", 4.0], ["water", 3.3], ["milk", 2.2], ["juice", 1.5], ["coffee", 0.9], ["nothing", 0.2], ["alcohol", -0.6], ["gravel", -2.0]];

  const S = (AL.state.playground = {
    text: SAMPLES[0][1], window: 8192, tokens: null,
    temp: 0.8, topp: 0.9,
    prompt: "The suspension must be", system: "", gentemp: 0.8, gentopp: 0.95, n: 3, outputs: null, busy: false, simulated: false,
  });
  let root;

  /* ------------------------------------------------------------- tokenizer */
  async function tokenize() {
    try { S.tokens = await api.post("/api/tokenize", { text: S.text }); } catch (e) { AL.toast(e.message, true); }
    const out = $("#tok-out");
    if (!out || !S.tokens) return;
    out.innerHTML = "";
    S.tokens.tokens.forEach((t) => out.append(t.trim() === "" ? document.createTextNode(t.includes("\n") ? "\n" : "") : h("span", { class: "tok", text: t })));
    $("#tok-count").textContent = AL.num(S.tokens.count);
    $("#tok-ratio").textContent = S.tokens.count ? (S.tokens.chars / S.tokens.count).toFixed(1) : "0";
    const pct = Math.min(100, (100 * S.tokens.count) / S.window);
    $("#tok-fill").style.width = pct + "%";
    $("#tok-pct").textContent = pct < 0.1 ? "<0.1%" : pct.toFixed(1) + "%";
  }
  const tokenizeSoon = AL.debounce(tokenize, 180);

  function tokenizerPanel() {
    const ta = h("textarea", { rows: 3, "aria-label": "Text to tokenize", oninput: (e) => { S.text = e.target.value; tokenizeSoon(); } });
    ta.value = S.text;
    return h("div", { class: "panel" },
      h("div", { class: "panel-h" }, h("h2", { text: "Tokens and the context window" })),
      h("div", { class: "panel-b stack" },
        h("p", { class: "small muted", style: { margin: 0 }, text: "Models read tokens, not words. The context window is the most tokens a model can handle at once, prompt and answer together. This view uses an approximate tokenizer, so exact counts differ by model." }),
        h("div", { class: "row tight" }, h("span", { class: "small muted", text: "Try:" }), SAMPLES.map(([n, t]) => h("button", { class: "btn sm", onclick: () => { S.text = t; ta.value = t; tokenize(); } }, n))),
        ta,
        h("div", { class: "tok-out", id: "tok-out", "aria-live": "polite" }),
        h("div", { class: "stat-row" },
          h("div", { class: "stat" }, h("b", { id: "tok-count", text: "0" }), h("span", { text: "tokens (approx.)" })),
          h("div", { class: "stat" }, h("b", { id: "tok-ratio", text: "0" }), h("span", { text: "characters per token" }))),
        h("div", { class: "field" },
          h("div", { class: "row", style: { justifyContent: "space-between" } }, h("label", { text: "Share of the context window used" }),
            h("select", { style: { width: "150px" }, "aria-label": "Context window size", onchange: (e) => { S.window = parseInt(e.target.value, 10); tokenize(); } },
              [4096, 8192, 32768, 131072].map((v) => h("option", { value: v, text: `${AL.num(v)} tokens`, selected: S.window === v })))),
          h("div", { class: "progress" }, h("i", { id: "tok-fill", style: { width: "0%" } })),
          h("div", { class: "hint", id: "tok-pct" })),
        h("p", { class: "small muted", style: { margin: 0 }, text: "Notice how the Hindi sentence needs far more tokens than the English one. Non-English text costs more context and more money on most models." })));
  }

  /* ----------------------------------------------------------- sampling lab */
  function distribution() {
    const T = Math.max(0.05, S.temp);
    const exps = WORDS.map(([w, l]) => [w, Math.exp(l / T)]);
    const Z = exps.reduce((a, [, e]) => a + e, 0);
    const probs = exps.map(([w, e]) => [w, e / Z]).sort((a, b) => b[1] - a[1]);
    let cum = 0; const kept = new Set();
    for (const [w, p] of probs) { kept.add(w); cum += p; if (cum >= S.topp) break; }
    return { probs, kept, keptMass: probs.filter(([w]) => kept.has(w)).reduce((a, [, p]) => a + p, 0) };
  }

  function drawDist() {
    const box = $("#dist");
    if (!box) return;
    const { probs, kept, keptMass } = distribution();
    box.innerHTML = "";
    probs.forEach(([w, p]) => {
      const isKept = kept.has(w);
      box.append(h("div", { class: isKept ? "" : "cut", style: { display: "contents" } },
        h("span", { class: "w", text: w }), h("div", { class: "bar" }, h("i", { style: { width: `${p * 100}%` } })), h("span", { class: "p", text: `${(p * 100).toFixed(1)}%` })));
    });
    $("#dist-note").textContent = `${kept.size} of ${probs.length} words stay in play (${(keptMass * 100).toFixed(0)}% of the probability). The model picks one of those at random, in proportion to its probability.`;
  }

  function samplingPanel() {
    const panel = h("div", { class: "panel" },
      h("div", { class: "panel-h" }, h("h2", { text: "Sampling lab" }), h("span", { class: "sub", text: "Temperature and top-p on a made-up next word" })),
      h("div", { class: "panel-b stack" },
        h("p", { class: "small muted", style: { margin: 0 }, text: "After \"The patient should take the medicine with\" a model scores every possible next word. Temperature reshapes those scores. Top-p throws away the unlikely tail. These scores are illustrative, not from a real model." }),
        AL.rangeEl({ label: "Temperature", min: 0.1, max: 2, step: 0.05, value: S.temp, onInput: (v) => { S.temp = v; drawDist(); }, format: (v) => v.toFixed(2), hint: "Low sharpens the favourite. High flattens the odds." }),
        AL.rangeEl({ label: "Top-p", min: 0.1, max: 1, step: 0.05, value: S.topp, onInput: (v) => { S.topp = v; drawDist(); }, format: (v) => v.toFixed(2) }),
        h("div", { class: "dist", id: "dist" }),
        h("p", { class: "small", id: "dist-note", style: { margin: 0 } })));
    setTimeout(drawDist, 0);
    return panel;
  }

  /* -------------------------------------------------------------- generation */
  async function generate() {
    S.busy = true; S.outputs = null; drawGen();
    try {
      const r = await api.post("/api/playground", { prompt: S.prompt, system: S.system, model: AL.settings.model, temperature: S.gentemp, top_p: S.gentopp, max_tokens: 120, n: S.n });
      S.outputs = r.outputs; S.simulated = r.simulated;
    } catch (e) { AL.toast(e.message, true); }
    S.busy = false; drawGen();
  }

  function drawGen() {
    const box = $("#gen-out");
    if (!box) return;
    box.innerHTML = "";
    if (S.busy) return box.append(h("div", { class: "thinking" }, h("span", { class: "beam-spin" }), h("span", { text: "Generating..." })));
    if (!S.outputs) return box.append(h("div", { class: "empty", text: "Run the model a few times and compare the samples." }));
    if (S.simulated) box.append(h("p", { class: "small", style: { color: "var(--warn)" }, text: "The offline simulator is a toy word-pair model trained on the demo documents. It makes the effect of temperature visible but its text is not meaningful. Pick an Ollama model for real output." }));
    S.outputs.forEach((o, i) => box.append(h("div", { class: "sample" }, h("div", { class: "small muted", style: { marginBottom: "4px" }, text: `Sample ${i + 1}, ${o.completion_tokens} tokens, ${o.latency_ms} ms` }), o.text)));
    if (S.gentemp === 0) box.append(h("p", { class: "small muted", text: "Temperature 0 is greedy decoding: the same prompt gives the same answer, so only one sample is shown." }));
  }

  function generatePanel() {
    return h("div", { class: "panel" },
      h("div", { class: "panel-h" }, h("h2", { text: "Talk to the model directly" }), h("span", { class: "sub", text: "No agent, no retrieval" })),
      h("div", { class: "panel-b stack" },
        h("div", { class: "field" }, h("label", { text: "Model" }), AL.modelSelect(AL.settings.model, (v) => { AL.settings.model = v; AL.saveSettings(); AL.updateStatus(); })),
        h("div", { class: "field" }, h("label", { text: "System prompt (optional)" }), h("textarea", { rows: 2, placeholder: "You are a concise pharmacy assistant.", oninput: (e) => { S.system = e.target.value; } }, S.system)),
        h("div", { class: "field" }, h("label", { text: "Prompt" }), h("textarea", { rows: 3, oninput: (e) => { S.prompt = e.target.value; } }, S.prompt)),
        h("div", { class: "grid cols-2", style: { gap: "14px" } },
          AL.rangeEl({ label: "Temperature", min: 0, max: 1.5, step: 0.05, value: S.gentemp, onInput: (v) => { S.gentemp = v; }, format: (v) => v.toFixed(2) }),
          AL.rangeEl({ label: "Top-p", min: 0.1, max: 1, step: 0.05, value: S.gentopp, onInput: (v) => { S.gentopp = v; }, format: (v) => v.toFixed(2) })),
        h("div", { class: "row" }, h("button", { class: "btn primary", onclick: generate }, AL.icon("play"), "Generate 3 samples")),
        h("div", { id: "gen-out" })));
  }

  function draw() {
    root.innerHTML = "";
    root.append(h("div", { class: "pad" }, h("div", { class: "grid cols-2", style: { alignItems: "start" } },
      h("div", { class: "stack" }, tokenizerPanel(), samplingPanel()), generatePanel())));
    tokenize(); drawGen();
  }

  AL.views.playground = {
    mount(view) {
      const bar = $("#topbar");
      bar.innerHTML = "";
      bar.append(h("h1", {}, "Playground", h("span", { class: "sub", text: "Tokens, sampling and raw model output." })));
      view.innerHTML = "";
      root = view;
      draw();
    },
  };
})();
