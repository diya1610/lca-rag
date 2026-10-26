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

import ebom  # noqa: E402
import rag  # noqa: E402
import server  # noqa: E402

SAMPLE = (ROOT / "web" / "sample_ebom.csv").read_text(encoding="utf-8")


def by_item(result: dict) -> dict:
    return {line["item_id"]: line for line in result["lines"]}


class EbomTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not rag.INDEX_PATH.exists():
            rag.build(verbose=False)
        cls.engine = rag.Engine()

    @classmethod
    def tearDownClass(cls):
        cls.engine.close()

    def test_column_detection_and_header_unit(self):
        text = "Part No;Component;Weight_kg\nP1;bottle-grade PET;0,5\nP2;HDPE;0.1\n"
        lines, cols = ebom.parse_csv(text)
        self.assertEqual(cols["material"], "Component")
        self.assertEqual(cols["quantity"], "Weight_kg")
        self.assertEqual(cols["unit_from_header"], "kg")
        self.assertEqual([l["item_id"] for l in lines], ["P1", "P2"])
        self.assertTrue(all(l["unit"] == "kg" for l in lines))
        self.assertEqual([ebom._quantity(x)[0] for x in ("0,5", "12,75", "1,234.5", "2")], [0.5, 12.75, 1234.5, 2.0])
        self.assertEqual(ebom._quantity("1,500"), (None, "ambiguous_quantity"))
        self.assertEqual(ebom._quantity("-3"), (None, "invalid_quantity"))
        with self.assertRaises(ebom.EbomError):
            ebom.parse_csv("colour,size\nred,3\n")

    def test_evidence_lines_are_unit_checked_calculations(self):
        res = by_item(ebom.analyse(self.engine, SAMPLE))
        pet = res["B-001"]
        self.assertEqual(pet["status"], "evidence")
        self.assertEqual(pet["match"]["id"], "bottles_processes:232")
        self.assertTrue(math.isclose(pet["contribution"], 3.49975646 * 0.025))
        self.assertEqual(res["B-006"]["unit"], "MJ")  # matched only against MJ records
        self.assertEqual(res["B-006"]["status"], "evidence")

    def test_units_are_never_assumed(self):
        res = by_item(ebom.analyse(self.engine, SAMPLE))
        self.assertEqual(res["B-009"]["status"], "not_covered")  # "g": no record has unit g
        self.assertEqual(res["B-009"]["reasons"], ["no_candidates"])
        self.assertEqual(res["B-010"]["status"], "invalid")      # empty quantity
        no_unit = "material,quantity\nbottle-grade PET,2\n"
        line = ebom.analyse(self.engine, no_unit)["lines"][0]
        self.assertEqual((line["status"], line["reasons"]), ("invalid", ["missing_unit"]))
        line = ebom.analyse(self.engine, no_unit, default_unit="kg")["lines"][0]
        self.assertEqual((line["status"], line["unit_source"]), ("evidence", "default"))

    def test_weak_matches_and_implausible_factors(self):
        out = ebom.analyse(self.engine, SAMPLE)
        res = by_item(out)
        self.assertEqual(res["B-005"]["status"], "not_covered")  # steel sheet must not use aluminium sheet
        self.assertEqual(res["B-007"]["status"], "not_covered")
        self.assertEqual([w["code"] for w in res["B-004"]["warnings"]], ["implausible_factor"])
        s = out["summary"]
        self.assertEqual(s["flagged_lines"], 1)
        self.assertLess(s["evidence_total_unflagged"], s["evidence_total"])

    def test_provisional_totals_stay_separate(self):
        calls = []

        def stub(**kw):
            calls.append(kw["query"])
            est = {"can_estimate": True, "value": 10.0, "lower": 5.0, "upper": 20.0,
                   "impact_unit": "kg CO2-eq", "explanation": "stub", "system_boundary": "cradle-to-gate",
                   "assumptions": ["a"], "missing_information": []}
            return {"status": "provisional", "provider": "stub", "model": "m", "label": "provisional",
                    "estimate": est}
        plain = ebom.analyse(self.engine, SAMPLE)
        with_ai = ebom.analyse(self.engine, SAMPLE, use_ai=True, fallback=stub)
        self.assertEqual(sorted(calls), sorted(l["material"] for l in plain["lines"]
                                               if l["status"] == "not_covered"))
        self.assertEqual(with_ai["summary"]["evidence_total"], plain["summary"]["evidence_total"])
        carbon = by_item(with_ai)["B-007"]
        self.assertEqual(carbon["status"], "provisional")
        self.assertIsNone(carbon["contribution"])  # never in the evidence column
        self.assertTrue(math.isclose(carbon["provisional"]["contribution"], 10.0 * 0.01))
        [group] = with_ai["summary"]["provisional_by_unit"]
        self.assertTrue(math.isclose(group["upper"], 20.0 * sum(
            l["quantity"] for l in with_ai["lines"] if l["status"] == "provisional")))

    def test_provisional_units_are_not_mixed(self):
        units = iter(["kg CO2 eq", "kg CO₂‑eq", "g CO2e", "kg CO2-eq"])

        def stub(**kw):
            est = {"can_estimate": True, "value": 1.0, "lower": 0.5, "upper": 2.0,
                   "impact_unit": next(units), "explanation": "", "system_boundary": "x",
                   "assumptions": ["a"], "missing_information": []}
            return {"status": "provisional", "estimate": est}
        out = ebom.analyse(self.engine, SAMPLE, use_ai=True, fallback=stub)
        groups = {g["impact_unit"]: g["lines"] for g in out["summary"]["provisional_by_unit"]}
        self.assertEqual(sorted(groups.values()), [1, 3])  # g CO2e alone; kg spellings merged
        self.assertIn("g CO2e", groups)

    def test_http_endpoint(self):
        httpd = server.make_server(self.engine, port=0)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        try:
            def post(body):
                req = urllib.request.Request(base + "/ebom", data=json.dumps(body).encode(), method="POST",
                                             headers={"Content-Type": "application/json"})
                try:
                    with urllib.request.urlopen(req, timeout=30) as r:
                        return r.status, json.loads(r.read())
                except urllib.error.HTTPError as e:
                    return e.code, json.loads(e.read())
            status, out = post({"csv": SAMPLE})
            self.assertEqual(status, 200)
            self.assertEqual(out["summary"]["lines"], 10)
            status, out = post({"csv": "a,b\n1,2\n"})
            self.assertEqual((status, out["error"]), (400, "invalid_ebom"))
            with urllib.request.urlopen(base + "/sample_ebom.csv", timeout=10) as r:
                self.assertIn(b"bottle-grade PET", r.read())
        finally:
            httpd.shutdown()
            httpd.server_close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
