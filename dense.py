"""Optional dense (cosine-similarity) retrieval, fused into rag.py as a third RRF list.

Requires: numpy, sentence-transformers (not needed for the core BM25 system).

    python dense.py build                 # improved config, all records
    python dense.py build --config upstream --scope processes

Two configurations are provided so the cosine score can be compared directly:

  upstream  - reproduces lca_backend: all-MiniLM-L6-v2, passage = name + tags + full
              category path, raw query text, no query instruction.
  improved  - BAAI/bge-small-en-v1.5 (asymmetric retrieval model) with its query
              instruction, passage text without boilerplate ("other" tags dropped,
              category reduced to its last two levels, unit and flow type added),
              and the rag.py abbreviation map applied to the query before embedding
              (e.g. "PET" -> "PET polyethylene terephthalate").

Cosine values are model-specific and are reported as ranking evidence only, never as
a confidence or as P(record is scientifically correct).
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
from pathlib import Path

import numpy as np

import rag

DENSE_DIR = rag.INDEX_DIR / "dense"

CONFIGS = {
    "upstream": {
        "model": "sentence-transformers/all-MiniLM-L6-v2",
        "query_prefix": "",
        "passage": "upstream",
        "expand_query": False,
    },
    "improved": {
        "model": "BAAI/bge-small-en-v1.5",
        "query_prefix": "Represent this sentence for searching relevant passages: ",
        "passage": "clean",
        "expand_query": True,
    },
}

SCOPES = {
    "processes": "kind IN ('process', 'impact_export')",
    "all": "1=1",
}


def passage_text(row, style: str) -> str:
    if style == "upstream":
        return " ".join(x for x in (row["name"], row["tags"], row["category"]) if x).strip()
    tags = " ".join(t for t in row["tags"].split() if t.lower() != "other")
    category = " > ".join(rag.split_category(row["category"])[-2:])
    parts = [row["name"]]
    if tags:
        parts.append(f"Material: {tags}")
    if category:
        parts.append(f"Category: {category}")
    if row["flow_type"]:
        parts.append(f"Type: {row['flow_type']}")
    if row["unit"]:
        parts.append(f"Unit: {row['unit']}")
    return ". ".join(parts)


def expand_query(text: str) -> str:
    extra = []
    for term in rag.tokens(text):
        extra.extend(rag.ABBREVIATIONS.get(term, ()))
    return f"{text} {' '.join(dict.fromkeys(extra))}".strip() if extra else text


def _device() -> str:
    import torch
    if torch.backends.mps.is_available():
        return "mps"
    return "cuda" if torch.cuda.is_available() else "cpu"


class DenseIndex:
    def __init__(self, config: str, scope: str, rids: np.ndarray, emb: np.ndarray, model=None):
        self.config_name = config
        self.config = CONFIGS[config]
        self.scope = scope
        self.rids = rids
        self.emb = emb.astype(np.float32)
        self._model = model
        self._meta = None  # rid -> filter columns, loaded lazily

    @property
    def model(self):
        if self._model is None:
            from sentence_transformers import SentenceTransformer
            self._model = SentenceTransformer(self.config["model"], device=_device())
        return self._model

    @staticmethod
    def path_for(config: str, scope: str) -> Path:
        return DENSE_DIR / f"{config}-{scope}.npz"

    @classmethod
    def build(cls, config: str = "improved", scope: str = "all",
              index_path: Path = rag.INDEX_PATH, batch_size: int = 256) -> "DenseIndex":
        conn = sqlite3.connect(index_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(f"SELECT rid, name, tags, category, unit, flow_type FROM records "
                            f"WHERE {SCOPES[scope]} ORDER BY rid").fetchall()
        conn.close()
        cfg = CONFIGS[config]
        texts = [passage_text(r, cfg["passage"]) for r in rows]
        inst = cls(config, scope, np.array([r["rid"] for r in rows], dtype=np.int64),
                   np.zeros((0, 0)))
        started = time.time()
        print(f"Embedding {len(texts):,} passages with {cfg['model']} on {_device()}...", flush=True)
        inst.emb = inst.model.encode(texts, batch_size=batch_size, normalize_embeddings=True,
                                     show_progress_bar=True, convert_to_numpy=True).astype(np.float32)
        print(f"Done in {time.time() - started:.0f}s")
        DENSE_DIR.mkdir(parents=True, exist_ok=True)
        np.savez(cls.path_for(config, scope), rids=inst.rids, emb=inst.emb.astype(np.float16),
                 info=json.dumps({"config": config, "scope": scope, **cfg}))
        return inst

    @classmethod
    def load(cls, config: str = "improved", scope: str = "all") -> "DenseIndex":
        path = cls.path_for(config, scope)
        if not path.exists():
            raise FileNotFoundError(f"{path} missing; run `python dense.py build --config {config} "
                                    f"--scope {scope}`")
        data = np.load(path)
        return cls(config, scope, data["rids"], data["emb"])

    def embed_query(self, text: str) -> np.ndarray:
        if self.config["expand_query"]:
            text = expand_query(text)
        vec = self.model.encode([self.config["query_prefix"] + text], normalize_embeddings=True)[0]
        return vec.astype(np.float32)

    def _filter_mask(self, engine: rag.Engine, filters: dict | None) -> np.ndarray:
        active = {k: v for k, v in (filters or {}).items() if v not in (None, "")}
        if not active:
            return np.ones(len(self.rids), dtype=bool)
        if self._meta is None:
            rows = engine._all("SELECT rid, source, kind, stage, location, unit FROM records")
            pos = {rid: i for i, rid in enumerate(self.rids.tolist())}
            self._meta = {c: np.empty(len(self.rids), dtype=object)
                          for c in ("source", "kind", "stage", "location", "unit")}
            for r in rows:
                i = pos.get(r["rid"])
                if i is not None:
                    for c in self._meta:
                        self._meta[c][i] = r[c]
        mask = np.ones(len(self.rids), dtype=bool)
        for key, value in active.items():
            mask &= self._meta[rag.FILTER_COLUMNS[key]] == value
        return mask

    def search(self, text: str, engine: rag.Engine, filters: dict | None = None,
               limit: int = rag.LEAF_LIMIT) -> list[tuple[int, float]]:
        """Return [(rid, cosine)] for the top `limit` records that pass the exact filters."""
        sims = self.emb @ self.embed_query(text)
        mask = self._filter_mask(engine, filters)
        idx = np.flatnonzero(mask)
        if idx.size == 0:
            return []
        k = min(limit, idx.size)
        top = idx[np.argpartition(-sims[idx], k - 1)[:k]]
        top = top[np.argsort(-sims[top], kind="stable")]
        return [(int(self.rids[i]), float(sims[i])) for i in top]


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--config", choices=CONFIGS, default="improved")
    b.add_argument("--scope", choices=SCOPES, default="all")
    q = sub.add_parser("query")
    q.add_argument("text")
    q.add_argument("--config", choices=CONFIGS, default="improved")
    q.add_argument("--scope", choices=SCOPES, default="all")
    args = parser.parse_args()
    if args.cmd == "build":
        DenseIndex.build(args.config, args.scope)
        return
    engine = rag.Engine()
    dense = DenseIndex.load(args.config, args.scope)
    for rid, cos in dense.search(args.text, engine, limit=10):
        row = engine._all("SELECT id, name FROM records WHERE rid=?", (rid,))[0]
        print(f"{cos:.4f}  {row['id']:32}  {row['name'][:90]}")


if __name__ == "__main__":
    main()
