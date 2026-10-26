"""Functional tests against the real index and a real loopback HTTP server.

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import math
import sqlite3
import sys
import tempfile
import threading
import unittest
import unittest.mock
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import ai_fallback  # noqa: E402
import rag  # noqa: E402
import server  # noqa: E402

PET_BOTTLE = "bottles_processes:232"
PET_BOTTLE_NAME = "Polyethylene terephthalate (PET) granulate, production mix, at plant, bottle grade"


def good_estimate(**overrides) -> dict:
    data = {
        "can_estimate": True,
        "explanation": "Analogous to virgin polymer granulate production.",
        "indicator": "Climate change",
        "value": 3.0, "lower": 2.0, "upper": 4.5,
        "impact_unit": "kg CO2-eq",
        "reference_unit": "kg",
        "system_boundary": "cradle-to-gate",
        "assumptions": ["European average electricity mix"],
        "missing_information": ["Recycled content"],
    }
    data.update(overrides)
    return data


def groq_transport(content: str):
    calls = []

    def transport(url, payload, headers):
        calls.append((url, payload, headers))
        return {"choices": [{"message": {"content": content}}]}
    transport.calls = calls
    return transport


def ollama_transport(content: str):
    def transport(url, payload, headers):
        return {"message": {"content": content}}
    return transport


class LcaRagTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not rag.INDEX_PATH.exists():
            rag.build(verbose=False)
        cls.hashes = {f: rag.sha256_file(rag.DATA_DIR / f) for f, _ in rag.SOURCES.values()}
        cls.engine = rag.Engine()
        cls.httpd = server.make_server(cls.engine, port=0)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.engine.close()

    def http(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read())

    # -- ingestion and provenance -------------------------------------------------

    def test_01_health_over_http(self):
        status, body = self.http("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["records"], 157_952)
        self.assertEqual(body["commit"], "975ca407202304e57d1d4c5a94b821c2338bf21a")
        self.assertEqual(self.httpd.server_address[0], "127.0.0.1")

    def test_02_all_rows_indexed(self):
        counts = dict(self.engine._all("SELECT source, COUNT(*) FROM records GROUP BY source"))
        self.assertEqual(counts, {"bottles_processes": 508, "lca_full_rag_data_units": 495,
                                  "openlca_flows": 91_472, "usda_flows": 65_477})
        fts = self.engine._all("SELECT COUNT(*) FROM records_fts")[0][0]
        self.assertEqual(fts, 157_952)

    def test_03_hierarchy_has_410_nodes(self):
        self.assertEqual(self.engine._all("SELECT COUNT(*) FROM nodes")[0][0], 410)
        self.assertEqual(self.engine.audit["hierarchy_nodes"], 410)

    def test_04_exact_value_preservation(self):
        rec = self.engine.record(PET_BOTTLE)
        self.assertEqual(rec["name"], PET_BOTTLE_NAME)
        self.assertEqual(rec["raw"]["Climate change"], "3.49975646")
        self.assertEqual(rec["impacts"]["Climate change"], 3.49975646)
        self.assertEqual(rec["provenance"]["file_sha256"], self.hashes["bottles_processes.csv"])
        self.assertEqual(rec["location"], "RER")
        self.assertEqual(rec["reference_unit"], "kg")

    def test_05_missing_and_zero_stay_distinct(self):
        rec = self.engine.record("bottles_processes:1")
        self.assertIn("Agricultural land occupation", rec["impacts"])
        self.assertEqual(rec["impacts"]["Agricultural land occupation"], 0.0)
        flow = self.engine.record("openlca_flows:1")
        self.assertEqual(flow["impacts"], {})  # absent, not zero
        self.assertIn("no_impact_indicators", [w["code"] for w in flow["warnings"]])
        self.assertIsNone(rag.parse_number(""))
        self.assertIsNone(rag.parse_number("nan"))
        self.assertEqual(rag.parse_number("0.0"), 0.0)

    # -- retrieval -----------------------------------------------------------------

    def test_06_exact_name_rescue(self):
        res = self.engine.search(PET_BOTTLE_NAME.upper(), {"source": "bottles_processes"})
        top = res["candidates"][0]
        self.assertEqual(top["id"], PET_BOTTLE)
        self.assertTrue(top["scores"]["exact_name"])
        self.assertGreaterEqual(top["scores"]["rrf"], 1.0)

    def test_07_hierarchical_route(self):
        res = self.engine.search("aluminium sheet", {"source": "bottles_processes"})
        self.assertIn("bottles_processes/Materials production/Metals and semimetals",
                      [n["node_id"] for n in res["route"]])
        top = res["candidates"][0]
        self.assertEqual(top["id"], "bottles_processes:16")
        self.assertEqual(top["hierarchy_path"],
                         "bottles_processes > Materials production > Metals and semimetals")
        self.assertIsNotNone(top["scores"]["routed_rank"])
        self.assertIsNotNone(top["scores"]["global_rank"])

    def test_08_hierarchy_browse_over_http(self):
        status, body = self.http("GET", "/hierarchy")
        self.assertEqual(status, 200)
        self.assertEqual({n["node_id"] for n in body["nodes"]}, set(rag.SOURCES))
        status, body = self.http("GET", "/hierarchy?parent=bottles_processes")
        self.assertIn("bottles_processes/Materials production", [n["node_id"] for n in body["nodes"]])
        self.assertTrue(all(n["depth"] == 1 for n in body["nodes"]))

    def test_09_location_filter_is_exact(self):
        res = self.engine.search("process steam natural gas",
                                 {"source": "bottles_processes", "location": "EU-27"}, top_k=10)
        self.assertTrue(res["candidates"])
        self.assertTrue(all(c["location"] == "EU-27" for c in res["candidates"]))
        res = self.engine.search("process steam natural gas", {"location": "eu-27"})
        self.assertEqual(res["candidates"], [])  # exact, case-sensitive

    def test_10_kind_stage_and_unit_filters_are_exact(self):
        res = self.engine.search("carbon dioxide", {"kind": "elementary_flow"}, top_k=10)
        self.assertTrue(res["candidates"])
        self.assertTrue(all(c["kind"] == "elementary_flow" for c in res["candidates"]))
        res = self.engine.search("steam", {"source": "bottles_processes", "unit": "MJ",
                                           "stage": "Product-as-a-Service"}, top_k=10)
        self.assertTrue(res["candidates"])
        self.assertTrue(all(c["reference_unit"] == "MJ" and c["stage"] == "Product-as-a-Service"
                            for c in res["candidates"]))
        with self.assertRaises(ValueError):
            self.engine.search("steam", {"colour": "green"})

    # -- abstention ----------------------------------------------------------------

    def test_11_unsupported_query_abstains(self):
        out = self.engine.query("unobtainium flux capacitor", indicator="Climate change")
        self.assertEqual(out["status"], "insufficient_evidence")
        self.assertIsNone(out["answer"])
        out = self.engine.query("zzqqxx")
        self.assertEqual(out["reasons"], ["no_candidates"])
        # partial lexical overlap must not turn into evidence for a different material
        for q in ("recycled carbon fibre composite", "stainless steel", "aluminium can"):
            out = self.engine.query(q, {"source": "bottles_processes"}, indicator="Climate change")
            self.assertEqual(out["reasons"], ["weak_term_coverage"], q)
            self.assertIsNone(out["answer"])
            self.assertTrue(out["candidates"])  # still shown for review

    def test_12_missing_indicator_abstains(self):
        out = self.engine.query("bottle-grade PET", {"source": "bottles_processes"},
                                indicator="Global warming 100a (incl. NMVOC av.)")
        self.assertEqual(out["status"], "insufficient_evidence")
        self.assertIn("missing_indicator", out["reasons"])

    def test_13_indicator_labels_are_not_merged(self):
        labels = self.engine.indicators("lca_full_rag_data_units")
        self.assertIn("Human toxicity 100a", labels)
        self.assertNotIn("Human toxicity", labels)
        out = self.engine.query("bottle-grade PET", {"source": "lca_full_rag_data_units"},
                                indicator="Human toxicity")
        self.assertIn("missing_indicator", out["reasons"])

    def test_14_missing_unit_blocks_scaling(self):
        out = self.engine.query("bottle-grade PET", {"source": "lca_full_rag_data_units"},
                                indicator="Human toxicity 100a")
        self.assertIn("missing_reference_unit", out["reasons"])
        with self.assertRaises(rag.CalculationError) as ctx:
            self.engine.calculate("lca_full_rag_data_units:232", "Human toxicity 100a", 1, "kg")
        self.assertEqual(ctx.exception.code, "missing_reference_unit")

    # -- calculation -----------------------------------------------------------------

    def test_15_unit_mismatch_rejected(self):
        with self.assertRaises(rag.CalculationError) as ctx:
            self.engine.calculate(PET_BOTTLE, "Climate change", 1000, "g")
        self.assertEqual(ctx.exception.code, "unit_mismatch")
        status, body = self.http("POST", "/calculate", {"record_id": PET_BOTTLE,
                                                        "indicator": "Climate change",
                                                        "quantity": 1, "unit": "t"})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "unit_mismatch")

    def test_16_invalid_quantities_rejected(self):
        for qty in (0, -1, float("nan"), float("inf"), "abc", None, True):
            with self.subTest(qty=qty):
                with self.assertRaises(rag.CalculationError) as ctx:
                    self.engine.calculate(PET_BOTTLE, "Climate change", qty, "kg")
                self.assertEqual(ctx.exception.code, "invalid_quantity")

    def test_17_valid_calculation_and_evidence_answer(self):
        out = self.engine.calculate(PET_BOTTLE, "Climate change", 2, "kg")
        self.assertTrue(math.isclose(out["result"], 2 * 3.49975646))
        self.assertIn("bottles_processes.csv record 232", out["citation"])
        q = self.engine.query("bottle-grade PET", {"source": "bottles_processes"},
                              indicator="Climate change")
        self.assertEqual(q["status"], "ok")
        self.assertEqual(q["answer"]["record_id"], PET_BOTTLE)
        self.assertEqual(q["answer"]["value"], 3.49975646)

    # -- AI fallback -----------------------------------------------------------------

    def test_18_groq_contract_mocked(self):
        transport = groq_transport(json.dumps(good_estimate()))
        with unittest.mock.patch.dict("os.environ", {"GROQ_API_KEY": "test-key"}):
            out = ai_fallback.estimate("recycled PET flakes", "Climate change", "kg", {},
                                       [{"id": PET_BOTTLE, "name": "x"}], provider="groq",
                                       transport=transport)
        self.assertEqual(out["status"], "provisional")
        self.assertTrue(out["provisional"])
        self.assertIn("not measured or independently validated", out["label"])
        self.assertEqual(out["context_record_ids"], [PET_BOTTLE])
        self.assertEqual(out["provider"], "groq")
        self.assertTrue(out["model"])
        self.assertTrue(out["generated_at"].endswith("+00:00"))
        url, payload, headers = transport.calls[0]
        self.assertIn("api.groq.com", url)
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertIn("untrusted", payload["messages"][1]["content"])

    def test_19_ollama_contract_mocked(self):
        declined = good_estimate(can_estimate=False, value=None, lower=None, upper=None)
        out = ai_fallback.estimate("mystery composite", None, None, {}, [], provider="ollama",
                                   transport=ollama_transport(json.dumps(declined)))
        self.assertEqual(out["status"], "declined")
        out = ai_fallback.estimate("recycled PET", "Climate change", "kg", {}, [],
                                   provider="ollama",
                                   transport=ollama_transport(json.dumps(good_estimate())))
        self.assertEqual(out["status"], "provisional")
        self.assertEqual(out["estimate"]["value"], 3.0)

    def test_20_malformed_estimates_rejected(self):
        bad = {
            "not_json": "Sure! The value is 3.",
            "array": "[1, 2]",
            "missing_field": json.dumps({k: v for k, v in good_estimate().items() if k != "upper"}),
            "unordered": json.dumps(good_estimate(lower=5.0)),
            "non_finite": '{"can_estimate": true, "value": NaN}',
            "changed_indicator": json.dumps(good_estimate(indicator="Water depletion")),
            "changed_unit": json.dumps(good_estimate(reference_unit="MJ")),
            "unknown_unit": json.dumps(good_estimate(reference_unit="bushel")),
            "no_assumptions": json.dumps(good_estimate(assumptions=[])),
            "oversized": json.dumps(good_estimate(explanation="x" * 30_000)),
            "bool_as_number": json.dumps(good_estimate(value=True)),
            "declined_with_value": json.dumps(good_estimate(can_estimate=False)),
        }
        for name, raw in bad.items():
            with self.subTest(name):
                out = ai_fallback.estimate("q", "Climate change", "kg", {}, [], provider="ollama",
                                           transport=ollama_transport(raw))
                self.assertEqual(out["status"], "rejected", out)
                self.assertNotIn("estimate", out)

    def test_21_provider_failure_is_unavailable(self):
        def boom(url, payload, headers):
            raise urllib.error.URLError("connection refused")
        out = ai_fallback.estimate("q", None, None, {}, [], provider="ollama", transport=boom)
        self.assertEqual(out["status"], "unavailable")
        with tempfile.TemporaryDirectory() as empty, \
                unittest.mock.patch.dict("os.environ", {}, clear=True), \
                unittest.mock.patch.object(ai_fallback, "SECRETS_DIR", Path(empty)):
            out = ai_fallback.estimate("q", None, None, {}, [], provider="groq")
        self.assertEqual(out["status"], "unavailable")  # no key: no network call
        out = ai_fallback.estimate("q", provider="nope")
        self.assertEqual(out["status"], "unavailable")

    def test_22_fallback_only_when_evidence_insufficient(self):
        calls = []

        def stub(**kwargs):
            calls.append(kwargs)
            return {"status": "provisional", "provisional": True}
        ok = self.engine.query("bottle-grade PET", {"source": "bottles_processes"},
                               indicator="Climate change", use_ai=True, fallback=stub)
        self.assertEqual(ok["status"], "ok")
        self.assertIsNone(ok["fallback"])
        self.assertEqual(calls, [])
        bad = self.engine.query("bottle-grade PET", {"source": "lca_full_rag_data_units"},
                                indicator="Human toxicity 100a", use_ai=True, fallback=stub)
        self.assertEqual(bad["status"], "insufficient_evidence")
        self.assertEqual(bad["fallback"]["status"], "provisional")
        self.assertLessEqual(len(calls[0]["context"]), 5)
        self.assertIsNone(bad["answer"])  # estimate never becomes evidence

    def test_23_disabling_ai(self):
        def stub(**kwargs):
            raise AssertionError("fallback must not be called when AI is disabled")
        out = self.engine.query("unobtainium flux capacitor", use_ai=False, fallback=stub)
        self.assertEqual(out["status"], "insufficient_evidence")
        self.assertIsNone(out["fallback"])

    def test_24_source_immutability(self):
        transport = ollama_transport(json.dumps(good_estimate()))
        self.engine.query("unobtainium", indicator="Climate change", use_ai=True,
                          fallback=lambda **kw: ai_fallback.estimate(**kw, transport=transport))
        for f, digest in self.hashes.items():
            self.assertEqual(rag.sha256_file(rag.DATA_DIR / f), digest)
        with self.assertRaises(sqlite3.OperationalError):
            self.engine._all("UPDATE records SET name='x' WHERE id=?", (PET_BOTTLE,))
        self.assertEqual(self.engine.record(PET_BOTTLE)["name"], PET_BOTTLE_NAME)

    def test_25_http_query_and_record(self):
        status, body = self.http("POST", "/query", {"query": "steel rebar",
                                                    "filters": {"source": "bottles_processes"},
                                                    "indicator": "Climate change"})
        self.assertEqual(status, 200)
        self.assertEqual(body["candidates"][0]["id"], "bottles_processes:72")
        self.assertEqual(body["status"], "ok")
        status, rec = self.http("GET", "/record?id=bottles_processes:72")
        self.assertEqual(status, 200)
        self.assertIn("process_name", rec["raw"])
        self.assertIn("record 72", rec["citation"])
        self.assertEqual(self.http("GET", "/record?id=nope:1")[0], 404)
        self.assertEqual(self.http("POST", "/query", {"query": ""})[0], 400)

    def test_27_claude_contract_mocked(self):
        from types import SimpleNamespace
        seen = {}

        def fake_create(**request):
            seen.update(request)
            text = SimpleNamespace(type="text", text=json.dumps(good_estimate()))
            return SimpleNamespace(stop_reason="end_turn", stop_details=None,
                                   content=[text], model=request["model"])
        out = ai_fallback.estimate("recycled PET", "Climate change", "kg", {}, [],
                                   provider="claude", transport=fake_create)
        self.assertEqual(out["status"], "provisional")
        self.assertEqual(out["model"], "claude-opus-5-5")
        self.assertEqual(seen["output_config"]["format"]["type"], "json_schema")
        self.assertEqual(seen["fallbacks"], "default")
        self.assertIn("untrusted", seen["messages"][0]["content"])

        def refusing(**request):
            return SimpleNamespace(stop_reason="refusal", content=[], model=request["model"],
                                   stop_details=SimpleNamespace(category="cyber"))
        out = ai_fallback.estimate("q", provider="claude", transport=refusing)
        self.assertEqual(out["status"], "unavailable")

    def test_28_rejects_cross_site_requests(self):
        req = urllib.request.Request(self.base + "/query", data=b'{"query": "steel"}', method="POST",
                                     headers={"Content-Type": "text/plain"})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(ctx.exception.code, 415)
        req = urllib.request.Request(self.base + "/health", headers={"Host": "evil.example:80"})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(ctx.exception.code, 403)
        status, page = 200, urllib.request.urlopen(self.base + "/", timeout=10).read()
        self.assertIn(b"<title>", page)
        status, meta = self.http("GET", "/meta")
        self.assertIn("Climate change", meta["indicators"]["bottles_processes"])
        self.assertEqual(set(meta["providers"]), {"groq", "claude", "ollama"})

    def test_26_rebuild_is_atomic(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "idx.sqlite"
            target.write_bytes(b"previous index")
            broken = Path(tmp) / "data"
            broken.mkdir()
            (broken / "bottles_processes.csv").write_text("process_name,unit\nx,kg\n")
            with self.assertRaises(FileNotFoundError):  # later source files are missing
                rag.build(broken, target, verbose=False)
            self.assertEqual(target.read_bytes(), b"previous index")
            self.assertEqual([p.name for p in Path(tmp).iterdir() if ".tmp-" in p.name], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
