"""Deployment behaviour: Redis-backed storage, rate limits, host and password guards.

Redis is replaced by an in-memory fake that speaks the same commands as Upstash's REST API.
"""

from __future__ import annotations

import base64
import json
import os
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
import ebom  # noqa: E402
import rag  # noqa: E402
import server  # noqa: E402
import store  # noqa: E402

SAMPLE = (ROOT / "web" / "sample_ebom.csv").read_text(encoding="utf-8")
REDIS_ENV = {"KV_REST_API_URL": "https://fake.upstash.io", "KV_REST_API_TOKEN": "t"}


class FakeRedis:
    def __init__(self):
        self.data = {}

    def __call__(self, *args):
        cmd, key, *rest = args
        if cmd == "GET":
            return self.data.get(key)
        if cmd == "SET":
            self.data[key] = rest[0]
            return "OK"
        if cmd == "INCR":
            self.data[key] = str(int(self.data.get(key, "0")) + 1)
            return int(self.data[key])
        if cmd == "EXPIRE":
            return 1
        raise AssertionError(cmd)


def stub_estimate():
    calls = []

    def stub(**kw):
        calls.append(kw["query"])
        est = {"can_estimate": True, "value": 10.0, "lower": 5.0, "upper": 20.0, "impact_unit": "kg CO2-eq",
               "explanation": "", "system_boundary": "x", "assumptions": ["a"], "missing_information": []}
        return {"status": "provisional", "estimate": est}
    stub.calls = calls
    return stub


class DeployTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not rag.INDEX_PATH.exists():
            rag.build(verbose=False)
        cls.engine = rag.Engine()

    @classmethod
    def tearDownClass(cls):
        cls.engine.close()

    def setUp(self):
        self.redis = FakeRedis()
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [
            unittest.mock.patch.dict(os.environ, REDIS_ENV),
            unittest.mock.patch.object(store, "command", self.redis),
            unittest.mock.patch.object(ebom, "RESULTS_DIR", Path(self.tmp.name) / "results"),
            unittest.mock.patch.object(ebom, "AI_CACHE_PATH", Path(self.tmp.name) / "ai.json"),
            unittest.mock.patch.object(ebom, "_AI_CACHE", None),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def test_values_are_compressed_and_round_trip(self):
        value = {"lines": [{"material": "HDPE", "impact": 0.004}] * 200}
        self.assertTrue(store.set_json("k", value))
        raw = self.redis.data["lca:k"]
        self.assertTrue(raw.startswith("z:"))
        self.assertLess(len(raw), len(json.dumps(value)) / 5)
        self.assertEqual(store.get_json("k"), value)
        self.assertIsNone(store.get_json("missing"))

    def test_saved_analysis_lives_in_redis(self):
        first = ebom.analyse_saved(self.engine, SAMPLE)
        self.assertFalse(first["saved"]["restored"])
        self.assertTrue(any(k.startswith("lca:res:") for k in self.redis.data))
        self.assertFalse((Path(self.tmp.name) / "results").exists())  # nothing written to disk
        again = ebom.analyse_saved(self.engine, SAMPLE)
        self.assertTrue(again["saved"]["restored"])
        self.assertEqual(again["product"]["total"], first["product"]["total"])

    def test_ai_answers_shared_through_redis_and_clearable(self):
        stub = stub_estimate()
        with unittest.mock.patch.object(ai_fallback, "estimate", stub):
            ebom.analyse(self.engine, SAMPLE, use_ai=True, provider="stub")
            n = len(stub.calls)
            self.assertGreater(n, 0)
            ebom._AI_CACHE = None  # a different serverless instance: empty memory
            ebom.analyse(self.engine, SAMPLE, use_ai=True, provider="stub")
            self.assertEqual(len(stub.calls), n)  # answers came from Redis
            ebom.clear_ai_cache()
            ebom.analyse(self.engine, SAMPLE, use_ai=True, provider="stub")
            self.assertEqual(len(stub.calls), 2 * n)  # cleared: asked again

    def test_rate_limit_and_guards(self):
        httpd = server.make_server(self.engine, port=0, fallback=lambda **kw: {"status": "unavailable"})
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{httpd.server_address[1]}"

        def post(body, headers=None):
            req = urllib.request.Request(base + "/query", data=json.dumps(body).encode(), method="POST",
                                         headers={"Content-Type": "application/json", **(headers or {})})
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    return r.status
            except urllib.error.HTTPError as e:
                return e.code
        try:
            with unittest.mock.patch.dict(os.environ, {"AI_RATE_LIMIT": "2"}):
                codes = [post({"query": "zzqq", "use_ai": True}) for _ in range(3)]
                self.assertEqual(codes, [200, 200, 429])
                self.assertEqual(post({"query": "zzqq"}), 200)  # searches without AI are not limited
            with unittest.mock.patch.dict(os.environ, {"SITE_PASSWORD": "pw", "ALLOWED_HOSTS": "lca.example.com"}):
                self.assertEqual(post({"query": "steel"}), 401)
                auth = {"Authorization": "Basic " + base64.b64encode(b"u:pw").decode()}
                self.assertEqual(post({"query": "steel"}, auth), 200)
                self.assertEqual(post({"query": "steel"}, {**auth, "Host": "lca.example.com"}), 200)
                self.assertEqual(post({"query": "steel"}, {**auth, "Host": "other.example.com"}), 403)
        finally:
            httpd.shutdown()
            httpd.server_close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
