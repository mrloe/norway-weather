"""Synthetic data in MET's real formats. Used only by --demo and by the tests.
Nothing here is real weather."""
import json
import math
import zlib
from datetime import datetime, timedelta, timezone


def _iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fake_forecast(city, now):
    """Locationforecast/2.0/compact-shaped JSON: hourly for 60 h, then 6-hourly to ~9 days."""
    seed = zlib.crc32(city["name"].encode()) % 1000 / 1000.0
    west = city["lon"] < 8.0
    north = city["lat"] > 64
    start = now.replace(minute=0, second=0, microsecond=0)
    steps, t = [], start
    while t < start + timedelta(hours=60):
        steps.append(t)
        t += timedelta(hours=1)
    while t < start + timedelta(hours=9 * 24 + 6):
        steps.append(t)
        t += timedelta(hours=6)

    def at(t):
        h = (t - start).total_seconds() / 3600
        base_t = 6 - (city["lat"] - 59) * 0.7 - (h / 24) * 0.25 + 3 * math.sin((h - 9) / 24 * 2 * math.pi)
        wind = 4 + 3 * seed + 2.5 * math.sin(h / 17 + seed * 6)
        rain = max(0.0, math.sin(h / 11 + seed * 9) - 0.55) * 2.2
        if west and 50 < h < 80:            # a front hits the west coast
            wind += 11 * math.exp(-((h - 64) / 9) ** 2)
            rain += 5 * math.exp(-((h - 68) / 8) ** 2)
        if north and 90 < h < 130:
            base_t -= 14
            wind += 4
        return round(base_t, 1), round(max(wind, 0.4), 1), rain

    ts = []
    for i, t in enumerate(steps):
        temp, wind, rain = at(t)
        gap = (steps[i + 1] - t).total_seconds() / 3600 if i + 1 < len(steps) else None
        sym = "snow" if temp < 0 and rain > 0.3 else ("rain" if rain > 0.3 else "partlycloudy_day")
        data = {"instant": {"details": {"air_temperature": temp, "wind_speed": wind}}}
        if gap == 1:
            data["next_1_hours"] = {"summary": {"symbol_code": sym}, "details": {"precipitation_amount": round(rain, 1)}}
        if gap is not None and gap <= 1:
            if i + 6 < len(steps):
                data["next_6_hours"] = {"summary": {"symbol_code": sym}, "details": {
                    "precipitation_amount": round(rain * 6, 1), "air_temperature_max": temp + 1, "air_temperature_min": temp - 1}}
        elif gap == 6:
            data["next_6_hours"] = {"summary": {"symbol_code": sym}, "details": {
                "precipitation_amount": round(rain * 6, 1), "air_temperature_max": temp + 1, "air_temperature_min": temp - 1}}
        ts.append({"time": _iso(t), "data": data})
    return {"type": "Feature", "properties": {"meta": {"updated_at": _iso(now)}, "timeseries": ts}}


def _poly(coords):
    """coords as (lat, lon) pairs -> georss text; closed ring."""
    pts = list(coords) + [coords[0]]
    return " ".join("%s %s" % (a, b) for a, b in pts)


def fake_alert_feeds(now):
    """Returns (rss_bytes, json_bytes) shaped like MetAlerts 2.0 current.rss / current.json.
    One alert exists only in the JSON, to exercise the merge path."""
    d = lambda h: _iso(now + timedelta(hours=h))
    ids = {k: "2.49.0.1.578.0.2099010100%02d00.0%02d" % (i, i) for i, k in
           enumerate(["snow", "wind", "rain", "mountain", "kuling", "jsononly"])}
    # polygons as (lat, lon)
    east = [(59.0, 9.5), (59.0, 11.6), (60.4, 11.6), (60.4, 9.5)]
    west = [(58.6, 4.8), (58.6, 6.2), (60.7, 6.2), (60.7, 4.8)]
    west_hole = [(60.30, 5.20), (60.30, 5.45), (60.45, 5.45), (60.45, 5.20)]  # Bergen area cut out of the "rain" alert
    mountain = [(60.5, 7.0), (60.5, 9.0), (61.5, 9.0), (61.5, 7.0)]
    sea = [(58.0, 3.0), (58.0, 4.5), (61.0, 4.5), (61.0, 3.0)]
    north = [(66.8, 13.5), (66.8, 15.0), (68.0, 15.0), (68.0, 13.5)]  # Bodoe

    items = [
        ("Snø", "gult", "Akershus, Oslo, Østfold, Vestfold og nedre Buskerud", d(-3), d(20), ids["snow"], [east]),
        ("Vind", "oransje", "Rogaland og Hordaland", d(52), d(74), ids["wind"], [west]),
        ("Kraftig snøfokk", "gult", "Deler av fjellet i Sør-Norge", d(60), d(84), ids["mountain"], [mountain]),
        ("Kuling", "gult", "Karmøy - Fedje", d(1), d(30), ids["kuling"], [sea]),
    ]
    rss = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<rss version="2.0" xmlns:georss="http://www.georss.org/georss"><channel><title>MET farevarsel</title>']
    for ev, col, area, s, e, gid, polys in items:
        rss.append("<item><title>%s, %s nivå, %s, %s, %s</title><description>Demo</description>"
                   "<link>https://api.met.no/weatherapi/metalerts/2.0/current?cap=%s</link><guid>%s</guid>"
                   "<pubDate>Thu, 12 Nov 2026 12:00:00 +0000</pubDate>%s</item>" % (
                       ev, col, area, s, e, gid, gid,
                       "".join("<georss:polygon>%s</georss:polygon>" % _poly(p) for p in polys)))
    rss.append("</channel></rss>")

    def feat(gid, name, event, level, domain, area, s, e, rings_latlon, hole=None):
        ring = [[lon, lat] for lat, lon in rings_latlon] + [[rings_latlon[0][1], rings_latlon[0][0]]]
        coords = [ring]
        if hole:
            coords.append([[lon, lat] for lat, lon in hole] + [[hole[0][1], hole[0][0]]])
        return {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": coords},
                "when": {"interval": [s, e]},
                "properties": {"id": gid, "status": "Actual", "eventAwarenessName": name, "event": event,
                               "awareness_level": "%d; %s; Moderate" % ({"yellow": 2, "orange": 3, "red": 4}[level], level),
                               "geographicDomain": domain, "area": area, "title": "%s, %s" % (name, area),
                               "consequences": "Demo: kan gi vanskelige kjøreforhold.", "instruction": "Demo: følg med.",
                               "web": "https://www.varsom.no"}}

    feats = [
        feat(ids["snow"], "Snø", "snow", "yellow", "land", items[0][2], d(-3), d(20), east),
        feat(ids["wind"], "Sterk vind", "wind", "orange", "land", items[1][2], d(52), d(74), west),
        feat(ids["mountain"], "Kraftig snøfokk", "blowingSnow", "yellow", "land", items[2][2], d(60), d(84), mountain),
        feat(ids["kuling"], "Kuling", "gale", "yellow", "marine", items[3][2], d(1), d(30), sea),
        # only in JSON (RSS "missed" it), with a hole so the point-in-polygon hole path is exercised
        feat(ids["jsononly"], "Styrtregn", "rain", "yellow", "land", "Vestlandet utenom Bergen", d(70), d(90), west, hole=west_hole),
        feat("2.49.0.1.578.0.2099010100.099", "Regn", "rain", "red", "land", "Nordland (demo)", d(30), d(40), north),
    ]
    return "\n".join(rss).encode("utf-8"), json.dumps({"type": "FeatureCollection", "features": feats}).encode("utf-8")
