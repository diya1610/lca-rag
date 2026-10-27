"use strict";
// eBOM dashboard: upload, KPIs, radar, Pareto, component view, recommendations,
// what-if simulator, sortable item table. Uses $, h, api, fmt, option, openRecord and
// state from app.js. All data is inserted as text.

const TIER_LABEL = { evidence: "Exact record", proxy: "Similar material", provisional: "AI estimate" };
const STATUS_LABEL = { evidence: "Exact record", proxy: "Similar material", provisional: "AI estimate",
  not_covered: "Not covered", invalid: "Invalid line" };
const LINE_REASONS = {
  ...REASONS,
  no_candidates: "No record with this material and this exact reference unit.",
  missing_unit: "No unit in the file and no default unit chosen; quantities are never assumed to be kg.",
  invalid_quantity: "Quantity is missing or not a positive number.",
  ambiguous_quantity: "Quantity like 1,500 could mean 1.5 or 1500; write it without the comma.",
  missing_material: "Material name is empty.",
  exact_factor_implausible: "The exact match's factor is implausible for its unit, so it is not used.",
  no_plausible_family_member: "No plausible material of the same family in the database.",
};
const eb = { text: null, name: null, result: null, sort: { key: "rank", dir: 1 }, scenarios: [] };

function errorBanner(title, msg) {
  return h("div", { class: "banner error" }, h("div", {}, h("h3", {}, title), h("p", {}, msg)));
}
const pct = (x, d = 0) => (x == null ? "—" : `${(x * 100).toFixed(d)}%`);
const gradeBadge = (g, big) => g
  ? h("span", { class: `grade g-${g}${big ? " big" : ""}`, title: `Grade ${g}` }, g)
  : h("span", { class: "muted" }, "—");

// ---------------------------------------------------------------- setup

function fillEbomIndicators() {
  const src = $("#e-source").value;
  const labels = src ? state.meta.indicators[src] : [...new Set(Object.values(state.meta.indicators).flat())];
  const sel = $("#e-indicator");
  const prev = sel.value || "Climate change";
  sel.replaceChildren(...labels.map((l) => option(l)));
  if (labels.includes(prev)) sel.value = prev;
}

function initEbom() {
  const m = state.meta;
  $("#e-source").replaceChildren(...Object.keys(m.sources).filter((s) => m.indicators[s].length).map((s) => option(s)),
    option("", "All sources with indicators"));
  $("#e-source").value = "bottles_processes";
  fillEbomIndicators();
  $("#e-source").addEventListener("change", fillEbomIndicators);
  $("#e-unit").replaceChildren(option("", "Don't assume (skip those lines)"), ...m.units.map((u) => option(u)));
  $("#e-provider").replaceChildren(...$("#o-provider").cloneNode(true).children);
  $("#e-provider").value = $("#o-provider").value;

  const file = $("#ebom-file");
  const drop = $("#drop");
  drop.addEventListener("click", () => file.click());
  drop.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); file.click(); } });
  file.addEventListener("change", () => file.files[0] && loadEbomFile(file.files[0]));
  drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("over"); });
  drop.addEventListener("dragleave", () => drop.classList.remove("over"));
  drop.addEventListener("drop", (e) => {
    e.preventDefault();
    drop.classList.remove("over");
    if (e.dataTransfer.files[0]) loadEbomFile(e.dataTransfer.files[0]);
  });
  $("#ebom-sample").addEventListener("click", async () => {
    const res = await fetch("/sample_ebom.csv");
    setEbomText(await res.text(), "sample_ebom.csv");
    analyseEbom();
  });
  $("#ebom-go").addEventListener("click", analyseEbom);
}

function loadEbomFile(f) {
  if (f.size > 900 * 1024) {
    $("#ebom-results").replaceChildren(errorBanner("File too large", "eBOM files are limited to about 900 KB."));
    return;
  }
  const reader = new FileReader();
  reader.onload = () => setEbomText(String(reader.result), f.name);
  reader.readAsText(f);
}

function setEbomText(text, name) {
  eb.text = text;
  eb.name = name;
  eb.scenarios = [];
  const rows = text.split(/\r?\n/).filter((l) => l.trim()).length - 1;
  $("#ebom-filename").textContent = `${name} · ${Math.max(rows, 0)} line${rows === 1 ? "" : "s"}`;
  $("#ebom-go").disabled = false;
}

function settings() {
  return {
    csv: eb.text,
    indicator: $("#e-indicator").value,
    source: $("#e-source").value || null,
    default_unit: $("#e-unit").value || null,
    use_ai: $("#e-ai").checked,
    provider: $("#e-provider").value,
  };
}

async function analyseEbom() {
  if (!eb.text) return;
  const body = settings();
  const btn = $("#ebom-go");
  btn.disabled = true;
  $("#ebom-results").replaceChildren(h("div", { class: "empty" }, h("span", { class: "spinner" }), " ",
    body.use_ai ? "Matching lines, finding similar materials and asking the AI about the rest…" : "Matching lines against the index…"));
  try {
    eb.result = await api("/ebom", body);
    renderEbom();
  } catch (err) {
    $("#ebom-results").replaceChildren(errorBanner("Could not analyse this eBOM", err.message));
  } finally {
    btn.disabled = false;
  }
}

// ---------------------------------------------------------------- dashboard

function renderEbom() {
  const r = eb.result;
  const out = [kpis(r)];
  if (r.product.flagged_lines) {
    out.push(h("div", { class: "banner insufficient" }, h("div", {},
      h("h3", {}, `${r.product.flagged_lines} source factor${r.product.flagged_lines === 1 ? "" : "s"} looked implausible`),
      h("p", { style: "margin:4px 0 0" }, "Some processes in the export appear to be recorded per tonne or per item while "
        + "labelled per kg (about 1000× too high). Those values are not used; a similar material or the AI is used instead, "
        + "and each affected line says so."))));
  }
  if (!r.climate) {
    out.push(h("div", { class: "banner insufficient" }, h("div", {}, h("h3", {}, "Scores and grades need Climate change"),
      h("p", { style: "margin:4px 0 0" }, `The grade, hotspots and recommendations use the GHG indicator. "${r.indicator}" is shown per line only.`))));
  }
  out.push(h("div", { class: "dash-grid" }, radarCard(r), componentChart(r)));
  out.push(paretoChart(r));
  if (r.recommendations.length) out.push(recommendationsPanel(r));
  out.push(whatIfPanel(r));
  out.push(itemsTable(r));
  out.push(materialsTable(r));
  out.push(methodology(r));
  $("#ebom-results").replaceChildren(...out);
  drawRows();
}

function kpis(r) {
  const p = r.product;
  const tierBar = h("div", { class: "tierbar", role: "img",
    "aria-label": Object.entries(p.tier_shares).map(([k, v]) => `${TIER_LABEL[k]} ${pct(v)}`).join(", ") },
    Object.entries(p.tier_shares).filter(([, v]) => v > 0).map(([k, v]) =>
      h("i", { class: `seg t-${k}`, style: `width:${v * 100}%`, title: `${TIER_LABEL[k]}: ${pct(v, 1)}` })));
  const conf = p.match_confidence;
  const scored = conf.high + conf.medium + conf.low;
  return h("div", { class: "kpis" },
    h("div", { class: "tile" },
      h("div", { class: "k" }, "Total GHG"),
      h("div", { class: "v" }, p.total == null ? "—" : fmt(p.total), h("small", {}, " kg CO₂-eq")),
      tierBar,
      h("div", { class: "s" }, Object.entries(p.tier_shares).map(([k, v]) => `${TIER_LABEL[k]} ${pct(v)}`).join(" · ")),
      p.intensity != null ? h("div", { class: "s" }, `${fmt(p.intensity)} kg CO₂-eq per kg of covered mass`) : null),
    h("div", { class: "tile" },
      h("div", { class: "k" }, "Overall score & grade"),
      h("div", { class: "grade-row" }, gradeBadge(p.grade, true),
        h("div", {}, h("div", { class: "v" }, p.score == null ? "—" : p.score.toFixed(1), h("small", {}, " / 100")),
          h("div", { class: "s" }, p.grade_text || ""))),
      h("div", { class: "s" }, `Data confidence: ${p.confidence} · ${pct(p.mass_coverage)} of mass covered`)),
    h("div", { class: "tile" + (p.hotspots.length ? " warn" : "") },
      h("div", { class: "k" }, "Hotspots"),
      h("div", { class: "v" }, p.hotspots.length),
      h("div", { class: "s" }, p.hotspots.length
        ? p.hotspots.map((x) => `${x.component} (${pct(x.share)})`).join(" · ")
        : "No item above 20% of total GHG")),
    h("div", { class: "tile" },
      h("div", { class: "k" }, "High-confidence matches"),
      h("div", { class: "v" }, pct(p.high_confidence_share)),
      h("div", { class: "s" }, `${conf.high} high · ${conf.medium} medium · ${conf.low} low of ${scored} scored lines`),
      h("div", { class: "s" }, `${p.counts.not_covered} not covered · ${p.counts.invalid} invalid`)));
}

function radarCard(r) {
  const s = r.product.subscores;
  const axes = [["ghg", "GHG"], ["toxicity", "Toxicity"], ["criticality", "Criticality"]];
  const size = 240, c = size / 2, R = 84;
  const pt = (i, v) => {
    const a = -Math.PI / 2 + (i * 2 * Math.PI) / axes.length;
    return [c + Math.cos(a) * R * (v / 100), c + Math.sin(a) * R * (v / 100)];
  };
  const NS = "http://www.w3.org/2000/svg";
  const el = (tag, attrs, text) => {
    const e = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
    if (text != null) e.textContent = text;
    return e;
  };
  const svg = el("svg", { viewBox: `0 0 ${size} ${size}`, class: "radar", role: "img",
    "aria-label": axes.map(([k, l]) => `${l} ${s[k] == null ? "n/a" : s[k].toFixed(0)}`).join(", ") });
  for (const ring of [25, 50, 75, 100]) {
    svg.append(el("polygon", { class: "ring", points: axes.map((_, i) => pt(i, ring).join(",")).join(" ") }));
  }
  axes.forEach(([k, label], i) => {
    const [x, y] = pt(i, 100);
    svg.append(el("line", { class: "axis", x1: c, y1: c, x2: x, y2: y }));
    const [lx, ly] = pt(i, 128);
    svg.append(el("text", { x: lx, y: ly, class: "rlabel", "text-anchor": "middle", "dominant-baseline": "middle" },
      `${label} ${s[k] == null ? "n/a" : s[k].toFixed(0)}`));
  });
  if (axes.every(([k]) => s[k] != null)) {
    const pts = axes.map(([k], i) => pt(i, s[k]).join(",")).join(" ");
    svg.append(el("polygon", { class: "shape", points: pts }));
    axes.forEach(([k, label], i) => {
      const [x, y] = pt(i, s[k]);
      const dot = el("circle", { cx: x, cy: y, r: 5, class: "dot" });
      dot.append(el("title", {}, `${label}: ${s[k].toFixed(1)} / 100`));
      svg.append(dot);
    });
  }
  return h("div", { class: "card" },
    h("h3", { class: "card-title" }, "Sub-scores (0–100, higher = worse)"),
    h("p", { class: "hint", style: "margin:0 0 6px" }, "GHG-weighted across items, as in the upstream engine. Composite = 30% criticality + 35% toxicity + 35% GHG."),
    svg);
}

function segmentBar(segments, max) {
  return h("div", { class: "track" }, h("div", { class: "stack", style: `width:${max > 0 ? (segments.reduce((a, s) => a + s.impact, 0) / max) * 100 : 0}%` },
    segments.filter((s) => s.impact > 0).map((s) => h("i", { class: `seg t-${s.tier}`, style: `flex:${s.impact}`,
      title: `${s.item_id ? s.item_id + " · " : ""}${s.material}: ${fmt(s.impact)} kg CO₂-eq (${TIER_LABEL[s.tier]})` }))));
}

function tierLegend() {
  return h("div", { class: "legend" }, Object.entries(TIER_LABEL).map(([k, l]) => h("span", {}, h("i", { class: `swatch t-${k}` }), l)));
}

function componentChart(r) {
  const comps = r.components.filter((c) => c.impact != null && c.impact > 0);
  const card = h("div", { class: "card chart" }, h("h3", { class: "card-title" }, "GHG by component"), tierLegend());
  if (!comps.length) {
    card.append(h("p", { class: "muted" }, "Nothing could be calculated."));
    return card;
  }
  const max = Math.max(...comps.map((c) => c.impact));
  for (const c of comps) {
    card.append(h("div", { class: "barrow comp" },
      h("div", { class: "lbl", title: c.materials.join(", ") }, c.name, c.hotspot ? h("span", { class: "hot" }, " ● hotspot") : null),
      segmentBar(c.segments, max),
      h("div", { class: "val" }, `${pct(c.share)}`, h("br"), h("span", { class: "hint" }, `mass ${pct(c.mass_share)}`))));
  }
  const uncovered = r.components.filter((c) => c.impact == null);
  if (uncovered.length) card.append(h("p", { class: "hint" }, `Not calculated: ${uncovered.map((c) => c.name).join(", ")}.`));
  return card;
}

function paretoChart(r) {
  const items = r.lines.filter((l) => l.impact != null).sort((a, b) => b.impact - a.impact);
  const card = h("div", { class: "card chart" },
    h("h3", { class: "card-title" }, "GHG contribution per item (Pareto)"), tierLegend(),
    h("p", { class: "hint", style: "margin:-4px 0 8px" }, "Sorted by contribution; the right column is the cumulative share. Items above 20% are hotspots."));
  const max = items.length ? items[0].impact : 0;
  let cum = 0;
  for (const l of items) {
    cum += l.share || 0;
    card.append(h("div", { class: "barrow pareto" },
      h("div", { class: "lbl", title: `${l.component} · ${l.material}` }, `${l.rank}. ${l.component}`, h("span", { class: "hint" }, ` · ${l.material}`)),
      h("div", { class: "track" }, h("div", { class: `bar t-${l.tier}`, style: `width:${max ? (l.impact / max) * 100 : 0}%`,
        title: `${fmt(l.impact)} kg CO₂-eq · ${TIER_LABEL[l.tier]}` })),
      h("div", { class: "val" }, `${pct(l.share, 1)}`, l.hotspot ? h("span", { class: "hot" }, " ●") : null),
      h("div", { class: "val cum" }, pct(cum))));
  }
  return card;
}

function recommendationsPanel(r) {
  return h("div", { class: "card" },
    h("h3", { class: "card-title" }, "Eco-design recommendations"),
    h("p", { class: "hint", style: "margin:0 0 10px" }, "Sister materials are lower-impact records of the same material family and unit in the database. "
      + "Cost: the LCA exports carry no cost data, so trade-offs show impact only" + (r.columns.cost ? "; your file's cost column is shown for the current item." : ".")),
    h("div", { class: "recs" }, r.recommendations.map((rec) => h("div", { class: "rec" + (rec.hotspot ? " hotspot" : "") },
      h("div", { class: "rec-head" }, h("b", {}, `${rec.component}`), h("span", { class: "hint" }, ` ${rec.item_id} · ${rec.material}`),
        rec.hotspot ? h("span", { class: "status not_covered", style: "margin-left:8px" }, `hotspot ${pct(rec.share)}`) : null),
      rec.advice.length ? h("ul", { class: "advice" }, rec.advice.map((a) => h("li", {}, a))) : null,
      rec.sisters.length ? h("div", { class: "sisters" },
        h("div", { class: "sub" }, "Lower-impact sister materials:"),
        rec.sisters.map((s) => h("div", { class: "sister" },
          h("div", { class: "sister-main" },
            h("button", { type: "button", class: "linkish", onclick: () => openRecord(s.id, eb.result.indicator) }, s.name),
            h("div", { class: "sub" }, `factor ${fmt(s.factor)} · item ${fmt(s.impact)} kg CO₂-eq · `,
              h("b", { class: "good" }, `−${pct(s.reduction)}`),
              ` · cost ${rec.current_cost ? `n/a (current ${rec.current_cost})` : "n/a"}`)),
          h("button", { type: "button", class: "chip", onclick: () => addSwap(rec.item_id, shortName(s.name)) }, "Try in what-if")))) : null))));
}

const shortName = (name) => name.split(",")[0].trim();

// ---------------------------------------------------------------- what-if

function addSwap(itemId, material) {
  if (!eb.scenarios.length) eb.scenarios.push({ name: "", swaps: [] });
  let sc = eb.scenarios.find((s) => !s.swaps.some((w) => w.item_id === itemId));
  if (!sc && eb.scenarios.length < 3) eb.scenarios.push((sc = { name: "", swaps: [] }));
  sc = sc || eb.scenarios[eb.scenarios.length - 1];
  const existing = sc.swaps.find((w) => w.item_id === itemId);
  if (existing) existing.material = material;
  else sc.swaps.push({ item_id: itemId, material });
  const panel = $("#whatif");
  panel.replaceWith(whatIfPanel(eb.result));
  $("#whatif").scrollIntoView({ behavior: "smooth", block: "start" });
}

function whatIfPanel(r) {
  if (!eb.scenarios.length) eb.scenarios.push({ name: "", swaps: [{ item_id: r.product.hotspots[0]?.item_id || r.lines[0].item_id, material: "" }] });
  const items = r.lines.filter((l) => l.status !== "invalid");
  const suggestions = [...new Set([
    ...r.recommendations.flatMap((rec) => rec.sisters.map((s) => shortName(s.name))),
    ...r.lines.map((l) => l.material),
  ])].filter(Boolean);
  const list = h("datalist", { id: "material-suggestions" }, suggestions.map((s) => option(s)));
  const cols = eb.scenarios.map((sc, si) => h("div", { class: "scenario" },
    h("input", { class: "sc-name", placeholder: `Scenario ${si + 1}`, value: sc.name, "aria-label": "Scenario name",
      oninput: (e) => { sc.name = e.target.value; } }),
    sc.swaps.map((w, wi) => h("div", { class: "swap" },
      (() => {
        const sel = h("select", { "aria-label": "Item", onchange: (e) => { w.item_id = e.target.value; } },
          items.map((l) => option(l.item_id, `${l.item_id} · ${l.component} (${l.material})`)));
        sel.value = w.item_id;
        return sel;
      })(),
      h("input", { list: "material-suggestions", placeholder: "Alternative material", value: w.material,
        "aria-label": "Alternative material", oninput: (e) => { w.material = e.target.value; } }),
      h("button", { type: "button", class: "ghost", "aria-label": "Remove swap",
        onclick: () => { sc.swaps.splice(wi, 1); $("#whatif").replaceWith(whatIfPanel(r)); } }, "✕"))),
    h("div", { class: "row-actions" },
      h("button", { type: "button", class: "chip", onclick: () => { sc.swaps.push({ item_id: items[0].item_id, material: "" }); $("#whatif").replaceWith(whatIfPanel(r)); } }, "+ swap"),
      eb.scenarios.length > 1 ? h("button", { type: "button", class: "chip", onclick: () => { eb.scenarios.splice(si, 1); $("#whatif").replaceWith(whatIfPanel(r)); } }, "Remove scenario") : null)));
  return h("div", { class: "card", id: "whatif" }, list,
    h("h3", { class: "card-title" }, "What-if simulator: material swaps"),
    h("p", { class: "hint", style: "margin:0 0 10px" }, "Pick items and alternative materials. The whole pipeline (matching, similar materials, AI, scoring) is re-run for each scenario, up to three side by side."),
    h("div", { class: "scenarios" }, cols),
    h("div", { class: "row-actions" },
      eb.scenarios.length < 3 ? h("button", { type: "button", class: "chip", onclick: () => { eb.scenarios.push({ name: "", swaps: [{ item_id: items[0].item_id, material: "" }] }); $("#whatif").replaceWith(whatIfPanel(r)); } }, "+ scenario") : null,
      h("button", { type: "button", class: "primary", id: "whatif-go", onclick: runWhatIf }, "Run scenarios")),
    h("div", { id: "whatif-results" }));
}

async function runWhatIf() {
  const scenarios = eb.scenarios.map((s) => ({ name: s.name, swaps: s.swaps.filter((w) => w.material.trim()) }))
    .filter((s) => s.swaps.length);
  const out = $("#whatif-results");
  if (!scenarios.length) {
    out.replaceChildren(h("p", { class: "calc-out err" }, "Add at least one swap with an alternative material."));
    return;
  }
  const btn = $("#whatif-go");
  btn.disabled = true;
  out.replaceChildren(h("div", { class: "empty" }, h("span", { class: "spinner" }), " Re-running the pipeline…"));
  try {
    renderWhatIf(await api("/ebom/whatif", { ...settings(), scenarios }));
  } catch (err) {
    out.replaceChildren(h("p", { class: "calc-out err" }, err.message));
  } finally {
    btn.disabled = false;
  }
}

function renderWhatIf(w) {
  const b = w.baseline;
  const col = (title, s, sc) => h("div", { class: "sc-result" + (sc ? "" : " base") },
    h("h4", {}, title),
    h("div", { class: "grade-row" }, gradeBadge(s.grade, true),
      h("div", {}, h("div", { class: "v" }, s.total == null ? "—" : fmt(s.total), h("small", {}, " kg CO₂-eq")),
        h("div", { class: "s" }, `score ${s.score == null ? "—" : s.score.toFixed(1)}`))),
    sc ? h("div", { class: "delta " + (sc.delta < 0 ? "down" : sc.delta > 0 ? "up" : "") },
      sc.delta == null ? "—" : `${sc.delta > 0 ? "+" : ""}${fmt(sc.delta)} kg CO₂-eq (${sc.delta_pct > 0 ? "+" : ""}${pct(sc.delta_pct, 1)})`,
      h("div", { class: "s" }, `grade ${sc.grade_change[0] || "—"} → ${sc.grade_change[1] || "—"}`)) : h("div", { class: "s" }, "Current design"),
    sc ? h("ul", { class: "advice" }, sc.changes.map((c) => h("li", {},
      `${c.component}: ${c.from} → ${c.to} `, h("span", { class: "hint" },
        c.to_impact == null ? "(no reliable data; excluded)" : `(${TIER_LABEL[c.to_tier]}${c.to_record ? ": " + shortName(c.to_record) : ""})`)))) : null,
    sc ? h("div", { class: "explain" + (sc.explanation.source === "ai" ? " ai" : "") },
      h("div", { class: "label" }, sc.explanation.source === "ai" ? `AI-written · ${sc.explanation.provider || ""} ${sc.explanation.model || ""}` : "Summary"),
      h("p", {}, sc.explanation.text), h("div", { class: "hint" }, sc.explanation.label)) : null);
  $("#whatif-results").replaceChildren(h("div", { class: "sc-grid" }, col("Baseline", b), w.scenarios.map((sc) => col(sc.name, sc.summary, sc))));
}

// ---------------------------------------------------------------- tables

const COLS = [
  ["rank", "#", (l) => l.rank ?? 999],
  ["item_id", "Item", (l) => l.item_id],
  ["component", "Component", (l) => l.component],
  ["material", "Material", (l) => l.material],
  ["quantity", "Qty", (l) => l.quantity ?? -1],
  ["status", "Basis", (l) => l.status],
  ["impact", "GHG", (l) => l.impact ?? -1],
  ["share", "Share", (l) => l.share ?? -1],
  ["crit", "Crit.", (l) => l.scores?.criticality ?? -1],
  ["tox", "Tox.", (l) => l.scores?.toxicity ?? -1],
  ["ghgs", "GHG score", (l) => l.scores?.ghg ?? -1],
  ["score", "Score", (l) => l.scores?.composite ?? -1],
  ["grade", "Grade", (l) => l.grade || "Z"],
  ["confidence", "Confidence", (l) => ({ high: 0, medium: 1, low: 2 })[l.confidence] ?? 3],
  ["hotspot", "Hotspot", (l) => (l.hotspot ? 0 : 1)],
];

function itemsTable(r) {
  const filters = h("div", { class: "filters-row" },
    h("input", { type: "search", id: "t-text", placeholder: "Filter items, components, materials…", "aria-label": "Filter", oninput: drawRows }),
    h("select", { id: "t-grade", "aria-label": "Grade", onchange: drawRows }, option("", "All grades"), ..."ABCD".split("").map((g) => option(g, `Grade ${g}`))),
    h("select", { id: "t-conf", "aria-label": "Confidence", onchange: drawRows }, option("", "Any confidence"), ...["high", "medium", "low"].map((c) => option(c))),
    h("select", { id: "t-status", "aria-label": "Basis", onchange: drawRows }, option("", "Any basis"), ...Object.entries(STATUS_LABEL).map(([k, v]) => option(k, v))),
    h("label", { class: "check" }, h("input", { type: "checkbox", id: "t-hot", onchange: drawRows }), "Hotspots only"));
  const head = h("tr", {}, COLS.map(([key, label]) => h("th", { class: "sortable", "data-key": key, tabindex: "0",
    onclick: () => sortBy(key), onkeydown: (e) => { if (e.key === "Enter") sortBy(key); } }, label)), h("th", {}, "Notes"));
  return h("div", { class: "card" },
    h("h3", { class: "card-title" }, "All items"),
    filters,
    h("div", { class: "table-wrap" }, h("table", { class: "lines", id: "items" }, h("thead", {}, head), h("tbody", { id: "items-body" }))),
    h("div", { class: "row-actions" }, h("button", { type: "button", class: "chip", onclick: downloadCsv }, "Download results CSV")),
    h("p", { class: "hint", id: "items-count" }));
}

function sortBy(key) {
  eb.sort = { key, dir: eb.sort.key === key ? -eb.sort.dir : 1 };
  drawRows();
}

function lineNotes(l) {
  const notes = [];
  if (l.converted_from) notes.push(`Converted ${l.converted_from.quantity} ${l.converted_from.unit} to ${fmt(l.quantity)} kg.`);
  if (l.unit_source === "default") notes.push(`Unit ${l.unit} taken from your default, not the file.`);
  for (const w of l.warnings) notes.push("⚠ " + w.message);
  if (l.status === "proxy") {
    notes.push(`Similar material (${l.proxy.kind}, ${l.family} family, ${l.proxy.family_size} plausible records, factors ${fmt(l.proxy.family_min)}–${fmt(l.proxy.family_max)}).`);
  } else if (l.status !== "evidence") {
    for (const c of l.reasons) notes.push(LINE_REASONS[c] || c);
  }
  const p = l.provisional;
  if (p) {
    if (p.contribution != null) notes.push(`AI (${p.provider}${p.model ? " · " + p.model : ""}): ≈${fmt(p.value)} ${p.impact_unit} per ${l.unit}, range ${fmt(p.lower)}–${fmt(p.upper)}. ${p.explanation || ""}`);
    else if (p.status === "declined") notes.push(`AI declined: ${p.explanation || ""}`);
    else notes.push(`AI ${p.status}: ${p.error || ""}`);
  }
  if (l.scores?.reweighted) notes.push("Score uses the available sub-scores only.");
  return notes;
}

function drawRows() {
  const r = eb.result;
  const text = ($("#t-text")?.value || "").toLowerCase();
  const grade = $("#t-grade")?.value, conf = $("#t-conf")?.value, status = $("#t-status")?.value, hot = $("#t-hot")?.checked;
  const getter = COLS.find(([k]) => k === eb.sort.key)[2];
  const rows = r.lines.filter((l) =>
    (!text || `${l.item_id} ${l.component} ${l.material}`.toLowerCase().includes(text))
    && (!grade || l.grade === grade) && (!conf || l.confidence === conf) && (!status || l.status === status)
    && (!hot || l.hotspot))
    .sort((a, b) => {
      const x = getter(a), y = getter(b);
      return (typeof x === "string" ? x.localeCompare(y) : x - y) * eb.sort.dir;
    });
  document.querySelectorAll("#items th.sortable").forEach((th) => {
    th.setAttribute("aria-sort", th.dataset.key === eb.sort.key ? (eb.sort.dir > 0 ? "ascending" : "descending") : "none");
  });
  const num = (v, d = 1) => (v == null ? "—" : v.toFixed(d));
  $("#items-body").replaceChildren(...rows.map((l) => {
    const rec = l.proxy || l.match;
    const recCell = l.tier && rec ? h("button", { type: "button", class: "linkish", title: rec.name, onclick: () => openRecord(rec.id, r.indicator) }, shortName(rec.name)) : null;
    const notes = lineNotes(l);
    return h("tr", { class: l.hotspot ? "hot-row" : "" },
      h("td", { class: "num" }, l.rank ?? "—"),
      h("td", {}, l.item_id),
      h("td", {}, l.component),
      h("td", {}, l.material || "(empty)", l.original_material ? h("div", { class: "sub" }, `was ${l.original_material}`) : null),
      h("td", { class: "num" }, l.quantity != null ? `${fmt(l.quantity)} ${l.unit || "?"}` : (l.quantity_raw || "—")),
      h("td", {}, h("span", { class: "status " + l.status }, STATUS_LABEL[l.status]), recCell ? h("div", { class: "sub" }, recCell) : null),
      h("td", { class: "num" }, l.impact == null ? "—" : fmt(l.impact)),
      h("td", { class: "num" }, pct(l.share, 1)),
      h("td", { class: "num" }, num(l.scores?.criticality, 0)),
      h("td", { class: "num" }, num(l.scores?.toxicity, 0)),
      h("td", { class: "num" }, num(l.scores?.ghg, 0)),
      h("td", { class: "num" }, num(l.scores?.composite)),
      h("td", {}, gradeBadge(l.grade)),
      h("td", {}, l.confidence ? h("span", { class: `conf c-${l.confidence}` }, l.confidence) : "—"),
      h("td", {}, l.hotspot ? h("span", { class: "hot" }, "● yes") : ""),
      h("td", { class: "notes" }, notes.length ? h("details", {}, h("summary", {}, `${notes.length} note${notes.length > 1 ? "s" : ""}`),
        h("ul", {}, notes.map((n) => h("li", {}, n)))) : h("span", { class: "muted" }, "—")));
  }));
  $("#items-count").textContent = `${rows.length} of ${r.lines.length} items shown. Click a column header to sort.`;
}

function materialsTable(r) {
  return h("div", { class: "card" },
    h("h3", { class: "card-title" }, "Material-wise analysis"),
    h("div", { class: "table-wrap" }, h("table", { class: "lines" },
      h("thead", {}, h("tr", {}, ["Material", "Family", "Used in", "Mass", "GHG", "Share", "kg CO₂-eq/kg", "Score", "Grade", "Basis"].map((t) => h("th", {}, t)))),
      h("tbody", {}, r.materials.map((m) => h("tr", { class: m.hotspot ? "hot-row" : "" },
        h("td", {}, m.name),
        h("td", {}, m.family || h("span", { class: "muted" }, "—")),
        h("td", {}, m.components.join(", ")),
        h("td", { class: "num" }, m.mass_kg == null ? "—" : `${fmt(m.mass_kg)} kg`),
        h("td", { class: "num" }, m.impact == null ? "—" : fmt(m.impact)),
        h("td", { class: "num" }, pct(m.share, 1)),
        h("td", { class: "num" }, m.intensity == null ? "—" : fmt(m.intensity)),
        h("td", { class: "num" }, m.score == null ? "—" : m.score.toFixed(1)),
        h("td", {}, gradeBadge(m.grade)),
        h("td", {}, Object.entries(m.tiers).filter(([, v]) => v > 0).map(([k]) => h("span", { class: "status " + k, style: "margin-right:4px" }, TIER_LABEL[k])),
          m.uncovered_lines ? h("span", { class: "status not_covered" }, `${m.uncovered_lines} not covered`) : null)))))));
}

function methodology(r) {
  const s = r.scoring;
  return h("details", { class: "card method" },
    h("summary", {}, "How the analysis and grade are calculated"),
    h("ul", {},
      h("li", {}, "Each line is resolved in order: exact database record → similar material of the same family (same unit, plausible factor) → AI estimate (if enabled) → not covered."),
      h("li", {}, `Item score = ${s.weights.criticality * 100}% criticality + ${s.weights.toxicity * 100}% toxicity + ${s.weights.ghg * 100}% GHG (0–100, higher = worse), following the upstream lca_backend engine. Sub-scores missing for a line (e.g. toxicity for AI estimates) are left out and the weights re-normalised.`),
      h("li", {}, `GHG sub-score = ${s.ghg_blend.intensity * 100}% intensity (vs ${fmt(s.references_p95_per_kg["Climate change"])} kg CO₂-eq/kg) + ${s.ghg_blend.absolute * 100}% absolute (vs ${s.abs_ghg_ref_kg} kg CO₂-eq per item).`),
      h("li", {}, `Toxicity = 70% human toxicity (vs ${fmt(s.references_p95_per_kg["Human toxicity"])}) + 30% metal depletion (vs ${fmt(s.references_p95_per_kg["Metal depletion"])}), per kg.`),
      h("li", {}, s.note),
      h("li", {}, `Grades: ${s.grade_bands.map((g) => g.max == null ? `${g.grade} above` : `${g.grade} ≤ ${g.max}`).join(", ")}. Product score = GHG-weighted mean of item scores. Hotspot = more than ${s.hotspot_share * 100}% of total GHG.`),
      h("li", {}, r.product.impact_unit || ""),
      h("li", {}, r.note)));
}

function downloadCsv() {
  const r = eb.result;
  const cols = ["rank", "item_id", "component", "material", "quantity", "unit", "basis", "record", "factor", "ghg",
    "share", "criticality", "toxicity", "ghg_score", "score", "grade", "confidence", "hotspot", "notes"];
  const esc = (v) => {
    const t = v == null ? "" : String(v);
    return /[",\n]/.test(t) ? `"${t.replaceAll('"', '""')}"` : t;
  };
  const rows = r.lines.map((l) => [l.rank, l.item_id, l.component, l.material, l.quantity, l.unit, l.status,
    (l.proxy || l.match)?.id, l.factor, l.impact, l.share, l.scores?.criticality, l.scores?.toxicity, l.scores?.ghg,
    l.scores?.composite, l.grade, l.confidence, l.hotspot, lineNotes(l).join(" | ")].map(esc).join(","));
  const blob = new Blob([[cols.join(","), ...rows].join("\n")], { type: "text/csv" });
  const a = h("a", { href: URL.createObjectURL(blob), download: `ebom_analysis_${(eb.name || "bom").replace(/\.\w+$/, "")}.csv` });
  document.body.append(a);
  a.click();
  a.remove();
}
