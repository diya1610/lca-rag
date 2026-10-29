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
IPHONE = (ROOT / "web" / "sample_iphone16e_bom.csv").read_text(encoding="utf-8")


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

    def test_same_file_gives_same_answer(self):
        import tempfile
        import unittest.mock
        values = iter([10.0, 99.0, 55.0, 77.0, 33.0, 44.0])

        def changing(**kw):  # an AI that answers differently every time
            v = next(values)
            est = {"can_estimate": True, "value": v, "lower": v / 2, "upper": v * 2, "impact_unit": "kg CO2-eq",
                   "explanation": "", "system_boundary": "x", "assumptions": ["a"], "missing_information": []}
            return {"status": "provisional", "estimate": est}
        with tempfile.TemporaryDirectory() as tmp, \
                unittest.mock.patch.object(ebom, "RESULTS_DIR", Path(tmp) / "results"), \
                unittest.mock.patch.object(ebom, "AI_CACHE_PATH", Path(tmp) / "ai.json"), \
                unittest.mock.patch.object(ebom, "_AI_CACHE", None), \
                unittest.mock.patch.object(ai_fallback, "estimate", changing):
            first = ebom.analyse_saved(self.engine, SAMPLE, use_ai=True, provider="stub")
            ebom._AI_CACHE = None  # even without the AI cache, the saved analysis is reused
            again = ebom.analyse_saved(self.engine, SAMPLE.replace("\n", "\r\n"), use_ai=True, provider="stub")
            self.assertTrue(again["saved"]["restored"])
            self.assertEqual(again["product"]["total"], first["product"]["total"])
            fresh = ebom.analyse_saved(self.engine, SAMPLE, refresh=True, use_ai=True, provider="stub")
            self.assertFalse(fresh["saved"]["restored"])
            other = ebom.analyse_saved(self.engine, SAMPLE, use_ai=True, provider="stub", grid="US-CAL-CISO")
            self.assertFalse(other["saved"]["restored"])  # different settings, separate result
        with tempfile.TemporaryDirectory() as tmp, \
                unittest.mock.patch.object(ebom, "RESULTS_DIR", Path(tmp) / "results"), \
                unittest.mock.patch.object(ebom, "AI_CACHE_PATH", Path(tmp) / "ai.json"), \
                unittest.mock.patch.object(ebom, "_AI_CACHE", None), \
                unittest.mock.patch.object(ai_fallback, "estimate", lambda **kw: {"status": "unavailable", "error": "429"}):
            out = ebom.analyse_saved(self.engine, SAMPLE, use_ai=True, provider="stub")
            self.assertFalse(out["saved"]["complete"])
            self.assertFalse((Path(tmp) / "results").exists())  # outages are never frozen
        failing = lambda row: {"status": "unavailable", "error": "429"}  # noqa: E731
        res = ebom.analyse(self.engine, IPHONE, split_mode="ai", use_ai=True, composer=failing,
                           fallback=stub_estimate())
        self.assertTrue(by_item(res)["01.1"]["composition"]["ai_failed"])
        self.assertFalse(ebom._complete(res))  # a fallback split must not be frozen either

    def test_ai_cache_survives_restart_with_compositions(self):
        import tempfile
        import unittest.mock
        calls = []

        def compose(component, materials, mass, unit, provider=None):
            calls.append(component)
            return {"status": "ok", "fractions": {m: 1 / len(materials) for m in materials}, "assumptions": ["even"]}
        with tempfile.TemporaryDirectory() as tmp, \
                unittest.mock.patch.object(ebom, "AI_CACHE_PATH", Path(tmp) / "ai.json"), \
                unittest.mock.patch.object(ebom, "_AI_CACHE", None), \
                unittest.mock.patch.object(ai_fallback, "composition", compose), \
                unittest.mock.patch.object(ai_fallback, "estimate", stub_estimate()):
            first = ebom.analyse(self.engine, IPHONE, split_mode="ai", use_ai=True, provider="stub")
            n = len(calls)
            ebom._AI_CACHE = None  # restart: everything must come back from disk
            again = ebom.analyse(self.engine, IPHONE, split_mode="ai", use_ai=True, provider="stub")
            self.assertEqual(len(calls), n)  # no composition asked twice
            self.assertGreater(len(ebom._ai_cache()), n)  # compositions and estimates all reloaded
            self.assertEqual(again["product"]["total"], first["product"]["total"])

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
        self.assertNotIn("label", exp)  # no provider/model or disclaimer labels are shown

    # -- multi-material eBOMs (iPhone 16e format) -------------------------------------

    def test_multi_material_format(self):
        rows, cols = ebom.parse_csv(IPHONE)
        self.assertEqual((cols["material"], cols["quantity"], cols["unit_from_header"]),
                         ("Main Materials", "Estimated Weight (g)", "g"))
        self.assertEqual((cols["energy"], cols["energy_unit"], cols["recyclability"]),
                         ("Electricity_Usage_kWh", "kWh", "Recyclability"))
        self.assertEqual(rows[0]["materials"], ["Li-ion (Li, Co, Ni, Graphite)", "Aluminum", "Copper"])
        lines = ebom.expand_rows(rows, cols)
        battery = [l for l in lines if l["component"] == "Battery"]
        self.assertEqual([l["item_id"] for l in battery], ["01.1", "01.2", "01.3", "01.E"])
        mats = [float(l["quantity_raw"]) for l in battery[:3]]
        self.assertAlmostEqual(sum(mats), 40.0)
        self.assertGreater(mats[0], mats[1])  # ranked: the first-listed material is the main one
        eq = ebom.expand_rows(rows, cols, "equal")
        self.assertAlmostEqual(float(eq[0]["quantity_raw"]), 40 / 3)
        self.assertEqual(battery[3]["unit"], "kWh")

    def test_electricity_needs_reviewed_relabel(self):
        off = by_item(ebom.analyse(self.engine, IPHONE, grid="EU-27"))
        self.assertEqual((off["01.E"]["status"], off["01.E"]["reasons"]), ("not_covered", ["unit_label_unreliable"]))
        on = ebom.analyse(self.engine, IPHONE, apply_corrections=True, grid="DE")
        bat = by_item(on)["01.E"]
        self.assertEqual((bat["status"], bat["unit"], bat["grid"]), ("evidence", "kWh", "DE"))
        self.assertTrue(0.3 < bat["factor"] < 1.3)  # a per-kWh grid intensity
        self.assertTrue(math.isclose(bat["impact"], bat["factor"] * 1.8))
        self.assertIn("corrected_factor", [w["code"] for w in bat["warnings"]])

    def test_reference_grid_and_eco_ideas(self):
        cn = by_item(ebom.analyse(self.engine, IPHONE))  # default grid: China reference value
        bat = cn["01.E"]
        self.assertEqual((bat["status"], bat["tier"], bat["grid"]), ("reference", "reference", "CN"))
        self.assertTrue(math.isclose(bat["impact"], 0.950 * 1.8))
        self.assertIn("reference_grid", [w["code"] for w in bat["warnings"]])

        def transport(url, payload, headers):
            return {"message": {"content": json.dumps({"ideas": [
                {"type": "material", "idea": "Use recycled cobalt in the cathode.", "rationale": "Battery is the hotspot."}]})}}
        ok = ai_fallback.ideas({"component": "Battery"}, provider="ollama", transport=transport)
        self.assertEqual((ok["status"], ok["ideas"][0]["type"]), ("ok", "material"))
        bad = ai_fallback.ideas({}, provider="ollama", transport=lambda *a: {"message": {"content": json.dumps(
            {"ideas": [{"type": "magic", "idea": "x", "rationale": "y"}]})}})
        self.assertEqual(bad["status"], "rejected")

    def test_generic_and_end_of_life_matches(self):
        lines = by_item(ebom.analyse(self.engine, IPHONE))
        glass = lines["14.1"]                 # bare "Glass": family typical, not an arbitrary record
        self.assertEqual((glass["status"], glass["proxy"]["kind"]), ("proxy", "family typical"))
        alu = lines["01.2"]                   # the top match for "Aluminum" is a waste process
        self.assertIn("exact_match_not_a_material", alu["reasons"])
        self.assertIsNone(lines["07.2"]["family"])  # aluminium oxide is not aluminium metal

    def test_ai_asked_once_per_material_and_composition(self):
        stub = stub_estimate()
        out = ebom.analyse(self.engine, IPHONE, use_ai=True, fallback=stub)
        self.assertEqual(len(stub.calls), len(set(stub.calls)))
        self.assertIn("Copper", stub.calls)
        coppers = [l for l in out["lines"] if l["material"] == "Copper"]
        self.assertTrue(len(coppers) > 3 and all(l["status"] == "provisional" for l in coppers))

        def composer(row):
            n = len(row["materials"])
            return {"status": "ok", "fractions": {m: 1 / n for m in row["materials"]}, "assumptions": ["even"]}
        split = by_item(ebom.analyse(self.engine, IPHONE, split_mode="ai", use_ai=True, fallback=stub,
                                     composer=composer))
        self.assertEqual(split["01.1"]["composition"]["mode"], "ai")
        self.assertAlmostEqual(split["01.1"]["composition"]["fraction"], 1 / 3)

        def bad(url, payload, headers):
            return {"message": {"content": json.dumps({"fractions": {"Glass": 0.9}, "assumptions": ["x"]})}}
        res = ai_fallback.composition("Back Glass", ["Glass", "Aluminum Oxide"], 30, "g",
                                      provider="ollama", transport=bad)
        self.assertEqual(res["status"], "rejected")  # must cover exactly the listed materials

    def test_component_hotspots_and_rate_limit_retry(self):
        import unittest.mock
        out = ebom.analyse(self.engine, IPHONE, apply_corrections=True)
        total = out["product"]["total"]
        for c in out["components"]:  # a row's material and electricity lines are summed
            lines = [l for l in out["lines"] if l["component"] == c["name"] and l["impact"] is not None]
            if c["impact"]:
                self.assertTrue(math.isclose(c["impact"], sum(l["impact"] for l in lines)))
                self.assertEqual(c["hotspot"], c["impact"] / total > 0.20)
            self.assertTrue(all(l["hotspot"] == c["hotspot"] for l in lines))
        bom = "component,material,quantity,unit\nBody,HDPE,30,g\nBody,LDPE,30,g\nBody,bottle-grade PET,10,g\nCap,steel rebar,20,g\n"
        res = ebom.analyse(self.engine, bom)
        body = [l for l in res["lines"] if l["component"] == "Body"]
        self.assertTrue(all(l["share"] < 0.5 for l in body))
        self.assertEqual([h["component"] for h in res["product"]["hotspots"]][0], "Body")
        calls = []

        class Resp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self, n): return b'{"ok": 1}'

        def urlopen(req, timeout):
            calls.append(1)
            if len(calls) == 1:
                raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", {"retry-after": "0"}, None)
            return Resp()
        with unittest.mock.patch("urllib.request.urlopen", urlopen), unittest.mock.patch("time.sleep"):
            self.assertEqual(ai_fallback._http_post_json("https://example.invalid", {}, {}), {"ok": 1})
        self.assertEqual(len(calls), 2)

    def test_energy_lines_do_not_zero_the_product_score(self):
        p = ebom.analyse(self.engine, IPHONE, apply_corrections=True)["product"]
        self.assertGreater(p["subscores"]["ghg"], 30)   # ~5 kg CO2-eq for a 148 g product
        self.assertTrue(math.isclose(p["intensity"], p["total"] / p["mass_total_kg"]))
        up = ebom.analyse(self.engine, IPHONE, apply_corrections=True, scoring="upstream")["product"]
        self.assertLess(up["score"], p["score"])  # upstream GHG-weighting is kept only in upstream mode

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
