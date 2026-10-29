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
    // no em dashes in the UI, including AI-written text
    el.append(kid instanceof Node ? kid : String(kid).replace(/\s*\u2014\s*/g, ", "));
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

// ---------------------------------------------------------------- motion helpers
// Cards fade and slide up as they enter the viewport; headline numbers count up;
// long requests show stepped progress. CSS disables all of it for reduced motion.
const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
const revealIO = "IntersectionObserver" in window ? new IntersectionObserver((entries) => {
  for (const e of entries) {
    if (!e.isIntersecting) continue;
    e.target.classList.add("in");
    e.target.querySelectorAll("[data-count]").forEach(countUp);
    if (e.target.dataset.count) countUp(e.target);
    revealIO.unobserve(e.target);
  }
}, { rootMargin: "0px 0px -40px 0px" }) : null;

function revealWithin(root) {
  root.querySelectorAll(".card, .tile, .banner, .rec, .sc-result").forEach((el) => {
    if (el.classList.contains("reveal") || el.closest(".drawer")) return;
    if (!revealIO || reduceMotion) { el.querySelectorAll("[data-count]").forEach(countUp); return; }
    el.classList.add("reveal");
    revealIO.observe(el);
  });
}

function countUp(el) {
  if (el.dataset.counted) return;
  el.dataset.counted = "1";
  const target = Number(el.dataset.count);
  const node = [...el.childNodes].find((n) => n.nodeType === Node.TEXT_NODE && n.textContent.trim());
  if (!node || !Number.isFinite(target)) return;
  const final = node.textContent;
  if (reduceMotion) return;
  const start = performance.now(), dur = 700;
  const step = (now) => {
    const t = Math.min(1, (now - start) / dur), k = 1 - Math.pow(1 - t, 3);
    node.textContent = t < 1 ? (Number.isInteger(target) ? String(Math.round(target * k)) : fmt(target * k)) : final;
    if (t < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}

function loadingSteps(container, steps, every = 1400) {
  const items = steps.filter(Boolean).map((t) => h("li", {}, t));
  container.replaceChildren(h("div", { class: "loading", role: "status" }, h("span", { class: "spinner" }), h("ol", {}, items),
    h("div", { class: "skeleton", "aria-hidden": "true" }, h("i"), h("i"), h("i"))));
  let i = 0;
  const mark = () => items.forEach((li, k) => { li.className = k < i ? "done" : k === i ? "now" : ""; });
  mark();
  const timer = setInterval(() => {
    if (!container.isConnected || !container.contains(items[0])) return clearInterval(timer);
    if (i < items.length - 1) { i += 1; mark(); }
  }, every);
}

new MutationObserver((muts) => {
  for (const m of muts) for (const n of m.addedNodes) if (n.nodeType === 1) revealWithin(n.parentElement || n);
}).observe(document.body, { childList: true, subtree: true });

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

// Internal dataset names are never shown; these are the display names.
const SOURCE_LABELS = {
  bottles_processes: "Process records",
  lca_full_rag_data_units: "Impact assessments",
  openlca_flows: "Flow catalogue A",
  usda_flows: "Flow catalogue B",
};
const sourceLabel = (s) => SOURCE_LABELS[s] || "Dataset";
const prettyPath = (path, sep = " › ") => {
  const parts = String(path || "").split(/\s*>\s*|\//);
  return [sourceLabel(parts[0]), ...parts.slice(1)].join(sep);
};

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
    ...groups.map(([s, labels]) => h("optgroup", { label: sourceLabel(s) }, labels.map((l) => option(l)))));
  if ([...sel.options].some((o) => o.value === prev)) sel.value = prev;
}

async function init() {
  const meta = await api("/meta");
  state.meta = meta;

  $("#f-source").replaceChildren(option("", "All data"), ...Object.keys(meta.sources).map((s) => option(s, sourceLabel(s))));
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
  initEbom();  // web/ebom.js
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
  loadingSteps($("#results"), ["Normalising the query", "Routing through the category hierarchy",
    "Ranking candidates with BM25 and rank fusion", "Checking indicator and reference unit",
    body.use_ai ? "Asking the AI only if the evidence is insufficient" : null], 700);
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
      r.route.slice(0, 5).map((n) => h("span", { class: "node", title: `${n.record_count} records` }, prettyPath(n.node_id)))));
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
    h("div", { class: "bignum", "data-count": a.value }, fmt(a.value), " ", h("small", {}, `${a.indicator} per 1 ${a.per_reference_unit}`)),
    h("p", { class: "note", style: "padding:0;margin:8px 0 0" }, a.note),
    h("button", { type: "button", class: "ghost", style: "margin-top:10px", onclick: () => openRecord(a.record_id, a.indicator) },
      "Open record & calculate"));
}

function fallbackCard(fb) {
  const head = [
    h("div", { class: "label" }, "Provisional AI estimate"),
  ];
  const meta = h("dl", { class: "kv" },
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
      h("div", { class: "path" }, prettyPath(c.hierarchy_path)),
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
  $("#d-title").textContent = "Loading…";
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
      h("dt", {}, "Category"), h("dd", {}, prettyPath(rec.hierarchy_path)),
      h("dt", {}, "Location"), h("dd", {}, rec.location || "n/a"),
      h("dt", {}, "Reference unit"), h("dd", {}, rec.reference_unit || "Not stated in source"),
      h("dt", {}, "Stage"), h("dd", {}, rec.stage || "n/a")),
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
      h("h4", {}, `Original indicator labels (${labels.length})`),
      h("table", { class: "impacts" }, h("tbody", {},
        labels.map((l) => h("tr", { class: rec.impacts[l] === 0 ? "zero" : "" }, h("td", {}, l), h("td", {}, fmt(rec.impacts[l]))))))));
  } else {
    parts.push(h("p", { class: "muted" }, "This record carries no impact indicators. It is descriptive evidence only and cannot be used for calculation."));
  }
  parts.push(h("div", {}, h("h4", {}, "Original data row (unmodified)"), h("pre", { class: "raw" }, JSON.stringify(rec.raw, null, 2))));
  $("#d-body").replaceChildren(...parts);
}

// ---------------------------------------------------------------- hierarchy

async function loadTree(parent, ul) {
  ul.replaceChildren(h("li", { class: "muted" }, "Loading…"));
  const { nodes } = await api("/hierarchy" + (parent ? "?parent=" + encodeURIComponent(parent) : ""));
  ul.replaceChildren(...nodes.map((n) => {
    const children = h("ul", { hidden: true });
    const label = n.depth === 0 ? sourceLabel(n.label) : n.label;
    const caret = h("button", { type: "button", class: "caret", "aria-label": "Expand " + label, "aria-expanded": "false" }, "▸");
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
    const name = h("button", { type: "button", class: "name", title: "Search this branch" }, label);
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
