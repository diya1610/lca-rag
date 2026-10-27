"""eBOM (engineering bill of materials) analysis: per-line matching, material- and
component-wise aggregation, and a product grade.

Every line is resolved through a fixed, disclosed ladder:

  1. evidence     - an exact database match (retrieval, abstention and unit rules as rag.py)
  2. proxy        - no exact match (or its factor is implausible): a *similar material* from
                    the same material family in the database, same reference unit
  3. provisional  - no plausible similar material: a validated AI estimate (optional)
  4. not_covered / invalid

The tier of every number is kept and shown. Items are scored like the upstream lca_backend
engine (criticality, toxicity and GHG sub-scores -> composite 0-100 -> grade A-D), with
reference values taken from the data (see SCORING). The product score is the GHG-weighted
mean of item scores, and the share each tier contributes is reported next to it.
what_if() re-runs the pipeline with material swaps for up to three scenarios.

    python ebom.py path/to/bom.csv [--indicator "Climate change"] [--ai --provider groq]

CSV columns are detected case-insensitively:
    material  (required)  material_name | material | part_name | name | description | item
    quantity  (required)  quantity | quantity_kg | qty | weight | weight_kg | mass | mass_kg | amount
    unit      (optional)  unit | uom | units          -- else "_kg" in the quantity header means kg
    component (optional)  component | part | assembly | subassembly (else the material name)
    item id   (optional)  item_id | part_no | part number | id | line
    location  (optional)  location | country | supplier_country | region
Lines without a unit are skipped unless the caller supplies `default_unit`. Mass units
(mg, g, t, lb, oz) are converted to kg explicitly, and the conversion is reported.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import re
import sys
from concurrent.futures import ThreadPoolExecutor

import rag

MAX_LINES = 500
MAX_AI_LINES = 25
AI_WORKERS = 4
# A factor this many times the median for the same indicator and unit is implausible.
# Whole families in the export look per-tonne while labelled "kg" (cement 903, zinc 3200,
# aluminium 2461-3229, copper 797-1323 for Climate change), so such values are never
# used as similar-material proxies and are flagged when matched exactly.
OUTLIER_RATIO = 50
HOTSPOT_SHARE = 0.20
MAX_SCENARIOS = 3
# AI answers are reused for the life of the process, so the dashboard and what-if runs
# show the same provisional values instead of re-asking a non-deterministic model.
_AI_CACHE: dict = {}
AI_CACHE_SIZE = 1000
SISTERS_PER_ITEM = 3

# Scoring reverse-engineered from lca_backend/engine.py: composite = 0.30 criticality +
# 0.35 toxicity + 0.35 GHG; GHG = 0.4 intensity + 0.6 absolute; grade A<=30, B<=60, C<=80, D.
# Upstream hard-coded per-kg references (GHG 12.5, toxicity 150, metal depletion 1.5) that
# its comment calls "95th percentile from bottles_processes", but the data's 95th percentiles
# are about 8.2, 0.44 and 0.016, so upstream toxicity scores were always ~0. Here the
# references are computed as that 95th percentile of plausible per-kg factors.
WEIGHTS = {"criticality": 0.30, "toxicity": 0.35, "ghg": 0.35}
GHG_BLEND = {"intensity": 0.4, "absolute": 0.6}
ABS_GHG_REF_KG = 500.0  # upstream: an item total of 500 kg CO2-eq scores 100
TOX_BLEND = {"Human toxicity": 70, "Metal depletion": 30}
GRADE_BANDS = (("A", 30.0), ("B", 60.0), ("C", 80.0))  # composite score; above: D
GRADE_TEXT = {"A": "low impact", "B": "moderate impact", "C": "high impact", "D": "critical impact"}
CRITICAL_MATERIALS = {  # upstream tiers (EU/US critical raw material lists)
    "high": ["cobalt", "lithium", "rare earth", "neodymium", "dysprosium", "indium", "gallium",
             "germanium", "platinum", "palladium", "iridium", "rhodium", "ruthenium", "osmium",
             "tellurium", "hafnium", "niobium", "tantalum", "tungsten", "antimony", "beryllium",
             "graphite", "magnesium", "silicon metal"],
    "medium": ["copper", "aluminium", "aluminum", "nickel", "chromium", "manganese", "lead",
               "zinc", "tin", "titanium", "vanadium", "molybdenum", "bismuth", "boron", "fluorspar"],
    "low": ["steel", "iron", "glass", "plastic", "polyethylene", "polypropylene", "nylon",
            "rubber", "paper", "wood", "cotton", "wool"],
}
CRITICALITY_SCORES = {"high": 90.0, "medium": 55.0, "low": 20.0, "unknown": 40.0}
# Fallback when no keyword matches (e.g. "bottle-grade PET"), by material family.
FAMILY_CRITICALITY = {"steel": "low", "plastics": "low", "glass": "low", "paper": "low", "wood": "low",
                      "concrete": "low", "aluminium": "medium", "copper": "medium", "zinc": "medium",
                      "lead": "medium", "tin": "medium", "titanium": "medium"}

MASS_TO_KG = {"mg": 1e-6, "g": 1e-3, "gram": 1e-3, "grams": 1e-3, "kg": 1.0, "kgs": 1.0,
              "t": 1e3, "tonne": 1e3, "tonnes": 1e3, "lb": 0.45359237, "lbs": 0.45359237,
              "oz": 0.028349523125}
GHG_TO_KG = {"gco2eq": 1e-3, "kgco2eq": 1.0, "tco2eq": 1e3}

# Material families: keywords that identify a family in a BOM line or a record name.
FAMILY_KEYWORDS = {
    "steel": "steel stainless iron rebar galvanized galvanised inox",
    "aluminium": "aluminium aluminum alu",
    "copper": "copper brass bronze",
    "zinc": "zinc",
    "lead": "lead",
    "tin": "tin",
    "titanium": "titanium",
    "plastics": "plastic plastics polymer polyethylene polypropylene polystyrene polyester "
                "polycarbonate polyamide nylon pet hdpe ldpe pp ps pvc abs pmma acrylic resin "
                "epoxy pe",
    "glass": "glass fiberglass fibreglass",
    "paper": "paper cardboard carton cartonboard corrugated kraft paperboard pulp",
    "wood": "wood timber plywood mdf osb spruce pine beech oak",
    "concrete": "concrete cement mortar",
}
KEYWORD_FAMILY = {kw: fam for fam, kws in FAMILY_KEYWORDS.items() for kw in kws.split()}
TAG_FAMILY = {"polyethylene": "plastics", "plastic": "plastics", "cement": "concrete"}
# Processes from these stages describe disposal or transport, not a material, so they are
# never used as a similar-material proxy.
NON_MATERIAL_STAGES = {"End-of-Life", "Transport & Packaging"}

MATERIAL_COLS = ["material_name", "material", "part_name", "part name", "name", "description",
                 "item", "component"]
QUANTITY_COLS = ["quantity", "quantity_kg", "qty", "weight", "weight_kg", "mass", "mass_kg",
                 "amount"]
UNIT_COLS = ["unit", "uom", "units"]
COMPONENT_COLS = ["component", "part", "assembly", "subassembly", "sub-assembly", "part_name",
                  "part name"]
ID_COLS = ["item_id", "part_no", "part no", "part number", "part_number", "id", "line"]
COST_COLS = ["cost", "unit_cost", "price", "cost_usd", "cost_eur", "cost_inr"]
LOCATION_COLS = ["location", "country", "supplier_country", "region"]


class EbomError(ValueError):
    pass


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def _pick(header_map: dict, candidates: list[str], exclude=()) -> str | None:
    for cand in candidates:
        col = header_map.get(cand)
        if col is not None and col not in exclude:
            return col
    return None


def parse_csv(text: str) -> tuple[list[dict], dict]:
    """Return (lines, column mapping). Raises EbomError with a readable message."""
    text = text.lstrip("﻿")
    if not text.strip():
        raise EbomError("The file is empty.")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    headers = [h for h in (reader.fieldnames or []) if h is not None]
    header_map = {h.strip().lower(): h for h in headers}
    mat = _pick(header_map, MATERIAL_COLS)
    qty = _pick(header_map, QUANTITY_COLS)
    if mat is None or qty is None:
        raise EbomError(f"Need a material column and a quantity column; found {headers}. "
                        "Name them e.g. 'material' and 'quantity' (plus 'unit').")
    unit_col = _pick(header_map, UNIT_COLS)
    comp_col = _pick(header_map, COMPONENT_COLS, exclude=(mat,))
    id_col = _pick(header_map, ID_COLS, exclude=(mat, comp_col))
    loc_col = _pick(header_map, LOCATION_COLS)
    cost_col = _pick(header_map, COST_COLS)
    header_unit = "kg" if unit_col is None and qty.strip().lower().endswith("kg") else None
    columns = {"material": mat, "quantity": qty, "unit": unit_col, "component": comp_col,
               "item_id": id_col, "location": loc_col, "cost": cost_col,
               "unit_from_header": header_unit}

    def cell(row, col):
        return (row.get(col) or "").strip() if col else ""

    lines = []
    for n, row in enumerate(reader, start=1):
        if not any((v or "").strip() for v in row.values() if isinstance(v, str)):
            continue  # blank row
        if len(lines) >= MAX_LINES:
            raise EbomError(f"Too many lines (max {MAX_LINES}).")
        material = cell(row, mat)
        lines.append({
            "line": n,
            "item_id": cell(row, id_col) or str(n),
            "component": cell(row, comp_col) or material or f"line {n}",
            "material": material,
            "quantity_raw": cell(row, qty),
            "unit": cell(row, unit_col) or header_unit,
            "location": cell(row, loc_col) or None,
            "cost": cell(row, cost_col) or None,
        })
    if not lines:
        raise EbomError("No BOM lines found under the header row.")
    return lines, columns


def _quantity(raw: str):
    """Return (value, reason). A comma is never guessed: "0,5" and "12,75" are decimal
    commas, "1,234.5" uses a thousands separator, and "1,500" (1.5 or 1500?) is ambiguous."""
    raw = raw.strip()
    if not raw:
        return None, "invalid_quantity"
    if "," in raw:
        if "." in raw:
            raw = raw.replace(",", "")
        elif raw.count(",") == 1 and len(raw.split(",")[1]) != 3:
            raw = raw.replace(",", ".")
        else:
            return None, "ambiguous_quantity"
    try:
        value = float(raw)
    except ValueError:
        return None, "invalid_quantity"
    if not math.isfinite(value) or value <= 0:
        return None, "invalid_quantity"
    return value, None


def _unit_key(unit: str | None) -> str:
    """Spelling-insensitive key, e.g. "kg CO₂‑eq" == "kg CO2 eq". It never converts g to kg."""
    t = (unit or "").lower().replace("₂", "2")
    t = re.sub(r"[\s\-‐-―_.]", "", t)
    return re.sub(r"(equivalents?|equiv|eq|e)$", "eq", t)


def family_of(text: str) -> str | None:
    """Family of the head material: the last family keyword in the text
    ("glass fibre reinforced plastic" -> plastics, "stainless steel bracket" -> steel)."""
    found = None
    for tok in rag.tokens(text):
        found = KEYWORD_FAMILY.get(tok, found)
    return found


def is_climate(indicator: str) -> bool:
    t = indicator.lower()
    return "climate" in t or "global warming" in t or "gwp" in t


def grade_for(score: float | None) -> str | None:
    if score is None:
        return None
    for letter, bound in GRADE_BANDS:
        if score <= bound:
            return letter
    return "D"


def criticality(material: str, family: str | None = None) -> tuple[str, float]:
    name = material.lower()
    for tier, keywords in CRITICAL_MATERIALS.items():
        if any(kw in name for kw in keywords):
            return tier, CRITICALITY_SCORES[tier]
    tier = FAMILY_CRITICALITY.get(family, "unknown")
    return tier, CRITICALITY_SCORES[tier]


def _clip(x: float) -> float:
    return max(0.0, min(100.0, x))


# ---------------------------------------------------------------------------
# Similar-material catalogue
# ---------------------------------------------------------------------------

class Catalog:
    """Records with indicators, their material family, plausibility, and scoring references."""

    def __init__(self, engine: rag.Engine, source: str | None, indicator: str):
        self.engine, self.source, self.indicator = engine, source, indicator
        sql = ("SELECT id, name, unit, stage, location, raw_json, impacts_json FROM records "
               "WHERE impacts_json != '{}' AND unit IS NOT NULL")
        rows = engine._all(sql + (" AND source = ?" if source else ""), (source,) if source else ())
        self.records, by_unit_label = [], {}
        for r in rows:
            impacts = json.loads(r["impacts_json"])
            tags = [t.strip().lower() for t in json.loads(r["raw_json"]).get("material_tags", "").split(",")]
            fam = next((TAG_FAMILY.get(t, t) for t in tags if TAG_FAMILY.get(t, t) in FAMILY_KEYWORDS), None)
            self.records.append({"id": r["id"], "name": r["name"], "unit": r["unit"], "stage": r["stage"],
                                 "location": r["location"], "impacts": impacts,
                                 "factor": impacts.get(indicator),
                                 # name fallback uses only the head before the first comma, so
                                 # "Chlorine, production mix for PVC production" is not a plastic
                                 "family": fam or family_of(r["name"].split(",")[0])})
            for label, v in impacts.items():
                if v:
                    by_unit_label.setdefault((r["unit"], label), []).append(abs(v))
        self.median = {k: sorted(v)[len(v) // 2] for k, v in by_unit_label.items() if len(v) >= 10}
        self.by_id = {r["id"]: r for r in self.records}
        # scoring references: 95th percentile of plausible positive per-kg factors
        self.refs = {}
        for label in ("Climate change", *TOX_BLEND):
            vals = sorted(r["impacts"][label] for r in self.records
                          if r["unit"] == "kg" and r["impacts"].get(label, 0) > 0
                          and not self.implausible(r["impacts"][label], "kg", label))
            if len(vals) >= 10:
                self.refs[label] = vals[int(0.95 * (len(vals) - 1))]

    def implausible(self, factor: float, unit: str, label: str | None = None) -> float | None:
        """Return the ratio to the median when the factor is implausibly large."""
        median = self.median.get((unit, label or self.indicator))
        if median and abs(factor) > OUTLIER_RATIO * median:
            return abs(factor) / median
        return None

    def family_pool(self, family: str | None, unit: str, exclude=()) -> list[dict]:
        if family is None:
            return []
        return [r for r in self.records
                if r["family"] == family and r["unit"] == unit and r["factor"] is not None
                and r["factor"] > 0 and r["stage"] not in NON_MATERIAL_STAGES
                and r["id"] not in exclude and not self.implausible(r["factor"], unit)]

    def proxy(self, material: str, unit: str, family: str | None, exclude: str | None = None):
        pool = self.family_pool(family, unit, exclude=(exclude,))
        if not pool:
            return None
        pool_ids = {r["id"] for r in pool}
        hits = self.engine.search(material, {"source": self.source, "unit": unit}, top_k=50)["candidates"]
        chosen, kind = None, "family typical"
        for c in hits:
            if c["id"] in pool_ids and c["term_coverage"] >= 0.5:
                chosen, kind = self.by_id[c["id"]], "similar record"
                break
        if chosen is None:
            chosen = sorted(pool, key=lambda r: r["factor"])[len(pool) // 2]  # family median
        factors = [r["factor"] for r in pool]
        return {"id": chosen["id"], "name": chosen["name"], "location": chosen["location"],
                "factor": chosen["factor"], "family": family, "kind": kind,
                "family_size": len(pool), "family_min": min(factors), "family_max": max(factors),
                "citation": self.engine.record(chosen["id"])["citation"]}

    def sisters(self, family: str | None, unit: str, factor: float, current_id: str | None) -> list[dict]:
        """Lower-impact records of the same family and unit ("sister materials")."""
        out, seen = [], set()
        for r in sorted(self.family_pool(family, unit, exclude=(current_id,)), key=lambda r: r["factor"]):
            if r["factor"] >= factor or r["name"] in seen:
                continue
            seen.add(r["name"])
            out.append({"id": r["id"], "name": r["name"], "location": r["location"], "factor": r["factor"],
                        "reduction": 1 - r["factor"] / factor if factor else None})
            if len(out) >= SISTERS_PER_ITEM:
                break
        return out


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

def analyse(engine: rag.Engine, text: str, indicator: str = "Climate change",
            source: str | None = "bottles_processes", default_unit: str | None = None,
            use_ai: bool = False, provider: str | None = None, fallback=None,
            overrides: dict | None = None, memo: dict | None = None) -> dict:
    """`overrides` maps item_id -> replacement material (what-if). `memo` caches line
    resolutions and AI answers across calls in one request."""
    lines, columns = parse_csv(text)
    memo = {} if memo is None else memo
    if overrides:
        known = {ln["item_id"] for ln in lines}
        unknown = sorted(set(overrides) - known)
        if unknown:
            raise EbomError(f"Unknown item id(s) in scenario: {', '.join(unknown)}")
        for ln in lines:
            if ln["item_id"] in overrides:
                ln["original_material"] = ln["material"]
                ln["material"] = overrides[ln["item_id"]].strip()
    catalog = memo.get(("catalog", source, indicator)) or Catalog(engine, source, indicator)
    memo[("catalog", source, indicator)] = catalog
    climate = is_climate(indicator)

    results = []
    for ln in lines:
        key = ("line", ln["material"], ln["quantity_raw"], ln["unit"], default_unit, indicator, source)
        if key not in memo:
            memo[key] = _resolve_line(engine, catalog, ln, indicator, source, default_unit)
        identity = ("line", "item_id", "component", "material", "quantity_raw", "location", "cost",
                    "original_material")
        results.append({**memo[key], **{k: ln[k] for k in identity if k in ln}})

    pending = [r for r in results if r["status"] == "not_covered"]
    if use_ai and pending:
        fallback_is_default = fallback is None
        if fallback is None:
            import ai_fallback
            fallback = ai_fallback.estimate

        def run(r):
            key = ("ai", r["material"], r["unit"], indicator, source, provider)
            cache = _AI_CACHE if fallback_is_default else memo
            if key not in cache:
                answer = fallback(query=r["material"], indicator=indicator, reference_unit=r["unit"],
                                  constraints={"source": source, "unit": r["unit"], "location": r["location"]},
                                  context=r.get("_context", [])[:5], provider=provider)
                if answer.get("status") == "unavailable":
                    r["provisional"] = _provisional(answer, r["quantity"], climate)
                    return  # do not cache outages
                if len(cache) >= AI_CACHE_SIZE:
                    cache.pop(next(iter(cache)))
                cache[key] = answer
            r["provisional"] = _provisional(cache[key], r["quantity"], climate)
            if r["provisional"]["contribution"] is not None:
                r.update(status="provisional", tier="provisional",
                         impact=r["provisional"]["impact_kg"] if climate else None)

        with ThreadPoolExecutor(max_workers=AI_WORKERS) as pool:
            list(pool.map(run, pending[:MAX_AI_LINES]))
        for r in pending[MAX_AI_LINES:]:
            r["provisional"] = {"status": "skipped", "error": f"AI limited to {MAX_AI_LINES} lines"}
    for r in results:
        r.pop("_context", None)

    _score_lines(results, catalog, climate)
    product = _product(results, climate)
    recs = _recommendations(results, catalog) if climate else []
    return {
        "indicator": indicator,
        "source": source,
        "default_unit": default_unit,
        "climate": climate,
        "columns": columns,
        "product": product,
        "components": _aggregate(results, "component", climate),
        "materials": _aggregate(results, "material_key", climate),
        "lines": results,
        "recommendations": recs,
        "scoring": {
            "weights": WEIGHTS, "ghg_blend": GHG_BLEND, "toxicity_blend": TOX_BLEND,
            "abs_ghg_ref_kg": ABS_GHG_REF_KG, "references_p95_per_kg": catalog.refs,
            "grade_bands": [{"grade": g, "max": b} for g, b in GRADE_BANDS] + [{"grade": "D", "max": None}],
            "hotspot_share": HOTSPOT_SHARE,
            "note": "Formula and weights follow lca_backend/engine.py. Per-kg references are the "
                    "95th percentile of plausible dataset factors (upstream's hard-coded values "
                    "were 12.5 / 150 / 1.5 and left toxicity near zero).",
        },
        "note": "Evidence uses exact source records; proxies use a similar material from the "
                "same family in the database; provisional values are AI estimates. Each "
                "number keeps its tier. Scores combine the tiers and report their shares.",
    }


def _resolve_line(engine, catalog: Catalog, ln: dict, indicator: str, source, default_unit) -> dict:
    qty, qty_problem = _quantity(ln["quantity_raw"])
    unit_in = ln["unit"] or default_unit
    res = {"material_key": rag.normalize_name(ln["material"]) or ln["material"],
           "quantity": qty, "unit": unit_in, "unit_source": "file" if ln["unit"] else ("default" if unit_in else None),
           "converted_from": None, "mass_kg": None, "family": family_of(ln["material"]),
           "status": None, "tier": None, "reasons": [], "warnings": [], "match": None, "record_id": None,
           "proxy": None, "provisional": None, "factor": None, "contribution": None, "impact": None}
    if not ln["material"]:
        return {**res, "status": "invalid", "reasons": ["missing_material"]}
    if qty is None:
        return {**res, "status": "invalid", "reasons": [qty_problem]}
    if unit_in is None:
        return {**res, "status": "invalid", "reasons": ["missing_unit"]}
    to_kg = MASS_TO_KG.get(unit_in.lower())
    if to_kg is not None:
        if unit_in != "kg":
            res["converted_from"] = {"quantity": qty, "unit": unit_in}
        qty, res["quantity"], res["unit"] = qty * to_kg, qty * to_kg, "kg"
        res["mass_kg"] = qty
    unit = res["unit"]

    out = engine.query(ln["material"], {"source": source, "unit": unit}, indicator, top_k=4)
    cands = out["candidates"]
    res["_context"] = cands
    if cands:
        c = cands[0]
        res["match"] = {"id": c["id"], "name": c["name"], "location": c["location"],
                        "citation": c["citation"], "term_coverage": c["term_coverage"]}
    res["reasons"] = list(out["reasons"])

    exact_id = None
    if out["status"] == "ok" and out["answer"]:
        exact_id = out["answer"]["record_id"]
        factor = out["answer"]["value"]
        ratio = catalog.implausible(factor, unit)
        if not ratio:
            calc = engine.calculate(exact_id, indicator, qty, unit)
            return {**res, "status": "evidence", "tier": "evidence", "factor": factor, "record_id": exact_id,
                    "contribution": calc["result"], "impact": calc["result"]}
        res["warnings"].append({
            "code": "implausible_factor", "record_id": exact_id, "factor": factor,
            "message": f"The exact match {exact_id} has factor {factor:.4g}, {ratio:.0f}x the median "
                       f"for {indicator} per {unit}; the source likely records it per tonne or per "
                       "item. It is not used."})
        res["reasons"] = ["exact_factor_implausible"]

    family = res["family"] or (catalog.by_id.get(exact_id) or {}).get("family")
    proxy = catalog.proxy(ln["material"], unit, family, exclude=exact_id)
    if proxy:
        contribution = proxy["factor"] * qty
        return {**res, "status": "proxy", "tier": "proxy", "family": family, "proxy": proxy,
                "record_id": proxy["id"], "factor": proxy["factor"], "contribution": contribution,
                "impact": contribution,
                "proxy_range": [proxy["family_min"] * qty, proxy["family_max"] * qty]}
    if family:
        res["reasons"].append("no_plausible_family_member")
    return {**res, "status": "not_covered", "family": family}


def _provisional(fb: dict, qty: float, climate: bool) -> dict:
    out = {k: fb.get(k) for k in ("status", "provider", "model", "label", "error", "generated_at")}
    out.update(contribution=None, lower=None, upper=None, value=None, impact_unit=None,
               impact_kg=None, explanation=None, assumptions=[], missing_information=[])
    est = fb.get("estimate")
    if est:
        out.update(explanation=est["explanation"], assumptions=est["assumptions"],
                   missing_information=est["missing_information"])
        if est["can_estimate"]:
            out.update(value=est["value"], impact_unit=est["impact_unit"],
                       contribution=est["value"] * qty, lower=est["lower"] * qty,
                       upper=est["upper"] * qty, system_boundary=est["system_boundary"])
            to_kg = GHG_TO_KG.get(_unit_key(est["impact_unit"])) if climate else None
            if to_kg is not None:  # g / kg / t CO2-eq are the same quantity in different units
                out["impact_kg"] = out["contribution"] * to_kg
                out["factor_kg"] = est["value"] * to_kg
            elif climate:
                out["contribution"] = None
                out["status"] = "unusable_unit"
                out["error"] = f"impact unit {est['impact_unit']!r} is not a CO2-eq mass unit"
    return out


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def _confidence(r: dict) -> str | None:
    if r["impact"] is None:
        return None
    if r["status"] == "evidence":
        return "high" if (r["match"] or {}).get("term_coverage", 0) >= 1.0 else "medium"
    if r["status"] == "proxy" and r["proxy"]["kind"] == "similar record":
        return "medium"
    return "low"


def _score_lines(results: list[dict], catalog: Catalog, climate: bool) -> None:
    total = sum(r["impact"] for r in results if r["impact"] is not None)
    ranked = sorted((r for r in results if r["impact"] is not None), key=lambda r: -r["impact"])
    for rank, r in enumerate(ranked, 1):
        r["rank"] = rank
    for r in results:
        r.setdefault("rank", None)
        r["confidence"] = _confidence(r)
        r["share"] = r["impact"] / total if total and r["impact"] is not None else None
        r["hotspot"] = bool(r["share"] and r["share"] > HOTSPOT_SHARE)
        r["scores"] = None
        if not climate or r["impact"] is None:
            r["grade"] = None
            continue
        is_mass = r["mass_kg"] is not None
        sub = {}
        tier, sub["criticality"] = criticality(r["material"], r["family"])
        if not is_mass:
            sub["criticality"] = None  # energy or service inputs are not materials
        ref = catalog.refs.get("Climate change")
        intensity = (r["impact"] / r["mass_kg"]) if is_mass else None
        abs_score = _clip(r["impact"] / ABS_GHG_REF_KG * 100)
        if intensity is not None and ref:
            sub["ghg"] = GHG_BLEND["intensity"] * _clip(intensity / ref * 100) + GHG_BLEND["absolute"] * abs_score
        else:
            sub["ghg"] = abs_score
        impacts = (catalog.by_id.get(r["record_id"]) or {}).get("impacts") if r["record_id"] else None
        if is_mass and impacts and all(l in impacts and l in catalog.refs for l in TOX_BLEND):
            sub["toxicity"] = _clip(sum(w * impacts[l] / catalog.refs[l] for l, w in TOX_BLEND.items()))
        else:
            sub["toxicity"] = None  # AI estimates cover only the requested indicator
        avail = {k: v for k, v in sub.items() if v is not None}
        weight = sum(WEIGHTS[k] for k in avail)
        composite = sum(WEIGHTS[k] * v for k, v in avail.items()) / weight
        r["scores"] = {**{k: (round(v, 2) if v is not None else None) for k, v in sub.items()},
                       "composite": round(composite, 2), "criticality_tier": tier if is_mass else None,
                       "intensity": intensity, "reweighted": len(avail) < 3}
        r["grade"] = grade_for(composite)


def _tiers(rows) -> dict:
    t = {"evidence": 0.0, "proxy": 0.0, "provisional": 0.0}
    for r in rows:
        if r["impact"] is not None:
            t[r["tier"]] += r["impact"]
    return t


def _weighted(rows, key) -> float | None:
    pairs = [(r["impact"], r["scores"][key]) for r in rows
             if r["scores"] and r["scores"].get(key) is not None and r["impact"] and r["impact"] > 0]
    w = sum(i for i, _ in pairs)
    return sum(i * v for i, v in pairs) / w if w else None


def _aggregate(results: list[dict], key: str, climate: bool) -> list[dict]:
    groups: dict[str, list] = {}
    for r in results:
        if r["status"] != "invalid":
            groups.setdefault(r[key], []).append(r)
    total = sum(r["impact"] for r in results if r["impact"] is not None) or None
    out = []
    for name, rows in groups.items():
        tiers = _tiers(rows)
        impact = sum(tiers.values())
        covered_mass = sum(r["mass_kg"] for r in rows if r["mass_kg"] and r["impact"] is not None)
        mass = sum(r["mass_kg"] for r in rows if r["mass_kg"])
        score = _weighted(rows, "composite")
        out.append({
            "name": rows[0]["component"] if key == "component" else rows[0]["material"],
            "lines": len(rows),
            "items": [r["item_id"] for r in rows],
            "materials": sorted({r["material"] for r in rows}),
            "components": sorted({r["component"] for r in rows}),
            "family": rows[0]["family"],
            "mass_kg": mass or None,
            "impact": impact if any(r["impact"] is not None for r in rows) else None,
            "tiers": tiers,
            "share": impact / total if total else None,
            "intensity": impact / covered_mass if covered_mass and climate else None,
            "score": score,
            "grade": grade_for(score),
            "uncovered_lines": sum(r["impact"] is None for r in rows),
            "segments": [{"material": r["material"], "item_id": r["item_id"], "tier": r["tier"],
                          "impact": r["impact"]} for r in rows if r["impact"] is not None],
        })
    mass_total = sum(g["mass_kg"] or 0 for g in out)
    for g in out:
        g["mass_share"] = (g["mass_kg"] or 0) / mass_total if mass_total else None
        g["hotspot"] = bool(g["share"] and g["share"] > HOTSPOT_SHARE)
    return sorted(out, key=lambda g: -(g["impact"] or 0))


def _product(results: list[dict], climate: bool) -> dict:
    tiers = _tiers(results)
    total = sum(tiers.values())
    scored = [r for r in results if r["impact"] is not None]
    mass_total = sum(r["mass_kg"] for r in results if r["mass_kg"])
    mass_covered = sum(r["mass_kg"] for r in scored if r["mass_kg"])
    mass_cov = mass_covered / mass_total if mass_total else None
    score = _weighted(results, "composite")
    counts = {s: sum(r["status"] == s for r in results)
              for s in ("evidence", "proxy", "provisional", "not_covered", "invalid")}
    conf = {c: sum(r["confidence"] == c for r in scored) for c in ("high", "medium", "low")}
    ev_share = tiers["evidence"] / total if total else 0.0
    if ev_share >= 0.8 and (mass_cov or 0) >= 0.9:
        confidence = "high"
    elif ev_share + tiers["proxy"] / (total or 1) >= 0.5 and (mass_cov or 0) >= 0.7:
        confidence = "medium"
    else:
        confidence = "low"
    hotspots = sorted((r for r in results if r["hotspot"]), key=lambda r: -r["impact"])
    return {
        "lines": len(results),
        "counts": counts,
        "flagged_lines": sum(any(w["code"] == "implausible_factor" for w in r["warnings"]) for r in results),
        "total": total if scored else None,
        "tiers": tiers,
        "tier_shares": {k: (v / total if total else 0.0) for k, v in tiers.items()},
        "mass_total_kg": mass_total or None,
        "mass_covered_kg": mass_covered or None,
        "mass_coverage": mass_cov,
        "intensity": total / mass_covered if climate and mass_covered else None,
        "score": score,
        "grade": grade_for(score),
        "grade_text": GRADE_TEXT.get(grade_for(score)),
        "subscores": {k: _weighted(results, k) for k in WEIGHTS},
        "hotspots": [{"item_id": r["item_id"], "component": r["component"], "material": r["material"],
                      "impact": r["impact"], "share": r["share"]} for r in hotspots],
        "match_confidence": conf,
        "high_confidence_share": conf["high"] / len(scored) if scored else None,
        "confidence": confidence,
        "impact_unit": "kg CO2-eq (assumed for source factors; the export does not state it)" if climate else None,
    }


def _recommendations(results: list[dict], catalog: Catalog) -> list[dict]:
    """Sister materials for hotspots and high-scoring items, plus rule-based advice."""
    recs = []
    for r in sorted((r for r in results if r["impact"] is not None), key=lambda r: -r["impact"]):
        priority = r["hotspot"] or (r["scores"] and r["scores"]["composite"] > GRADE_BANDS[1][1])
        factor = r["factor"] if r["status"] != "provisional" else r["provisional"].get("factor_kg")
        sisters = []
        if r["unit"] and factor and r["family"]:
            sisters = catalog.sisters(r["family"], r["unit"], factor, r["record_id"])
            for s in sisters:
                s["impact"] = s["factor"] * r["quantity"]
                s["saving"] = r["impact"] - s["impact"]
                s["cost"] = None  # the LCA exports carry no cost data
        advice = []
        if r["scores"] and r["scores"]["criticality_tier"] == "high":
            advice.append("Critical raw material: assess supply risk and substitutes.")
        if r["status"] in ("proxy", "provisional"):
            advice.append("Value is not from an exact database record; confirm with supplier or primary data.")
        if r["hotspot"]:
            advice.append(f"Carbon hotspot: {r['share'] * 100:.0f}% of product GHG; prioritise in redesign.")
        if not (priority or sisters) or not (sisters or advice):
            continue
        recs.append({"item_id": r["item_id"], "component": r["component"], "material": r["material"],
                     "hotspot": r["hotspot"], "impact": r["impact"], "share": r["share"],
                     "current_cost": r.get("cost"), "sisters": sisters, "advice": advice})
    return recs[:12]


# ---------------------------------------------------------------------------
# What-if scenarios
# ---------------------------------------------------------------------------

def _summary(a: dict) -> dict:
    p = a["product"]
    return {"total": p["total"], "score": p["score"], "grade": p["grade"], "intensity": p["intensity"],
            "hotspots": len(p["hotspots"]), "counts": p["counts"], "tier_shares": p["tier_shares"],
            "subscores": p["subscores"]}


def what_if(engine: rag.Engine, text: str, scenarios: list[dict], indicator: str = "Climate change",
            source: str | None = "bottles_processes", default_unit: str | None = None,
            use_ai: bool = False, provider: str | None = None, fallback=None, explainer=None) -> dict:
    """Re-run the full pipeline for up to three scenarios of material swaps."""
    if not scenarios or len(scenarios) > MAX_SCENARIOS:
        raise EbomError(f"Give between 1 and {MAX_SCENARIOS} scenarios.")
    memo: dict = {}
    base = analyse(engine, text, indicator, source, default_unit, use_ai, provider, fallback, memo=memo)
    base_lines = {l["item_id"]: l for l in base["lines"]}
    out = []
    for i, sc in enumerate(scenarios, 1):
        swaps = {s["item_id"]: s["material"] for s in sc.get("swaps", []) if s.get("item_id") and s.get("material", "").strip()}
        if not swaps:
            raise EbomError(f"Scenario {i} has no material swaps.")
        res = analyse(engine, text, indicator, source, default_unit, use_ai, provider, fallback,
                      overrides=swaps, memo=memo)
        b, s = _summary(base), _summary(res)
        delta = (s["total"] - b["total"]) if s["total"] is not None and b["total"] is not None else None
        changes = []
        for l in res["lines"]:
            if l["item_id"] in swaps:
                old = base_lines[l["item_id"]]
                changes.append({"item_id": l["item_id"], "component": l["component"],
                                "from": old["material"], "to": l["material"],
                                "from_impact": old["impact"], "to_impact": l["impact"],
                                "from_tier": old["tier"], "to_tier": l["tier"], "to_status": l["status"],
                                "to_record": (l["proxy"] or l["match"] or {}).get("name") if l["tier"] else None})
        scenario = {"name": sc.get("name") or f"Scenario {i}", "swaps": swaps, "summary": s,
                    "delta": delta, "delta_pct": delta / b["total"] if delta is not None and b["total"] else None,
                    "grade_change": [b["grade"], s["grade"]], "changes": changes, "explanation": None}
        scenario["explanation"] = _explain(scenario, b, indicator, use_ai, provider, explainer)
        out.append(scenario)
    return {"baseline": _summary(base), "scenarios": out, "indicator": indicator}


def _explain(sc: dict, base: dict, indicator: str, use_ai: bool, provider, explainer) -> dict:
    """A short design note: AI-written when enabled and available, else a deterministic one."""
    def r4(x):
        return None if x is None else float(f"{x:.4g}")

    def brief(s):
        return {"total_kg_co2eq": r4(s["total"]), "score_0_100": r4(s["score"]), "grade": s["grade"],
                "subscores_0_100_higher_is_worse": {k: r4(v) for k, v in s["subscores"].items()}}
    facts = {"indicator": indicator, "baseline": brief(base), "scenario": brief(sc["summary"]),
             "change_kg_co2eq": r4(sc["delta"]),
             "change_percent": None if sc["delta_pct"] is None else round(sc["delta_pct"] * 100, 1),
             "item_changes": [{"component": c["component"], "from": c["from"], "to": c["to"],
                               "from_kg_co2eq": r4(c["from_impact"]), "to_kg_co2eq": r4(c["to_impact"]),
                               "data_basis_of_new_value": c["to_tier"]} for c in sc["changes"]]}
    if use_ai:
        if explainer is None:
            import ai_fallback
            explainer = ai_fallback.explain
        result = explainer(facts, provider=provider)
        if result.get("status") == "ok":
            return {"source": "ai", "text": result["text"], "provider": result.get("provider"),
                    "model": result.get("model"), "label": "AI-written explanation; check against the numbers."}
    parts = []
    for c in sc["changes"]:
        if c["to_impact"] is None:
            parts.append(f"{c['from']} -> {c['to']} ({c['component']}): no reliable data for the new material, "
                         "so its impact is excluded.")
        elif c["from_impact"] is not None:
            d = c["to_impact"] - c["from_impact"]
            parts.append(f"{c['from']} -> {c['to']} ({c['component']}): {d:+.4g} kg CO2-eq "
                         f"({c['to_tier']} value).")
    if sc["delta"] is not None:
        parts.append(f"Product total changes by {sc['delta']:+.4g} kg CO2-eq"
                     + (f" ({sc['delta_pct'] * 100:+.1f}%)" if sc["delta_pct"] is not None else "")
                     + f"; grade {sc['grade_change'][0]} -> {sc['grade_change'][1]}.")
    return {"source": "rules", "text": " ".join(parts), "label": "Generated from the numbers above."}


def main() -> int:

    parser = argparse.ArgumentParser(description="Analyse an eBOM CSV against the LCA index.")
    parser.add_argument("csv_path")
    parser.add_argument("--indicator", default="Climate change")
    parser.add_argument("--source", default="bottles_processes")
    parser.add_argument("--default-unit")
    parser.add_argument("--ai", action="store_true")
    parser.add_argument("--provider", choices=["groq", "claude", "ollama"])
    args = parser.parse_args()
    with open(args.csv_path, encoding="utf-8-sig", newline="") as fh:
        text = fh.read()
    try:
        out = analyse(rag.Engine(), text, args.indicator, args.source or None, args.default_unit,
                      args.ai, args.provider)
    except EbomError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
