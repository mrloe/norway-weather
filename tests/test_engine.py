import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import demo_data  # noqa: E402
import engine  # noqa: E402

UTC = timezone.utc
CFG = engine.load_config(os.path.join(os.path.dirname(__file__), "..", "config.json"))


class TimeTests(unittest.TestCase):
    def test_oslo_dst_boundaries(self):
        # 2026: DST starts Sun 29 Mar 01:00 UTC, ends Sun 25 Oct 01:00 UTC
        self.assertEqual(engine.oslo_offset_hours(datetime(2026, 3, 29, 0, 59, tzinfo=UTC)), 1)
        self.assertEqual(engine.oslo_offset_hours(datetime(2026, 3, 29, 1, 0, tzinfo=UTC)), 2)
        self.assertEqual(engine.oslo_offset_hours(datetime(2026, 10, 25, 0, 59, tzinfo=UTC)), 2)
        self.assertEqual(engine.oslo_offset_hours(datetime(2026, 10, 25, 1, 0, tzinfo=UTC)), 1)
        self.assertEqual(engine.oslo_offset_hours(datetime(2026, 11, 12, 12, 0, tzinfo=UTC)), 1)

    def test_day_key_crosses_midnight_in_oslo_not_utc(self):
        self.assertEqual(engine.day_key(datetime(2026, 11, 12, 23, 30, tzinfo=UTC)), "2026-11-13")
        self.assertEqual(engine.day_key(datetime(2026, 7, 12, 22, 30, tzinfo=UTC)), "2026-07-13")

    def test_next_day_keys_unique_and_consecutive_over_dst(self):
        keys = engine.next_day_keys(datetime(2026, 10, 22, 12, tzinfo=UTC), 8)
        self.assertEqual(len(set(keys)), 8)
        self.assertEqual(keys[0], "2026-10-22")
        self.assertEqual(keys[-1], "2026-10-29")

    def test_day_bounds_length(self):
        s, e = engine.day_bounds_epoch("2026-11-12")
        self.assertEqual((e - s) / 3600, 24)
        s, e = engine.day_bounds_epoch("2026-10-25")  # 25 h day
        self.assertEqual((e - s) / 3600, 25)


class TitleTests(unittest.TestCase):
    """Titles copied from the live feed seen on 2026-11-12."""

    def test_with_times(self):
        ev, lvl, area, times = engine.parse_title(
            "Kraftig snøfokk, gult nivå, Deler av fjellet i Sør-Norge, 2026-11-14T08:00:00+00:00, 2026-11-15T08:00:00+00:00")
        self.assertEqual((ev, lvl, area), ("Kraftig snøfokk", "yellow", "Deler av fjellet i Sør-Norge"))
        self.assertEqual(len(times), 2)

    def test_area_with_commas_and_no_times(self):
        ev, lvl, area, times = engine.parse_title(
            "Snø, gult nivå, Akershus, Oslo, Østfold, Vestfold, nedre Buskerud og sørlige Innlandet")
        self.assertEqual((ev, lvl), ("Snø", "yellow"))
        self.assertEqual(area, "Akershus, Oslo, Østfold, Vestfold, nedre Buskerud og sørlige Innlandet")
        self.assertEqual(times, [])

    def test_marine_code_without_level(self):
        ev, lvl, area, _ = engine.parse_title("Storm, B2")
        self.assertEqual((ev, lvl, area), ("Storm", "", "B2"))

    def test_orange_red(self):
        self.assertEqual(engine.parse_title("Vind, oransje nivå, X")[1], "orange")
        self.assertEqual(engine.parse_title("Regn, rødt nivå, X")[1], "red")


class GeometryTests(unittest.TestCase):
    def test_point_in_polygon_with_hole(self):
        outer = [[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]]
        hole = [[4, 4], [6, 4], [6, 6], [4, 6], [4, 4]]
        shapes = [[outer, hole]]
        self.assertTrue(engine.point_in_shapes(2, 2, shapes))
        self.assertFalse(engine.point_in_shapes(5, 5, shapes))
        self.assertFalse(engine.point_in_shapes(12, 5, shapes))

    def test_georss_order_is_lat_lon(self):
        ring = engine._ring_from_text("58.2 6.3 58.3 6.4 58.1 6.5")
        self.assertEqual(ring[0], [6.3, 58.2])  # [lon, lat]


class ForecastTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 12, 10, 30, tzinfo=UTC)
        self.city = {"name": "Testby", "lat": 59.9, "lon": 10.7, "region": "Østlandet"}
        self.fc = demo_data.fake_forecast(self.city, self.now)
        self.res = engine.summarize_forecast(self.fc, self.now)

    def test_days_cover_about_nine_days(self):
        self.assertGreaterEqual(len(self.res["days"]), 9)

    def test_rain_total_matches_independent_sum(self):
        # independent calculation straight from the raw series: hourly part + 6-hourly part
        ts = self.fc["properties"]["timeseries"]
        times = [engine.parse_iso(x["time"]) for x in ts]
        total = 0.0
        for i, x in enumerate(ts):
            if times[i] < self.now.replace(minute=0):
                continue
            gap = (times[i + 1] - times[i]).total_seconds() / 3600 if i + 1 < len(ts) else None
            if gap == 1 and "next_1_hours" in x["data"]:
                total += x["data"]["next_1_hours"]["details"]["precipitation_amount"]
            elif gap == 6 and "next_6_hours" in x["data"]:
                total += x["data"]["next_6_hours"]["details"]["precipitation_amount"]
        got = sum(d["rain"] for d in self.res["days"].values())
        self.assertAlmostEqual(got, total, delta=0.6)  # per-day rounding to 0.1 mm

    def test_coverage_never_exceeds_24h(self):
        for d in self.res["days"].values():
            self.assertLessEqual(d["cov"], 24.0)

    def test_series_aligned(self):
        s = self.res["series"]
        self.assertTrue(len(s["t"]) == len(s["temp"]) == len(s["wind"]) == len(s["rain"]) == len(s["hrs"]))
        self.assertEqual(s["t"], sorted(s["t"]))


class AssessTests(unittest.TestCase):
    th = CFG["thresholds"]

    def base(self, **kw):
        d = {"max_wind": 5, "min_t": 2, "max_t": 6, "rain": 3, "snow": 0, "thunder": 0, "cov": 24, "sym": "cloudy"}
        d.update(kw)
        return d

    def test_low(self):
        self.assertEqual(engine.assess(self.base(), self.th), (0, []))

    def test_elevated_and_high_with_reasons(self):
        lvl, why = engine.assess(self.base(rain=30), self.th)
        self.assertEqual(lvl, 1)
        self.assertEqual(why[0]["k"], "rain")
        self.assertEqual(engine.assess(self.base(max_wind=21), self.th)[0], 2)
        self.assertEqual(engine.assess(self.base(min_t=-26), self.th)[0], 2)
        self.assertEqual(engine.assess(self.base(thunder=1), self.th)[0], 1)

    def test_multiple_reasons_listed(self):
        lvl, why = engine.assess(self.base(max_wind=15, rain=60), self.th)
        self.assertEqual(lvl, 2)
        self.assertEqual({w["k"] for w in why}, {"wind", "rain"})


class AlertTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 12, 14, 0, tzinfo=UTC)
        rss, js = demo_data.fake_alert_feeds(self.now)
        self.items = engine.parse_rss(rss)
        self.json = engine.parse_json_alerts(js)
        self.alerts = engine.build_alerts(self.items, self.json, CFG, self.now)

    def test_rss_polygons_parsed(self):
        self.assertEqual(len(self.items), 4)
        self.assertTrue(all(i["shapes"] for i in self.items))

    def test_marine_excluded_by_default(self):
        self.assertNotIn("Kuling", [a["event"] for a in self.alerts])
        cfg = dict(CFG, include_marine=True)
        self.assertIn("Kuling", [a["event"] for a in engine.build_alerts(self.items, self.json, cfg, self.now)])

    def test_json_only_alert_is_kept(self):
        self.assertIn("json", {a["source"] for a in self.alerts})

    def test_merge_prefers_json_level(self):
        a = [x for x in self.alerts if x["event"] == "Vind"][0]
        self.assertEqual(a["level"], "orange")
        self.assertEqual(a["source"], "rss+json")

    def test_expired_alert_dropped(self):
        later = self.now + timedelta(days=10)
        self.assertEqual(engine.build_alerts(self.items, self.json, CFG, later), [])

    def test_sorted_worst_first(self):
        ranks = [a["rank"] for a in self.alerts]
        self.assertEqual(ranks, sorted(ranks, reverse=True))

    def test_alert_days_and_when_text(self):
        a = {"start": "2026-11-14T22:00:00+00:00", "end": "2026-11-16T10:00:00+00:00", "time_unknown": False}
        self.assertEqual(engine.alert_days(a), ["2026-11-14", "2026-11-15", "2026-11-16"])
        self.assertEqual(engine.when_text(a, "2026-11-14"), "from 23:00")
        self.assertEqual(engine.when_text(a, "2026-11-15"), "all day")
        self.assertEqual(engine.when_text(a, "2026-11-16"), "until 11:00")

    def test_cancelled_title_skipped(self):
        items = [{"id": "x", "title": "Kansellert varsel for kuling, Stad", "description": "", "link": "", "pub": "",
                  "shapes": [[[[0, 0], [1, 0], [1, 1], [0, 0]]]]}]
        self.assertEqual(engine.build_alerts(items, {}, CFG, self.now), [])


class EndToEndTests(unittest.TestCase):
    def test_demo_model(self):
        now = datetime(2026, 11, 12, 14, 0, tzinfo=UTC)
        cities = engine.load_cities()
        self.assertEqual(len(cities), 48)
        fcs = [engine.summarize_forecast(demo_data.fake_forecast(c, now), now) for c in cities]
        rss, js = demo_data.fake_alert_feeds(now)
        alerts = engine.build_alerts(engine.parse_rss(rss), engine.parse_json_alerts(js), CFG, now)
        m = engine.build_model(cities, fcs, alerts, CFG, now, {"demo": True, "messages": []})
        by = {c["name"]: c for c in m["cities"]}
        # Oslo (inside the eastern snow polygon) must carry the yellow snow warning today
        self.assertEqual(by["Oslo"]["days"][0]["rank"], 1)
        # Bergen is inside the west polygon (orange wind) but inside the hole of the json-only rain alert
        names = {alerts[ai]["name"] for d in by["Bergen"]["days"] for ai, _ in d["al"]}
        self.assertNotIn("Styrtregn", names)
        # Stavanger is in the west polygon and not in the hole: should get both
        names = {alerts[ai]["name"] for d in by["Stavanger"]["days"] for ai, _ in d["al"]}
        self.assertIn("Styrtregn", names)
        # Bodoe is only covered by the JSON-only red alert
        self.assertEqual(max(d["rank"] for d in by["Bodø"]["days"]), 3)
        # Tromsoe is outside everything
        self.assertEqual(max(d["rank"] for d in by["Tromsø"]["days"]), 0)
        self.assertEqual(len(m["days"]), 10)

    def test_output_files(self):
        import tempfile
        now = datetime(2026, 11, 12, 14, 0, tzinfo=UTC)
        cities = engine.load_cities()
        fcs = [engine.summarize_forecast(demo_data.fake_forecast(c, now), now) for c in cities]
        rss, js = demo_data.fake_alert_feeds(now)
        alerts = engine.build_alerts(engine.parse_rss(rss), engine.parse_json_alerts(js), CFG, now)
        m = engine.build_model(cities, fcs, alerts, CFG, now, {"demo": True, "messages": []})
        with tempfile.TemporaryDirectory() as tmp:
            engine.write_outputs(m, tmp)
            for f in ("index.html", "data.json", "daily.csv", "alerts.csv", "digest.txt"):
                self.assertTrue(os.path.getsize(os.path.join(tmp, f)) > 100, f)
            html = open(os.path.join(tmp, "index.html"), encoding="utf-8").read()
            self.assertNotIn("__DATA__", html)
            blob = html.split('type="application/json">')[1].split("</script>")[0]
            self.assertEqual(json.loads(blob.replace("<\\/", "</"))["meta"]["horizon_days"], 10)
            rows = open(os.path.join(tmp, "daily.csv"), encoding="utf-8-sig").read().strip().split("\n")
            self.assertEqual(len(rows), 1 + 48 * 10)


class TrustTests(unittest.TestCase):
    def meta(self, age_min=5, n_ok=47, n_failed=0, n_stale=0, fetch_age=5, rss="ok", js="ok"):
        now = 1_800_000_000
        mk = lambda state, a: {"state": state, "fetched": None if state == "failed" else now - a * 60, "items": 3, "error": "boom" if state == "failed" else ""}
        meta = {"generated_epoch": now - age_min * 60, "fresh_minutes": 90, "stale_after_minutes": 180,
                "status": {"forecast": {"n_total": 47, "n_ok": n_ok, "n_failed": n_failed, "n_stale": n_stale,
                                         "oldest_fetch": now - fetch_age * 60, "newest_fetch": now - 60,
                                         "met_updated_oldest": "", "met_updated_newest": ""},
                           "warnings": {"rss": mk(rss, 5), "json": mk(js, 5)}}}
        return meta, now

    def level(self, **kw):
        meta, now = self.meta(**kw)
        return engine.assess_trust(meta, now)

    def test_fresh(self):
        self.assertEqual(self.level(), (0, []))

    def test_aging_and_stale_by_age(self):
        self.assertEqual(self.level(age_min=120)[0], 1)
        self.assertEqual(self.level(age_min=200)[0], 2)

    def test_old_forecast_fetch_even_if_engine_ran_recently(self):
        lvl, why = self.level(fetch_age=240)
        self.assertEqual(lvl, 2)
        self.assertIn("Oldest forecast", why[0])

    def test_failures(self):
        self.assertEqual(self.level(n_failed=3, n_ok=44)[0], 1)
        self.assertEqual(self.level(n_stale=2)[0], 1)
        self.assertEqual(self.level(n_ok=0, n_failed=47)[0], 2)

    def test_warning_feed_states(self):
        self.assertEqual(self.level(rss="failed")[0], 1)
        self.assertEqual(self.level(js="stale")[0], 1)
        lvl, why = self.level(rss="failed", js="failed")
        self.assertEqual(lvl, 2)
        self.assertTrue(any("does NOT mean none exist" in w for w in why))

    def test_digest_and_status_file_report_problems(self):
        now = datetime(2026, 11, 12, 14, 0, tzinfo=UTC)
        cities = engine.load_cities()[:4]
        fcs = [engine.summarize_forecast(demo_data.fake_forecast(c, now), now) for c in cities]
        for f in fcs:
            f["fetched"], f["src"] = now.timestamp() - 3600 * 5, "stale"   # fetched 5 h ago
        ws = {"rss": {"state": "failed", "fetched": None, "items": 0, "error": "timeout"},
              "json": {"state": "failed", "fetched": None, "items": 0, "error": "timeout"}}
        m = engine.build_model(cities, fcs, [], CFG, now, {"warn_status": ws, "messages": []})
        text = engine.make_digest(m)
        self.assertIn("DATA STATUS when written: STALE", text)
        self.assertIn("PROBLEM:", text)
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            engine.write_outputs(m, tmp)
            st = json.load(open(os.path.join(tmp, "status.json")))
            self.assertEqual(st["trust_at_write"], "stale")
            self.assertTrue(st["reasons"])


if __name__ == "__main__":
    unittest.main()
