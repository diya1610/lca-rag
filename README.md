# LCA RAG — Hierarchical Retrieval for Evidence-Grounded Life Cycle Assessment

Implementation of *"Hierarchical Retrieval Augmented Generation for Evidence-Grounded Life
Cycle Assessment"* (Srivastav, Patel, Bhathena — MPSTME, NMIMS). An auditable capstone
prototype. **It is not a certified LCA calculator.**


source → category → record hierarchy (410 nodes). It then:

- retrieves candidates with SQLite FTS5 BM25 and reciprocal rank fusion;
- returns source-faithful evidence with citations;
- refuses unit-unsafe calculations;
- abstains when evidence is insufficient, or optionally asks Groq, Claude or Ollama for a
  **validated, clearly-labelled provisional** estimate.

## Quick start

**Web interface (recommended):**

```bash
python server.py --open        # http://127.0.0.1:8765, opens in your browser
```

The web page has search with exact filters and an indicator picker. It shows:

- an evidence card with its citation;
- a separate, dashed **provisional AI estimate** card with its range, assumptions and
  missing information;
- ranked candidate cards. Click one to see the original CSV row, provenance, every
  indicator, and a unit-checked calculator.

A hierarchy browser and AI-provider status are also included. AI keys are read from
`GROQ_API_KEY` / `ANTHROPIC_API_KEY` or from private files in `~/.config/lca_rag/`
(`groq_key`, `anthropic_key`). The server accepts only JSON POSTs addressed to
`127.0.0.1`/`localhost`, so other websites cannot trigger queries or AI calls through
your browser.

**Other entry points:**

```bash
python rag.py build            # ~6 s; atomic rebuild of index/lca_rag.sqlite
python desktop.py              # Tkinter evidence client
python server.py               # loopback API on http://127.0.0.1:8765
python -m unittest discover -s tests -v    # 51 tests (the paper's 26 + Claude, web, eBOM)
python eval/evaluate.py        # 12-query development benchmark
```

After cloning, the index is built automatically on first start (or run `python rag.py build`).
The core needs Python 3.10+ only, with no third-party packages. Your SQLite must include
FTS5, which the standard CPython builds do.

CLI examples:

```bash
python rag.py query "bottle-grade PET" --source bottles_processes --indicator "Climate change"
python rag.py calc bottles_processes:232 "Climate change" 2 kg
python rag.py query "recycled carbon fibre composite" --indicator "Climate change" --ai --provider ollama
```

## eBOM analysis dashboard

Upload a bill-of-materials CSV in the **eBOM analysis** tab, or run `python ebom.py bom.csv`.
`web/sample_ebom.csv` is an example. Columns are detected case-insensitively:

- material and quantity (required);
- unit (strongly recommended); a header like `weight_kg` also counts as the unit;
- optionally component or assembly, item ID, country and cost.

**Resolution ladder.** Each line is resolved in this order, and its basis is shown
everywhere:

1. **Exact record:** a database match, using the same retrieval, abstention and unit rules
   as a single query. The line's unit is an exact filter.
2. **Similar material:** used when there is no exact match, or when the match's factor is
   implausible. It is a record from the same material family (steel, plastics, paper,
   glass, …; from the export's `material_tags`) with the same unit and a plausible factor.
   It is either the closest lexical match in the family or the family's median.
3. **AI estimate:** used when no plausible similar material exists and the AI fallback is
   enabled. It is validated and labelled. AI answers are cached for the session, so every
   view shows the same value.
4. **Not covered** or **invalid** (no quantity, no unit, or an ambiguous quantity such as
   `1,500`).

**Units.** Mass units (g, mg, t, lb, oz) are converted to kg explicitly and reported. Other
units are never converted or assumed. AI estimates in g, kg or t CO₂-eq are normalised
before summing; any other impact unit is excluded.

**Implausible source data.** Whole families in the export look recorded per tonne while
labelled "kg": Climate change for cement is 903, zinc 3200, aluminium 2461–3229 and copper
797–1323. A factor more than 50× the median for its unit is never used as evidence or as a
similar-material proxy, and the affected line says why.

**Scoring.** The scoring is reverse-engineered from `lca_backend/engine.py`:

- **Criticality:** EU/US critical-raw-material keyword tiers (90/55/20, unknown 40), falling
  back to the material family when no keyword matches.
- **Toxicity:** 70% human toxicity + 30% metal depletion, per kg.
- **GHG:** 40% intensity + 60% absolute (500 kg CO₂-eq item reference).
- **Item score:** 30% criticality + 35% toxicity + 35% GHG, from 0 to 100 (higher is worse).
- **Grades:** A ≤ 30, B ≤ 60, C ≤ 80, D above.
- **Product score:** the GHG-weighted mean of the item scores.
- **Hotspot:** an item above 20% of total GHG.

Upstream hard-coded the per-kg references (GHG 12.5, toxicity 150, metal depletion 1.5) and
described them as dataset 95th percentiles. The real 95th percentiles are 8.24, 0.44 and
0.016, so upstream's toxicity score was always about 0. Here the references are computed
from the data. Sub-scores a line cannot have (for example toxicity for an AI estimate) are
left out, and the weights are re-normalised.

**Multi-material eBOMs** (e.g. `web/sample_iphone16e_bom.csv`, the iPhone 16e teardown BoM
also used in the AI4LCI Kaggle notebook):

- A plural material header such as `Main Materials` is split on top-level commas, so
  "Li-ion (Li, Co, Ni, Graphite), Aluminum, Copper" becomes three materials.
- Mass units in headers (`Estimated Weight (g)`) are recognised.
- Each component's mass is split across its materials using one of three modes, and the
  split is always labelled:
  - **by listed order** (default; the first-listed material is the main one);
  - **equally** (as the notebook does);
  - by an **AI-estimated composition**. This is validated: fractions of exactly the listed
    materials, summing to 1, with assumptions.
- An `Electricity_Usage_kWh` column becomes one electricity line per component.
- A recyclability column is shown, and "difficult to recycle" adds advice.
- Bare material names ("Glass", "Copper") use the family's typical value. Disposal and
  transport processes are never used as a material's evidence.

**Electricity.** The database's grid mixes are labelled per MJ, but their values are per kWh.
Read per MJ, Germany would emit 2.3 kg CO₂ per kWh, more than any power plant. They are
therefore used only with reviewed corrections on (see `data/corrections.json`). The database
has only European grids, so `data/grid_references.json` adds labelled **reference values**
for manufacturing regions: China 950 and US-California 400 g CO₂-eq/kWh, the notebook's
fallback intensities. China is the default grid. Reference-value lines form their own basis,
separate from database evidence.

**AI eco-design ideas.** Each hotspot component has an "Ask AI for eco-design ideas" button.
It returns material, energy and circularity suggestions. They are validated, labelled
AI-written, and saved.

**Caution on AI estimates.** Two calls for the same material can differ by orders of
magnitude. In testing, gold came back once as 25,000 and once as 25 kg CO₂-eq/kg, an
apparent tonne/kg slip. Saved answers keep views consistent, but they are still provisional:
review the AI rows (each shows its range and explanation) before relying on a total.

**Options** (eBOM tab, or `analyse(..., scoring=, grade_basis=, apply_corrections=, split_mode=, grid=)`):

- *Grade by*: the combined score (default) or **GHG only**. Both grades are always shown.
- *Scoring references*: from the data (default) or the **upstream constants**. Upstream mode
  reproduces lca_backend exactly, which means toxicity is about 0, so you can compare.
- *Apply reviewed unit corrections* (off by default). It applies the entries in
  `data/corrections.json`: cement and zinc are divided by 1000, each with its reason and
  review date. Corrected values are labelled **corrected** everywhere. Aluminium and copper
  are deliberately not corrected, because dividing by 1000 still gives implausible values,
  so they need re-exporting from openLCA.
- **AI answers are saved** to `index/ai_cache.json`, which is not committed. Demos therefore
  show the same provisional values after a restart. "Clear saved AI answers" or
  `POST /ebom/clear-ai-cache` makes the next run ask again.

**Dashboard contents:**

- **KPI cards:** total GHG split by basis, overall score and grade, hotspots, and the share of
  high-confidence matches.
- **Charts:** a radar of the three sub-scores, GHG by component (stacked by basis), and a
  Pareto chart per item with cumulative share.
- **Eco-design recommendations:** lower-impact *sister materials* of the same family and
  unit. Cost shows as n/a, because the LCA exports have no cost data.
- **What-if simulator:** up to three scenarios of material swaps, each re-running the full
  pipeline. Each shows the GHG change (kg and %), the grade change, and a short design note.
  The note is AI-written and labelled when the AI is enabled, and rule-based otherwise.
- **Tables:** a sortable, filterable item table (score, grade, confidence, hotspot, notes),
  a material-wise table, and a CSV export.

## Deploying (Vercel + Upstash Redis + a GoDaddy domain)

The whole app runs as one Python serverless function (`api/index.py`, configured by
`vercel.json`). It needs no third-party packages.

- **Search index:** on a cold start the function builds the index into `/tmp` from the
  bundled CSVs. The 209 MB index does not fit Vercel's 250 MB function limit alongside the
  code. The build takes roughly 6–15 s, once per instance.
- **Storage:** AI answers and saved analyses go to **Redis**, via `store.py` and Upstash's REST
  API, because serverless functions have no persistent disk. Without Redis they last only as
  long as one instance.
- **Production guards:** hosts outside `ALLOWED_HOSTS` and Vercel's own URLs are rejected. An
  optional `SITE_PASSWORD` turns on HTTP Basic auth. AI-using requests are limited per
  visitor per hour (`AI_RATE_LIMIT`, default 30). Clearing saved AI answers is disabled on a
  public deployment.

Steps:

1. **Vercel:** on vercel.com, choose *Add New → Project* and import this GitHub repository.
   Framework preset: *Other*; leave the build command empty.
2. **Redis:** in the project, open *Storage → Marketplace → Upstash for Redis → Create* and
   connect it to the project. This sets `KV_REST_API_URL` and `KV_REST_API_TOKEN`.
3. **Environment variables:** set `GROQ_API_KEY` (your key), `LCA_AI_PROVIDER=groq`,
   `ALLOWED_HOSTS=yourdomain.com,www.yourdomain.com` and, optionally, `SITE_PASSWORD`.
   Then redeploy.
4. **Domain:** in Vercel, open *Settings → Domains* and add `yourdomain.com` and
   `www.yourdomain.com`. In GoDaddy, open *DNS* for the domain and add the records Vercel
   shows: usually an **A** record for `@` pointing to `76.76.21.21` and a **CNAME** record for
   `www` pointing to `cname.vercel-dns.com`. Remove any conflicting GoDaddy
   "parked"/forwarding records. Vercel issues HTTPS automatically once DNS resolves.

## Layout

| File | Role |
|---|---|
| `rag.py` | Ingestion, provenance and audit, hierarchy, FTS5 index, routing, RRF, abstention, unit-checked calculation, CLI |
| `ai_fallback.py` | Groq / Claude / Ollama provisional fallback: JSON contract, validator, provenance tags |
| `server.py` | Standard-library HTTP API, bound to `127.0.0.1` only |
| `desktop.py` | Tkinter client: search, record-kind, source, stage, exact location and unit filters, evidence table, raw-record view, indicator selector, calculator, AI checkbox |
| `dense.py` | *Optional* cosine-similarity retrieval, fused as a third RRF list (see below) |
| `data/` | The four source CSVs plus `SOURCE.json` (repository and commit) |
| `index/` | Built index, `provenance.json` (commit, SHA-256 per file, timestamp), `audit.json` |
| `eval/` | Benchmark cases, paraphrase cases, `evaluate.py`, result JSON |
| `ebom.py` | eBOM analysis: column detection, exact → similar material → AI resolution, upstream-style scoring and grades, hotspots, sister materials, what-if scenarios |
| `web/` | Web interface served by `server.py` (no build step, no external assets) |
| `tests/` | 51 functional tests (the paper's 26 plus Claude, web and eBOM): real index, real loopback HTTP server, mocked providers |

## How it maps to the paper

- **Record identity (V-A).**
  - Each CSV row becomes the record `source:record-number` (1-based data row), with the
    full original row kept as JSON.
  - Numeric indicators are parsed only when finite. An empty cell is *missing*, not zero,
    and non-numeric cells are kept but never used.
- **Hierarchy (V-B).**
  - Every prefix of `category_path` becomes a node under its source node.
  - Each node stores its parent, label, depth, record count, and a vocabulary of the top
    300 terms from descendant names and tags. Nodes are indexed in their own FTS5 table.
- **Retrieval (V-C).**
  - The query is stop-word filtered, and a small abbreviation map expands PET, HDPE, LDPE
    and aluminum/aluminium.
  - BM25 routes the query to up to 12 branches. A leaf BM25 search then runs both inside
    those branches and globally, and the two rankings are merged with
    `S = 1/(60+r_g) + 1/(60+r_r)`.
  - An exact normalized name match adds a rescue score of 1.
  - Exact SQL predicates for source, stage, location, unit and kind are applied before
    ranking.
  - Results with identical displayed payloads are collapsed. Their IDs stay listed in
    `duplicate_ids`.
- **Abstention (V-D).** The status is `insufficient_evidence` when any of these hold:
  - there are no candidates;
  - the best match covers less than 75% of the query terms. At 50%, "stainless steel"
    returned steel rebar and "recycled carbon fibre composite" returned corrugated board;
  - the requested indicator is absent;
  - the reference unit is missing.

  Calculation additionally requires an exact unit match and a finite, positive quantity.
  The system never assumes kg.
- **AI fallback (V-E).**
  - Up to 5 candidate summaries are sent to the model as *untrusted data*.
  - The validator rejects any of the following:
    - non-JSON or oversized responses;
    - missing or mistyped fields, or booleans in place of numbers;
    - non-finite or unordered ranges;
    - a changed indicator or unit, or an unknown unit;
    - estimates with no assumptions or no system boundary.
  - Output is tagged with the provider, model, UTC time, context record IDs, and
    *"not measured or independently validated"*. It is never written to the index.
  - Keys are read from `GROQ_API_KEY` or `ANTHROPIC_API_KEY` (or an `ant auth login`
    profile) and are never stored.
  - The **Claude** provider (`--provider claude`, needs `pip install anthropic`) is an addition
    to the paper. It uses `claude-opus-5-5` (override with `LCA_CLAUDE_MODEL`) and JSON-schema
    structured output, so the reply always has the contract's shape. A refused request
    reports `unavailable`. The same validator still runs on every reply.

### API (Table III)

| Endpoint | Purpose |
|---|---|
| `GET /health` | Readiness and corpus counts |
| `GET /hierarchy[?parent=node_id]` | Browse source and category nodes |
| `GET /record?id=bottles_processes:232` | Original row, citation, provenance |
| `POST /query` `{query, filters?, indicator?, top_k?, use_ai?, provider?, dense?}` | Evidence, plus the fallback when needed |
| `POST /calculate` `{record_id, indicator, quantity, unit}` | Unit-checked scaling; returns 400 on refusal |

## Results (reproduced on macOS, Apple Silicon)

| Measure | Paper | This build |
|---|---|---|
| Indexed records | 157,952 | 157,952 |
| Hierarchy nodes | 410 | 410 |
| Distinct bottle process names | 210 | 210 normalized (215 raw strings) |
| Automated tests | 26/26 | 26/26 of the paper's tests (51/51 including the additions) |
| Dev benchmark Hit@1 / Hit@5 / MRR@5 | 1.00 / 1.00 / 1.00 | 1.00 / 1.00 / 1.00 |
| Mean retrieval latency | 98.4 ms (Windows) | ~10–100 ms, depending on machine load |

The 12 benchmark cases were selected during development, so they measure regression
behaviour, not generalization or scientific accuracy (paper §IX).

## Cosine similarity: what changed and why

The upstream `lca_backend` matched materials with dense cosine similarity:

- it embedded `name + tags + full category path` with `all-MiniLM-L6-v2`;
- it embedded the raw query (for example, just `"PET"`);
- it trusted the top-1 match whenever cosine ≥ 0.60.

It also forced `unit = "kg"` on the impact export and merged "Human toxicity 100a" into
"Human toxicity". This project does neither.

`dense.py` adds cosine retrieval as an **optional third RRF list**. Install it with
`pip install -r requirements-dense.txt`, then build with `python dense.py build`. Its
*improved* configuration makes three changes:

1. **An asymmetric retrieval model.** `BAAI/bge-small-en-v1.5`, with its query instruction
   prefix.
2. **Cleaner passages.** The `"other"` tag is dropped, the category is reduced to its last
   two levels, and unit and flow type are added.
3. **Query expansion before embedding.** The same abbreviation map is applied, so
   `PET → polyethylene terephthalate`.

Dense-only results on the bottle-process source:

| Config | Dev Hit@1 | Dev MRR@5 | Dev cos(relevant) | Paraphrase Hit@1 | Paraphrase MRR@5 | Paraphrase cos(relevant) |
|---|---|---|---|---|---|---|
| upstream (MiniLM, raw text) | 0.83 | 0.88 | 0.560 | 0.58 | 0.66 | 0.455 |
| MiniLM + clean text + expansion | 1.00 | 1.00 | 0.673 | 0.58 | 0.68 | 0.440 |
| **improved (BGE-small)** | **1.00** | **1.00** | **0.811** | **0.67** | **0.75** | **0.683** |
| BM25 + RRF (paper system) | 1.00 | 1.00 | — | 0.42 | 0.46 | — |
| hybrid (BM25 + improved dense) | 1.00 | 1.00 | — | 0.50 | 0.64 | — |

How to read this table:

- **A higher raw cosine is not, by itself, a better system.** Each model has its own
  cosine scale. BGE scores everything higher, so its margin over the best *wrong* record
  is no larger than MiniLM's (~0.09 vs ~0.10 on the dev set). What improved is the
  **ranking**: Hit@1 and MRR.
- **Do not reuse the upstream 0.60 threshold** with a different model. This system never
  uses cosine as a confidence or as an acceptance gate. Cosine appears only as
  `scores.cosine` next to the candidate.
- **Dense helps where wording differs** ("fiberglass" vs "glass wool", "reinforcing bars"
  vs "rebar"). BM25 is better on exact technical names.
- **The hybrid keeps the lexical benchmark at 1.00** and lifts paraphrase Hit@5 from 0.58
  to 0.83. Raising the dense weight from 2 to 4 changes paraphrase Hit@1 only from 0.50 to
  0.58, so I kept the balanced weight of 2 rather than tune it on 24 queries.
- **All 24 cases were written during development.** The numbers are indicative only. An
  expert-labelled held-out set is still future work (paper §XI).

Run the comparison yourself:

```bash
python dense.py build --config improved && python dense.py build --config upstream
python eval/evaluate.py --compare-dense
python eval/evaluate.py --compare-dense --cases eval/paraphrase_cases.json
python desktop.py --dense      # or: python server.py --dense; then POST /query {"dense": true}
```

Building the full corpus takes about 4–6 minutes per config on Apple Silicon (MPS).

## Notes and deviations

- The fallback JSON contract adds an `indicator` field to the paper's field list. The
  validator needs it to detect a changed indicator.
- The impact unit is not stated anywhere in the source exports, so answers report the
  value per one reference unit, with `impact_unit: null`.
- Flow records get the kinds `elementary_flow`, `product_flow` and `waste_flow` from the
  export's `flow_type`, so product and elementary flows can be filtered apart.
- Licensing, characterization methods, allocation, validity periods and system boundaries
  of the upstream data are **not** verified by this project.
