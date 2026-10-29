"""Vercel entry point: the whole app runs as one Python serverless function.

vercel.json rewrites every path here. The search index is built into /tmp from the
bundled CSVs on a cold start (the 209 MB index would not fit Vercel's 250 MB function
limit together with the code), and AI answers and saved analyses are kept in Redis
(see store.py) because serverless functions have no persistent disk.
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("LCA_STATE_DIR", "/tmp/lca_state")  # writable scratch space

import rag  # noqa: E402

INDEX = rag.INDEX_PATH if rag.INDEX_PATH.exists() else Path("/tmp/lca_rag/lca_rag.sqlite")
if not INDEX.exists():
    rag.build(rag.DATA_DIR, INDEX, verbose=False)

import server  # noqa: E402

engine = rag.Engine(INDEX)


class handler(server.make_handler(engine)):  # noqa: N801 (Vercel looks for a top-level "handler")
    """The app's request handler, as the class Vercel's Python runtime serves."""
