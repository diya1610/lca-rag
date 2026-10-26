"use strict";

// Every value from the API is inserted as text (never innerHTML): source rows and
// AI output are untrusted.
const $ = (sel, el = document) => el.querySelector(sel);

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "style") el.style.cssText = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid == null || kid === false) continue;
    el.append(kid instanceof Node ? kid : String(kid));
  }
  return el;
}

async function api(path, body) {
  const opts = body === undefined ? {} : {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({ error: "bad_response" }));
  if (!res.ok) {
    const err = new Error(data.message || data.error || res.statusText);
    err.code = data.error;
    throw err;
  }
  return data;
}

const fmt = (v) => {
  if (typeof v !== "number") return String(v);
  if (v === 0) return "0";
  const a = Math.abs(v);
  return a >= 1e5 || a < 1e-3 ? v.toExponential(4) : Number(v.toPrecision(6)).toString();
};

const REASONS = {
  no_candidates: "No record matched the query.",
  weak_term_coverage: "The best match covers less than 75% of the query terms, so it may be a different material or process.",
  missing_indicator: "The top record does not have the requested indicator (missing is never treated as zero).",
  missing_reference_unit: "The top record has no explicit reference unit, so its value cannot be scaled.",
};
const WARNINGS = {
  missing_reference_unit: "no reference unit",
  no_impact_indicators: "no impact indicators",
  missing_indicator_values: "missing values",
  non_numeric_indicator_values: "non-numeric values",
  missing_name: "no name",
};
const EXAMPLES = [
  ["bottle-grade PET", { source: "bottles_processes", indicator: "Climate change" }],
  ["steel rebar", { source: "bottles_processes", indicator: "Climate change" }],
  ["process steam natural gas", { source: "bottles_processes", location: "EU-27" }],
  ["carbon dioxide", { kind: "elementary_flow" }],
  ["recycled carbon fibre composite", { source: "bottles_processes", indicator: "Climate change", ai: true }],
];

const state = { meta: null };

// ---------------------------------------------------------------- setup

function option(value, label) {
  return h("option", { value }, label ?? value);
}

function fillSelect(sel, values, anyLabel = "Any") {
  sel.replaceChildren(option("", anyLabel), ...values.map((v) => option(v)));
}

function fillIndicators() {
  const sel = $("#f-indicator");
  const prev = sel.value;
  const src = $("#f-source").value;
  const groups = Object.entries(state.meta.indicators).filter(([s, l]) => l.length && (!src || s === src));
  sel.replaceChildren(option("", "None (retrieve only)"),
    ...groups.map(([s, labels]) => h("optgroup", { label: s }, labels.map((l) => option(l)))));
  if ([...sel.options].some((o) => o.value === prev)) sel.value = prev;
}

async function init() {
  const [health, meta] = await Promise.all([api("/health"), api("/meta")]);
  state.meta = meta;
  const pills = [
    h("span", { class: "pill" }, h("b", {}, health.records.toLocaleString()), "records"),
    h("span", { class: "pill" }, h("b", {}, health.hierarchy_nodes), "hierarchy nodes"),
    h("span", { class: "pill", title: health.commit || "" }, "commit", h("b", { class: "mono" }, (health.commit || "").slice(0, 7))),
  ];
  for (const [name, ok] of Object.entries(meta.providers)) {
    pills.push(h("span", { class: "pill", title: ok ? `${name} ready` : `${name} not configured` },
      h("i", { class: "dot" + (ok ? " on" : "") }), name));
  }
  $("#stats").replaceChildren(...pills);

  fillSelect($("#f-source"), Object.keys(meta.sources), "All sources");
  fillSelect($("#f-kind"), meta.kinds, "Any kind");
  fillSelect($("#f-stage"), meta.stages, "Any stage");
  fillIndicators();
  $("#f-source").addEventListener("change", fillIndicators);

  const providers = Object.entries(meta.providers);
  $("#o-provider").replaceChildren(...providers.map(([n, ok]) => option(n, ok ? n : `${n} (not configured)`)));
  const firstReady = providers.find(([, ok]) => ok);
  if (firstReady) $("#o-provider").value = firstReady[0];
  if (!meta.dense) {
    $("#o-dense").disabled = true;
    $("#dense-wrap").title = "Start the server with --dense to enable";
    $("#dense-wrap").classList.add("muted");
  }

  $("#examples").replaceChildren(h("span", { class: "hint" }, "Try:"),
    ...EXAMPLES.map(([q, preset]) => h("button", { type: "button", class: "chip", onclick: () => runExample(q, preset) }, q)));

  $("#search-form").addEventListener("submit", (e) => { e.preventDefault(); search(); });
  document.querySelectorAll(".tab").forEach((t) => t.addEventListener("click", () => showTab(t.dataset.tab)));
  $("#d-close").addEventListener("click", closeDrawer);
  $("#scrim").addEventListener("click", closeDrawer);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDrawer(); });
  initEbom();
  $("#q").focus();
}

function runExample(q, preset) {
  $("#q").value = q;
  $("#f-source").value = preset.source || "";
  fillIndicators();
  $("#f-kind").value = preset.kind || "";
  $("#f-stage").value = "";
  $("#f-location").value = preset.location || "";
  $("#f-unit").value = "";
  $("#f-indicator").value = preset.indicator || "";
  $("#o-ai").checked = Boolean(preset.ai);
  search();
}

function showTab(name) {
  document.querySelectorAll(".tab").forEach((t) => {
    const on = t.dataset.tab === name;
    t.classList.toggle("active", on);
    t.setAttribute("aria-selected", on);
  });
  $("#tab-search").hidden = name !== "search";
  $("#tab-browse").hidden = name !== "browse";
  $("#tab-ebom").hidden = name !== "ebom";
  if (name === "browse" && !$("#tree").childElementCount) loadTree(null, $("#tree"));
}

// ---------------------------------------------------------------- search

async function search() {
  const text = $("#q").value.trim();
  if (!text) return;
  const filters = {
    source: $("#f-source").value || null,
    kind: $("#f-kind").value || null,
    stage: $("#f-stage").value || null,
    location: $("#f-location").value.trim() || null,
    unit: $("#f-unit").value.trim() || null,
  };
  const body = {
    query: text,
    filters,
    indicator: $("#f-indicator").value || null,
    top_k: Number($("#o-topk").value) || 8,
    use_ai: $("#o-ai").checked,
    provider: $("#o-provider").value,
    dense: $("#o-dense").checked,
  };
  const btn = $("#go");
  btn.disabled = true;
  $("#results").replaceChildren(h("div", { class: "empty" }, h("span", { class: "spinner" }), " ",
    body.use_ai ? "Retrieving evidence (the AI fallback may take a few seconds)…" : "Retrieving evidence…"));
  try {
    renderResult(await api("/query", body));
  } catch (err) {
    $("#results").replaceChildren(h("div", { class: "banner error" }, h("div", {}, h("h3", {}, "Request failed"), h("p", {}, err.message))));
  } finally {
    btn.disabled = false;
  }
}

function renderResult(r) {
  const out = [];
  const ok = r.status === "ok";
  out.push(h("div", { class: "banner " + (ok ? "ok" : "insufficient") },
    h("div", {},
      h("h3", {}, ok ? (r.answer ? "Evidence found" : "Candidates found") : "Insufficient evidence"),
      ok ? h("p", { class: "muted", style: "margin:4px 0 0" },
        r.answer ? "A source record supplies the requested indicator with an explicit reference unit."
          : "Pick an indicator to get a value; otherwise these are ranked candidate records.")
        : h("ul", {}, r.reasons.map((code) => h("li", {}, REASONS[code] || code)))),
    h("div", { class: "meta" }, `${r.latency_ms} ms`, h("br"), `${r.candidates.length} candidate${r.candidates.length === 1 ? "" : "s"}`)));

  if (r.route.length) {
    out.push(h("div", { class: "route" }, "Routed branches:",
      r.route.slice(0, 5).map((n) => h("span", { class: "node", title: `${n.record_count} records` }, n.node_id.replaceAll("/", " › ")))));
  }
  if (r.answer) out.push(answerCard(r.answer));
  if (r.fallback) out.push(fallbackCard(r.fallback));

  if (r.candidates.length) {
    out.push(h("div", { class: "section-title" }, "Ranked candidates"));
    r.candidates.forEach((c, i) => out.push(candidateCard(c, i + 1, r.indicator)));
    out.push(h("p", { class: "note" }, r.selection_note));
  }
  $("#results").replaceChildren(...out);
}

function answerCard(a) {
  return h("div", { class: "card answer" },
    h("div", { class: "label" }, "Source evidence"),
    h("div", { class: "bignum" }, fmt(a.value), " ", h("small", {}, `${a.indicator} per 1 ${a.per_reference_unit}`)),
    h("div", { class: "cite" }, "Cited from ", h("span", { class: "mono" }, a.citation)),
    h("p", { class: "note", style: "padding:0;margin:8px 0 0" }, a.note),
    h("button", { type: "button", class: "ghost", style: "margin-top:10px", onclick: () => openRecord(a.record_id, a.indicator) },
      "Open record & calculate"));
}

function fallbackCard(fb) {
  const head = [
    h("div", { class: "label" }, "Provisional AI estimate"),
    h("div", { class: "disclaimer" }, fb.label),
  ];
  const meta = h("dl", { class: "kv" },
    h("dt", {}, "Provider"), h("dd", {}, `${fb.provider}${fb.model ? " · " + fb.model : ""}`),
    h("dt", {}, "Generated"), h("dd", {}, fb.generated_at),
    h("dt", {}, "Context records"), h("dd", { class: "mono" }, fb.context_record_ids.join(", ") || "none"),
    h("dt", {}, "Status"), h("dd", {}, fb.status));

  if (fb.status === "unavailable" || fb.status === "rejected") {
    const msg = fb.status === "rejected"
      ? "The model's response failed validation and was discarded: "
      : "The AI provider could not be used: ";
    return h("div", { class: "card provisional" }, head, h("p", {}, msg, h("span", { class: "mono" }, fb.error || "")), meta);
  }
  const e = fb.estimate;
  const body = [];
  if (e.can_estimate) {
    const span = e.upper - e.lower;
    const pos = span > 0 ? ((e.value - e.lower) / span) * 100 : 50;
    body.push(
      h("div", { class: "bignum" }, "≈ ", fmt(e.value), " ", h("small", {}, `${e.impact_unit} per 1 ${e.reference_unit}`)),
      h("div", { class: "muted" }, `${e.indicator || "Requested indicator"} · ${e.system_boundary}`),
      h("div", { class: "range", role: "img", "aria-label": `range ${e.lower} to ${e.upper}` },
        h("span", { class: "marker", style: `left:${pos}%` })),
      h("div", { class: "range-labels" }, h("span", {}, `low ${fmt(e.lower)}`), h("span", {}, `high ${fmt(e.upper)}`)));
  } else {
    body.push(h("p", { style: "font-weight:600;margin:0" }, "The model declined to estimate."));
  }
  body.push(h("p", { style: "margin:10px 0 0" }, e.explanation));
  body.push(h("div", { class: "two-col" },
    h("div", {}, h("h4", {}, "Assumptions"),
      e.assumptions.length ? h("ul", {}, e.assumptions.map((a) => h("li", {}, a))) : h("p", { class: "muted" }, "None")),
    h("div", {}, h("h4", {}, "Missing information"),
      e.missing_information.length ? h("ul", {}, e.missing_information.map((a) => h("li", {}, a))) : h("p", { class: "muted" }, "None"))));
  return h("div", { class: "card provisional" }, head, body, meta);
}

function candidateCard(c, rank, indicator) {
  const tags = [
    h("span", { class: "tag" }, c.kind.replaceAll("_", " ")),
    c.location && h("span", { class: "tag", title: "location" }, "📍 " + c.location),
    c.reference_unit ? h("span", { class: "tag", title: "reference unit" }, "per " + c.reference_unit) : null,
    c.stage && h("span", { class: "tag" }, c.stage),
    c.scores.exact_name && h("span", { class: "tag good" }, "exact name"),
    c.scores.cosine != null && h("span", { class: "tag", title: "dense rank " + c.scores.dense_rank }, "cos " + c.scores.cosine.toFixed(3)),
    c.duplicate_ids.length ? h("span", { class: "tag", title: c.duplicate_ids.join(", ") }, `+${c.duplicate_ids.length} identical`) : null,
    ...c.warnings.map((w) => h("span", { class: "tag warn", title: w.message }, "⚠ " + (WARNINGS[w.code] || w.code))),
  ];
  const cov = Math.round(c.term_coverage * 100);
  return h("button", { type: "button", class: "card cand", onclick: () => openRecord(c.id, indicator) },
    h("div", { class: "rank" }, rank),
    h("div", {},
      h("h3", {}, c.name || "(unnamed)"),
      h("div", { class: "path" }, c.hierarchy_path, " · ", h("span", { class: "mono" }, c.id)),
      h("div", { class: "tags" }, tags)),
    h("div", { class: "score" },
      `RRF ${c.scores.rrf.toFixed(4)}`, h("br"),
      `term coverage ${cov}%`,
      h("div", { class: "meter" }, h("i", { style: `width:${cov}%` }))));
}

// ---------------------------------------------------------------- record drawer

function closeDrawer() {
  $("#drawer").hidden = true;
  $("#scrim").hidden = true;
}

async function openRecord(id, indicator) {
  $("#d-title").textContent = id;
  $("#d-body").replaceChildren(h("div", { class: "empty" }, h("span", { class: "spinner" })));
  $("#drawer").hidden = false;
  $("#scrim").hidden = false;
  let rec;
  try {
    rec = await api("/record?id=" + encodeURIComponent(id));
  } catch (err) {
    $("#d-body").replaceChildren(h("p", { class: "calc-out err" }, err.message));
    return;
  }
  $("#d-title").textContent = rec.name || id;
  const labels = Object.keys(rec.impacts);
  const parts = [];

  parts.push(h("div", {},
    h("dl", { class: "kv" },
      h("dt", {}, "Record"), h("dd", { class: "mono" }, rec.id),
      h("dt", {}, "Citation"), h("dd", {}, rec.citation),
      h("dt", {}, "Hierarchy"), h("dd", {}, rec.hierarchy_path),
      h("dt", {}, "Location"), h("dd", {}, rec.location || "—"),
      h("dt", {}, "Reference unit"), h("dd", {}, rec.reference_unit || "— (not stated in source)"),
      h("dt", {}, "Stage"), h("dd", {}, rec.stage || "—"),
      h("dt", {}, "File SHA-256"), h("dd", { class: "mono" }, rec.provenance.file_sha256.slice(0, 16) + "…"),
      h("dt", {}, "Ingested"), h("dd", {}, rec.provenance.ingested_at)),
    rec.warnings.length ? h("div", { class: "tags" },
      rec.warnings.map((w) => h("span", { class: "tag warn", title: w.message }, "⚠ " + (WARNINGS[w.code] || w.code)))) : null));

  if (labels.length) {
    const sel = h("select", { "aria-label": "Indicator" }, labels.map((l) => option(l)));
    if (indicator && labels.includes(indicator)) sel.value = indicator;
    const qty = h("input", { type: "number", value: "1", step: "any", "aria-label": "Quantity" });
    const unit = h("input", { value: rec.reference_unit || "", placeholder: "unit", "aria-label": "Unit" });
    const result = h("div", { class: "calc-out" });
    const calc = async () => {
      result.className = "calc-out";
      result.textContent = "…";
      try {
        const r = await api("/calculate", { record_id: rec.id, indicator: sel.value, quantity: qty.value === "" ? null : Number(qty.value), unit: unit.value });
        result.className = "calc-out ok";
        result.textContent = `${r.indicator}: ${fmt(r.result)}   (${fmt(r.factor)} × ${r.quantity} ${r.unit})`;
      } catch (err) {
        result.className = "calc-out err";
        result.textContent = `Refused [${err.code}]: ${err.message}`;
      }
    };
    parts.push(h("div", {},
      h("h4", {}, "Unit-checked calculation"),
      h("div", { class: "calc" },
        h("label", {}, "Indicator", sel), h("label", {}, "Quantity", qty), h("label", {}, "Unit", unit),
        h("button", { type: "button", class: "primary", onclick: calc }, "Calculate")),
      result,
      h("p", { class: "hint", style: "margin:6px 0 0" }, "The unit must match the record's reference unit exactly; no conversions are made.")));

    parts.push(h("div", {},
      h("h4", {}, `Indicators (${labels.length}) — original labels`),
      h("table", { class: "impacts" }, h("tbody", {},
        labels.map((l) => h("tr", { class: rec.impacts[l] === 0 ? "zero" : "" }, h("td", {}, l), h("td", {}, fmt(rec.impacts[l]))))))));
  } else {
    parts.push(h("p", { class: "muted" }, "This record carries no impact indicators. It is descriptive evidence only and cannot be used for calculation."));
  }
  parts.push(h("div", {}, h("h4", {}, "Original CSV row (unmodified)"), h("pre", { class: "raw" }, JSON.stringify(rec.raw, null, 2))));
  $("#d-body").replaceChildren(...parts);
}

// ---------------------------------------------------------------- eBOM

const EBOM_STATUS = {
  evidence: "Evidence",
  provisional: "Provisional AI",
  not_covered: "Not covered",
  invalid: "Invalid line",
};
const EBOM_REASONS = {
  ...REASONS,
  no_candidates: "No record with this material and this exact reference unit.",
  missing_unit: "No unit in the file and no default unit chosen; quantities are never assumed to be kg.",
  invalid_quantity: "Quantity is missing or not a positive number.",
  ambiguous_quantity: "Quantity like 1,500 could mean 1.5 or 1500; write it without the comma.",
  missing_material: "Material name is empty.",
};
const ebomState = { text: null, name: null, result: null };

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
  ebomState.text = text;
  ebomState.name = name;
  const rows = text.split(/\r?\n/).filter((l) => l.trim()).length - 1;
  $("#ebom-filename").textContent = `${name} · ${Math.max(rows, 0)} line${rows === 1 ? "" : "s"}`;
  $("#ebom-go").disabled = false;
}

function errorBanner(title, msg) {
  return h("div", { class: "banner error" }, h("div", {}, h("h3", {}, title), h("p", {}, msg)));
}

async function analyseEbom() {
  if (!ebomState.text) return;
  const useAi = $("#e-ai").checked;
  const btn = $("#ebom-go");
  btn.disabled = true;
  $("#ebom-results").replaceChildren(h("div", { class: "empty" }, h("span", { class: "spinner" }), " ",
    useAi ? "Matching lines and asking the AI about uncovered ones…" : "Matching lines against the index…"));
  try {
    ebomState.result = await api("/ebom", {
      csv: ebomState.text,
      indicator: $("#e-indicator").value,
      source: $("#e-source").value || null,
      default_unit: $("#e-unit").value || null,
      use_ai: useAi,
      provider: $("#e-provider").value,
    });
    renderEbom(ebomState.result);
  } catch (err) {
    $("#ebom-results").replaceChildren(errorBanner("Could not analyse this eBOM", err.message));
  } finally {
    btn.disabled = false;
  }
}

function renderEbom(r) {
  const s = r.summary;
  const tiles = h("div", { class: "tiles" },
    h("div", { class: "tile" },
      h("div", { class: "k" }, h("i", { class: "swatch ev" }), "Evidence total"),
      h("div", { class: "v" }, fmt(s.evidence_total)),
      h("div", { class: "s" }, `${r.indicator} · ${s.evidence_lines} of ${s.lines} lines · source factors only`)),
    provisionalTile(s),
    h("div", { class: "tile" },
      h("div", { class: "k" }, "Not covered"),
      h("div", { class: "v" }, s.not_covered_lines + s.invalid_lines),
      h("div", { class: "s" }, `${s.not_covered_lines} no reliable match · ${s.invalid_lines} invalid lines`)),
    h("div", { class: "tile" + (s.flagged_lines ? " warn" : "") },
      h("div", { class: "k" }, s.flagged_lines ? "⚠ Review flagged" : "Flagged factors"),
      h("div", { class: "v" }, s.flagged_lines),
      h("div", { class: "s" }, s.flagged_lines
        ? `Evidence total without flagged lines: ${fmt(s.evidence_total_unflagged)}`
        : "No implausible source factors")));

  const out = [tiles];
  if (s.flagged_lines) {
    out.push(h("div", { class: "banner insufficient" }, h("div", {},
      h("h3", {}, "Some source factors look implausible"),
      h("p", { style: "margin:4px 0 0" }, "A flagged factor is far above the typical value for this indicator and unit. "
        + "The export may record that process per tonne or per item while labelling it per kg. "
        + "Its value is shown as recorded; review it before using the total."))));
  }
  out.push(contributionChart(r));
  out.push(linesTable(r));
  out.push(h("div", { class: "examples" },
    h("button", { type: "button", class: "chip", onclick: () => downloadEbomCsv(r) }, "Download results CSV")));
  out.push(h("p", { class: "note" }, r.note));
  $("#ebom-results").replaceChildren(...out);
}

function provisionalTile(s) {
  const groups = s.provisional_by_unit;
  const head = h("div", { class: "k" }, h("i", { class: "swatch pv" }), "Provisional AI total");
  if (!groups.length) {
    return h("div", { class: "tile prov" }, head, h("div", { class: "v" }, "—"),
      h("div", { class: "s" }, "No AI estimates (enable the fallback to estimate uncovered lines)"));
  }
  // One headline only when every estimate uses the same impact unit; never sum across units.
  return h("div", { class: "tile prov" }, head,
    groups.length === 1 ? h("div", { class: "v" }, "≈ " + fmt(groups[0].total)) : h("div", { class: "v" }, `${groups.length} units`),
    groups.map((g) => h("div", { class: "s" },
      `${groups.length > 1 ? "≈ " + fmt(g.total) + " " : ""}${g.impact_unit} · range ${fmt(g.lower)}–${fmt(g.upper)} · ${g.lines} line${g.lines === 1 ? "" : "s"}`)),
    h("div", { class: "s" }, groups.length > 1 ? "Different impact units are listed separately, not summed." : "Not added to the evidence total."));
}

function contributionChart(r) {
  // Only one provisional unit group shares the axis; other units stay in the table.
  const groups = r.summary.provisional_by_unit;
  const offAxis = r.lines.filter((l) => l.status === "provisional" && l.provisional.unit_group !== 0).length;
  const rows = r.lines.filter((l) => l.status === "evidence" || (l.status === "provisional" && l.provisional.unit_group === 0))
    .map((l) => ({ l, v: l.status === "evidence" ? l.contribution : l.provisional.contribution }))
    .sort((a, b) => b.v - a.v);
  const card = h("div", { class: "card chart" }, h("h3", {}, `${r.indicator} by BOM line`));
  if (!rows.length) {
    card.append(h("p", { class: "muted" }, "No line could be calculated."));
    return card;
  }
  const max = Math.max(...rows.map(({ l, v }) => l.status === "provisional" ? l.provisional.upper : v));
  card.append(h("div", { class: "legend" },
    h("span", {}, h("i", { class: "swatch ev" }), "Source evidence"),
    h("span", {}, h("i", { class: "swatch pv" }), "Provisional AI estimate (whisker = range)")),
    h("p", { class: "hint", style: "margin:-4px 0 10px" },
      "Evidence factors are plotted as recorded (the source does not state their impact unit)"
      + (groups.length ? `; provisional estimates are in ${groups[0].impact_unit}` : "")
      + (offAxis ? `. ${offAxis} provisional line${offAxis === 1 ? " uses" : "s use"} other units and appear only in the table.` : ".")));
  for (const { l, v } of rows) {
    const pct = (x) => `${max > 0 ? (x / max) * 100 : 0}%`;
    const prov = l.status === "provisional";
    const tip = prov
      ? `${l.material}: ≈${fmt(v)} (range ${fmt(l.provisional.lower)}–${fmt(l.provisional.upper)}) ${l.provisional.impact_unit} · provisional`
      : `${l.material}: ${fmt(v)} = ${fmt(l.factor)} × ${l.quantity} ${l.unit} · ${l.match.id}`;
    card.append(h("div", { class: "barrow", title: tip },
      h("div", { class: "lbl" }, `${l.item_id} · ${l.material}`, l.warnings.length ? " ⚠" : ""),
      h("div", { class: "track" },
        h("div", { class: "bar " + (prov ? "pv" : "ev"), style: `width:${pct(v)}` }),
        prov ? h("div", { class: "whisker", style: `left:${pct(l.provisional.lower)};width:calc(${pct(l.provisional.upper)} - ${pct(l.provisional.lower)})` }) : null),
      h("div", { class: "val" }, (prov ? "≈" : "") + fmt(v))));
  }
  return card;
}

function linesTable(r) {
  const body = r.lines.map((l) => {
    let matchCell = h("span", { class: "muted" }, "—");
    if (l.match) {
      matchCell = h("div", {},
        h("button", { type: "button", class: "linkish", onclick: () => openRecord(l.match.id, r.indicator) }, l.match.name),
        h("div", { class: "sub" }, `${l.status === "evidence" ? "" : "closest candidate, not used · "}${l.match.id} · ${l.match.location || "no location"} · coverage ${Math.round(l.match.term_coverage * 100)}%`));
    }
    const notes = [];
    for (const code of l.reasons) notes.push(EBOM_REASONS[code] || code);
    for (const w of l.warnings) notes.push("⚠ " + w.message);
    if (l.unit_source === "default") notes.push(`Unit ${l.unit} taken from your default, not the file.`);
    const p = l.provisional;
    if (p) {
      if (p.contribution != null) notes.push(`AI (${p.provider}${p.model ? " · " + p.model : ""}): ≈${fmt(p.value)} ${p.impact_unit} per ${l.unit}, range ${fmt(p.lower)}–${fmt(p.upper)}. ${p.explanation || ""}`);
      else if (p.status === "declined") notes.push(`AI declined: ${p.explanation || ""}`);
      else notes.push(`AI ${p.status}: ${p.error || ""}`);
    }
    const contribution = l.status === "evidence" ? fmt(l.contribution)
      : l.status === "provisional" ? "≈" + fmt(p.contribution) : "—";
    return h("tr", {},
      h("td", {}, h("div", {}, l.item_id), h("div", { class: "sub" }, `line ${l.line}`)),
      h("td", {}, l.material || h("span", { class: "muted" }, "(empty)"), l.location ? h("div", { class: "sub" }, l.location) : null),
      h("td", { class: "num" }, l.quantity != null ? `${l.quantity} ${l.unit || "?"}` : (l.quantity_raw || "—")),
      h("td", {}, h("span", { class: "status " + l.status }, EBOM_STATUS[l.status])),
      h("td", {}, matchCell),
      h("td", { class: "num" }, l.factor != null ? fmt(l.factor) : "—"),
      h("td", { class: "num" }, contribution),
      h("td", { class: "num" }, l.share_of_evidence != null ? `${(l.share_of_evidence * 100).toFixed(1)}%` : "—"),
      h("td", { style: "min-width:220px" }, notes.length ? h("ul", { style: "margin:0;padding-left:16px" }, notes.map((n) => h("li", {}, n))) : h("span", { class: "muted" }, "—")));
  });
  const c = r.columns;
  return h("div", { class: "card" },
    h("h3", { style: "font-size:15px;margin-bottom:4px" }, "Line details"),
    h("p", { class: "hint", style: "margin:0 0 10px" },
      `Columns used: material = "${c.material}", quantity = "${c.quantity}", unit = ${c.unit ? `"${c.unit}"` : c.unit_from_header ? `"${c.unit_from_header}" (from the quantity header)` : "none"}`
      + `${c.item_id ? `, item = "${c.item_id}"` : ""}${c.location ? `, location = "${c.location}"` : ""}. Matching uses the line's unit as an exact filter.`),
    h("div", { class: "table-wrap" }, h("table", { class: "lines" },
      h("thead", {}, h("tr", {}, ["Item", "Material", "Quantity", "Status", "Matched record", "Factor", "Contribution", "Share", "Notes"].map((t) => h("th", {}, t)))),
      h("tbody", {}, body))));
}

function downloadEbomCsv(r) {
  const cols = ["line", "item_id", "material", "quantity", "unit", "status", "matched_record", "matched_name",
    "factor", "evidence_contribution", "provisional_contribution", "provisional_lower", "provisional_upper",
    "provisional_impact_unit", "warnings", "reasons"];
  const esc = (v) => {
    const t = v == null ? "" : String(v);
    return /[",\n]/.test(t) ? `"${t.replaceAll('"', '""')}"` : t;
  };
  const rows = r.lines.map((l) => [l.line, l.item_id, l.material, l.quantity, l.unit, l.status,
    l.match?.id, l.match?.name, l.factor, l.status === "evidence" ? l.contribution : null,
    l.provisional?.contribution, l.provisional?.lower, l.provisional?.upper, l.provisional?.impact_unit,
    l.warnings.map((w) => w.code).join(" "), l.reasons.join(" ")].map(esc).join(","));
  const blob = new Blob([[cols.join(","), ...rows].join("\n")], { type: "text/csv" });
  const a = h("a", { href: URL.createObjectURL(blob), download: `ebom_${r.indicator.replace(/\W+/g, "_")}.csv` });
  document.body.append(a);
  a.click();
  a.remove();
}

// ---------------------------------------------------------------- hierarchy

async function loadTree(parent, ul) {
  ul.replaceChildren(h("li", { class: "muted" }, "Loading…"));
  const { nodes } = await api("/hierarchy" + (parent ? "?parent=" + encodeURIComponent(parent) : ""));
  ul.replaceChildren(...nodes.map((n) => {
    const children = h("ul", { hidden: true });
    const caret = h("button", { type: "button", class: "caret", "aria-label": "Expand " + n.label, "aria-expanded": "false" }, "▸");
    caret.addEventListener("click", async () => {
      const open = children.hidden;
      children.hidden = !open;
      caret.textContent = open ? "▾" : "▸";
      caret.setAttribute("aria-expanded", open);
      if (open && !children.childElementCount) {
        await loadTree(n.node_id, children);
        if (!children.childElementCount) caret.disabled = true;
      }
    });
    const source = n.node_id.split("/")[0];
    const name = h("button", { type: "button", class: "name", title: "Search this branch" }, n.label);
    name.addEventListener("click", () => {
      showTab("search");
      $("#q").value = n.depth === 0 ? "" : n.label;
      $("#f-source").value = source;
      fillIndicators();
      if (n.depth > 0) search();
      else $("#q").focus();
    });
    return h("li", {}, h("div", { class: "row" }, caret, name, h("span", { class: "count" }, n.record_count.toLocaleString())), children);
  }));
}

init().catch((err) => {
  $("#results").replaceChildren(h("div", { class: "banner error" },
    h("div", {}, h("h3", {}, "Could not reach the LCA RAG server"), h("p", {}, err.message))));
});
