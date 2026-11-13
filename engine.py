#!/usr/bin/env python3
"""
Norway weather watch: forecast + official warning engine for a fixed list of cities.

Sources (MET Norway, free, CC BY 4.0):
  - Locationforecast 2.0 (compact): forecast per point, about 9-10 days
  - MetAlerts 2.0: official yellow/orange/red warnings. RSS (with polygons) is the primary
    source, the GeoJSON feed enriches it (land/sea flag, texts) and is used as fallback.

Output (folder "site" by default):
  index.html   self-contained dashboard (open it, host it, or print it to PDF)
  data.json    everything the dashboard shows
  daily.csv    one row per city per day (for Looker Studio, Gemini, BigQuery, Excel)
  alerts.csv   active official warnings and the cities they cover
  digest.txt   plain-text summary for Slack/email

Usage:
  python engine.py                 fetch live data, write site/
  python engine.py --demo          synthetic data, to preview the dashboard offline
  python engine.py --serve         build, then serve site/ on http://localhost:8000
  python engine.py --watch 30      rebuild every 30 minutes
Standard library only. Python 3.9+.
"""
import argparse
import csv
import gzip
import hashlib
import http.server
import json
import os
import re
import socketserver
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

HERE = os.path.dirname(os.path.abspath(__file__))
FORECAST_URL = "https://api.met.no/weatherapi/locationforecast/2.0/compact"
ALERTS_RSS = "https://api.met.no/weatherapi/metalerts/2.0/current.rss"
ALERTS_JSON = "https://api.met.no/weatherapi/metalerts/2.0/current.json"

LEVEL_RANK = {"yellow": 1, "orange": 2, "red": 3}
LEVEL_LABEL = {0: "None", 1: "Yellow", 2: "Orange", 3: "Red"}
NO_LEVEL = {"gult": "yellow", "gul": "yellow", "oransje": "orange", "rødt": "red", "rød": "red"}
RISK_LABEL = {0: "Low", 1: "Elevated", 2: "High"}

DEFAULT_CONFIG = {
    "user_agent": "",
    "days": 10,
    "workers": 6,
    "timeout_s": 15,
    "include_marine": False,
    "marine_events": ["kuling", "storm", "orkan", "polart lavtrykk", "ising på fartøy"],
    "fresh_minutes": 90,
    "stale_after_minutes": 180,
    "thresholds": {
        "wind_elevated": 13.9, "wind_high": 20.8,
        "rain_elevated": 25.0, "rain_high": 50.0,
        "cold_elevated": -15.0, "cold_high": -25.0,
    },
}

# ---------------------------------------------------------------------------
# Time (Oslo) without needing tzdata on Windows
# ---------------------------------------------------------------------------

def _last_sunday(year, month):
    d = datetime(year, month, 31, tzinfo=timezone.utc)
    return d - timedelta(days=(d.weekday() + 1) % 7)


def oslo_offset_hours(dt_utc):
    start = _last_sunday(dt_utc.year, 3).replace(hour=1)
    end = _last_sunday(dt_utc.year, 10).replace(hour=1)
    return 2 if start <= dt_utc < end else 1


def to_oslo(dt_utc):
    return dt_utc + timedelta(hours=oslo_offset_hours(dt_utc))


def day_key(dt_utc):
    return to_oslo(dt_utc).strftime("%Y-%m-%d")


def parse_iso(s):
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def day_bounds_epoch(key):
    """Oslo midnight to next Oslo midnight, as UTC epoch seconds."""
    d = datetime.strptime(key, "%Y-%m-%d").replace(tzinfo=timezone.utc)

    def midnight(day):
        for off in (1, 2):
            cand = day - timedelta(hours=off)
            if oslo_offset_hours(cand) == off:
                return cand
        return day - timedelta(hours=1)

    return int(midnight(d).timestamp()), int(midnight(d + timedelta(days=1)).timestamp())


def next_day_keys(now, n):
    keys, t = [], now
    while len(keys) < n:
        k = day_key(t)
        if k not in keys:
            keys.append(k)
        t += timedelta(hours=12)  # 12 h steps: DST changes cannot skip or repeat a day
    return keys


def day_label(key):
    return datetime.strptime(key, "%Y-%m-%d").strftime("%a %d %b")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config(path=None):
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    path = path or os.path.join(HERE, "config.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            user = json.load(f)
        th = user.pop("thresholds", {})
        cfg.update(user)
        cfg["thresholds"].update(th)
    env_ua = os.environ.get("WEATHER_USER_AGENT", "").strip()
    if env_ua:
        cfg["user_agent"] = env_ua
    return cfg


def load_cities(path=None):
    path = path or os.path.join(HERE, "cities.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# HTTP with disk cache that follows MET's rules (Expires, If-Modified-Since, gzip)
# ---------------------------------------------------------------------------

class FetchError(Exception):
    pass


class Fetcher:
    def __init__(self, cache_dir, user_agent, timeout=30):
        self.dir = cache_dir
        self.ua = user_agent
        self.timeout = timeout
        os.makedirs(cache_dir, exist_ok=True)

    def _paths(self, url):
        h = hashlib.sha1(url.encode()).hexdigest()
        return os.path.join(self.dir, h + ".meta"), os.path.join(self.dir, h + ".body")

    def get(self, url):
        """Returns (bytes, source, fetched_epoch); source in cache / network / 304 / stale.
        fetched_epoch is when MET last confirmed this data (for stale: the last time that worked)."""
        meta_p, body_p = self._paths(url)
        meta, body = None, None
        if os.path.exists(meta_p) and os.path.exists(body_p):
            try:
                with open(meta_p, encoding="utf-8") as f:
                    meta = json.load(f)
                with open(body_p, "rb") as f:
                    body = f.read()
            except (OSError, ValueError):
                meta, body = None, None
        if meta and body is not None and meta.get("expires", 0) > time.time():
            return body, "cache", meta.get("fetched", 0)

        headers = {"User-Agent": self.ua, "Accept-Encoding": "gzip"}
        if meta and body is not None and meta.get("last_modified"):
            headers["If-Modified-Since"] = meta["last_modified"]

        last_err = None
        for attempt in range(3):
            try:
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    raw = r.read()
                    if r.headers.get("Content-Encoding", "").lower() == "gzip":
                        raw = gzip.decompress(raw)
                    self._store(meta_p, body_p, raw, r.headers)
                    return raw, "network", time.time()
            except urllib.error.HTTPError as e:
                if e.code == 304 and body is not None:
                    self._touch(meta_p, meta, e.headers)
                    return body, "304", time.time()
                if e.code == 403:
                    raise FetchError("HTTP 403 from MET. Set a real user_agent in config.json "
                                     "(app name + your contact email).")
                last_err = "HTTP %s" % e.code
                if e.code not in (429, 500, 502, 503, 504):
                    break
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last_err = str(e)
            time.sleep(2 ** attempt)
        if body is not None:
            return body, "stale", (meta or {}).get("fetched", 0)
        raise FetchError("%s: %s" % (url, last_err))

    @staticmethod
    def _expires(headers, default_s=600):
        try:
            return parsedate_to_datetime(headers.get("Expires")).timestamp()
        except Exception:
            return time.time() + default_s

    def _store(self, meta_p, body_p, raw, headers):
        with open(body_p, "wb") as f:
            f.write(raw)
        with open(meta_p, "w", encoding="utf-8") as f:
            json.dump({"expires": self._expires(headers), "fetched": time.time(),
                       "last_modified": headers.get("Last-Modified", "")}, f)

    def _touch(self, meta_p, meta, headers):
        meta = dict(meta or {})
        meta["expires"] = self._expires(headers)
        meta["fetched"] = time.time()
        with open(meta_p, "w", encoding="utf-8") as f:
            json.dump(meta, f)


# ---------------------------------------------------------------------------
# Forecast
# ---------------------------------------------------------------------------

def summarize_forecast(fc, now):
    """Locationforecast compact JSON -> per Oslo-day numbers plus a compact series.

    The series is hourly for about 60 h, then 6-hourly. Precipitation comes from next_1_hours
    when the next step is 1 h away, else from next_6_hours. A period counts for the day of the
    step it starts on (a 6 h period can spill up to 1 h past midnight).
    """
    ts = fc["properties"]["timeseries"]
    if not ts:
        raise ValueError("empty timeseries")
    times = [parse_iso(x["time"]) for x in ts]
    idx = 0
    for i, t in enumerate(times):
        if t <= now:
            idx = i
        else:
            break

    days = {}
    series = {"t": [], "temp": [], "wind": [], "rain": [], "hrs": [], "sym": []}
    for i in range(idx, len(ts)):
        t = times[i]
        gap = (times[i + 1] - t).total_seconds() / 3600 if i + 1 < len(ts) else None
        d = days.setdefault(day_key(t), {
            "max_wind": None, "min_t": None, "max_t": None, "rain": 0.0, "snow": 0,
            "thunder": 0, "cov": 0.0, "sym": None, "_nd": 99.0})
        data = ts[i]["data"]
        inst = data["instant"]["details"]
        w, temp = inst.get("wind_speed"), inst.get("air_temperature")
        if w is not None:
            d["max_wind"] = w if d["max_wind"] is None else max(d["max_wind"], w)
        if temp is not None:
            d["min_t"] = temp if d["min_t"] is None else min(d["min_t"], temp)
            d["max_t"] = temp if d["max_t"] is None else max(d["max_t"], temp)

        n1, n6 = data.get("next_1_hours"), data.get("next_6_hours")
        per, hrs, scale = None, 0.0, 1.0
        if n1 and gap is not None and gap <= 1.5:
            per, hrs = n1, 1.0
        elif n6:
            per = n6
            hrs = min(gap, 6.0) if gap is not None else 6.0
            scale = hrs / 6.0
        elif n1:
            per, hrs = n1, 1.0

        rain_step, sym = 0.0, None
        if per:
            pd = per.get("details", {})
            if pd.get("precipitation_amount") is not None:
                rain_step = pd["precipitation_amount"] * scale
                d["rain"] += rain_step
            if pd.get("air_temperature_max") is not None:
                d["max_t"] = pd["air_temperature_max"] if d["max_t"] is None else max(d["max_t"], pd["air_temperature_max"])
            if pd.get("air_temperature_min") is not None:
                d["min_t"] = pd["air_temperature_min"] if d["min_t"] is None else min(d["min_t"], pd["air_temperature_min"])
            sym = (per.get("summary") or {}).get("symbol_code")
            if sym:
                if "snow" in sym or "sleet" in sym:
                    d["snow"] = 1
                if "thunder" in sym:
                    d["thunder"] = 1
            d["cov"] += hrs
        if sym:
            lt = to_oslo(t)
            dist = abs(lt.hour + lt.minute / 60 - 12)
            if dist < d["_nd"]:
                d["_nd"], d["sym"] = dist, sym

        series["t"].append(int(t.timestamp()))
        series["temp"].append(temp)
        series["wind"].append(w)
        series["rain"].append(round(rain_step, 2))
        series["hrs"].append(hrs or (gap or 1))
        series["sym"].append(sym)

    for d in days.values():
        d.pop("_nd", None)
        d["rain"] = round(d["rain"], 1)
        d["cov"] = min(round(d["cov"], 1), 24.0)
        for k in ("max_wind", "min_t", "max_t"):
            if d[k] is not None:
                d[k] = round(d[k], 1)
    meta = fc["properties"].get("meta") or {}
    return {"updated": meta.get("updated_at", ""), "days": days, "series": series}


def assess(d, th):
    """Forecast-only risk (our rule, not an official warning). Returns (0|1|2, reasons)."""
    if not d:
        return 0, []
    lvl, why = 0, []

    def bump(l, kind, text):
        nonlocal lvl
        lvl = max(lvl, l)
        why.append({"k": kind, "txt": text})

    w, rain, tmin = d["max_wind"], d["rain"], d["min_t"]
    if w is not None:
        if w >= th["wind_high"]:
            bump(2, "wind", "Mean wind %g m/s (high limit %g)" % (w, th["wind_high"]))
        elif w >= th["wind_elevated"]:
            bump(1, "wind", "Mean wind %g m/s (limit %g)" % (w, th["wind_elevated"]))
    if rain >= th["rain_high"]:
        bump(2, "rain", "Rain %g mm in the day (high limit %g)" % (rain, th["rain_high"]))
    elif rain >= th["rain_elevated"]:
        bump(1, "rain", "Rain %g mm in the day (limit %g)" % (rain, th["rain_elevated"]))
    if tmin is not None:
        if tmin <= th["cold_high"]:
            bump(2, "cold", "Min temperature %g C (high limit %g)" % (tmin, th["cold_high"]))
        elif tmin <= th["cold_elevated"]:
            bump(1, "cold", "Min temperature %g C (limit %g)" % (tmin, th["cold_elevated"]))
    if d["thunder"]:
        bump(1, "thunder", "Thunder in the forecast symbols")
    return lvl, why


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------

ISO_RE = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{2}:\d{2})"
LEVEL_RE = re.compile(r"^(gult|gul|oransje|rødt|rød|yellow|orange|red)\b", re.I)


def parse_title(title):
    """'Kuling, gult nivå, Åna-Sira - Karmøy, <start>, <end>' -> (event, level, area, times)."""
    times = re.findall(ISO_RE, title)
    base = re.sub(r",\s*" + ISO_RE, "", title)
    parts = [p.strip() for p in base.split(",") if p.strip()]
    if not parts:
        return "", "", "", times
    event, rest, level = parts[0], parts[1:], ""
    if rest:
        m = LEVEL_RE.match(rest[0])
        if m:
            w = m.group(1).lower()
            level = NO_LEVEL.get(w, w)
            rest = rest[1:]
    return event, level, ", ".join(rest), times


def _ring_from_text(text):
    """georss/CAP polygon text -> ring of [lon, lat]. Input is 'lat lon lat lon ...' or 'lat,lon lat,lon'."""
    nums = [float(x) for x in re.split(r"[\s,]+", (text or "").strip()) if x]
    return [[nums[i + 1], nums[i]] for i in range(0, len(nums) - 1, 2)]


def parse_rss(raw):
    root = ET.fromstring(raw)
    out = []
    for it in root.iter("item"):
        shapes = []
        for el in it.iter():
            tag = el.tag.split("}")[-1] if isinstance(el.tag, str) else ""
            if tag == "polygon":
                ring = _ring_from_text(el.text)
                if len(ring) >= 3:
                    shapes.append([ring])
        out.append({
            "id": (it.findtext("guid") or "").strip(),
            "title": (it.findtext("title") or "").strip(),
            "description": (it.findtext("description") or "").strip(),
            "link": (it.findtext("link") or "").strip(),
            "pub": (it.findtext("pubDate") or "").strip(),
            "shapes": shapes,
        })
    return out


def geometry_to_shapes(g):
    if not g:
        return []
    if g.get("type") == "Polygon":
        return [g["coordinates"]]
    if g.get("type") == "MultiPolygon":
        return list(g["coordinates"])
    return []


def level_from_props(p):
    aw = str(p.get("awareness_level") or "")
    if ";" in aw:
        c = aw.split(";")[1].strip().lower()
        if c in LEVEL_RANK:
            return c
    c = str(p.get("riskMatrixColor") or "").strip().lower()
    return c if c in LEVEL_RANK else ""


def parse_json_alerts(raw):
    gj = json.loads(raw)
    out = {}
    for f in gj.get("features", []):
        p = f.get("properties") or {}
        aid = p.get("id")
        if not aid or (p.get("status") and p["status"] != "Actual"):
            continue
        iv = (f.get("when") or {}).get("interval") or []
        out[aid] = {
            "id": aid, "name": p.get("eventAwarenessName") or p.get("event") or "",
            "event": p.get("event") or "", "level": level_from_props(p),
            "domain": p.get("geographicDomain") or "",
            "start": iv[0] if len(iv) > 0 else "",
            "end": iv[1] if len(iv) > 1 else (p.get("eventEndingTime") or ""),
            "area": p.get("area") or "", "title": p.get("title") or "",
            "description": p.get("description") or "", "consequences": p.get("consequences") or "",
            "instruction": p.get("instruction") or "", "web": p.get("web") or "",
            "shapes": geometry_to_shapes(f.get("geometry")),
        }
    return out


def build_alerts(rss_items, json_by_id, cfg, now):
    """Merge RSS (primary) and JSON (enrichment/fallback) into one clean list of land alerts."""
    marine = {m.lower() for m in cfg["marine_events"]}
    merged, seen = [], set()

    def finish(a, source):
        a["source"] = source
        a["rank"] = LEVEL_RANK.get(a["level"], 0)
        if not a["domain"]:
            a["domain"] = "marine" if a["event"].lower() in marine else "land"
        merged.append(a)

    for it in rss_items:
        if not it["id"] or it["title"].lower().startswith("kansellert"):
            continue
        seen.add(it["id"])
        event, level, area, times = parse_title(it["title"])
        if len(times) < 2:
            times = re.findall(ISO_RE, it["description"])
        j = json_by_id.get(it["id"])
        a = {
            "id": it["id"], "event": event, "name": (j or {}).get("name") or event,
            "level": (j or {}).get("level") or level, "domain": (j or {}).get("domain", ""),
            "start": (j or {}).get("start") or (times[0] if len(times) > 0 else ""),
            "end": (j or {}).get("end") or (times[1] if len(times) > 1 else ""),
            "area": (j or {}).get("area") or area, "title": it["title"],
            "description": (j or {}).get("description") or "",
            "consequences": (j or {}).get("consequences", ""),
            "instruction": (j or {}).get("instruction", ""),
            "web": (j or {}).get("web") or it["link"],
            "shapes": it["shapes"] or (j or {}).get("shapes", []),
        }
        finish(a, "rss+json" if j else "rss")
    for aid, j in json_by_id.items():
        if aid not in seen:
            a = dict(j)
            a["web"] = a.get("web", "")
            finish(a, "json")

    out = []
    for a in merged:
        if a["domain"] == "marine" and not cfg["include_marine"]:
            continue
        if not a["rank"] or not a["shapes"]:
            continue
        a["time_unknown"] = not (a["start"] and a["end"])
        if a["end"] and parse_iso(a["end"]) < now:
            continue
        out.append(a)
    out.sort(key=lambda a: (-a["rank"], a["start"]))
    return out


def _ring_contains(ring, x, y):
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi, xj, yj = ring[i][0], ring[i][1], ring[j][0], ring[j][1]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def point_in_shapes(x, y, shapes):
    for rings in shapes:
        if not rings or not _ring_contains(rings[0], x, y):
            continue
        if any(_ring_contains(h, x, y) for h in rings[1:]):
            continue
        return True
    return False


def alert_days(a):
    if not (a["start"] or a["end"]):
        return []
    s = parse_iso(a["start"] or a["end"])
    e = parse_iso(a["end"] or a["start"])
    end_key, keys, t = day_key(e), [], s
    for _ in range(90):
        k = day_key(t)
        if k not in keys:
            keys.append(k)
        if k >= end_key:
            break
        t += timedelta(hours=12)
    return keys


def when_text(a, key):
    if a.get("time_unknown"):
        return "time not stated"
    s, e = parse_iso(a["start"]), parse_iso(a["end"])
    son, eon = day_key(s) == key, day_key(e) == key
    f = lambda d: to_oslo(d).strftime("%H:%M")
    if son and eon:
        return "%s-%s" % (f(s), f(e))
    if son:
        return "from " + f(s)
    if eon:
        return "until " + f(e)
    return "all day"


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------

def forecast_status(forecasts):
    ok = [f for f in forecasts if f and not f.get("error")]
    fetched = [f["fetched"] for f in ok if f.get("fetched")]
    upd = [f["updated"] for f in ok if f.get("updated")]
    return {"n_total": len(forecasts), "n_ok": len(ok), "n_failed": len(forecasts) - len(ok),
            "n_stale": sum(1 for f in ok if f.get("src") == "stale"),
            "oldest_fetch": min(fetched) if fetched else None, "newest_fetch": max(fetched) if fetched else None,
            "met_updated_oldest": min(upd) if upd else "", "met_updated_newest": max(upd) if upd else ""}


def human_minutes(m):
    m = max(0, m)
    return "%d min" % round(m) if m < 90 else "%.1f h" % (m / 60)


def fmt_epoch(ep):
    return to_oslo(datetime.fromtimestamp(ep, timezone.utc)).strftime("%a %d %b %H:%M") if ep else "never"


TRUST_NAMES = ["FRESH", "AGING", "STALE"]


def assess_trust(meta, now_epoch):
    """How far to trust the data right now. Level 0 fresh, 1 aging, 2 stale. The page runs the same rules in JS."""
    fresh, stale = meta["fresh_minutes"], meta["stale_after_minutes"]
    st = meta["status"]
    level, why = 0, []

    def up(l, msg):
        nonlocal level
        level = max(level, l)
        why.append(msg)

    def age_rule(ep, label):
        a = (now_epoch - ep) / 60
        if a > stale:
            up(2, "%s %s old." % (label, human_minutes(a)))
        elif a > fresh:
            up(1, "%s %s old." % (label, human_minutes(a)))

    age_rule(meta["generated_epoch"], "The newest data is")
    f = st["forecast"]
    if f["n_ok"] == 0:
        up(2, "No forecast data at all.")
    else:
        if f["n_failed"]:
            up(1, "%d of %d cities have no forecast." % (f["n_failed"], f["n_total"]))
        if f["n_stale"]:
            up(1, "%d cities show old cached forecasts because MET could not be reached." % f["n_stale"])
        if f["oldest_fetch"]:
            age_rule(f["oldest_fetch"], "Oldest forecast data is")
    w = st["warnings"]
    states = [w["rss"]["state"], w["json"]["state"]]
    if states == ["failed", "failed"]:
        up(2, "No warning data. Seeing no warnings does NOT mean none exist.")
    else:
        for name in ("rss", "json"):
            x = w[name]
            if x["state"] == "failed":
                up(1, "Warnings %s feed failed (%s)." % (name.upper(), x.get("error") or "no detail"))
            elif x["state"] == "stale":
                up(1, "Warnings %s feed is from old cache." % name.upper())
            if x["state"] != "failed" and x.get("fetched"):
                age_rule(x["fetched"], "Warnings %s data is" % name.upper())
    return level, why


def build_model(cities, forecasts, alerts, cfg, now, notes):
    keys = next_day_keys(now, cfg["days"])
    th = cfg["thresholds"]
    adays = [alert_days(a) for a in alerts]

    city_out = []
    alert_cities = {i: [] for i in range(len(alerts))}
    for ci, c in enumerate(cities):
        f = forecasts[ci]
        hits = [ai for ai, a in enumerate(alerts) if point_in_shapes(c["lon"], c["lat"], a["shapes"])]
        for ai in hits:
            alert_cities[ai].append(ci)
        days = []
        for k in keys:
            d = (f or {}).get("days", {}).get(k) if f and not f.get("error") else None
            risk, why = assess(d, th)
            day_alerts = [[ai, when_text(alerts[ai], k)] for ai in hits if k in adays[ai]]
            day_alerts.sort(key=lambda x: -alerts[x[0]]["rank"])
            days.append({
                "w": d["max_wind"] if d else None, "tmin": d["min_t"] if d else None,
                "tmax": d["max_t"] if d else None, "rain": d["rain"] if d else None,
                "snow": d["snow"] if d else 0, "thunder": d["thunder"] if d else 0,
                "cov": d["cov"] if d else 0, "sym": d["sym"] if d else None,
                "risk": risk, "why": why, "al": day_alerts,
                "rank": max([alerts[x[0]]["rank"] for x in day_alerts] or [0]),
            })
        city_out.append({
            "name": c["name"], "region": c["region"], "lat": c["lat"], "lon": c["lon"],
            "error": (f or {}).get("error") if f else "no data",
            "updated": (f or {}).get("updated", "") if f else "",
            "fetched": (f or {}).get("fetched") if f else None,
            "stale": bool(f and f.get("src") == "stale"),
            "days": days, "series": (f or {}).get("series") if f and not f.get("error") else None,
        })

    alert_out = []
    for ai, a in enumerate(alerts):
        alert_out.append({
            "id": a["id"], "name": a["name"], "event": a["event"], "level": a["level"], "rank": a["rank"],
            "start": a["start"], "end": a["end"], "area": a["area"], "title": a["title"],
            "description": a["description"], "consequences": a["consequences"],
            "instruction": a["instruction"], "web": a["web"], "source": a["source"],
            "time_unknown": a["time_unknown"], "cities": alert_cities[ai],
        })

    def count(rank, upto):
        return sum(1 for c in city_out if any(d["rank"] == rank for d in c["days"][:upto]))

    stats = {"h%d" % n: {"red": count(3, n), "orange": count(2, n), "yellow": count(1, n),
                         "risk_high": sum(1 for c in city_out if any(d["risk"] == 2 for d in c["days"][:n])),
                         "risk_elev": sum(1 for c in city_out if any(d["risk"] == 1 for d in c["days"][:n]))}
             for n in (1, 3, cfg["days"])}

    last_end = max([a["end"] for a in alerts if a["end"]] or [""])
    return {
        "meta": {
            "generated_epoch": int(now.timestamp()),
            "generated_local": to_oslo(now).strftime("%a %d %b %H:%M"),
            "horizon_days": cfg["days"], "demo": bool(notes.get("demo")),
            "stale_after_minutes": cfg["stale_after_minutes"], "fresh_minutes": cfg["fresh_minutes"],
            "status": {"forecast": forecast_status(forecasts), "warnings": notes.get("warn_status") or {
                "rss": {"state": "failed", "fetched": None, "items": 0, "error": "unknown"},
                "json": {"state": "failed", "fetched": None, "items": 0, "error": "unknown"}}},
            "thresholds": th, "stats": stats, "notes": notes.get("messages", []),
            "alerts_total_land": len(alerts), "last_alert_end": last_end,
            "alert_sources": sorted({a["source"] for a in alerts}),
            "include_marine": cfg["include_marine"],
        },
        "days": [dict(key=k, label=day_label(k), wd=day_label(k).split()[0],
                      start=day_bounds_epoch(k)[0], end=day_bounds_epoch(k)[1], today=(i == 0))
                 for i, k in enumerate(keys)],
        "cities": city_out, "alerts": alert_out,
        "regions": sorted({c["region"] for c in cities}),
    }


# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------

def write_outputs(model, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "data.json"), "w", encoding="utf-8") as f:
        json.dump(model, f, ensure_ascii=False, separators=(",", ":"))

    with open(os.path.join(HERE, "template.html"), encoding="utf-8") as f:
        tpl = f.read()
    blob = json.dumps(model, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    with open(os.path.join(out_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write(tpl.replace("__DATA__", blob))

    gen = datetime.fromtimestamp(model["meta"]["generated_epoch"], timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    alerts = model["alerts"]
    with open(os.path.join(out_dir, "daily.csv"), "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "weekday", "city", "region", "lat", "lon", "official_level", "official_warnings",
                    "max_wind_ms", "min_temp_c", "max_temp_c", "rain_mm", "snow_or_sleet", "thunder",
                    "forecast_risk", "risk_reasons", "coverage_hours", "forecast_updated", "forecast_fetched_utc", "generated_utc"])
        for c in model["cities"]:
            for dm, d in zip(model["days"], c["days"]):
                warn = "; ".join("%s %s (%s)" % (LEVEL_LABEL[alerts[ai]["rank"]], alerts[ai]["name"], wt)
                                 for ai, wt in d["al"])
                w.writerow([dm["key"], dm["wd"], c["name"], c["region"], c["lat"], c["lon"],
                            LEVEL_LABEL[d["rank"]], warn, d["w"], d["tmin"], d["tmax"], d["rain"],
                            d["snow"], d["thunder"], RISK_LABEL[d["risk"]] if d["w"] is not None else "",
                            "; ".join(x["txt"] for x in d["why"]), d["cov"], c["updated"],
                            (datetime.fromtimestamp(c["fetched"], timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if c.get("fetched") else ""), gen])
    with open(os.path.join(out_dir, "alerts.csv"), "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["level", "event", "area", "valid_from_utc", "valid_to_utc", "cities", "title", "source", "id"])
        for a in alerts:
            w.writerow([LEVEL_LABEL[a["rank"]], a["name"], a["area"], a["start"], a["end"],
                        ", ".join(model["cities"][i]["name"] for i in a["cities"]), a["title"], a["source"], a["id"]])
    with open(os.path.join(out_dir, "digest.txt"), "w", encoding="utf-8") as f:
        f.write(make_digest(model))
    level, why = assess_trust(model["meta"], time.time())
    with open(os.path.join(out_dir, "status.json"), "w", encoding="utf-8") as f:
        json.dump({"generated_utc": gen, "generated_epoch": model["meta"]["generated_epoch"],
                   "trust_at_write": TRUST_NAMES[level].lower(), "reasons": why,
                   "status": model["meta"]["status"], "demo": model["meta"]["demo"]}, f, indent=1)
    open(os.path.join(out_dir, ".nojekyll"), "w").close()


def make_digest(model):
    m, alerts, cities, days = model["meta"], model["alerts"], model["cities"], model["days"]
    L = ["Norway weather watch, updated %s (Oslo time)%s" % (m["generated_local"], "  [DEMO DATA]" if m["demo"] else "")]
    level, why = assess_trust(m, time.time())
    st = m["status"]
    f, w = st["forecast"], st["warnings"]
    L.append("DATA STATUS when written: %s" % TRUST_NAMES[level])
    L.append("- Forecasts: %d of %d cities OK, oldest data fetched %s; MET model run %s to %s UTC."
             % (f["n_ok"], f["n_total"], fmt_epoch(f["oldest_fetch"]),
                f["met_updated_oldest"][:16].replace("T", " "), f["met_updated_newest"][:16].replace("T", " ")))
    L.append("- Warnings: RSS %s (fetched %s), JSON %s (fetched %s)."
             % (w["rss"]["state"], fmt_epoch(w["rss"]["fetched"]), w["json"]["state"], fmt_epoch(w["json"]["fetched"])))
    for r in why:
        L.append("- PROBLEM: " + r)
    L.append("- If this text is much older than expected, the engine has stopped: do not rely on it.")
    L.append("")
    s = m["stats"]["h%d" % m["horizon_days"]]
    L.append("Cities with official warnings in the next %d days: red %d, orange %d, yellow %d."
             % (m["horizon_days"], s["red"], s["orange"], s["yellow"]))
    L.append("")
    hit = [a for a in alerts if a["cities"]]
    if hit:
        L.append("Official warnings covering your cities:")
        for a in hit:
            names = ", ".join(cities[i]["name"] for i in a["cities"])
            L.append("- %s %s (%s): %s to %s UTC. Cities: %s" % (
                LEVEL_LABEL[a["rank"]], a["name"], a["area"], a["start"][:16].replace("T", " "),
                a["end"][:16].replace("T", " "), names))
    else:
        L.append("No official land warnings cover your cities right now.")
    L.append("")
    L.append("Forecast-based flags (our rule, not official warnings):")
    any_flag = False
    for di, dm in enumerate(days):
        flagged = []
        for c in cities:
            d = c["days"][di]
            if d["risk"] > 0:
                flagged.append("%s (%s)" % (c["name"], ", ".join(x["k"] for x in d["why"])))
        if flagged:
            any_flag = True
            L.append("- %s: %s" % (dm["label"], "; ".join(flagged)))
    if not any_flag:
        L.append("- none")
    return "\n".join(L) + "\n"


def notify_slack(model, cache_dir, webhook):
    path = os.path.join(cache_dir, "notified.json")
    try:
        with open(path, encoding="utf-8") as f:
            seen = json.load(f)
    except (OSError, ValueError):
        seen = {}
    now = time.time()
    seen = {k: v for k, v in seen.items() if v > now}
    lines = []
    for a in model["alerts"]:
        if a["rank"] >= 2 and a["cities"] and a["id"] not in seen:
            names = ", ".join(model["cities"][i]["name"] for i in a["cities"])
            lines.append("%s %s (%s), %s to %s UTC. Cities: %s" % (
                LEVEL_LABEL[a["rank"]].upper(), a["name"], a["area"], a["start"][:16], a["end"][:16], names))
            seen[a["id"]] = (parse_iso(a["end"]).timestamp() if a["end"] else now) + 3600
    if lines:
        req = urllib.request.Request(webhook, data=json.dumps({"text": "New MET warnings:\n" + "\n".join(lines)}).encode(),
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=20).read()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(seen, f)


# ---------------------------------------------------------------------------
# Live run and demo run
# ---------------------------------------------------------------------------

def run_live(cfg, cities, cache_dir, now):
    if not cfg["user_agent"] or "example" in cfg["user_agent"].lower():
        raise SystemExit("Set user_agent in config.json (or env WEATHER_USER_AGENT), e.g. "
                         "'NorwayWeatherWatch/1.0 you@yourmail.com'. MET blocks anonymous clients.")
    fx = Fetcher(cache_dir, cfg["user_agent"], cfg["timeout_s"])
    messages = []

    # Fail fast and loudly if MET cannot be reached at all (no internet, VPN, firewall, DNS).
    print("Contacting api.met.no ...", flush=True)
    try:
        fx.get(ALERTS_RSS)
    except FetchError as e:
        if "403" in str(e):
            raise SystemExit(str(e))
        raise SystemExit("Cannot reach api.met.no: %s\nCheck internet, VPN or firewall. Nothing was written; "
                         "the old site folder is unchanged." % e)
    print("Fetching forecasts for %d cities ..." % len(cities), flush=True)

    def one(c):
        url = "%s?lat=%.4f&lon=%.4f" % (FORECAST_URL, c["lat"], c["lon"])
        try:
            raw, src, fetched = fx.get(url)
            r = summarize_forecast(json.loads(raw), now)
            r["fetched"], r["src"] = fetched, src
            if src == "stale":
                messages.append("Forecast for %s is from cache (MET unreachable)." % c["name"])
            return r
        except FetchError as e:
            if "403" in str(e):
                raise
            return {"error": str(e)[:120]}
        except Exception as e:  # keep one bad city from killing the run
            return {"error": "%s: %s" % (type(e).__name__, e)}

    with ThreadPoolExecutor(max_workers=cfg["workers"]) as ex:
        forecasts = list(ex.map(one, cities))
    ok = sum(1 for f in forecasts if not f.get("error"))
    if ok == 0:
        raise SystemExit("No forecasts could be fetched. First error: %s" % forecasts[0].get("error"))
    if ok < len(cities):
        messages.append("%d of %d cities have no forecast (see data.json)." % (len(cities) - ok, len(cities)))

    rss_items, json_by_id = [], {}
    wstat = {"rss": {"state": "failed", "fetched": None, "items": 0, "error": ""},
             "json": {"state": "failed", "fetched": None, "items": 0, "error": ""}}
    try:
        raw, src, fetched = fx.get(ALERTS_RSS)
        rss_items = parse_rss(raw)
        wstat["rss"].update(state="stale" if src == "stale" else "ok", fetched=fetched, items=len(rss_items))
        if src == "stale":
            messages.append("Warnings RSS is from cache (MET unreachable).")
    except Exception as e:
        wstat["rss"]["error"] = str(e)[:100]
        messages.append("Warnings RSS failed: %s" % str(e)[:100])
    try:
        raw, src, fetched = fx.get(ALERTS_JSON)
        json_by_id = parse_json_alerts(raw)
        wstat["json"].update(state="stale" if src == "stale" else "ok", fetched=fetched, items=len(json_by_id))
    except Exception as e:
        wstat["json"]["error"] = str(e)[:100]
        messages.append("Warnings JSON failed: %s" % str(e)[:100])
    if wstat["rss"]["state"] == "failed" and wstat["json"]["state"] == "failed":
        messages.append("NO WARNING DATA: absence of warnings below does NOT mean no warnings exist.")
    alerts = build_alerts(rss_items, json_by_id, cfg, now)
    return forecasts, alerts, {"messages": messages, "warn_status": wstat}


def run_demo(cfg, cities, now):
    import demo_data
    fcs = [summarize_forecast(demo_data.fake_forecast(c, now), now) for c in cities]
    for f in fcs:
        f["fetched"], f["src"] = now.timestamp(), "network"
    rss_raw, json_raw = demo_data.fake_alert_feeds(now)
    alerts = build_alerts(parse_rss(rss_raw), parse_json_alerts(json_raw), cfg, now)
    ws = {"rss": {"state": "ok", "fetched": now.timestamp(), "items": 4, "error": ""},
          "json": {"state": "ok", "fetched": now.timestamp(), "items": 6, "error": ""}}
    return fcs, alerts, {"demo": True, "warn_status": ws, "messages": [
        "DEMO MODE: synthetic data for previewing the layout. This is NOT real weather."]}


def build(args):
    cfg = load_config(args.config)
    if args.days:
        cfg["days"] = args.days
    cities = load_cities()
    now = datetime.now(timezone.utc)
    cache_dir = os.path.join(HERE, ".cache")
    if args.demo:
        forecasts, alerts, notes = run_demo(cfg, cities, now)
    else:
        forecasts, alerts, notes = run_live(cfg, cities, cache_dir, now)
    model = build_model(cities, forecasts, alerts, cfg, now, notes)
    out = args.out if os.path.isabs(args.out) else os.path.join(HERE, args.out)
    write_outputs(model, out)
    hook = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
    if hook and not args.demo:
        try:
            notify_slack(model, cache_dir, hook)
        except Exception as e:
            print("Slack notification failed:", e, file=sys.stderr)
    s = model["meta"]["stats"]["h%d" % cfg["days"]]
    print("Wrote %s  (cities with official warnings: red %d, orange %d, yellow %d; %d land alerts)"
          % (out, s["red"], s["orange"], s["yellow"], len(alerts)))
    for msg in notes.get("messages", []):
        print("NOTE:", msg)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config")
    ap.add_argument("--out", default="site")
    ap.add_argument("--days", type=int)
    ap.add_argument("--demo", action="store_true", help="synthetic data, for previewing the layout offline")
    ap.add_argument("--serve", action="store_true", help="after building, serve the folder on localhost:8000")
    ap.add_argument("--watch", type=int, metavar="MIN", help="rebuild every MIN minutes (min 10)")
    args = ap.parse_args()

    out = build(args)
    if args.watch:
        mins = max(10, args.watch)
        if args.serve:
            import threading
            threading.Thread(target=serve, args=(out,), daemon=True).start()
        while True:
            time.sleep(mins * 60)
            try:
                build(args)
            except SystemExit as e:
                print("Run failed:", e, file=sys.stderr)
    elif args.serve:
        serve(out)


def serve(directory, port=8000):
    handler = lambda *a, **k: http.server.SimpleHTTPRequestHandler(*a, directory=directory, **k)
    with socketserver.TCPServer(("", port), handler) as httpd:
        print("Serving %s on http://localhost:%d  (Ctrl+C to stop)" % (directory, port))
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
