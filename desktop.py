"""Tkinter desktop client for evidence review, unit-checked calculation, and AI fallback.

    python desktop.py [--dense]
"""

from __future__ import annotations

import argparse
import json
import queue
import threading
import tkinter as tk
from tkinter import ttk

import rag

ANY = "(any)"
KINDS = [ANY, "process", "impact_export", "elementary_flow", "product_flow", "waste_flow"]


class App(tk.Tk):
    def __init__(self, engine: rag.Engine):
        super().__init__()
        self.engine = engine
        self.results: dict[str, dict] = {}
        self._pending: queue.Queue = queue.Queue()  # worker results, drained on the Tk thread
        self.title("LCA RAG - evidence-grounded retrieval")
        self.geometry("1280x780")
        self.minsize(960, 600)
        self._build()
        h = engine.health()
        self.status.set(f"{h['records']:,} records | {h['hierarchy_nodes']} nodes | "
                        f"commit {str(h['commit'])[:12]}")

    # -- layout -----------------------------------------------------------
    def _build(self) -> None:
        pad = {"padx": 4, "pady": 3}
        top = ttk.Frame(self, padding=6)
        top.pack(fill="x")

        ttk.Label(top, text="Query").grid(row=0, column=0, sticky="w", **pad)
        self.query = tk.StringVar()
        entry = ttk.Entry(top, textvariable=self.query, width=48)
        entry.grid(row=0, column=1, columnspan=3, sticky="ew", **pad)
        entry.bind("<Return>", lambda _e: self.search())
        entry.focus_set()

        ttk.Label(top, text="Record kind").grid(row=0, column=4, sticky="w", **pad)
        self.kind = ttk.Combobox(top, values=KINDS, state="readonly", width=16)
        self.kind.set(ANY)
        self.kind.grid(row=0, column=5, **pad)

        ttk.Label(top, text="Source").grid(row=0, column=6, sticky="w", **pad)
        self.source = ttk.Combobox(top, values=[ANY, *rag.SOURCES], state="readonly", width=24)
        self.source.set(ANY)
        self.source.grid(row=0, column=7, **pad)
        self.source.bind("<<ComboboxSelected>>", lambda _e: self._refresh_indicators())

        ttk.Label(top, text="Stage").grid(row=1, column=0, sticky="w", **pad)
        self.stage = ttk.Combobox(top, values=[ANY, *self.engine.distinct("stage")], state="readonly", width=22)
        self.stage.set(ANY)
        self.stage.grid(row=1, column=1, sticky="w", **pad)

        ttk.Label(top, text="Location (exact)").grid(row=1, column=2, sticky="w", **pad)
        self.location = tk.StringVar()
        ttk.Entry(top, textvariable=self.location, width=10).grid(row=1, column=3, sticky="w", **pad)

        ttk.Label(top, text="Unit (exact)").grid(row=1, column=4, sticky="w", **pad)
        self.unit_filter = tk.StringVar()
        ttk.Entry(top, textvariable=self.unit_filter, width=10).grid(row=1, column=5, sticky="w", **pad)

        ttk.Label(top, text="Indicator").grid(row=1, column=6, sticky="w", **pad)
        self.indicator = ttk.Combobox(top, state="readonly", width=40)
        self.indicator.grid(row=1, column=7, **pad)
        self._refresh_indicators()

        opts = ttk.Frame(top)
        opts.grid(row=2, column=0, columnspan=8, sticky="w", **pad)
        self.use_ai = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts, text="AI fallback (provisional)", variable=self.use_ai).pack(side="left")
        self.provider = ttk.Combobox(opts, values=["ollama", "groq", "claude"], state="readonly", width=8)
        self.provider.set("ollama")
        self.provider.pack(side="left", padx=6)
        self.use_dense = tk.BooleanVar(value=False)
        dense_cb = ttk.Checkbutton(opts, text="Dense (cosine) fusion", variable=self.use_dense)
        dense_cb.pack(side="left", padx=12)
        if self.engine.dense is None:
            dense_cb.state(["disabled"])
        ttk.Button(opts, text="Search", command=self.search).pack(side="left", padx=12)
        top.columnconfigure(1, weight=1)

        panes = ttk.PanedWindow(self, orient="vertical")
        panes.pack(fill="both", expand=True, padx=6)

        cols = ("rank", "id", "name", "location", "unit", "stage", "rrf", "coverage", "warnings")
        self.table = ttk.Treeview(panes, columns=cols, show="headings", height=10)
        widths = (40, 190, 420, 70, 60, 140, 80, 70, 200)
        for c, w in zip(cols, widths):
            self.table.heading(c, text=c)
            self.table.column(c, width=w, stretch=(c == "name"))
        self.table.bind("<<TreeviewSelect>>", lambda _e: self._show_selected())
        panes.add(self.table, weight=2)

        bottom = ttk.PanedWindow(panes, orient="horizontal")
        self.summary = tk.Text(bottom, wrap="word", height=14)
        self.raw = tk.Text(bottom, wrap="none", height=14, font=("Menlo", 11))
        bottom.add(self.summary, weight=1)
        bottom.add(self.raw, weight=1)
        panes.add(bottom, weight=3)

        calc = ttk.Frame(self, padding=6)
        calc.pack(fill="x")
        ttk.Label(calc, text="Quantity").pack(side="left")
        self.qty = tk.StringVar(value="1")
        ttk.Entry(calc, textvariable=self.qty, width=10).pack(side="left", padx=4)
        ttk.Label(calc, text="Unit").pack(side="left")
        self.calc_unit = tk.StringVar()
        ttk.Entry(calc, textvariable=self.calc_unit, width=8).pack(side="left", padx=4)
        ttk.Button(calc, text="Calculate for selected record", command=self.calculate).pack(side="left", padx=8)
        self.calc_out = tk.StringVar()
        ttk.Label(calc, textvariable=self.calc_out).pack(side="left", padx=8)

        self.status = tk.StringVar()
        ttk.Label(self, textvariable=self.status, relief="sunken", anchor="w").pack(fill="x", side="bottom")

    def _refresh_indicators(self) -> None:
        src = self.source.get()
        labels = self.engine.indicators(None if src in ("", ANY) else src)
        self.indicator["values"] = ["(none)", *labels]
        if self.indicator.get() not in labels:
            self.indicator.set("(none)")

    # -- actions ----------------------------------------------------------
    def _filters(self) -> dict:
        def val(v):
            return None if v in ("", ANY) else v
        return {"kind": val(self.kind.get()), "source": val(self.source.get()),
                "stage": val(self.stage.get()), "location": val(self.location.get().strip()),
                "unit": val(self.unit_filter.get().strip())}

    def search(self) -> None:
        text = self.query.get().strip()
        if not text:
            return
        indicator = None if self.indicator.get() in ("", "(none)") else self.indicator.get()
        self.status.set("Searching...")
        args = (text, self._filters(), indicator, 10, self.use_ai.get(), self.provider.get(),
                self.use_dense.get())

        def work():
            try:
                out = self.engine.query(*args[:4], use_ai=args[4], provider=args[5], use_dense=args[6])
            except Exception as exc:  # surface any error in the UI
                out = {"error": str(exc)}
            self._pending.put(out)

        threading.Thread(target=work, daemon=True).start()
        self.after(50, self._poll)

    def _poll(self) -> None:
        try:
            out = self._pending.get_nowait()
        except queue.Empty:
            self.after(50, self._poll)
            return
        self._render(out)

    def _render(self, out: dict) -> None:
        self.table.delete(*self.table.get_children())
        self.results.clear()
        if "error" in out:
            self.status.set(f"Error: {out['error']}")
            return
        for i, c in enumerate(out["candidates"], 1):
            self.results[c["id"]] = c
            warns = ", ".join(w["code"] for w in c["warnings"])
            self.table.insert("", "end", iid=c["id"], values=(
                i, c["id"], c["name"], c["location"] or "", c["reference_unit"] or "",
                c["stage"] or "", c["scores"]["rrf"], c["term_coverage"], warns))
        lines = [f"Status: {out['status']}" + (f"  ({', '.join(out['reasons'])})" if out["reasons"] else ""),
                 f"Terms: {' '.join(out['expanded_terms'])}",
                 "Route: " + " | ".join(n["node_id"] for n in out["route"][:5]),
                 f"Latency: {out['latency_ms']} ms", ""]
        if out["answer"]:
            a = out["answer"]
            lines += [f"EVIDENCE: {a['indicator']} = {a['value']} per 1 {a['per_reference_unit']}",
                      f"Citation: {a['citation']}", a["note"], ""]
        lines.append(out["selection_note"])
        fb = out.get("fallback")
        if fb:
            lines += ["", "=" * 60, fb["label"], f"Provider: {fb['provider']} / {fb.get('model')}  "
                      f"status: {fb['status']}"]
            if fb.get("estimate"):
                e = fb["estimate"]
                if e["can_estimate"]:
                    lines.append(f"PROVISIONAL {e['indicator'] or ''}: {e['value']} "
                                 f"[{e['lower']} - {e['upper']}] {e['impact_unit']} per {e['reference_unit']}")
                    lines.append(f"Boundary: {e['system_boundary']}")
                lines.append(f"Explanation: {e['explanation']}")
                lines += [f"  assumption: {a}" for a in e["assumptions"]]
                lines += [f"  missing: {m}" for m in e["missing_information"]]
            elif fb.get("error"):
                lines.append(f"Error: {fb['error']}")
        self.summary.delete("1.0", "end")
        self.summary.insert("1.0", "\n".join(lines))
        self.status.set(f"{len(out['candidates'])} candidates - {out['status']}")
        if out["candidates"]:
            self.table.selection_set(out["candidates"][0]["id"])

    def _show_selected(self) -> None:
        sel = self.table.selection()
        if not sel:
            return
        rec = self.engine.record(sel[0])
        self.calc_unit.set(rec["reference_unit"] or "")
        self.raw.delete("1.0", "end")
        self.raw.insert("1.0", json.dumps({k: rec[k] for k in ("id", "citation", "hierarchy_path",
                                                             "warnings", "provenance", "impacts", "raw")},
                                          indent=2, ensure_ascii=False))

    def calculate(self) -> None:
        sel = self.table.selection()
        indicator = self.indicator.get()
        if not sel or indicator in ("", "(none)"):
            self.calc_out.set("Select a record and an indicator first.")
            return
        try:
            r = self.engine.calculate(sel[0], indicator, self.qty.get(), self.calc_unit.get())
            self.calc_out.set(f"{r['indicator']}: {r['result']:.6g}  ({r['formula']})")
        except rag.CalculationError as exc:
            self.calc_out.set(f"Refused [{exc.code}]: {exc}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dense", action="store_true", help="load optional dense index")
    args = parser.parse_args()
    if not rag.INDEX_PATH.exists():
        rag.build()
    engine = rag.Engine()
    if args.dense:
        import dense
        engine.dense = dense.DenseIndex.load()
    App(engine).mainloop()


if __name__ == "__main__":
    main()
