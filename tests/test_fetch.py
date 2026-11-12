"""Exercises the live-fetch path against a local fake MET server (no internet needed)."""
import gzip
import json
import os
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import demo_data  # noqa: E402
import engine  # noqa: E402

NOW = datetime.now(timezone.utc)
STATE = {"hits": [], "expires_in": 3600, "mode": "ok"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        path = self.path.split("?")[0]
        STATE["hits"].append((path, self.headers.get("User-Agent"), self.headers.get("If-Modified-Since")))
        if STATE["mode"] == "403" or path == "/forbidden":
            self.send_response(403); self.end_headers(); return
        if STATE["mode"] == "down":
            self.send_response(503); self.end_headers(); return
        lm = formatdate(NOW.timestamp() - 600, usegmt=True)
        if self.headers.get("If-Modified-Since") == lm:
            self.send_response(304)
            self.send_header("Expires", formatdate(NOW.timestamp() + STATE["expires_in"], usegmt=True))
            self.end_headers(); return
        if path == "/fc":
            body = json.dumps(demo_data.fake_forecast({"name": "X", "lat": 59.9, "lon": 10.7}, NOW)).encode()
            ctype = "application/json"
        elif path == "/alerts.rss":
            body, _ = demo_data.fake_alert_feeds(NOW); ctype = "application/rss+xml"
        elif path == "/alerts.json":
            _, body = demo_data.fake_alert_feeds(NOW); ctype = "application/json"
        else:
            self.send_response(404); self.end_headers(); return
        body = gzip.compress(body)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Encoding", "gzip")
        self.send_header("Last-Modified", lm)
        self.send_header("Expires", formatdate(NOW.timestamp() + STATE["expires_in"], usegmt=True))
        self.end_headers()
        self.wfile.write(body)


class FetchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(("127.0.0.1", 0), Handler)
        cls.base = "http://127.0.0.1:%d" % cls.srv.server_port
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        STATE.update(hits=[], expires_in=3600, mode="ok")
        self.tmp = tempfile.mkdtemp()

    def test_cache_then_conditional_get(self):
        fx = engine.Fetcher(self.tmp, "TestAgent/1.0 t@t.no", 5)
        body, src, fetched = fx.get(self.base + "/fc?lat=1&lon=2")
        self.assertEqual(src, "network")
        self.assertAlmostEqual(fetched, __import__("time").time(), delta=5)
        self.assertIn(b"timeseries", body)                     # gzip was decoded
        self.assertEqual(STATE["hits"][0][1], "TestAgent/1.0 t@t.no")
        body2, src2, f2 = fx.get(self.base + "/fc?lat=1&lon=2")    # still fresh: no request
        self.assertEqual((src2, len(STATE["hits"])), ("cache", 1))
        # force expiry, expect If-Modified-Since and a 304 reusing the cached body
        meta_p, _ = fx._paths(self.base + "/fc?lat=1&lon=2")
        meta = json.load(open(meta_p)); meta["expires"] = 0; json.dump(meta, open(meta_p, "w"))
        body3, src3, f3 = fx.get(self.base + "/fc?lat=1&lon=2")
        self.assertEqual(src3, "304")
        self.assertEqual(body3, body)
        self.assertIsNotNone(STATE["hits"][-1][2])

    def test_403_gives_actionable_error(self):
        fx = engine.Fetcher(self.tmp, "x", 5)
        with self.assertRaises(engine.FetchError) as cm:
            fx.get(self.base + "/forbidden")
        self.assertIn("user_agent", str(cm.exception))

    def test_stale_fallback_when_server_down(self):
        fx = engine.Fetcher(self.tmp, "x", 5)
        url = self.base + "/alerts.rss"
        fx.get(url)
        meta_p, _ = fx._paths(url)
        meta = json.load(open(meta_p)); meta["expires"] = 0; meta["last_modified"] = ""
        json.dump(meta, open(meta_p, "w"))
        STATE["mode"] = "down"
        orig = engine.time.sleep
        engine.time.sleep = lambda s: None
        try:
            body, src, fetched = fx.get(url)
        finally:
            engine.time.sleep = orig
        self.assertEqual(src, "stale")
        self.assertGreater(fetched, 0)  # remembers when it last worked, not "now"
        self.assertLess(fetched, __import__("time").time() + 1)

    def test_run_live_end_to_end(self):
        engine.FORECAST_URL = self.base + "/fc"
        engine.ALERTS_RSS = self.base + "/alerts.rss"
        engine.ALERTS_JSON = self.base + "/alerts.json"
        cfg = engine.load_config(os.path.join(os.path.dirname(__file__), "..", "config.json"))
        cfg["user_agent"] = "TestAgent/1.0 t@t.no"
        cities = engine.load_cities()[:6]
        forecasts, alerts, notes = engine.run_live(cfg, cities, self.tmp, NOW)
        self.assertTrue(all(not f.get("error") for f in forecasts))
        self.assertTrue(len(alerts) >= 3)
        model = engine.build_model(cities, forecasts, alerts, cfg, NOW, notes)
        self.assertEqual(len(model["cities"]), 6)
        self.assertEqual(notes["messages"], [])
        self.assertEqual(notes["warn_status"]["rss"]["state"], "ok")
        self.assertEqual(notes["warn_status"]["json"]["state"], "ok")
        self.assertTrue(all(f.get("fetched") for f in forecasts))
        self.assertEqual(model["meta"]["status"]["forecast"]["n_ok"], 6)

    def test_run_live_fails_fast_when_met_unreachable(self):
        engine.FORECAST_URL = self.base + "/fc"
        engine.ALERTS_RSS = self.base + "/alerts.rss"
        engine.ALERTS_JSON = self.base + "/alerts.json"
        cfg = engine.load_config(os.path.join(os.path.dirname(__file__), "..", "config.json"))
        cfg["user_agent"] = "TestAgent/1.0 t@t.no"
        STATE["mode"] = "down"
        orig = engine.time.sleep
        engine.time.sleep = lambda s: None
        try:
            with self.assertRaises(SystemExit) as cm:
                engine.run_live(cfg, engine.load_cities()[:3], self.tmp, NOW)
        finally:
            engine.time.sleep = orig
        self.assertIn("Cannot reach api.met.no", str(cm.exception))
        self.assertEqual(len([h for h in STATE["hits"] if h[0] == "/fc"]), 0)  # did not hammer 47 forecast calls

    def test_run_live_refuses_placeholder_agent(self):
        cfg = engine.load_config(os.path.join(os.path.dirname(__file__), "..", "config.json"))
        cfg["user_agent"] = "NorwayWeatherWatch/1.0 CHANGE_ME@example.com"  # independent of the user's own config.json
        with self.assertRaises(SystemExit):
            engine.run_live(cfg, engine.load_cities()[:1], self.tmp, NOW)


if __name__ == "__main__":
    unittest.main()
