"""Evidence-grounded hierarchical retrieval over public LCA CSV exports.

Usage:
    python rag.py build                  # (re)build index/lca_rag.sqlite atomically
    python rag.py query "bottle-grade PET" --indicator "Climate change"
    python rag.py calc bottles_processes:232 "Climate change" 2 kg

Only the Python standard library is required. SQLite must provide FTS5.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import re
import sqlite3
import sys
import threading
import time
import unicodedata
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
INDEX_DIR = ROOT / "index"
INDEX_PATH = INDEX_DIR / "lca_rag.sqlite"
SCHEMA_VERSION = 1

# source id -> (file name, record kind for rows without flow_type)
SOURCES = {
    "bottles_processes": ("bottles_processes.csv", "process"),
    "lca_full_rag_data_units": ("lca_full_rag_data_units.csv", "impact_export"),
    "openlca_flows": ("openlca_flows.csv", "flow"),
    "usda_flows": ("usda_flows.csv", "flow"),
}

# Columns that are descriptive rather than indicators in the process/impact exports.
NON_INDICATOR_COLUMNS = {
    "process_name", "lifecycle_stage", "category_path", "location", "unit",
    "ref_flow", "description", "material_tags", "semantic_text",
}

RRF_K = 60
# The lexical side contributes two lists (global + routed); weight the single dense list
# equally so cosine evidence is not outvoted by construction.
DENSE_RRF_WEIGHT = 2.0
ROUTE_LIMIT = 12
LEAF_LIMIT = 100
NODE_VOCAB_SIZE = 300
# A value is reported only when the best match covers at least this share of the query's
# content terms. At 0.5, "stainless steel" answered with steel rebar and "recycled carbon
# fibre composite" with corrugated board; every benchmark query covers 1.0.
MIN_TERM_COVERAGE = 0.75
BM25_WEIGHTS = (10.0, 4.0, 2.0, 1.0)  # name, tags, category, description

STOPWORDS = frozenset("""
a an and are as at be by for from how i in is it of on or per the to what which with
impact impacts factor factors value values emission emissions footprint lca data
""".split())

# Small, explicit abbreviation map (see paper Sec. V-C). Keys are query tokens.
ABBREVIATIONS = {
    "pet": ("polyethylene", "terephthalate"),
    "hdpe": ("polyethylene", "high", "density", "pe", "hd"),
    "ldpe": ("polyethylene", "low", "density", "pe", "ld"),
    "aluminum": ("aluminium",),
    "alu": ("aluminium",),
}

_TOKEN_RE = re.compile(r"[a-z0-9]+")


class CalculationError(ValueError):
    """A calculation request failed validation; `code` is machine readable."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# Text normalisation
# ---------------------------------------------------------------------------

def fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    return "".join(ch for ch in text if not unicodedata.combining(ch)).lower()


def tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall(fold(text))


def normalize_name(text: str) -> str:
    return " ".join(tokens(text))


def query_terms(text: str) -> tuple[list[str], list[str]]:
    """Return (content terms, expanded search terms), both de-duplicated in order."""
    content = list(dict.fromkeys(t for t in tokens(text) if t not in STOPWORDS))
    expanded: list[str] = []
    for term in content:
        expanded.append(term)
        expanded.extend(ABBREVIATIONS.get(term, ()))
    return content, list(dict.fromkeys(expanded))


def fts_or(terms: list[str]) -> str:
    return " OR ".join(f'"{t}"' for t in terms)


def parse_number(raw: str):
    """Return a finite float, or None when the cell is not a measurement."""
    if raw is None:
        return None
    raw = raw.strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def split_category(path: str) -> list[str]:
    return [p.strip() for p in (path or "").split(">") if p.strip()]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE records (
    rid INTEGER PRIMARY KEY,
    id TEXT NOT NULL UNIQUE,
    source TEXT NOT NULL,
    source_file TEXT NOT NULL,
    record_no INTEGER NOT NULL,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    norm_name TEXT NOT NULL,
    tags TEXT NOT NULL,
    category TEXT NOT NULL,
    description TEXT NOT NULL,
    node_id TEXT NOT NULL,
    stage TEXT,
    location TEXT,
    unit TEXT,
    flow_type TEXT,
    impacts_json TEXT NOT NULL,
    warnings_json TEXT NOT NULL,
    raw_json TEXT NOT NULL
);
CREATE INDEX records_norm_name ON records(norm_name);
CREATE INDEX records_node ON records(node_id);
CREATE INDEX records_filters ON records(source, kind, stage, location, unit);
CREATE VIRTUAL TABLE records_fts USING fts5(
    name, tags, category, description,
    content='records', content_rowid='rid', tokenize='unicode61 remove_diacritics 2'
);
CREATE TABLE nodes (
    node_id TEXT PRIMARY KEY,
    parent_id TEXT,
    source TEXT NOT NULL,
    label TEXT NOT NULL,
    depth INTEGER NOT NULL,
    record_count INTEGER NOT NULL,
    vocab TEXT NOT NULL
);
CREATE INDEX nodes_parent ON nodes(parent_id);
CREATE VIRTUAL TABLE nodes_fts USING fts5(
    node_id UNINDEXED, label, vocab, tokenize='unicode61 remove_diacritics 2'
);
"""


def _record_kind(source_kind: str, flow_type: str) -> str:
    if source_kind != "flow":
        return source_kind
    slug = normalize_name(flow_type).replace(" ", "_")
    return slug or "flow"


def _row_to_record(source: str, file_name: str, source_kind: str, record_no: int,
                   row: dict, indicator_columns: list[str]) -> dict:
    name = (row.get("process_name") or row.get("flow_name") or "").strip()
    category = (row.get("category_path") or "").strip()
    parts = split_category(category)
    node_id = "/".join([source, *parts])
    tag_fields = [row.get(k) or "" for k in ("material_tags", "synonyms", "ref_flow",
                                              "cas_number", "chemical_formula")]
    unit = (row.get("unit") or "").strip() or None

    impacts: dict[str, float] = {}
    missing, non_numeric = [], []
    for col in indicator_columns:
        raw = row.get(col)
        value = parse_number(raw)
        if value is not None:
            impacts[col] = value
        elif raw is None or not raw.strip():
            missing.append(col)
        else:
            non_numeric.append(col)

    warnings = []
    if unit is None:
        warnings.append({"code": "missing_reference_unit",
                         "message": "Source row has no explicit reference unit; scaling is blocked."})
    if not impacts:
        warnings.append({"code": "no_impact_indicators",
                         "message": "Row carries no impact indicators; it is descriptive evidence only."})
    if missing:
        warnings.append({"code": "missing_indicator_values", "columns": missing,
                         "message": "Empty indicator cells are missing, not zero."})
    if non_numeric:
        warnings.append({"code": "non_numeric_indicator_values", "columns": non_numeric,
                         "message": "Non-numeric cells are kept in the raw row but not used."})
    if not name:
        warnings.append({"code": "missing_name", "message": "Row has no process or flow name."})

    flow_type = (row.get("flow_type") or "").strip() or None
    return {
        "id": f"{source}:{record_no}",
        "source": source,
        "source_file": file_name,
        "record_no": record_no,
        "kind": _record_kind(source_kind, flow_type or ""),
        "name": name,
        "norm_name": normalize_name(name),
        "tags": " ".join(t.strip() for t in tag_fields if t.strip()),
        "category": category,
        "description": (row.get("description") or "").strip(),
        "node_id": node_id,
        "stage": (row.get("lifecycle_stage") or "").strip() or None,
        "location": (row.get("location") or "").strip() or None,
        "unit": unit,
        "flow_type": flow_type,
        "impacts_json": json.dumps(impacts, ensure_ascii=False),
        "warnings_json": json.dumps(warnings, ensure_ascii=False),
        "raw_json": json.dumps(row, ensure_ascii=False),
    }


def build(data_dir: Path = DATA_DIR, index_path: Path = INDEX_PATH, verbose: bool = True) -> dict:
    """Ingest all CSVs into a temporary database and atomically replace the index."""
    started = time.time()
    index_path = Path(index_path)
    index_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = index_path.with_name(f"{index_path.name}.tmp-{os.getpid()}")
    if tmp_path.exists():
        tmp_path.unlink()

    source_info = {}
    source_meta_path = Path(data_dir) / "SOURCE.json"
    if source_meta_path.exists():
        source_info = json.loads(source_meta_path.read_text(encoding="utf-8"))

    csv.field_size_limit(sys.maxsize)
    conn = sqlite3.connect(tmp_path)
    try:
        conn.executescript(SCHEMA)
        files, audit = {}, {"sources": {}}
        node_counts: Counter = Counter()
        node_vocab: dict[str, Counter] = {}
        node_meta: dict[str, tuple] = {}

        for source, (file_name, source_kind) in SOURCES.items():
            path = Path(data_dir) / file_name
            files[file_name] = {"sha256": sha256_file(path), "bytes": path.stat().st_size}
            stats = Counter()
            names, raw_names = set(), set()
            batch = []
            node_meta[source] = (None, source, source, 0)
            with open(path, newline="", encoding="utf-8-sig") as fh:
                reader = csv.DictReader(fh)
                indicator_columns = [c for c in reader.fieldnames or []
                                     if c not in NON_INDICATOR_COLUMNS
                                     and c not in ("synonyms", "flow_name", "flow_type",
                                                   "cas_number", "chemical_formula")]
                for record_no, row in enumerate(reader, start=1):
                    rec = _row_to_record(source, file_name, source_kind, record_no, row,
                                         indicator_columns)
                    batch.append(rec)
                    names.add(rec["norm_name"])
                    raw_names.add(rec["name"])
                    stats["rows"] += 1
                    if rec["unit"] is None:
                        stats["missing_unit"] += 1
                    if rec["impacts_json"] == "{}":
                        stats["empty_impact_map"] += 1
                    impacts = json.loads(rec["impacts_json"])
                    stats["zero_values"] += sum(1 for v in impacts.values() if v == 0.0)
                    for w in json.loads(rec["warnings_json"]):
                        if w["code"] == "missing_indicator_values":
                            stats["missing_values"] += len(w["columns"])
                        if w["code"] == "non_numeric_indicator_values":
                            stats["non_numeric_values"] += len(w["columns"])

                    # hierarchy bookkeeping: every ancestor node of the record
                    parts = split_category(rec["category"])
                    vocab_terms = {t for t in tokens(rec["name"] + " " + rec["tags"])
                                   if t not in STOPWORDS and not t.isdigit()}
                    for depth in range(0, len(parts) + 1):
                        nid = "/".join([source, *parts[:depth]])
                        if nid not in node_meta:
                            parent = "/".join([source, *parts[:depth - 1]])
                            node_meta[nid] = (parent, source, parts[depth - 1], depth)
                        node_counts[nid] += 1
                        node_vocab.setdefault(nid, Counter()).update(vocab_terms)

                    if len(batch) >= 5000:
                        _insert_records(conn, batch)
                        batch.clear()
                _insert_records(conn, batch)

            audit["sources"][source] = {
                "file": file_name,
                "rows": stats["rows"],
                "missing_reference_unit": stats["missing_unit"],
                "empty_impact_map": stats["empty_impact_map"],
                "distinct_names": len(names),
                "distinct_raw_names": len(raw_names),
                "indicator_columns": len(indicator_columns),
                "missing_indicator_cells": stats["missing_values"],
                "non_numeric_indicator_cells": stats["non_numeric_values"],
                "zero_indicator_values": stats["zero_values"],
            }
            if verbose:
                print(f"  {file_name}: {stats['rows']:,} rows", flush=True)

        conn.execute("INSERT INTO records_fts(records_fts) VALUES ('rebuild')")

        for nid, (parent, source, label, depth) in node_meta.items():
            vocab = " ".join(t for t, _ in node_vocab.get(nid, Counter()).most_common(NODE_VOCAB_SIZE))
            conn.execute("INSERT INTO nodes VALUES (?,?,?,?,?,?,?)",
                         (nid, parent, source, label, depth, node_counts[nid], vocab))
            conn.execute("INSERT INTO nodes_fts(node_id, label, vocab) VALUES (?,?,?)",
                         (nid, label, vocab))

        total = sum(s["rows"] for s in audit["sources"].values())
        built_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        audit.update({"total_records": total, "hierarchy_nodes": len(node_meta)})
        provenance = {
            "schema_version": SCHEMA_VERSION,
            "repository": source_info.get("repository"),
            "commit": source_info.get("commit"),
            "extraction": source_info.get("extraction"),
            "files": files,
            "ingested_at": built_at,
            "record_id_format": "source:record-number (1-based CSV data record)",
        }
        conn.executemany("INSERT INTO meta VALUES (?,?)", [
            ("provenance", json.dumps(provenance)),
            ("audit", json.dumps(audit)),
            ("built_at", built_at),
        ])
        conn.commit()
    except BaseException:
        conn.close()
        tmp_path.unlink(missing_ok=True)
        raise
    conn.close()
    os.replace(tmp_path, index_path)

    (index_path.parent / "provenance.json").write_text(json.dumps(provenance, indent=2))
    (index_path.parent / "audit.json").write_text(json.dumps(audit, indent=2))
    if verbose:
        print(f"Indexed {total:,} records into {len(node_meta)} hierarchy nodes "
              f"in {time.time() - started:.1f}s -> {index_path}")
    return {"provenance": provenance, "audit": audit}


def _insert_records(conn: sqlite3.Connection, batch: list[dict]) -> None:
    conn.executemany(
        """INSERT INTO records (id, source, source_file, record_no, kind, name, norm_name, tags,
               category, description, node_id, stage, location, unit, flow_type,
               impacts_json, warnings_json, raw_json)
           VALUES (:id, :source, :source_file, :record_no, :kind, :name, :norm_name, :tags,
               :category, :description, :node_id, :stage, :location, :unit, :flow_type,
               :impacts_json, :warnings_json, :raw_json)""",
        batch,
    )


# ---------------------------------------------------------------------------
# Retrieval engine
# ---------------------------------------------------------------------------

FILTER_COLUMNS = {"source": "source", "stage": "stage", "location": "location",
                  "unit": "unit", "kind": "kind"}


class Engine:
    """Read-only query interface over a built index. Safe to share across threads."""

    def __init__(self, index_path: Path = INDEX_PATH, dense=None):
        index_path = Path(index_path)
        if not index_path.exists():
            raise FileNotFoundError(f"Index not found at {index_path}; run `python rag.py build`.")
        uri = f"file:{index_path}?mode=ro"
        self._conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self.provenance = json.loads(self._meta("provenance"))
        self.audit = json.loads(self._meta("audit"))
        self.dense = dense  # optional dense.DenseIndex

    def close(self) -> None:
        self._conn.close()

    def _meta(self, key: str) -> str:
        return self._conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()[0]

    def _all(self, sql: str, params=()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    # -- metadata -----------------------------------------------------------

    def health(self) -> dict:
        return {
            "status": "ok",
            "records": self.audit["total_records"],
            "hierarchy_nodes": self.audit["hierarchy_nodes"],
            "sources": {s: v["rows"] for s, v in self.audit["sources"].items()},
            "commit": self.provenance.get("commit"),
            "built_at": self.provenance.get("ingested_at"),
            "dense": self.dense is not None,
        }

    def hierarchy(self, parent: str | None = None) -> list[dict]:
        if parent:
            rows = self._all("SELECT * FROM nodes WHERE parent_id=? ORDER BY label", (parent,))
        else:
            rows = self._all("SELECT * FROM nodes WHERE parent_id IS NULL ORDER BY label")
        return [{"node_id": r["node_id"], "parent_id": r["parent_id"], "label": r["label"],
                 "depth": r["depth"], "record_count": r["record_count"]} for r in rows]

    def distinct(self, column: str) -> list[str]:
        col = FILTER_COLUMNS[column]
        rows = self._all(f"SELECT DISTINCT {col} FROM records WHERE {col} IS NOT NULL ORDER BY 1")
        return [r[0] for r in rows]

    def indicators(self, source: str | None = None) -> list[str]:
        """Original indicator labels, never merged across naming conventions."""
        labels: dict[str, None] = {}
        for src, (file_name, _) in SOURCES.items():
            if source and src != source:
                continue
            row = self._all("SELECT impacts_json FROM records WHERE source=? AND impacts_json != '{}' "
                            "LIMIT 1", (src,))
            if row:
                labels.update(dict.fromkeys(json.loads(row[0][0])))
        return list(labels)

    def record(self, record_id: str) -> dict | None:
        rows = self._all("SELECT * FROM records WHERE id=?", (record_id,))
        if not rows:
            return None
        ev = self._evidence(rows[0])
        ev["impacts"] = json.loads(rows[0]["impacts_json"])
        ev["raw"] = json.loads(rows[0]["raw_json"])
        ev["provenance"] = {
            "repository": self.provenance.get("repository"),
            "commit": self.provenance.get("commit"),
            "file_sha256": self.provenance["files"][rows[0]["source_file"]]["sha256"],
            "ingested_at": self.provenance.get("ingested_at"),
        }
        return ev

    # -- retrieval ----------------------------------------------------------

    def _filter_sql(self, filters: dict | None) -> tuple[str, list]:
        clauses, params = [], []
        for key, value in (filters or {}).items():
            if value in (None, ""):
                continue
            if key not in FILTER_COLUMNS:
                raise ValueError(f"Unknown filter: {key}")
            clauses.append(f"r.{FILTER_COLUMNS[key]} = ?")
            params.append(value)
        return "".join(f" AND {c}" for c in clauses), params

    def route(self, terms: list[str], source: str | None = None) -> list[dict]:
        if not terms:
            return []
        sql = ("SELECT n.node_id, n.label, n.depth, n.record_count, bm25(nodes_fts, 5.0, 1.0) AS s "
               "FROM nodes_fts JOIN nodes n ON n.node_id = nodes_fts.node_id "
               "WHERE nodes_fts MATCH ? AND n.depth >= 1")
        params: list = [fts_or(terms)]
        if source:
            sql += " AND n.source = ?"
            params.append(source)
        sql += " ORDER BY s LIMIT ?"
        params.append(ROUTE_LIMIT)
        return [{"node_id": r["node_id"], "label": r["label"], "depth": r["depth"],
                 "record_count": r["record_count"], "bm25": round(r["s"], 4)}
                for r in self._all(sql, params)]

    def _leaf_search(self, terms, filters, nodes=None, limit=LEAF_LIMIT) -> list[int]:
        where, params = self._filter_sql(filters)
        if nodes is not None:
            if not nodes:
                return []
            ors = " OR ".join("r.node_id = ? OR r.node_id LIKE ? ESCAPE '\\'" for _ in nodes)
            where += f" AND ({ors})"
            for n in nodes:
                escaped = n.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                params.extend([n, escaped + "/%"])
        w = ", ".join(str(x) for x in BM25_WEIGHTS)
        sql = (f"SELECT r.rid FROM records_fts JOIN records r ON r.rid = records_fts.rowid "
               f"WHERE records_fts MATCH ?{where} ORDER BY bm25(records_fts, {w}) LIMIT ?")
        return [r[0] for r in self._all(sql, [fts_or(terms), *params, limit])]

    def _exact_name(self, text: str, filters) -> list[int]:
        norm = normalize_name(text)
        if not norm:
            return []
        where, params = self._filter_sql(filters)
        return [r[0] for r in self._all(
            f"SELECT r.rid FROM records r WHERE r.norm_name = ?{where} ORDER BY r.rid LIMIT 50",
            [norm, *params])]

    def search(self, text: str, filters: dict | None = None, top_k: int = 5,
               use_dense: bool = False) -> dict:
        """Hierarchical retrieval: route -> routed + global leaf search -> RRF -> collapse."""
        started = time.perf_counter()
        content, expanded = query_terms(text)
        route = self.route(expanded, (filters or {}).get("source"))
        routed_nodes = [n["node_id"] for n in route]
        global_ids = self._leaf_search(expanded, filters) if expanded else []
        routed_ids = self._leaf_search(expanded, filters, routed_nodes) if expanded else []
        exact_ids = self._exact_name(text, filters)

        dense_ranks, cosines = {}, {}
        if use_dense and self.dense is not None:
            for rank, (rid, cos) in enumerate(self.dense.search(text, self, filters, LEAF_LIMIT), 1):
                dense_ranks[rid] = rank
                cosines[rid] = cos

        g_rank = {rid: i for i, rid in enumerate(global_ids, 1)}
        r_rank = {rid: i for i, rid in enumerate(routed_ids, 1)}
        scores: dict[int, float] = {}
        for rid in set(g_rank) | set(r_rank) | set(exact_ids) | set(dense_ranks):
            s = 0.0
            if rid in g_rank:
                s += 1.0 / (RRF_K + g_rank[rid])
            if rid in r_rank:
                s += 1.0 / (RRF_K + r_rank[rid])
            if rid in dense_ranks:
                s += DENSE_RRF_WEIGHT / (RRF_K + dense_ranks[rid])
            if rid in exact_ids:
                s += 1.0  # exact normalized-name rescue
            scores[rid] = s
        ordered = sorted(scores, key=lambda rid: (-scores[rid], g_rank.get(rid, 10**9), rid))

        candidates, seen = [], {}
        for rid in ordered:
            row = self._all("SELECT * FROM records WHERE rid=?", (rid,))[0]
            key = (row["source"], row["name"], row["stage"], row["location"], row["unit"],
                   row["category"], row["impacts_json"])
            if key in seen:
                seen[key]["duplicate_ids"].append(row["id"])
                continue
            ev = self._evidence(row)
            ev["scores"] = {
                "rrf": round(scores[rid], 6),
                "global_rank": g_rank.get(rid),
                "routed_rank": r_rank.get(rid),
                "exact_name": rid in exact_ids,
            }
            if rid in dense_ranks:
                ev["scores"].update(dense_rank=dense_ranks[rid], cosine=round(cosines[rid], 4))
            ev["term_coverage"] = round(term_coverage(content, row), 3)
            seen[key] = ev
            candidates.append(ev)
            if len(candidates) >= top_k:
                break
        return {
            "query": text,
            "terms": content,
            "expanded_terms": expanded,
            "filters": {k: v for k, v in (filters or {}).items() if v not in (None, "")},
            "route": route,
            "candidates": candidates,
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
        }

    def _evidence(self, row: sqlite3.Row) -> dict:
        path = " > ".join([row["source"], *split_category(row["category"])])
        return {
            "id": row["id"],
            "source": row["source"],
            "record_no": row["record_no"],
            "kind": row["kind"],
            "name": row["name"],
            "hierarchy_path": path,
            "node_id": row["node_id"],
            "stage": row["stage"],
            "location": row["location"],
            "reference_unit": row["unit"],
            "flow_type": row["flow_type"],
            "indicator_count": len(json.loads(row["impacts_json"])),
            "warnings": json.loads(row["warnings_json"]),
            "duplicate_ids": [],
            "citation": (f"{row['source_file']} record {row['record_no']} "
                         f"({self.provenance.get('repository')}@{(self.provenance.get('commit') or '')[:12]})"),
        }

    # -- answers ------------------------------------------------------------

    def query(self, text: str, filters: dict | None = None, indicator: str | None = None,
              top_k: int = 5, use_ai: bool = False, provider: str | None = None,
              use_dense: bool = False, fallback=None) -> dict:
        """Extractive, evidence-first answer. Never fabricates a value from the index."""
        result = self.search(text, filters, top_k, use_dense=use_dense)
        reasons = []
        top = result["candidates"][0] if result["candidates"] else None
        impacts = {}
        if top is None:
            reasons.append("no_candidates")
        else:
            impacts = json.loads(self._all("SELECT impacts_json FROM records WHERE id=?",
                                           (top["id"],))[0][0])
            if top["term_coverage"] < MIN_TERM_COVERAGE:
                reasons.append("weak_term_coverage")
            if indicator is not None:
                if indicator not in impacts:
                    reasons.append("missing_indicator")
                if top["reference_unit"] is None:
                    reasons.append("missing_reference_unit")

        answer = None
        if top is not None and not reasons and indicator is not None:
            answer = {
                "record_id": top["id"],
                "indicator": indicator,
                "value": impacts[indicator],
                "per_reference_unit": top["reference_unit"],
                "impact_unit": None,
                "note": "Impact unit is not stated in the source export; the value is per one "
                        "reference unit under the source's (unstated) characterization method.",
                "citation": top["citation"],
            }
        result.update({
            "status": "insufficient_evidence" if reasons else "ok",
            "reasons": reasons,
            "indicator": indicator,
            "answer": answer,
            "selection_note": "Candidates are ranked lexical matches, not scientifically "
                              "verified process selections. Review geography, stage, and unit.",
            "fallback": None,
        })
        if reasons and use_ai:
            if fallback is None:
                import ai_fallback
                fallback = ai_fallback.estimate
            result["fallback"] = fallback(
                query=text, indicator=indicator,
                reference_unit=(filters or {}).get("unit") or (top or {}).get("reference_unit"),
                constraints=result["filters"], context=result["candidates"][:5],
                provider=provider)
        return result

    def calculate(self, record_id: str, indicator: str, quantity, unit: str) -> dict:
        rows = self._all("SELECT * FROM records WHERE id=?", (record_id,))
        if not rows:
            raise CalculationError("unknown_record", f"No record {record_id!r}.")
        row = rows[0]
        impacts = json.loads(row["impacts_json"])
        if indicator not in impacts:
            raise CalculationError("missing_indicator",
                                   f"Record {record_id} has no indicator {indicator!r}; "
                                   "missing values are never treated as zero.")
        if row["unit"] is None:
            raise CalculationError("missing_reference_unit",
                                   f"Record {record_id} has no explicit reference unit; "
                                   "scaling is refused rather than assuming kg.")
        if not isinstance(unit, str) or unit.strip() != row["unit"]:
            raise CalculationError("unit_mismatch",
                                   f"Input unit {unit!r} does not match reference unit "
                                   f"{row['unit']!r}; no conversion is performed.")
        if isinstance(quantity, bool):
            raise CalculationError("invalid_quantity", "Quantity must be a number.")
        try:
            qty = float(quantity)
        except (TypeError, ValueError):
            raise CalculationError("invalid_quantity", "Quantity must be a number.") from None
        if not math.isfinite(qty) or qty <= 0:
            raise CalculationError("invalid_quantity", "Quantity must be finite and positive.")
        factor = impacts[indicator]
        return {
            "record_id": record_id,
            "name": row["name"],
            "indicator": indicator,
            "factor": factor,
            "quantity": qty,
            "unit": row["unit"],
            "result": factor * qty,
            "formula": f"{factor} x {qty} {row['unit']}",
            "impact_unit": None,
            "citation": self._evidence(row)["citation"],
        }


def term_coverage(content_terms: list[str], row) -> float:
    """Share of the query's content terms (or their expansions) found in the record."""
    if not content_terms:
        return 0.0
    vocab = set(tokens(f"{row['name']} {row['tags']} {row['category']}"))
    covered = 0
    for term in content_terms:
        expansions = ABBREVIATIONS.get(term, ())
        if term in vocab or (expansions and all(t in vocab for t in expansions)):
            covered += 1
    return covered / len(content_terms)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("build", help="ingest CSVs and rebuild the index atomically")
    q = sub.add_parser("query", help="retrieve evidence for a query")
    q.add_argument("text")
    q.add_argument("--indicator")
    q.add_argument("--top-k", type=int, default=5)
    for f in FILTER_COLUMNS:
        q.add_argument(f"--{f}")
    q.add_argument("--ai", action="store_true", help="enable provisional AI fallback")
    q.add_argument("--provider", choices=["groq", "claude", "ollama"])
    q.add_argument("--dense", action="store_true", help="fuse optional dense (cosine) ranking")
    c = sub.add_parser("calc", help="unit-checked calculation")
    c.add_argument("record_id")
    c.add_argument("indicator")
    c.add_argument("quantity")
    c.add_argument("unit")
    args = parser.parse_args(argv)

    if args.cmd == "build":
        build()
        return 0
    engine = Engine()
    if args.cmd == "query":
        if args.dense:
            import dense
            engine.dense = dense.DenseIndex.load()
        filters = {f: getattr(args, f) for f in FILTER_COLUMNS}
        out = engine.query(args.text, filters, args.indicator, args.top_k,
                           use_ai=args.ai, provider=args.provider, use_dense=args.dense)
    else:
        try:
            out = engine.calculate(args.record_id, args.indicator, args.quantity, args.unit)
        except CalculationError as exc:
            out = {"error": exc.code, "message": str(exc)}
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
