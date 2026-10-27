"""eBOM analysis tests against the real index (AI providers stubbed).

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import math
import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import ai_fallback  # noqa: E402
import ebom  # noqa: E402
import rag  # noqa: E402
import server  # noqa: E402

SAMPLE = (ROOT / "web" / "sample_ebom.csv").read_text(encoding="utf-8")


def by_item(result: dict) -> dict:
    return {line["item_id"]: line for line in result["lines"]}


def stub_estimate(value=10.0, unit="kg CO2-eq"):
    calls = []

    def stub(**kw):
        calls.append(kw["query"])
        est = {"can_estimate": True, "value": value, "lower": value / 2, "upper": value * 2,
               "impact_unit": unit if isinstance(unit, str) else next(unit), "explanation": "stub",
               "system_boundary": "cradle-to-gate", "assumptions": ["a"], "missing_information": []}
        return {"status": "provisional", "provider": "stub", "model": "m", "label": "provisional",
                "estimate": est}
    stub.calls = calls
    return stub


class EbomTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not rag.INDEX_PATH.exists():
            rag.build(verbose=False)
        cls.engine = rag.Engine()
        cls.base = ebom.analyse(cls.engine, SAMPLE)
        cls.lines = by_item(cls.base)

    @classmethod
    def tearDownClass(cls):
        cls.engine.close()

    # -- parsing ------------------------------------------------------------------

    def test_column_detection_and_quantities(self):
        text = "Part No;Assembly;Material;Weight_kg;Cost\nP1;Body;bottle-grade PET;0,5;1.2\nP2;Cap;HDPE;0.1;\n"
        lines, cols = ebom.parse_csv(text)
        self.assertEqual((cols["material"], cols["quantity"], cols["component"], cols["cost"]),
                         ("Material", "Weight_kg", "Assembly", "Cost"))
        self.assertEqual(cols["unit_from_header"], "kg")
        self.assertEqual([(l["item_id"], l["component"], l["unit"]) for l in lines],
                         [("P1", "Body", "kg"), ("P2", "Cap", "kg")])
        self.assertEqual([ebom._quantity(x)[0] for x in ("0,5", "12,75", "1,234.5", "2")], [0.5, 12.75, 1234.5, 2.0])
        self.assertEqual(ebom._quantity("1,500"), (None, "ambiguous_quantity"))
        self.assertEqual(ebom._quantity("-3"), (None, "invalid_quantity"))
        with self.assertRaises(ebom.EbomError):
            ebom.parse_csv("colour,size\nred,3\n")

    def test_units(self):
        self.assertEqual(self.lines["B-010"]["status"], "invalid")  # empty quantity
        alu = self.lines["B-009"]                                     # 0.003 g
        self.assertEqual((alu["unit"], alu["converted_from"]), ("kg", {"quantity": 0.003, "unit": "g"}))
        self.assertTrue(math.isclose(alu["mass_kg"], 3e-6))
        no_unit = "material,quantity\nbottle-grade PET,2\n"
        line = ebom.analyse(self.engine, no_unit)["lines"][0]
        self.assertEqual((line["status"], line["reasons"]), ("invalid", ["missing_unit"]))
        line = ebom.analyse(self.engine, no_unit, default_unit="kg")["lines"][0]
        self.assertEqual((line["status"], line["unit_source"]), ("evidence", "default"))

    # -- resolution ladder ------------------------------------------------------------

    def test_exact_evidence(self):
        pet = self.lines["B-001"]
        self.assertEqual((pet["status"], pet["record_id"]), ("evidence", "bottles_processes:232"))
        self.assertTrue(math.isclose(pet["impact"], 3.49975646 * 0.025))
        self.assertEqual(self.lines["B-006"]["unit"], "MJ")
        self.assertEqual(self.lines["B-006"]["status"], "evidence")

    def test_similar_material_proxy(self):
        steel = self.lines["B-005"]                       # no exact "steel sheet" record
        self.assertEqual((steel["status"], steel["family"]), ("proxy", "steel"))
        self.assertIn("Steel", steel["proxy"]["name"])
        board = self.lines["B-004"]                       # exact factor 1159 is implausible
        self.assertEqual(board["status"], "proxy")
        self.assertEqual(board["warnings"][0]["code"], "implausible_factor")
        self.assertNotEqual(board["record_id"], "bottles_processes:110")
        self.assertLess(board["factor"], 5)

    def test_implausible_families_are_never_proxies(self):
        alu = self.lines["B-009"]  # every aluminium record is on a x1000 scale
        self.assertEqual(alu["status"], "not_covered")
        self.assertIn("no_plausible_family_member", alu["reasons"])
        self.assertEqual(self.lines["B-007"]["status"], "not_covered")  # no family: carbon fibre

    def test_ai_only_after_proxy_and_units_not_mixed(self):
        stub = stub_estimate(unit=iter(["kg CO2 eq", "g CO2e"]))
        out = ebom.analyse(self.engine, SAMPLE, use_ai=True, fallback=stub)
        self.assertEqual(sorted(stub.calls), ["aluminium sheet", "recycled carbon fibre composite"])
        lines = by_item(out)
        for item in ("B-007", "B-009"):
            p = lines[item]["provisional"]
            scale = 1e-3 if p["impact_unit"] == "g CO2e" else 1.0
            self.assertTrue(math.isclose(lines[item]["impact"], p["contribution"] * scale))
        self.assertEqual(out["product"]["tiers"]["evidence"], self.base["product"]["tiers"]["evidence"])

    def test_ai_answers_are_saved_and_reused(self):
        import tempfile
        import unittest.mock
        stub = stub_estimate()
        with tempfile.TemporaryDirectory() as tmp, \
                unittest.mock.patch.object(ebom, "AI_CACHE_PATH", Path(tmp) / "ai_cache.json"), \
                unittest.mock.patch.object(ebom, "_AI_CACHE", None), \
                unittest.mock.patch.object(ai_fallback, "estimate", stub):
            first = ebom.analyse(self.engine, SAMPLE, use_ai=True, provider="stub-cache")
            ebom.what_if(self.engine, SAMPLE, [{"swaps": [{"item_id": "B-001", "material": "HDPE"}]}],
                         use_ai=True, provider="stub-cache")
            self.assertEqual(len(stub.calls), 2)  # once per uncovered material, not per run
            ebom._AI_CACHE = None                 # simulate a restart: reload from disk
            ebom.analyse(self.engine, SAMPLE, use_ai=True, provider="stub-cache")
            self.assertEqual(len(stub.calls), 2)
            self.assertEqual(ebom.clear_ai_cache(), 2)
            self.assertFalse((Path(tmp) / "ai_cache.json").exists())
        self.assertEqual(by_item(first)["B-007"]["status"], "provisional")

    def test_upstream_scoring_mode(self):
        up = ebom.analyse(self.engine, SAMPLE, scoring="upstream")
        self.assertEqual(up["scoring"]["references_p95_per_kg"], ebom.UPSTREAM_REFS)
        pet = by_item(up)["B-001"]["scores"]
        self.assertLess(pet["toxicity"], 1)          # the upstream constants zero out toxicity
        self.assertEqual(pet["criticality"], 40.0)   # keyword-only: "PET" is unknown upstream
        with self.assertRaises(ebom.EbomError):
            ebom.analyse(self.engine, SAMPLE, scoring="nope")

    def test_ghg_only_grade(self):
        out = ebom.analyse(self.engine, SAMPLE, grade_basis="ghg")
        p = out["product"]
        self.assertEqual(p["grade"], ebom.grade_for(p["ghg_score"]))
        self.assertEqual(p["combined_grade"], ebom.grade_for(p["combined_score"]))
        steel = by_item(out)["B-005"]
        self.assertEqual(steel["grade"], steel["ghg_grade"])
        self.assertEqual(steel["combined_grade"], "B")  # toxicity 100 drives the combined grade
        self.assertEqual(steel["ghg_grade"], "A")

    def test_reviewed_corrections_are_opt_in_and_labelled(self):
        bom = "item_id,material,quantity,unit\nC1,Portland cement,2,kg\nZ1,special high grade zinc,0.5,kg\n"
        off = by_item(ebom.analyse(self.engine, bom))
        self.assertNotEqual(off["C1"]["record_id"], "bottles_processes:169")  # 903.5 is never used raw
        self.assertEqual(off["C1"]["status"], "not_covered")  # concrete is not a cement proxy
        on = by_item(ebom.analyse(self.engine, bom, apply_corrections=True))
        self.assertEqual((on["C1"]["status"], on["C1"]["record_id"]), ("evidence", "bottles_processes:169"))
        self.assertTrue(math.isclose(on["C1"]["factor"], 903.502 / 1000, rel_tol=1e-3))
        self.assertIn("corrected_factor", [w["code"] for w in on["C1"]["warnings"]])
        self.assertTrue(math.isclose(on["Z1"]["impact"], 3199.696 / 1000 * 0.5, rel_tol=1e-3))

    # -- scoring, hotspots, recommendations ---------------------------------------------

    def test_scores_follow_upstream_formula(self):
        pet = self.lines["B-001"]["scores"]
        refs = self.base["scoring"]["references_p95_per_kg"]
        ghg = 0.4 * min(100, 3.49975646 / refs["Climate change"] * 100) + 0.6 * (3.49975646 * 0.025 / 500 * 100)
        self.assertAlmostEqual(pet["ghg"], ghg, places=1)
        self.assertEqual(pet["criticality"], 20.0)  # plastics family -> low tier
        composite = 0.30 * pet["criticality"] + 0.35 * pet["toxicity"] + 0.35 * pet["ghg"]
        self.assertAlmostEqual(pet["composite"], composite, places=1)
        self.assertEqual(ebom.grade_for(30), "A")
        self.assertEqual(ebom.grade_for(30.01), "B")
        self.assertEqual(ebom.grade_for(80.5), "D")
        p = self.base["product"]
        self.assertIn(p["grade"], "ABCD")
        self.assertEqual(set(p["subscores"]), {"criticality", "toxicity", "ghg"})

    def test_hotspots_and_ranking(self):
        p = self.base["product"]
        self.assertEqual(p["hotspots"][0]["item_id"], "B-001")
        self.assertTrue(all(h["share"] > 0.20 for h in p["hotspots"]))
        ranks = sorted((l["rank"], l["impact"]) for l in self.base["lines"] if l["rank"])
        self.assertEqual([i for _, i in ranks], sorted((i for _, i in ranks), reverse=True))

    def test_sister_materials_are_same_family_and_lower(self):
        rec = next(r for r in self.base["recommendations"] if r["item_id"] == "B-001")
        self.assertTrue(rec["sisters"])
        catalog = ebom.Catalog(self.engine, "bottles_processes", "Climate change")
        for s in rec["sisters"]:
            self.assertLess(s["factor"], 3.49975646)
            self.assertEqual(catalog.by_id[s["id"]]["family"], "plastics")
        self.assertNotIn("Sodium chloride", " ".join(s["name"] for s in rec["sisters"]))

    # -- what-if ------------------------------------------------------------------

    def test_what_if_scenarios(self):
        out = ebom.what_if(self.engine, SAMPLE, [
            {"name": "HDPE body", "swaps": [{"item_id": "B-001", "material": "HDPE"}]},
            {"swaps": [{"item_id": "B-001", "material": "glass container"}]},
        ])
        base = out["baseline"]["total"]
        a, b = out["scenarios"]
        self.assertTrue(math.isclose(a["delta"], (1.99987771 - 3.49975646) * 0.025, rel_tol=1e-6))
        self.assertTrue(math.isclose(a["delta_pct"], a["delta"] / base))
        self.assertEqual(a["changes"][0]["from"], "bottle-grade PET")
        self.assertEqual(a["explanation"]["source"], "rules")
        self.assertEqual(b["name"], "Scenario 2")
        with self.assertRaises(ebom.EbomError):
            ebom.what_if(self.engine, SAMPLE, [{"swaps": [{"item_id": "NOPE", "material": "HDPE"}]}])
        with self.assertRaises(ebom.EbomError):
            ebom.what_if(self.engine, SAMPLE, [{"swaps": [{"item_id": "B-001", "material": "x"}]}] * 4)

    def test_ai_explanation_is_labelled_and_validated(self):
        def transport(url, payload, headers):
            return {"message": {"content": json.dumps({"explanation": "Swapping PET for HDPE lowers the total."})}}
        out = ai_fallback.explain({"delta": -1}, provider="ollama", transport=transport)
        self.assertEqual(out["status"], "ok")
        bad = ai_fallback.explain({}, provider="ollama",
                                  transport=lambda *a: {"message": {"content": "not json"}})
        self.assertEqual(bad["status"], "rejected")
        res = ebom.what_if(self.engine, SAMPLE, [{"swaps": [{"item_id": "B-001", "material": "HDPE"}]}],
                           use_ai=True, fallback=stub_estimate(),
                           explainer=lambda facts, provider=None: {"status": "ok", "text": "AI note", "provider": "stub"})
        exp = res["scenarios"][0]["explanation"]
        self.assertEqual((exp["source"], exp["text"]), ("ai", "AI note"))
        self.assertIn("AI-written", exp["label"])

    # -- HTTP -----------------------------------------------------------------------

    def test_http_endpoints(self):
        httpd = server.make_server(self.engine, port=0)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{httpd.server_address[1]}"

        def post(path, body):
            req = urllib.request.Request(base + path, data=json.dumps(body).encode(), method="POST",
                                         headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    return r.status, json.loads(r.read())
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read())
        try:
            status, out = post("/ebom", {"csv": SAMPLE})
            self.assertEqual((status, out["product"]["lines"]), (200, 10))
            status, out = post("/ebom", {"csv": "a,b\n1,2\n"})
            self.assertEqual((status, out["error"]), (400, "invalid_ebom"))
            status, out = post("/ebom/whatif", {"csv": SAMPLE, "scenarios": [
                {"swaps": [{"item_id": "B-001", "material": "HDPE"}]}]})
            self.assertEqual(status, 200)
            self.assertLess(out["scenarios"][0]["delta"], 0)
            with urllib.request.urlopen(base + "/sample_ebom.csv", timeout=10) as r:
                self.assertIn(b"bottle-grade PET", r.read())
        finally:
            httpd.shutdown()
            httpd.server_close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
