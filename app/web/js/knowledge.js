/* Knowledge view: manage documents, tune chunking, test retrieval, explore the embedding map and the SQL data. */
(function () {
  "use strict";
  const { h, $, api } = AL;

  const DARK_PALETTE = ["#34d399", "#fbbf24", "#38bdf8", "#fb7185", "#a78bfa", "#f472b6", "#facc15", "#2dd4bf"];
  const PALETTE = ["#164766", "#f0ae2e", "#148468", "#bf3a4d", "#7a5bc0", "#2f8fc4", "#a26b1a", "#4f6b3c"];
  const S = (AL.state.knowledge = {
    tab: "docs", kb: null, expanded: {}, query: "What should be done if a dose is missed?", mode: "hybrid", k: 4, hits: null,
    chunkSize: null, overlap: null, embedder: null, map: null, schema: null, busy: false,
  });
  let root;

  async function load() {
    S.kb = await api.get("/api/knowledge");
    if (S.chunkSize === null) { S.chunkSize = S.kb.index.chunk_size; S.overlap = S.kb.index.overlap; S.embedder = S.kb.index.embedder; }
    S.map = await api.get("/api/knowledge/map?query=" + encodeURIComponent(S.hits ? S.query : ""));
  }

  async function refresh() {
    try { await load(); } catch (e) { AL.toast(e.message, true); }
    draw();
  }

  /* ------------------------------------------------------------- documents */
  function docRow(d) {
    const row = h("div", {});
    row.append(h("div", { class: "doc" },
      h("div", {}, h("div", { class: "nm", text: d.name }), h("div", { class: "small muted", text: `${(d.bytes / 1024).toFixed(1)} KB` })),
      AL.chip(d.source === "demo" ? "demo" : "uploaded", d.source === "demo" ? "line" : "info"),
      h("button", { class: "btn sm", onclick: async () => {
        if (S.expanded[d.name]) delete S.expanded[d.name];
        else S.expanded[d.name] = (await api.get("/api/knowledge/chunks?doc=" + encodeURIComponent(d.name))).chunks;
        draw();
      } }, `${d.chunks} chunks`),
      h("button", { class: "btn ghost sm danger", title: `Remove ${d.name}`, "aria-label": `Remove ${d.name}`, onclick: async () => {
        if (!confirm(`Remove ${d.name} from the knowledge base?`)) return;
        try { await api.del("/api/knowledge/docs/" + encodeURIComponent(d.name)); delete S.expanded[d.name]; S.hits = null; await refresh(); AL.toast("Removed " + d.name); } catch (e) { AL.toast(e.message, true); }
      } }, AL.icon("trash"))));
    if (S.expanded[d.name]) {
      row.append(h("div", { class: "doc-chunks" }, S.expanded[d.name].map((c, i) =>
        h("div", { class: "ck" }, h("div", { class: "small muted", style: { marginBottom: "3px" }, text: `Chunk ${i + 1}${c.heading ? " under \"" + c.heading + "\"" : ""}, about ${c.tokens} tokens` }), c.text.length > 320 ? c.text.slice(0, 320) + "..." : c.text))));
    }
    return row;
  }

  async function upload(files) {
    if (!files.length) return;
    const form = new FormData();
    for (const f of files) form.append("files", f);
    try {
      AL.toast(`Uploading ${files.length} file${files.length > 1 ? "s" : ""}...`);
      await api.upload("/api/knowledge/upload", form);
      S.hits = null; await refresh(); AL.toast("Added. The index was rebuilt.");
    } catch (e) { AL.toast(e.message, true); }
  }

  function docsPanel() {
    const input = h("input", { type: "file", multiple: true, accept: ".pdf,.txt,.md", class: "sr", onchange: (e) => upload(e.target.files) });
    const drop = h("div", { class: "drop",
      ondragover: (e) => { e.preventDefault(); drop.classList.add("over"); }, ondragleave: () => drop.classList.remove("over"),
      ondrop: (e) => { e.preventDefault(); drop.classList.remove("over"); upload(e.dataTransfer.files); } },
      h("div", { text: "Drop PDF, TXT or Markdown files here" }),
      h("div", { style: { marginTop: "8px" } }, h("button", { class: "btn", onclick: () => input.click() }, AL.icon("plus"), "Choose files"), input));
    return h("div", { class: "panel" },
      h("div", { class: "panel-h" }, h("h2", { text: "Documents" }), h("span", { class: "sub", text: `${S.kb.docs.length} files, ${S.kb.index.chunks} chunks` }),
        h("button", { class: "btn ghost sm", style: { marginLeft: "auto" }, onclick: async () => { await api.post("/api/knowledge/reset-demo"); S.hits = null; S.expanded = {}; await refresh(); AL.toast("Demo documents restored."); } }, AL.icon("refresh"), "Restore demo set")),
      h("div", { class: "panel-b" }, drop, h("div", { style: { marginTop: "10px" } }, S.kb.docs.length ? S.kb.docs.map(docRow) : h("div", { class: "empty", text: "No documents yet. Add some to enable retrieval." }))));
  }

  function indexPanel() {
    const dirty = S.chunkSize !== S.kb.index.chunk_size || S.overlap !== S.kb.index.overlap || S.embedder !== S.kb.index.embedder;
    return h("div", { class: "panel" },
      h("div", { class: "panel-h" }, h("h2", { text: "Chunking and embeddings" })),
      h("div", { class: "panel-b stack" },
        h("p", { class: "small muted", style: { margin: 0 }, text: "Documents are cut into chunks, each turned into a vector. Smaller chunks are precise but lose context. Larger chunks keep context but dilute the match." }),
        AL.rangeEl({ label: "Chunk size (characters)", min: 150, max: 1500, step: 50, value: S.chunkSize, onInput: (v) => { S.chunkSize = v; markDirty(); }, format: AL.num }),
        AL.rangeEl({ label: "Overlap (characters)", min: 0, max: 300, step: 10, value: S.overlap, onInput: (v) => { S.overlap = v; markDirty(); }, format: AL.num }),
        h("div", { class: "field" }, h("label", { text: "Embedding model" }),
          h("select", { onchange: (e) => { S.embedder = e.target.value; markDirty(); } },
            [["hash", "Offline hashing (no download)"], ["ollama:nomic-embed-text", "Ollama: nomic-embed-text"], ["ollama:bge-m3", "Ollama: bge-m3 (multilingual)"]].map(([v, l]) => h("option", { value: v, text: l, selected: S.embedder === v }))),
          h("div", { class: "hint", text: "Neural embeddings need the model pulled in Ollama first, for example: ollama pull nomic-embed-text" })),
        h("div", { class: "row" },
          h("button", { class: "btn primary", id: "rebuild", disabled: S.busy, onclick: rebuild }, AL.icon("refresh"), "Rebuild index"),
          h("span", { class: "small muted", id: "dirty-note", text: dirty ? "Settings changed. Rebuild to apply." : "" })),
        h("div", { class: "stat-row", style: { marginTop: "6px" } },
          h("div", { class: "stat" }, h("b", { text: S.kb.index.chunks }), h("span", { text: "chunks" })),
          h("div", { class: "stat" }, h("b", { text: S.kb.avg_chunk_tokens }), h("span", { text: "avg tokens per chunk" })),
          h("div", { class: "stat" }, h("b", { text: S.kb.index.embedder === "hash" ? "offline" : "neural" }), h("span", { text: "embedding type" })))));
  }
  function markDirty() { const n = $("#dirty-note"); if (n) n.textContent = "Settings changed. Rebuild to apply."; }

  async function rebuild() {
    S.busy = true; draw();
    try { await api.post("/api/knowledge/reindex", { chunk_size: S.chunkSize, overlap: S.overlap, embedder: S.embedder }); S.expanded = {}; S.hits = null; await load(); AL.toast("Index rebuilt."); }
    catch (e) { AL.toast(e.message, true); S.embedder = S.kb.index.embedder; }
    S.busy = false; draw();
  }

  /* --------------------------------------------------------- retrieval lab */
  async function search() {
    if (!S.query.trim()) return;
    try {
      const r = await api.post("/api/knowledge/search", { query: S.query, k: S.k, mode: S.mode });
      S.hits = r;
      S.map = await api.get("/api/knowledge/map?query=" + encodeURIComponent(S.query));
    } catch (e) { AL.toast(e.message, true); }
    draw();
  }

  function highlight(text, query) {
    const terms = [...new Set(query.toLowerCase().split(/\W+/).filter((w) => w.length > 3))];
    const frag = document.createDocumentFragment();
    if (!terms.length) { frag.append(text); return frag; }
    const rx = new RegExp(`(${terms.map((t) => t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|")})`, "ig");
    text.split(rx).forEach((part, i) => frag.append(i % 2 ? h("mark", { text: part }) : part));
    return frag;
  }

  function labPanel() {
    const q = h("input", { type: "search", value: S.query, "aria-label": "Search query", onkeydown: (e) => e.key === "Enter" && search(), oninput: (e) => { S.query = e.target.value; } });
    const out = h("div", { style: { marginTop: "8px" } });
    if (S.hits) {
      out.append(h("div", { class: "small muted", text: `${S.hits.hits.length} of ${S.hits.total_chunks} chunks shown, ${S.hits.ms} ms` }));
      S.hits.hits.forEach((x) => out.append(h("div", { class: "hit" },
        h("div", { class: "hit-h" }, h("span", { class: "rank", text: x.rank }), h("b", { text: x.doc }), x.heading ? h("span", { class: "muted small", text: x.heading }) : null,
          h("span", { style: { marginLeft: "auto" } }), AL.chip(`score ${x.score.toFixed(2)}`, x.score >= 0.12 ? "ok" : "warn")),
        h("div", { class: "scores" }, h("span", { text: "embedding" }), h("div", { class: "bar" }, h("i", { style: { width: `${Math.max(0, Math.min(100, x.dense * 100))}%` } })),
          h("span", { text: "keyword" }), h("div", { class: "bar beam" }, h("i", { style: { width: `${Math.min(100, (x.bm25 / 12) * 100)}%` } }))),
        h("p", {}, highlight(x.text.length > 360 ? x.text.slice(0, 360) + "..." : x.text, S.query)))));
    } else out.append(h("div", { class: "empty", text: "Search to see which chunks the agent would retrieve, and why." }));
    return h("div", { class: "panel" },
      h("div", { class: "panel-h" }, h("h2", { text: "Retrieval lab" }), h("span", { class: "sub", text: "Test search without the model" })),
      h("div", { class: "panel-b" },
        h("div", { class: "row" }, h("div", { style: { flex: "1 1 260px" } }, q), h("button", { class: "btn primary", onclick: search }, "Search")),
        h("div", { class: "row", style: { marginTop: "10px" } }, AL.segEl([["hybrid", "Hybrid"], ["dense", "Embeddings"], ["bm25", "Keyword"]], S.mode, (v) => { S.mode = v; }),
          h("label", { class: "small muted", for: "kk", text: "Top-k" }), h("select", { id: "kk", style: { width: "72px" }, onchange: (e) => { S.k = parseInt(e.target.value, 10); } }, [1, 2, 3, 4, 5, 6, 8, 10].map((n) => h("option", { value: n, text: n, selected: S.k === n })))),
        out));
  }

  /* ----------------------------------------------------------- embedding map */
  function mapPanel() {
    const canvas = h("canvas", { class: "map", role: "img", "aria-label": "Two-dimensional map of document chunks" });
    const tip = h("div", { class: "small", style: { minHeight: "20px", marginTop: "6px", color: "var(--ink-2)" }, text: "Hover a point to read the chunk." });
    const docs = [...new Set((S.map?.points || []).map((p) => p.doc))];
    const pal = document.documentElement.dataset.mode === "dark" ? DARK_PALETTE : PALETTE;
    const color = (d) => pal[docs.indexOf(d) % pal.length];
    setTimeout(() => paintMap(canvas, tip, color), 0);
    return h("div", { class: "panel" },
      h("div", { class: "panel-h" }, h("h2", { text: "Embedding map" }), h("span", { class: "sub", text: "Every chunk, flattened to 2D (PCA)" })),
      h("div", { class: "panel-b" }, canvas, tip,
        h("div", { class: "row tight", style: { marginTop: "8px" } }, docs.map((d) => h("span", { class: "chip line" }, h("i", { style: { width: "9px", height: "9px", borderRadius: "50%", background: color(d), display: "inline-block" } }), d.replace(/\.(md|txt|pdf)$/, "")))),
        S.map && S.map.query ? h("p", { class: "small muted", style: { marginTop: "8px", marginBottom: 0 }, text: "The gold star is your query. Chunks that land near it are the likely matches." }) : null));
  }

  function paintMap(canvas, tip, color) {
    const pts = S.map?.points || [];
    const dpr = window.devicePixelRatio || 1;
    const w = canvas.clientWidth, hgt = canvas.clientHeight;
    if (!w) return;
    canvas.width = w * dpr; canvas.height = hgt * dpr;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.scale(dpr, dpr);
    const pad = 28;
    const X = (x) => pad + ((x + 1.1) / 2.2) * (w - 2 * pad);
    const Y = (y) => pad + ((1.1 - y) / 2.2) * (hgt - 2 * pad);
    const hitIds = new Set((S.hits?.hits || []).map((x) => x.id));
    const css = getComputedStyle(document.documentElement);
    const gridCol = css.getPropertyValue("--line-2").trim() || "#e4ecf1", inkCol = css.getPropertyValue("--ink").trim() || "#0d2b40";
    const frame = (k) => {
      ctx.clearRect(0, 0, w, hgt);
      ctx.strokeStyle = gridCol; ctx.lineWidth = 1;
      for (let i = 0; i <= 10; i++) {
        ctx.beginPath(); ctx.moveTo(pad + (i * (w - 2 * pad)) / 10, pad); ctx.lineTo(pad + (i * (w - 2 * pad)) / 10, hgt - pad); ctx.stroke();
        ctx.beginPath(); ctx.moveTo(pad, pad + (i * (hgt - 2 * pad)) / 10); ctx.lineTo(w - pad, pad + (i * (hgt - 2 * pad)) / 10); ctx.stroke();
      }
      pts.forEach((p) => {
        const hit = hitIds.has(p.id);
        ctx.beginPath(); ctx.arc(X(p.x * k), Y(p.y * k), (hit ? 8 : 5.5) * (0.3 + 0.7 * k), 0, Math.PI * 2);
        ctx.fillStyle = color(p.doc); ctx.globalAlpha = (hitIds.size && !hit ? 0.35 : 0.9) * k; ctx.fill(); ctx.globalAlpha = 1;
        if (hit) { ctx.lineWidth = 2; ctx.strokeStyle = inkCol; ctx.stroke(); }
      });
      if (S.map?.query) {
        const qx = X(Math.max(-1.1, Math.min(1.1, S.map.query.x))), qy = Y(Math.max(-1.1, Math.min(1.1, S.map.query.y)));
        const grow = 0.4 + 0.6 * k;
        ctx.fillStyle = (css.getPropertyValue("--beam").trim() || "#f0ae2e"); ctx.strokeStyle = inkCol; ctx.lineWidth = 1.5; ctx.beginPath();
        for (let i = 0; i < 10; i++) { const r = (i % 2 ? 6 : 14) * grow, a = (Math.PI / 5) * i - Math.PI / 2 + (1 - k) * 1.2; ctx.lineTo(qx + r * Math.cos(a), qy + r * Math.sin(a)); }
        ctx.closePath(); ctx.fill(); ctx.stroke();
      }
    };
    const reduce = window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (S._animated === S.map || reduce) frame(1);
    else {
      S._animated = S.map;
      const t0 = performance.now();
      const tick = (t) => { const k = Math.min(1, (t - t0) / 650), e = 1 - Math.pow(1 - k, 3); frame(e); if (k < 1 && canvas.isConnected) requestAnimationFrame(tick); };
      requestAnimationFrame(tick);
    }
    canvas.onmousemove = (e) => {
      const r = canvas.getBoundingClientRect(); const mx = e.clientX - r.left, my = e.clientY - r.top;
      let best = null, bd = 12;
      pts.forEach((p) => { const d = Math.hypot(X(p.x) - mx, Y(p.y) - my); if (d < bd) { bd = d; best = p; } });
      tip.textContent = best ? `${best.doc}${best.heading ? " / " + best.heading : ""}: ${best.text}...` : "Hover a point to read the chunk.";
    };
  }

  /* ------------------------------------------------------------------ data */
  function dataTab() {
    const wrap = h("div", { class: "panel" }, h("div", { class: "panel-h" }, h("h2", { text: "Sales database" }), h("span", { class: "sub", text: "Synthetic data the SQL tool can query. Read-only." })));
    const body = h("div", { class: "panel-b" });
    if (!S.schema) { body.append(h("div", { class: "empty", text: "Loading schema..." })); api.get("/api/data/schema").then((s) => { S.schema = s; draw(); }); }
    else {
      S.schema.tables.forEach((t) => {
        const head = h("tr", {}, t.columns.map((c) => h("th", { text: c })));
        const rows = t.rows.map((r) => h("tr", {}, r.map((v) => h("td", { text: v === null ? "" : String(v) }))));
        body.append(h("div", { class: "schema-t" },
          h("h4", {}, t.name, AL.chip(t.kind, "line"), h("span", { class: "small muted", text: `${AL.num(t.count)} rows` })),
          h("div", { class: "tbl-wrap" }, h("table", { class: "mini-table" }, h("thead", {}, head), h("tbody", {}, rows)))));
      });
    }
    wrap.append(body);
    return wrap;
  }

  /* ------------------------------------------------------------------ draw */
  function draw() {
    if (!root) return;
    root.innerHTML = "";
    if (!S.kb) return root.append(h("div", { class: "pad" }, h("div", { class: "empty", text: "Loading..." })));
    const tabs = h("div", { class: "tabs", style: { marginBottom: "18px", padding: 0 } },
      [["docs", "Documents and retrieval"], ["data", "SQL data"]].map(([id, l]) => h("button", { role: "tab", "aria-selected": String(S.tab === id), onclick: () => { S.tab = id; draw(); } }, l)));
    const content = S.tab === "docs"
      ? h("div", { class: "grid cols-2", style: { alignItems: "start" } }, h("div", { class: "stack" }, docsPanel(), indexPanel()), h("div", { class: "stack" }, labPanel(), mapPanel()))
      : dataTab();
    root.append(h("div", { class: "pad" }, tabs, content));
  }

  AL.views.knowledge = {
    mount(view) {
      const bar = $("#topbar");
      bar.innerHTML = "";
      bar.append(h("h1", {}, "Knowledge", h("span", { class: "sub", text: "What the agent can read, and how it finds it." })));
      view.innerHTML = "";
      root = view;
      draw();
      refresh();
    },
  };
})();
