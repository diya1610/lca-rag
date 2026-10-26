"""Retrieval benchmark: Hit@1, Hit@5, MRR@5, latency, and cosine diagnostics.

    python eval/evaluate.py                         # BM25 + RRF (paper system), cases.json
    python eval/evaluate.py --compare-dense         # also dense-only (upstream/improved) and hybrid
    python eval/evaluate.py --cases eval/paraphrase_cases.json --compare-dense

A record counts as relevant if it is an expected record or has an identical displayed
payload (the engine collapses such duplicates). Results on development-selected cases
measure regression behaviour, not generalization or scientific accuracy.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import rag  # noqa: E402

K = 5


def relevant_ids(engine: rag.Engine, source: str, record_nos: list[int]) -> set[str]:
    out = set()
    for no in record_nos:
        row = engine._all("SELECT * FROM records WHERE id=?", (f"{source}:{no}",))[0]
        same = engine._all(
            "SELECT id FROM records WHERE source=? AND name=? AND stage IS ? AND location IS ? "
            "AND unit IS ? AND category=? AND impacts_json=?",
            (row["source"], row["name"], row["stage"], row["location"], row["unit"],
             row["category"], row["impacts_json"]))
        out.update(r[0] for r in same)
    return out


def first_rank(ranked_ids: list[list[str]], relevant: set[str]) -> int | None:
    for i, ids in enumerate(ranked_ids, 1):
        if relevant.intersection(ids):
            return i
    return None


def run_mode(engine, cases, mode: str, dense_by_cfg: dict) -> dict:
    rows, latencies = [], []
    for case in cases:
        source = case["filters"].get("source", "bottles_processes")
        rel = relevant_ids(engine, source, case["expected"])
        t0 = time.perf_counter()
        diag = {}
        if mode in ("bm25", "hybrid"):
            res = engine.search(case["query"], case["filters"], top_k=K, use_dense=(mode == "hybrid"))
            ranked = [[c["id"], *c["duplicate_ids"]] for c in res["candidates"]]
        else:
            dense = dense_by_cfg[mode.split("-", 1)[1]]
            hits = dense.search(case["query"], engine, case["filters"], limit=50)
            id_of = {rid: engine._all("SELECT id FROM records WHERE rid=?", (rid,))[0][0]
                     for rid, _ in hits}
            ranked = [[id_of[rid]] for rid, _ in hits]
            cos_rel = [c for rid, c in hits if id_of[rid] in rel]
            cos_non = [c for rid, c in hits if id_of[rid] not in rel]
            diag = {"top1_cosine": hits[0][1] if hits else None,
                    "relevant_cosine": max(cos_rel) if cos_rel else None,
                    "margin": (max(cos_rel) - max(cos_non)) if cos_rel and cos_non else None}
        latencies.append((time.perf_counter() - t0) * 1000)
        rank = first_rank(ranked[:K], rel)
        rows.append({"query": case["query"], "rank": rank, "top1": ranked[0][0] if ranked else None, **diag})

    ranks = [r["rank"] for r in rows]
    summary = {
        "mode": mode,
        "n": len(rows),
        "hit@1": sum(1 for r in ranks if r == 1) / len(rows),
        "hit@5": sum(1 for r in ranks if r is not None) / len(rows),
        "mrr@5": sum(1 / r for r in ranks if r) / len(rows),
        "mean_latency_ms": round(statistics.mean(latencies), 1),
        "cases": rows,
    }
    if mode.startswith("dense-"):
        for key in ("relevant_cosine", "margin"):
            vals = [r[key] for r in rows if r.get(key) is not None]
            summary[f"mean_{key}"] = round(statistics.mean(vals), 4) if vals else None
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", default=str(ROOT / "eval" / "cases.json"))
    parser.add_argument("--compare-dense", action="store_true")
    parser.add_argument("--scope", default="all", choices=["all", "processes"])
    parser.add_argument("--out", help="write JSON results here")
    args = parser.parse_args()

    cases = json.loads(Path(args.cases).read_text())["cases"]
    engine = rag.Engine()
    modes, dense_by_cfg = ["bm25"], {}
    if args.compare_dense:
        import dense
        for cfg in ("upstream", "improved"):
            dense_by_cfg[cfg] = dense.DenseIndex.load(cfg, args.scope)
            dense_by_cfg[cfg].search("warm up", engine)  # load model outside timing
        engine.dense = dense_by_cfg["improved"]
        modes += ["dense-upstream", "dense-improved", "hybrid"]

    results = [run_mode(engine, cases, m, dense_by_cfg) for m in modes]
    print(f"{Path(args.cases).name}: {len(cases)} cases, top-{K}\n")
    print(f"{'mode':16} {'Hit@1':>6} {'Hit@5':>6} {'MRR@5':>6} {'ms':>7} {'cos(rel)':>9} {'margin':>8}")
    for s in results:
        cos = s.get("mean_relevant_cosine")
        mar = s.get("mean_margin")
        print(f"{s['mode']:16} {s['hit@1']:6.2f} {s['hit@5']:6.2f} {s['mrr@5']:6.2f} "
              f"{s['mean_latency_ms']:7.1f} {'' if cos is None else f'{cos:9.3f}'}"
              f"{'' if mar is None else f'{mar:9.3f}'}")
    misses = [(s["mode"], r["query"], r["rank"], r["top1"]) for s in results for r in s["cases"]
              if r["rank"] != 1]
    if misses:
        print("\nNot at rank 1:")
        for m in misses:
            print(f"  {m[0]:16} {m[1]!r:45} rank={m[2]} top1={m[3]}")
    if args.out:
        Path(args.out).write_text(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
