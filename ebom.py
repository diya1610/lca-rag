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
import hashlib
import io
import json
import math
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import rag

MAX_LINES = 500
MAX_AI_LINES = 25
AI_WORKERS = 2  # free-tier providers rate-limit bursts
# A factor this many times the median for the same indicator and unit is implausible.
# Whole families in the export look per-tonne while labelled "kg" (cement 903, zinc 3200,
# aluminium 2461-3229, copper 797-1323 for Climate change), so such values are never
# used as similar-material proxies and are flagged when matched exactly.
OUTLIER_RATIO = 50
HOTSPOT_SHARE = 0.20
MAX_SCENARIOS = 3
# AI answers are saved and reused (also across restarts), so the dashboard, what-if runs
# and later demos show the same provisional values instead of re-asking a
# non-deterministic model. Clear with clear_ai_cache() or POST /ebom/clear-ai-cache.
AI_CACHE_PATH = rag.INDEX_DIR / "ai_cache.json"
AI_CACHE_SIZE = 1000
_AI_CACHE: dict | None = None
_AI_LOCK = threading.Lock()
CORRECTIONS_PATH = rag.DATA_DIR / "corrections.json"
GRID_REFERENCES_PATH = rag.DATA_DIR / "grid_references.json"
UPSTREAM_REFS = {"Climate change": 12.5, "Human toxicity": 150.0, "Metal depletion": 1.5}
SCORING_MODES = ("data", "upstream")
GRADE_BASES = ("combined", "ghg")
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
                      "concrete": "low", "cement": "low", "aluminium": "medium", "copper": "medium", "zinc": "medium",
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
    "concrete": "concrete",
    # cement is ~7x more carbon-intensive than concrete, so it is not a concrete proxy
    "cement": "cement mortar clinker",
}
KEYWORD_FAMILY = {kw: fam for fam, kws in FAMILY_KEYWORDS.items() for kw in kws.split()}
TAG_FAMILY = {"polyethylene": "plastics", "plastic": "plastics"}
# Processes from these stages describe disposal or transport, not a material, so they are
# never used as a similar-material proxy.
NON_MATERIAL_STAGES = {"End-of-Life", "Transport & Packaging"}

MATERIAL_COLS = ["material_name", "material", "materials", "main material", "main materials",
                 "part_name", "part name", "name", "description", "item", "component"]
QUANTITY_COLS = ["quantity", "quantity_kg", "qty", "weight", "weight_kg", "mass", "mass_kg",
                 "amount"]
UNIT_COLS = ["unit", "uom", "units"]
COMPONENT_COLS = ["component", "part", "assembly", "subassembly", "sub-assembly", "part_name",
                  "part name"]
ID_COLS = ["item_id", "part_no", "part no", "part number", "part_number", "id", "line"]
COST_COLS = ["cost", "unit_cost", "price", "cost_usd", "cost_eur", "cost_inr"]
LOCATION_COLS = ["location", "country", "supplier_country", "region"]
ENERGY_TO_KWH = {"kwh": 1.0, "wh": 1e-3, "mwh": 1e3, "mj": 1 / 3.6, "gj": 1000 / 3.6}
# Bare material words that name a whole family. A BOM line saying only "Glass" or "Steel"
# gets the family's typical value rather than whichever specific record ranks first.
GENERIC_MATERIALS = {"steel", "iron", "glass", "plastic", "plastics", "polymer", "paper", "wood",
                     "copper", "aluminium", "aluminum", "zinc", "tin", "lead", "titanium",
                     "concrete", "cement", "resin", "cardboard"}
SPLIT_MODES = ("ranked", "equal", "ai")


class EbomError(ValueError):
    pass


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def _norm_header(h: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"\(.*?\)", "", h).replace("_", " ")).strip().lower()


def _header_unit(h: str) -> str | None:
    """Unit written in a header: "Estimated Weight (g)", "weight_kg", "Electricity_Usage_kWh"."""
    m = re.search(r"\(([^)]*)\)", h)
    tokens = [m.group(1).strip().lower()] if m else []
    tokens += [t.lower() for t in re.split(r"[\s_\-]+", h.strip())][-1:]
    for t in tokens:
        if t in MASS_TO_KG or t in ENERGY_TO_KWH:
            return {"kwh": "kWh", "mj": "MJ", "wh": "Wh", "mwh": "MWh", "gj": "GJ"}.get(t, t)
    return None


def _pick(header_map: dict, candidates: list[str], exclude=(), contains=()) -> str | None:
    for cand in candidates:
        col = header_map.get(_norm_header(cand))
        if col is not None and col not in exclude:
            return col
    for key, col in header_map.items():  # fuzzy: "Estimated Weight (g)" -> weight
        if col not in exclude and any(w in key.split() for w in contains):
            return col
    return None


def split_materials(cell: str) -> list[str]:
    """Split "Li-ion (Li, Co, Ni, Graphite), Aluminum, Copper" on top-level commas/semicolons."""
    parts, depth, cur = [], 0, ""
    for ch in cell:
        depth += ch in "([{"
        depth -= ch in ")]}"
        if ch in ",;" and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    parts.append(cur)
    return [p.strip() for p in parts if p.strip()]


def parse_csv(text: str) -> tuple[list[dict], dict]:
    """Return (rows, column mapping). Each row may list several materials (when the material
    header is plural, e.g. "Main Materials") and an electricity amount. Raises EbomError."""
    text = text.lstrip("﻿")
    if not text.strip():
        raise EbomError("The file is empty.")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    headers = [h for h in (reader.fieldnames or []) if h is not None]
    header_map = {_norm_header(h): h for h in headers}
    energy = next((h for k, h in header_map.items() if ("electricity" in k or "energy" in k)
                   and (_header_unit(h) or "").lower() in ENERGY_TO_KWH), None)
    mat = _pick(header_map, MATERIAL_COLS, exclude=(energy,), contains=("material", "materials"))
    qty = _pick(header_map, QUANTITY_COLS, exclude=(energy,),
                contains=("weight", "mass", "quantity", "qty", "amount"))
    if mat is None or qty is None:
        raise EbomError(f"Need a material column and a quantity column; found {headers}. "
                        "Name them e.g. 'material' and 'quantity' (plus 'unit').")
    unit_col = _pick(header_map, UNIT_COLS)
    comp_col = _pick(header_map, COMPONENT_COLS, exclude=(mat,))
    id_col = _pick(header_map, ID_COLS, exclude=(mat, comp_col))
    loc_col = _pick(header_map, LOCATION_COLS)
    cost_col = _pick(header_map, COST_COLS)
    recy_col = next((h for k, h in header_map.items() if "recycl" in k), None)
    header_unit = _header_unit(qty) if unit_col is None and (_header_unit(qty) or "").lower() in MASS_TO_KG else None
    multi = _norm_header(mat).endswith("materials")
    columns = {"material": mat, "quantity": qty, "unit": unit_col, "component": comp_col,
               "item_id": id_col, "location": loc_col, "cost": cost_col, "recyclability": recy_col,
               "energy": energy, "energy_unit": _header_unit(energy) if energy else None,
               "unit_from_header": header_unit, "multi_material": multi}

    def cell(row, col):
        return (row.get(col) or "").strip() if col else ""

    rows = []
    for n, row in enumerate(reader, start=1):
        if not any((v or "").strip() for v in row.values() if isinstance(v, str)):
            continue  # blank row
        if len(rows) >= MAX_LINES:
            raise EbomError(f"Too many lines (max {MAX_LINES}).")
        material = cell(row, mat)
        rows.append({
            "line": n,
            "item_id": cell(row, id_col) or f"{n:02d}",
            "component": cell(row, comp_col) or material or f"line {n}",
            "materials": split_materials(material) if multi else [material],
            "material_cell": material,
            "quantity_raw": cell(row, qty),
            "unit": cell(row, unit_col) or header_unit,
            "location": cell(row, loc_col) or None,
            "cost": cell(row, cost_col) or None,
            "recyclability": cell(row, recy_col) or None,
            "energy_raw": cell(row, energy),
        })
    if not rows:
        raise EbomError("No BOM lines found under the header row.")
    return rows, columns


def _split_weights(n: int, mode: str) -> list[float]:
    if mode == "equal":
        return [1 / n] * n
    w = [0.5 ** i for i in range(n)]  # "ranked": listed first = main material
    return [x / sum(w) for x in w]


def expand_rows(rows: list[dict], columns: dict, split_mode: str = "ranked",
                composer=None) -> list[dict]:
    """One line per material (component mass split across its listed materials) plus one
    electricity line per component with an electricity amount. `composer(row)` may return
    AI fractions {material: share}; otherwise the split is `split_mode` and labelled."""
    lines = []
    for row in rows:
        mats = [m for m in row["materials"] if m] or [""]
        qty, problem = _quantity(row["quantity_raw"])
        fractions, how, notes, ai_failed = None, None, [], False
        if len(mats) > 1 and qty is not None:
            if composer:
                answer = composer(row)
                if answer and answer.get("status") == "ok":
                    fractions, how, notes = [answer["fractions"][m] for m in mats], "ai", answer.get("assumptions", [])
            if fractions is None:
                ai_failed = composer is not None  # AI composition was asked for but unavailable
                how = "equal" if split_mode == "equal" else "ranked"
                fractions = _split_weights(len(mats), how)
        for k, mat in enumerate(mats, 1):
            ln = {k2: row[k2] for k2 in ("line", "component", "location", "cost", "recyclability")}
            ln["item_id"] = row["item_id"] if len(mats) == 1 else f"{row['item_id']}.{k}"
            ln["material"] = mat
            ln["unit"] = row["unit"]
            ln["composition"] = None
            if fractions:
                ln["quantity_raw"] = repr(qty * fractions[k - 1])
                ln["composition"] = {"mode": how, "fraction": fractions[k - 1], "of": row["quantity_raw"],
                                     "unit": row["unit"], "cell": row["material_cell"], "assumptions": notes,
                                     "ai_failed": ai_failed}
            else:
                ln["quantity_raw"] = row["quantity_raw"]
            lines.append(ln)
        if row.get("energy_raw"):
            e, _ = _quantity(row["energy_raw"])
            if e is not None:
                lines.append({"line": row["line"], "component": row["component"], "location": row["location"],
                              "cost": None, "recyclability": None, "item_id": f"{row['item_id']}.E",
                              "material": "electricity", "quantity_raw": row["energy_raw"],
                              "unit": columns["energy_unit"], "composition": None, "energy": True})
    return lines


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
    toks = rag.tokens(text)
    if "oxide" in toks:  # aluminium oxide is a ceramic, not aluminium metal
        return None
    found = None
    for i, tok in enumerate(toks):
        # "glass filled", "glass fibre reinforced": the keyword only modifies the material
        if tok in KEYWORD_FAMILY and not {"filled", "reinforced"} & set(toks[i + 1:i + 3]):
            found = KEYWORD_FAMILY[tok]
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

def _record_family(tag_family: str | None, name: str) -> str | None:
    """Family of a database record. The name's head (before the first comma) decides; a
    material tag is used only when the head names no other family. This keeps e.g.
    "Porocalce plaster production" (tagged glass) out of the glass family, and
    "Chlorine, production mix for PVC production" out of plastics."""
    head = family_of(name.split(",")[0])
    if head:
        return head
    if tag_family and any(w in rag.tokens(name.split(",")[0]) for w in ("plaster", "mortar", "lime", "sand")):
        return None
    return tag_family


class Catalog:
    """Records with indicators, their material family, plausibility, and scoring references."""

    def __init__(self, engine: rag.Engine, source: str | None, indicator: str,
                 scoring: str = "data", corrections: dict | None = None, grid: str = "CN"):
        self.engine, self.source, self.indicator = engine, source, indicator
        self.scoring, self.corrections, self.grid = scoring, corrections or {}, grid
        self.apply_relabels = corrections is not None
        self.relabels = load_relabels()
        sql = ("SELECT id, name, unit, stage, location, kind, raw_json, impacts_json FROM records "
               "WHERE impacts_json != '{}' AND unit IS NOT NULL")
        rows = engine._all(sql + (" AND source = ?" if source else ""), (source,) if source else ())
        self.records, by_unit_label = [], {}
        for r in rows:
            impacts = json.loads(r["impacts_json"])
            if r["id"] in self.corrections:
                div = self.corrections[r["id"]]["divide_by"]
                impacts = {k: v / div for k, v in impacts.items()}
            tags = [t.strip().lower() for t in json.loads(r["raw_json"]).get("material_tags", "").split(",")]
            fam = next((TAG_FAMILY.get(t, t) for t in tags if TAG_FAMILY.get(t, t) in FAMILY_KEYWORDS), None)
            relabel = self.relabel_for(r["name"], r["unit"])
            self.records.append({"id": r["id"], "name": r["name"], "unit": r["unit"], "stage": r["stage"],
                                 "relabel": relabel,
                                 "location": r["location"], "impacts": impacts,
                                 "factor": impacts.get(indicator),
                                 # name fallback uses only the head before the first comma, so
                                 # "Chlorine, production mix for PVC production" is not a plastic
                                 "family": _record_family(fam, r["name"])})
            for label, v in impacts.items():
                if v:
                    by_unit_label.setdefault((r["unit"], label), []).append(abs(v))
        self.median = {k: sorted(v)[len(v) // 2] for k, v in by_unit_label.items() if len(v) >= 10}
        # With all sources, only record kinds that carry this indicator and a unit can be
        # evidence; flows (no indicators) must not outrank them.
        kinds = {r["kind"] for r in rows if json.loads(r["impacts_json"]).get(indicator) is not None}
        self.match_filters = {"source": source, "kind": next(iter(kinds)) if len(kinds) == 1 and not source else None}
        self.by_id = {r["id"]: r for r in self.records}
        # scoring references: 95th percentile of plausible positive per-kg factors
        self.refs = {}
        if scoring == "upstream":
            self.refs = dict(UPSTREAM_REFS)
            return
        for label in ("Climate change", *TOX_BLEND):
            vals = sorted(r["impacts"][label] for r in self.records
                          if r["unit"] == "kg" and r["impacts"].get(label, 0) > 0
                          and not self.implausible(r["impacts"][label], "kg", label))
            if len(vals) >= 10:
                self.refs[label] = vals[int(0.95 * (len(vals) - 1))]

    def relabel_for(self, name: str, unit: str) -> dict | None:
        for rl in self.relabels:
            if unit == rl["unit"] and any(name.startswith(p) for p in rl["name_prefixes"]):
                return rl
        return None

    def grid_record(self, location: str | None) -> tuple[dict | None, str | None]:
        """A grid-mix record for `location` (else the default grid), preferring the
        medium-voltage consumption mix that supplies manufacturing."""
        grids = [r for r in self.records if r["relabel"] and r["name"].startswith("Electricity Mix")]
        for loc in [location, self.grid] if location else [self.grid]:
            pool = [r for r in grids if r["location"] == loc]
            if pool:
                pool.sort(key=lambda r: ("1kV - 60kV" not in r["name"], r["name"]))
                return pool[0], loc
        return None, None

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
                if r["family"] == family and not r["relabel"] and r["unit"] == unit and r["factor"] is not None
                and r["factor"] > 0 and r["stage"] not in NON_MATERIAL_STAGES
                and r["id"] not in exclude and not self.implausible(r["factor"], unit)]

    def proxy(self, material: str, unit: str, family: str | None, exclude: str | None = None,
              generic: bool = False):
        pool = self.family_pool(family, unit, exclude=(exclude,))
        if not pool:
            return None
        pool_ids = {r["id"] for r in pool}
        hits = [] if generic else self.engine.search(material, {**self.match_filters, "unit": unit}, top_k=50)["candidates"]
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
                "corrected": self.corrections.get(chosen["id"]),
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

def load_corrections() -> dict:
    data = json.loads(CORRECTIONS_PATH.read_text(encoding="utf-8"))
    return {c["record_id"]: c for c in data["corrections"]}


def load_grid_references() -> dict:
    data = json.loads(GRID_REFERENCES_PATH.read_text(encoding="utf-8"))
    return {g["code"]: {**g, "source": data["source"], "url": data["url"]} for g in data["grids"]}


def load_relabels() -> list[dict]:
    return json.loads(CORRECTIONS_PATH.read_text(encoding="utf-8")).get("unit_relabels", [])


def is_electricity(material: str) -> bool:
    return "electricity" in rag.tokens(material) or "electric power" in material.lower()


def _ck(key) -> str:
    """Canonical text form of a cache key. Keys contain nested tuples (a component's
    materials); JSON turns those into lists, so tuples would not match after a reload."""
    return key if isinstance(key, str) else json.dumps(key, ensure_ascii=False)


def _ai_cache() -> dict:
    global _AI_CACHE
    if _AI_CACHE is None:
        try:
            entries = json.loads(AI_CACHE_PATH.read_text(encoding="utf-8"))
            _AI_CACHE = {_ck(e["key"]): e["answer"] for e in entries}
        except (OSError, ValueError, KeyError, TypeError):
            _AI_CACHE = {}
    return _AI_CACHE


def _save_ai_cache() -> None:
    entries = [{"key": k, "answer": v} for k, v in _ai_cache().items()]
    tmp = AI_CACHE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
    tmp.replace(AI_CACHE_PATH)


def clear_ai_cache() -> int:
    with _AI_LOCK:
        n = len(_ai_cache())
        _ai_cache().clear()
        AI_CACHE_PATH.unlink(missing_ok=True)
    return n


def analyse(engine: rag.Engine, text: str, indicator: str = "Climate change",
            source: str | None = None, default_unit: str | None = None,
            use_ai: bool = False, provider: str | None = None, fallback=None,
            overrides: dict | None = None, memo: dict | None = None,
            scoring: str = "data", grade_basis: str = "combined", apply_corrections: bool = False,
            split_mode: str = "ranked", grid: str = "CN", composer=None) -> dict:
    """`overrides` maps item_id -> replacement material (what-if). `memo` caches line
    resolutions and AI answers across calls in one request. `scoring` picks data-derived or
    upstream reference values, `grade_basis` grades on the combined or GHG-only score, and
    `apply_corrections` applies the reviewed scale corrections in data/corrections.json."""
    if scoring not in SCORING_MODES or grade_basis not in GRADE_BASES or split_mode not in SPLIT_MODES:
        raise EbomError(f"scoring must be one of {SCORING_MODES}; grade_basis one of {GRADE_BASES}; "
                        f"split_mode one of {SPLIT_MODES}.")
    rows, columns = parse_csv(text)
    memo = {} if memo is None else memo
    if split_mode == "ai" and use_ai:
        composer = composer or _ai_composer(provider)
    lines = expand_rows(rows, columns, "ranked" if split_mode == "ai" else split_mode, composer)
    if overrides:
        known = {ln["item_id"] for ln in lines}
        unknown = sorted(set(overrides) - known)
        if unknown:
            raise EbomError(f"Unknown item id(s) in scenario: {', '.join(unknown)}")
        for ln in lines:
            if ln["item_id"] in overrides:
                ln["original_material"] = ln["material"]
                ln["material"] = overrides[ln["item_id"]].strip()
    ckey = ("catalog", source, indicator, scoring, apply_corrections, grid)
    catalog = memo.get(ckey) or Catalog(engine, source, indicator, scoring,
                                        load_corrections() if apply_corrections else None, grid)
    memo[ckey] = catalog
    climate = is_climate(indicator)

    results = []
    for ln in lines:
        key = ("line", ln["material"], ln["quantity_raw"], ln["unit"], default_unit, indicator, source,
               apply_corrections, grid, ln["location"])
        if key not in memo:
            memo[key] = _resolve_line(engine, catalog, ln, indicator, source, default_unit)
        identity = ("line", "item_id", "component", "material", "quantity_raw", "location", "cost",
                    "original_material", "composition", "recyclability")
        results.append({**memo[key], **{k: ln[k] for k in identity if k in ln}})

    pending = [r for r in results if r["status"] == "not_covered"]
    if use_ai and pending:
        fallback_is_default = fallback is None
        if fallback is None:
            import ai_fallback
            fallback = ai_fallback.estimate

        def run(r):
            persistent = fallback_is_default
            key = ("ai", r["material"], r["unit"], indicator, source, provider)
            key = _ck(key) if persistent else key
            with _AI_LOCK:
                answer = (_ai_cache() if persistent else memo).get(key)
            if answer is None:
                answer = fallback(query=r["material"], indicator=indicator, reference_unit=r["unit"],
                                  constraints={"source": source, "unit": r["unit"], "location": r["location"]},
                                  context=r.get("_context", [])[:5], provider=provider)
                if answer.get("status") != "unavailable":  # never cache outages
                    with _AI_LOCK:
                        cache = _ai_cache() if persistent else memo
                        if persistent and len(cache) >= AI_CACHE_SIZE:
                            cache.pop(next(iter(cache)))
                        cache[key] = answer
                        if persistent:
                            _save_ai_cache()
            r["provisional"] = _provisional(answer, r["quantity"], climate)
            if r["provisional"]["contribution"] is not None:
                r.update(status="provisional", tier="provisional",
                         impact=r["provisional"]["impact_kg"] if climate else None)

        # one request per unique material, then apply the answer to every line using it
        groups: dict = {}
        for r in pending:
            groups.setdefault((r["material"].lower(), r["unit"]), []).append(r)
        firsts = [g[0] for g in groups.values()]
        with ThreadPoolExecutor(max_workers=AI_WORKERS) as pool:
            list(pool.map(run, firsts[:MAX_AI_LINES]))
        for g in groups.values():
            for r in g[1:]:
                run(r)  # served from the cache filled above
        for r in [g[0] for g in list(groups.values())[MAX_AI_LINES:]]:
            r["provisional"] = {"status": "skipped", "error": f"AI limited to {MAX_AI_LINES} materials"}
    for r in results:
        r.pop("_context", None)

    _score_lines(results, catalog, climate, grade_basis)
    components = _aggregate(results, "component", climate, grade_basis)
    # A hotspot is a BOM row (component) above the share threshold; its lines inherit the flag.
    hot = {c["name"] for c in components if c["hotspot"]}
    for r in results:
        r["hotspot"] = r["component"] in hot
    product = _product(results, climate, grade_basis, scoring, catalog.refs)
    product["hotspots"] = [{"item_id": ", ".join(c["items"]), "component": c["name"],
                            "material": ", ".join(c["materials"]), "impact": c["impact"], "share": c["share"]}
                           for c in components if c["hotspot"]]
    recs = _recommendations(results, catalog) if climate else []
    return {
        "indicator": indicator,
        "source": source,
        "default_unit": default_unit,
        "climate": climate,
        "columns": columns,
        "product": product,
        "components": components,
        "materials": _aggregate(results, "material_key", climate, grade_basis),
        "options": {"scoring": scoring, "grade_basis": grade_basis, "apply_corrections": apply_corrections,
                    "split_mode": split_mode if split_mode != "ai" or use_ai else "ranked", "grid": grid},
        "relabels": load_relabels() if apply_corrections else [],
        "corrections": list(catalog.corrections.values()),
        "lines": results,
        "recommendations": recs,
        "scoring": {
            "weights": WEIGHTS, "ghg_blend": GHG_BLEND, "toxicity_blend": TOX_BLEND,
            "abs_ghg_ref_kg": ABS_GHG_REF_KG, "references_p95_per_kg": catalog.refs,
            "grade_bands": [{"grade": g, "max": b} for g, b in GRADE_BANDS] + [{"grade": "D", "max": None}],
            "hotspot_share": HOTSPOT_SHARE,
            "mode": scoring, "grade_basis": grade_basis,
            "note": ("Formula and weights follow the original scoring method. Per-kg references are the "
                     "95th percentile of plausible dataset factors (the original hard-coded values "
                     "were 12.5 / 150 / 1.5 and left toxicity near zero).") if scoring == "data" else
                    ("Original constants: hard-coded references 12.5 / 150 / 1.5 and keyword-only "
                     "criticality, reproduced for comparison. With this data the toxicity sub-score is ~0."),
        },
        "note": "Evidence uses exact source records; proxies use a similar material from the "
                "same family in the database; provisional values are AI estimates. Each "
                "number keeps its tier. Scores combine the tiers and report their shares.",
    }


def _ai_composer(provider):
    """Wrap ai_fallback.composition with the persistent AI cache."""
    import ai_fallback

    def compose(row):
        key = _ck(("composition", row["component"], tuple(row["materials"]), row["quantity_raw"], provider))
        with _AI_LOCK:
            answer = _ai_cache().get(key)
        if answer is None:
            answer = ai_fallback.composition(row["component"], row["materials"], row["quantity_raw"],
                                             row["unit"], provider=provider)
            if answer.get("status") == "ok":
                with _AI_LOCK:
                    _ai_cache()[key] = answer
                    _save_ai_cache()
        return answer
    return compose


def eco_ideas(facts: dict, provider: str | None = None, generator=None) -> dict:
    """AI eco-design ideas for one component, cached like other AI answers."""
    if generator is None:
        import ai_fallback
        generator = ai_fallback.ideas
    key = _ck(("ideas", facts.get("component"), tuple(facts.get("materials") or ()), provider))
    with _AI_LOCK:
        cached = _ai_cache().get(key)
    if cached is not None:
        return cached
    answer = generator(facts, provider=provider)
    if answer.get("status") == "ok":
        with _AI_LOCK:
            _ai_cache()[key] = answer
            _save_ai_cache()
    return answer


# ---------------------------------------------------------------------------
# Saved analyses: the same file with the same settings gives the same answer
# ---------------------------------------------------------------------------

RESULTS_DIR = rag.INDEX_DIR / "results"
_CODE_FILES = ("ebom.py", "rag.py", "ai_fallback.py")


def _code_version() -> str:
    """Changes whenever the analysis code or the reviewed reference data changes."""
    h = hashlib.sha256()
    for name in _CODE_FILES:
        h.update((Path(__file__).parent / name).read_bytes())
    for path in (CORRECTIONS_PATH, GRID_REFERENCES_PATH):
        h.update(path.read_bytes())
    return h.hexdigest()[:16]


def _result_key(text: str, opts: dict) -> str:
    payload = json.dumps({"csv": text.replace("\r\n", "\n").strip(), "opts": opts, "code": _code_version()},
                         sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _complete(result: dict) -> bool:
    """Only freeze results without AI outages, so a later run can still fill those gaps."""
    return not any((l.get("provisional") or {}).get("status") in ("unavailable", "skipped")
                   or (l.get("composition") or {}).get("ai_failed") for l in result["lines"])


def analyse_saved(engine: rag.Engine, text: str, refresh: bool = False, **opts) -> dict:
    """analyse() with the result saved per (file content, settings, code version)."""
    key = _result_key(text, {k: opts.get(k) for k in sorted(opts) if k not in ("fallback", "composer")})
    path = RESULTS_DIR / f"{key}.json"
    if not refresh and path.exists():
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
            saved["saved"] = {**saved.get("saved", {}), "restored": True}
            return saved
        except (OSError, ValueError):
            pass
    result = analyse(engine, text, **opts)
    result["saved"] = {"restored": False, "complete": _complete(result),
                       "at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "key": key[:12]}
    if result["saved"]["complete"] and not opts.get("fallback"):
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    return result


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
    if is_electricity(ln["material"]) and unit_in.lower() in ENERGY_TO_KWH:
        return _resolve_electricity(engine, catalog, ln, res, qty * ENERGY_TO_KWH[unit_in.lower()], unit_in, qty, indicator)
    if unit_in.lower() in ENERGY_TO_KWH and unit_in != "MJ":  # e.g. steam given in kWh
        res["converted_from"] = {"quantity": qty, "unit": unit_in}
        qty = qty * ENERGY_TO_KWH[unit_in.lower()] * 3.6
        res["quantity"], res["unit"] = qty, "MJ"
    unit = res["unit"]
    material_l = ln["material"].lower()
    generic = rag.normalize_name(ln["material"]) in GENERIC_MATERIALS

    out = engine.query(ln["material"], {**catalog.match_filters, "unit": unit}, indicator, top_k=4)
    cands = out["candidates"]
    res["_context"] = cands
    if cands:
        c = cands[0]
        res["match"] = {"id": c["id"], "name": c["name"], "location": c["location"],
                        "citation": c["citation"], "term_coverage": c["term_coverage"]}
    res["reasons"] = list(out["reasons"])

    exact_id = None
    top = cands[0] if cands else None
    eol_words = ("landfill", "incineration", "recycling", "disposal", "transport", "waste")
    if top and out["status"] == "ok" and (top["stage"] in NON_MATERIAL_STAGES
                                          and not any(w in material_l for w in eol_words)):
        res["reasons"] = ["exact_match_not_a_material"]  # e.g. "Landfill of glass" for "glass"
    elif top and out["status"] == "ok" and catalog.relabel_for(top["name"], top["reference_unit"] or ""):
        res["reasons"] = ["unit_label_unreliable"]
    elif generic and out["status"] == "ok":
        res["reasons"] = ["generic_material"]  # "Glass" alone: use the family's typical value
    elif out["status"] == "ok" and out["answer"]:
        exact_id = out["answer"]["record_id"]
        factor = out["answer"]["value"]
        correction = catalog.corrections.get(exact_id)
        if correction:
            factor /= correction["divide_by"]
        ratio = catalog.implausible(factor, unit)
        if not ratio:
            engine.calculate(exact_id, indicator, qty, unit)  # unit and quantity checks
            if correction:
                res["warnings"].append({"code": "corrected_factor", "record_id": exact_id,
                                        "message": f"Source factor divided by {correction['divide_by']:g} "
                                                   f"(reviewed correction): {correction['reason']}"})
            return {**res, "status": "evidence", "tier": "evidence", "factor": factor, "record_id": exact_id,
                    "correction": correction, "contribution": factor * qty, "impact": factor * qty}
        res["warnings"].append({
            "code": "implausible_factor", "record_id": exact_id, "factor": factor,
            "message": f"The exact match has factor {factor:.4g}, {ratio:.0f}x the median "
                       f"for {indicator} per {unit}; the source likely records it per tonne or per "
                       "item. It is not used."})
        res["reasons"] = ["exact_factor_implausible"]

    family = res["family"] or (catalog.by_id.get(exact_id) or {}).get("family")
    proxy = catalog.proxy(ln["material"], unit, family, exclude=exact_id, generic=generic)
    if proxy:
        contribution = proxy["factor"] * qty
        return {**res, "status": "proxy", "tier": "proxy", "family": family, "proxy": proxy,
                "record_id": proxy["id"], "factor": proxy["factor"], "contribution": contribution,
                "impact": contribution,
                "proxy_range": [proxy["family_min"] * qty, proxy["family_max"] * qty]}
    if family:
        res["reasons"].append("no_plausible_family_member")
    return {**res, "status": "not_covered", "family": family}


def _resolve_electricity(engine, catalog: Catalog, ln: dict, res: dict, kwh: float, unit_in: str,
                         qty_in: float, indicator: str) -> dict:
    """Electricity is matched to a grid mix per kWh. The export labels grid mixes "MJ" but
    their values are per kWh (see data/corrections.json), so they are used only when the
    reviewed corrections are on."""
    if unit_in != "kWh":
        res["converted_from"] = {"quantity": qty_in, "unit": unit_in}
    res.update(quantity=kwh, unit="kWh", family=None)
    refs = load_grid_references()
    code = ln.get("location") if ln.get("location") in refs else (catalog.grid if catalog.grid in refs else None)
    if code and is_climate(indicator):
        g = refs[code]
        factor = g["g_co2_per_kwh"] / 1000.0
        res["match"] = {"id": f"reference:{code}", "name": g["name"], "location": code,
                        "citation": f"{g['source']} ({g['url']})", "term_coverage": 1.0}
        res["warnings"].append({"code": "reference_grid", "message":
                                f"{g['name']}: {g['g_co2_per_kwh']} g CO2-eq/kWh, an external reference value "
                                "(not in the LCA database)."})
        return {**res, "status": "reference", "tier": "reference", "grid": code, "factor": factor,
                "reference": {"code": code, **g}, "contribution": factor * kwh, "impact": factor * kwh}
    record, loc = catalog.grid_record(ln.get("location"))
    if record is None:
        return {**res, "status": "not_covered", "reasons": ["no_grid_mix"]}
    res["match"] = {"id": record["id"], "name": record["name"], "location": record["location"],
                    "citation": engine.record(record["id"])["citation"], "term_coverage": 1.0}
    if not catalog.apply_relabels:
        res["_context"] = [{"id": record["id"], "name": record["name"], "location": record["location"],
                            "reference_unit": "kWh (labelled MJ in source)"}]
        return {**res, "status": "not_covered", "reasons": ["unit_label_unreliable"]}
    factor = record["impacts"].get(indicator)
    if factor is None:
        return {**res, "status": "not_covered", "reasons": ["missing_indicator"]}
    rl = record["relabel"]
    res["warnings"].append({"code": "corrected_factor", "record_id": record["id"],
                            "message": f"Grid mix {record['location']} used per kWh although the source labels it "
                                       f"per MJ (reviewed correction): {rl['reason']}"})
    if ln.get("location") and loc != ln["location"]:
        res["warnings"].append({"code": "grid_substituted", "message":
                                f"No grid mix for {ln['location']} in the database; used {loc}."})
    return {**res, "status": "evidence", "tier": "evidence", "record_id": record["id"], "factor": factor,
            "correction": {"divide_by": 1, "reason": rl["reason"], "relabel": f"{rl['unit']} -> {rl['actual_unit']}"},
            "grid": loc, "contribution": factor * kwh, "impact": factor * kwh}


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
    if r["status"] == "reference":
        return "medium"
    return "low"


def _score_lines(results: list[dict], catalog: Catalog, climate: bool, grade_basis: str = "combined") -> None:
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
        tier, sub["criticality"] = criticality(r["material"], r["family"] if catalog.scoring == "data" else None)
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
        r["ghg_grade"] = grade_for(sub["ghg"])
        r["combined_grade"] = grade_for(composite)
        r["grade"] = r["combined_grade"] if grade_basis == "combined" else r["ghg_grade"]


def _tiers(rows) -> dict:
    t = {"evidence": 0.0, "proxy": 0.0, "reference": 0.0, "provisional": 0.0}
    for r in rows:
        if r["impact"] is not None:
            t[r["tier"]] += r["impact"]
    return t


def _weighted(rows, key) -> float | None:
    pairs = [(r["impact"], r["scores"][key]) for r in rows
             if r["scores"] and r["scores"].get(key) is not None and r["impact"] and r["impact"] > 0]
    w = sum(i for i, _ in pairs)
    return sum(i * v for i, v in pairs) / w if w else None


def _aggregate(results: list[dict], key: str, climate: bool, grade_basis: str = "combined") -> list[dict]:
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
        score = _weighted(rows, "composite" if grade_basis == "combined" else "ghg")
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


def _product_subscores(results: list[dict], refs: dict, total: float, product_mass: float) -> dict:
    """Product-level sub-scores. GHG uses the product's own intensity (all GHG, energy
    included, per kg of product mass) and total; criticality and toxicity are mass-weighted
    over material lines. Weighting item scores by GHG (upstream) lets energy lines, which have
    no material sub-scores and tiny absolute scores, pull the product score towards zero."""
    out = {}
    ref = refs.get("Climate change")
    if product_mass and ref and total is not None:
        out["ghg"] = (GHG_BLEND["intensity"] * _clip(total / product_mass / ref * 100)
                      + GHG_BLEND["absolute"] * _clip(total / ABS_GHG_REF_KG * 100))
    else:
        out["ghg"] = None
    for key in ("criticality", "toxicity"):
        pairs = [(r["mass_kg"], r["scores"][key]) for r in results
                 if r["mass_kg"] and r["scores"] and r["scores"].get(key) is not None]
        w = sum(m for m, _ in pairs)
        out[key] = sum(m * v for m, v in pairs) / w if w else None
    return {k: out[k] for k in WEIGHTS}


def _product(results: list[dict], climate: bool, grade_basis: str = "combined",
             product_scoring: str = "data", refs: dict | None = None) -> dict:
    tiers = _tiers(results)
    total = sum(tiers.values())
    scored = [r for r in results if r["impact"] is not None]
    mass_total = sum(r["mass_kg"] for r in results if r["mass_kg"])
    mass_covered = sum(r["mass_kg"] for r in scored if r["mass_kg"])
    mass_cov = mass_covered / mass_total if mass_total else None
    if not climate:
        subscores = {k: None for k in WEIGHTS}
        combined = ghg_only = None
    elif product_scoring == "upstream":
        subscores = {k: _weighted(results, k) for k in WEIGHTS}
        combined = _weighted(results, "composite")
        ghg_only = subscores["ghg"]
    else:
        subscores = _product_subscores(results, refs or {}, total, mass_total)
        avail = {k: v for k, v in subscores.items() if v is not None}
        combined = (sum(WEIGHTS[k] * v for k, v in avail.items()) / sum(WEIGHTS[k] for k in avail)) if avail else None
        ghg_only = subscores["ghg"]
    score = combined if grade_basis == "combined" else ghg_only
    counts = {s: sum(r["status"] == s for r in results)
              for s in ("evidence", "proxy", "reference", "provisional", "not_covered", "invalid")}
    conf = {c: sum(r["confidence"] == c for r in scored) for c in ("high", "medium", "low")}
    ev_share = tiers["evidence"] / total if total else 0.0
    if ev_share >= 0.8 and (mass_cov or 0) >= 0.9:
        confidence = "high"
    elif ev_share + tiers["proxy"] / (total or 1) >= 0.5 and (mass_cov or 0) >= 0.7:
        confidence = "medium"
    else:
        confidence = "low"
    hotspots = sorted((r for r in results if r["hotspot"] and r["impact"] is not None),
                      key=lambda r: -r["impact"])
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
        # per kg of the whole product; uncovered lines lower confidence, not the denominator
        "intensity": total / mass_total if climate and mass_total else None,
        "score": score,
        "grade": grade_for(score),
        "grade_text": GRADE_TEXT.get(grade_for(score)),
        "grade_basis": grade_basis,
        "combined_score": combined, "combined_grade": grade_for(combined),
        "ghg_score": ghg_only, "ghg_grade": grade_for(ghg_only),
        "subscores": subscores,
        "product_scoring": product_scoring,
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
        if r["hotspot"]:
            advice.append(f"Part of a carbon hotspot ({r['component']}); prioritise in redesign.")
        recy = (r.get("recyclability") or "").lower()
        if "difficult" in recy or "not recyclable" in recy or "non-recyclable" in recy:
            advice.append(f"Recyclability in your eBOM: '{r['recyclability']}'. Consider design for "
                          "disassembly, material recovery or a take-back scheme.")
            priority = True
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
            source: str | None = None, default_unit: str | None = None,
            use_ai: bool = False, provider: str | None = None, fallback=None, explainer=None,
            scoring: str = "data", grade_basis: str = "combined", apply_corrections: bool = False,
            split_mode: str = "ranked", grid: str = "CN", composer=None) -> dict:
    """Re-run the full pipeline for up to three scenarios of material swaps."""
    if not scenarios or len(scenarios) > MAX_SCENARIOS:
        raise EbomError(f"Give between 1 and {MAX_SCENARIOS} scenarios.")
    memo: dict = {}
    opts = dict(scoring=scoring, grade_basis=grade_basis, apply_corrections=apply_corrections,
                split_mode=split_mode, grid=grid, composer=composer)
    base = analyse(engine, text, indicator, source, default_unit, use_ai, provider, fallback, memo=memo, **opts)
    base_lines = {l["item_id"]: l for l in base["lines"]}
    out = []
    for i, sc in enumerate(scenarios, 1):
        swaps = {s["item_id"]: s["material"] for s in sc.get("swaps", []) if s.get("item_id") and s.get("material", "").strip()}
        if not swaps:
            raise EbomError(f"Scenario {i} has no material swaps.")
        res = analyse(engine, text, indicator, source, default_unit, use_ai, provider, fallback,
                      overrides=swaps, memo=memo, **opts)
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
                    "model": result.get("model")}
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
    return {"source": "rules", "text": " ".join(parts)}


def main() -> int:

    parser = argparse.ArgumentParser(description="Analyse an eBOM CSV against the LCA index.")
    parser.add_argument("csv_path")
    parser.add_argument("--indicator", default="Climate change")
    parser.add_argument("--source", default=None, help="limit matching to one source (default: all)")
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
