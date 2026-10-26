"""eBOM (engineering bill of materials) analysis over the evidence-grounded index.

Each BOM line is matched with the same retrieval, abstention, and unit rules as a single
query. Evidence-backed and provisional (AI) contributions are totalled separately and
never added together.

    python ebom.py path/to/bom.csv [--indicator "Climate change"] [--ai --provider groq]

CSV columns are detected case-insensitively:
    material  (required)  material_name | material | component | part_name | name | description | item
    quantity  (required)  quantity | quantity_kg | qty | weight | weight_kg | mass | mass_kg | amount
    unit      (optional)  unit | uom | units          -- else "_kg" in the quantity header means kg
    item id   (optional)  item_id | part_no | part_number | id | component (when not the material)
    location  (optional)  location | country | supplier_country | region
Lines without an explicit unit are not scaled unless the caller supplies `default_unit`.
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
# A factor this many times the median for the same indicator and unit is flagged for review.
# The export labels some processes "kg" whose factors look per-tonne or per-item, e.g.
# corrugated board boxes 1159 vs corrugated board sheets 0.71 for Climate change.
OUTLIER_RATIO = 50

MATERIAL_COLS = ["material_name", "material", "component", "part_name", "part name", "name",
                 "description", "item"]
QUANTITY_COLS = ["quantity", "quantity_kg", "qty", "weight", "weight_kg", "mass", "mass_kg",
                 "amount"]
UNIT_COLS = ["unit", "uom", "units"]
ID_COLS = ["item_id", "part_no", "part no", "part number", "part_number", "id", "line", "component"]
LOCATION_COLS = ["location", "country", "supplier_country", "region"]


class EbomError(ValueError):
    pass


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
    id_col = _pick(header_map, ID_COLS, exclude=(mat,))
    loc_col = _pick(header_map, LOCATION_COLS)
    header_unit = "kg" if unit_col is None and qty.strip().lower().endswith("kg") else None
    columns = {"material": mat, "quantity": qty, "unit": unit_col, "item_id": id_col,
               "location": loc_col, "unit_from_header": header_unit}

    lines = []
    for n, row in enumerate(reader, start=1):
        material = (row.get(mat) or "").strip()
        if not material and not any((v or "").strip() for v in row.values() if isinstance(v, str)):
            continue  # blank row
        if len(lines) >= MAX_LINES:
            raise EbomError(f"Too many lines (max {MAX_LINES}).")
        raw_qty = (row.get(qty) or "").strip()
        unit = ((row.get(unit_col) or "").strip() if unit_col else "") or header_unit
        lines.append({
            "line": n,
            "item_id": (row.get(id_col) or "").strip() if id_col else str(n),
            "material": material,
            "quantity_raw": raw_qty,
            "unit": unit or None,
            "location": (row.get(loc_col) or "").strip() or None if loc_col else None,
        })
    if not lines:
        raise EbomError("No BOM lines found under the header row.")
    return lines, columns


def _median_factor(engine: rag.Engine, source, unit: str, indicator: str, cache: dict):
    key = (source, unit, indicator)
    if key not in cache:
        sql = "SELECT impacts_json FROM records WHERE unit = ?" + (" AND source = ?" if source else "")
        rows = engine._all(sql, (unit, source) if source else (unit,))
        vals = sorted(abs(v) for r in rows
                      if (v := json.loads(r[0]).get(indicator)) is not None and v != 0)
        cache[key] = vals[len(vals) // 2] if len(vals) >= 10 else None
    return cache[key]


def _unit_key(unit: str | None) -> str:
    """Spelling-insensitive key, e.g. "kg CO₂‑eq" == "kg CO2 eq". It never converts g to kg."""
    t = (unit or "").lower().replace("₂", "2")
    t = re.sub(r"[\s\-\u2010-\u2015_.]", "", t)
    return re.sub(r"(equivalents?|equiv|eq|e)$", "eq", t)


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


def analyse(engine: rag.Engine, text: str, indicator: str = "Climate change",
            source: str | None = "bottles_processes", default_unit: str | None = None,
            use_ai: bool = False, provider: str | None = None, fallback=None) -> dict:
    """Match every line, calculate where evidence allows, and keep totals separate."""
    lines, columns = parse_csv(text)
    results, medians = [], {}
    for ln in lines:
        qty, qty_problem = _quantity(ln["quantity_raw"])
        unit = ln["unit"] or default_unit
        res = {**ln, "quantity": qty, "unit": unit,
               "unit_source": "file" if ln["unit"] else ("default" if unit else None),
               "status": None, "reasons": [], "match": None, "alternatives": [],
               "factor": None, "contribution": None, "provisional": None, "warnings": []}
        if not ln["material"]:
            res.update(status="invalid", reasons=["missing_material"])
        elif qty is None:
            res.update(status="invalid", reasons=[qty_problem])
        elif unit is None:
            res.update(status="invalid", reasons=["missing_unit"])
        else:
            # The unit is an exact pre-ranking filter, so any match is directly scalable.
            out = engine.query(ln["material"], {"source": source, "unit": unit}, indicator, top_k=4)
            cands = out["candidates"]
            res["alternatives"] = [{"id": c["id"], "name": c["name"], "location": c["location"]}
                                   for c in cands[1:4]]
            if cands:
                c = cands[0]
                res["match"] = {"id": c["id"], "name": c["name"], "location": c["location"],
                                "reference_unit": c["reference_unit"], "citation": c["citation"],
                                "term_coverage": c["term_coverage"]}
            res["reasons"] = out["reasons"]
            if out["status"] == "ok" and out["answer"]:
                calc = engine.calculate(out["answer"]["record_id"], indicator, qty, unit)
                res.update(status="evidence", factor=calc["factor"], contribution=calc["result"])
                median = _median_factor(engine, source, unit, indicator, medians)
                if median and abs(calc["factor"]) > OUTLIER_RATIO * median:
                    res["warnings"].append({
                        "code": "implausible_factor",
                        "message": f"Factor {calc['factor']:.4g} is {abs(calc['factor']) / median:.0f}x the "
                                   f"median ({median:.3g}) for {indicator} per {unit}; the source may "
                                   "use a different reference amount. Review before use."})
            else:
                res["status"] = "not_covered"
                res["_context"] = cands
        results.append(res)

    pending = [r for r in results if r["status"] == "not_covered"]
    if use_ai and pending:
        if fallback is None:
            import ai_fallback
            fallback = ai_fallback.estimate

        def run(r):
            fb = fallback(query=r["material"], indicator=indicator, reference_unit=r["unit"],
                          constraints={"source": source, "unit": r["unit"], "location": r["location"]},
                          context=r["_context"][:5], provider=provider)
            r["provisional"] = _provisional(fb, r["quantity"])
            if r["provisional"]["contribution"] is not None:
                r["status"] = "provisional"

        with ThreadPoolExecutor(max_workers=AI_WORKERS) as pool:
            list(pool.map(run, pending[:MAX_AI_LINES]))
        for r in pending[MAX_AI_LINES:]:
            r["provisional"] = {"status": "skipped", "error": f"AI limited to {MAX_AI_LINES} lines"}
    for r in results:
        r.pop("_context", None)

    evidence = [r for r in results if r["status"] == "evidence"]
    prov = [r for r in results if r["status"] == "provisional"]
    ev_total = sum(r["contribution"] for r in evidence)
    summary = {
        "lines": len(results),
        "evidence_lines": len(evidence),
        "provisional_lines": len(prov),
        "not_covered_lines": sum(r["status"] == "not_covered" for r in results),
        "flagged_lines": sum(bool(r["warnings"]) for r in results),
        "evidence_total_unflagged": sum(r["contribution"] for r in evidence if not r["warnings"]),
        "invalid_lines": sum(r["status"] == "invalid" for r in results),
        "evidence_total": ev_total,
        # Estimates in different impact units are never summed together.
        "provisional_by_unit": _group_by_unit(prov),
    }
    for r in evidence:
        r["share_of_evidence"] = r["contribution"] / ev_total if ev_total else None
    return {
        "indicator": indicator,
        "source": source,
        "default_unit": default_unit,
        "columns": columns,
        "summary": summary,
        "lines": results,
        "note": "Evidence totals use source factors only; provisional AI estimates are summed "
                "separately and are never added to evidence. Matches are ranked candidates, "
                "not verified process selections.",
    }


def _group_by_unit(prov: list[dict]) -> list[dict]:
    groups: dict[str, dict] = {}
    for r in prov:
        p = r["provisional"]
        g = groups.setdefault(_unit_key(p["impact_unit"]), {
            "impact_unit": p["impact_unit"], "lines": 0, "total": 0.0, "lower": 0.0, "upper": 0.0})
        g["lines"] += 1
        g["total"] += p["contribution"]
        g["lower"] += p["lower"]
        g["upper"] += p["upper"]
    ordered = sorted(groups.items(), key=lambda kv: -kv[1]["lines"])
    index = {key: i for i, (key, _) in enumerate(ordered)}
    for r in prov:
        r["provisional"]["unit_group"] = index[_unit_key(r["provisional"]["impact_unit"])]
    return [g for _, g in ordered]


def _provisional(fb: dict, qty: float) -> dict:
    out = {k: fb.get(k) for k in ("status", "provider", "model", "label", "error", "generated_at")}
    out.update(contribution=None, lower=None, upper=None, value=None, impact_unit=None,
               explanation=None, assumptions=[], missing_information=[])
    est = fb.get("estimate")
    if est:
        out.update(explanation=est["explanation"], assumptions=est["assumptions"],
                   missing_information=est["missing_information"])
        if est["can_estimate"]:
            out.update(value=est["value"], impact_unit=est["impact_unit"],
                       contribution=est["value"] * qty, lower=est["lower"] * qty,
                       upper=est["upper"] * qty, system_boundary=est["system_boundary"])
    return out


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
