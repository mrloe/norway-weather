#!/usr/bin/env python3
"""One-off sanity check: compare each city's point in cities.json with Kartverket's
official place-name register (ws.geonorge.no, open data). Prints a table and flags
cities whose point is more than THRESHOLD_KM from the nearest town-like place of the
same name. It only reports; it never edits cities.json.

Run:  python tools/check_coords.py
"""
import json
import math
import os
import sys
import urllib.parse
import urllib.request

THRESHOLD_KM = 2.0
URL = "https://ws.geonorge.no/stedsnavn/v1/navn?sok=%s&fuzzy=false&treffPerSide=50&utkoordsys=4326"
TOWNISH = ("by", "tettsted", "tettsteddel", "tettbebyggelse", "bydel", "kommune", "tettsted/by")


def km(a, b, c, d):
    r = 6371.0
    p1, p2 = math.radians(a), math.radians(c)
    dp, dl = p2 - p1, math.radians(d - b)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


DEBUG_SHOWN = [False]


def fetch(name):
    req = urllib.request.Request(URL % urllib.parse.quote(name), headers={"User-Agent": "NorwayWeatherCoordCheck/1.0"})
    last = None
    for _ in range(3):
        try:
            with urllib.request.urlopen(req, timeout=40) as r:
                return json.load(r)
        except Exception as e:
            last = e
    raise last


def candidates(name):
    data = fetch(name)
    if not DEBUG_SHOWN[0]:
        DEBUG_SHOWN[0] = True
        print("DEBUG top-level keys:", list(data.keys()))
        print("DEBUG first record:", json.dumps((data.get("navn") or [None])[0], ensure_ascii=False)[:900])
    out = []
    for n in data.get("navn", []):
        typ = str(n.get("navneobjekttype", "")).lower()
        pt = n.get("representasjonspunkt") or {}
        if "nord" not in pt or "øst" not in pt:
            continue
        names = [s.get("skrivemåte", "") for s in n.get("stedsnavn", [])] + [str(n.get("skrivemåte", ""))]
        if not any(x.lower() == name.lower() for x in names):
            continue
        out.append((typ, pt["nord"], pt["øst"], ", ".join(k.get("kommunenavn", "") for k in n.get("kommuner", []))))
    return out


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    cities = json.load(open(os.path.join(here, "..", "cities.json"), encoding="utf-8"))
    flagged = 0
    matched = 0
    print("%-14s %-9s %-24s %s" % ("city", "dist km", "nearest official place", "official point"))
    for c in cities:
        try:
            allc = candidates(c["name"])
            cand = [x for x in allc if x[0] in TOWNISH] or allc
        except Exception as e:  # keep going, report per city
            print("%-14s ERROR %s" % (c["name"], e))
            continue
        if not cand:
            print("%-14s no match in register" % c["name"])
            continue
        matched += 1
        best = min(cand, key=lambda x: km(c["lat"], c["lon"], x[1], x[2]))
        dist = km(c["lat"], c["lon"], best[1], best[2])
        flag = "  <-- CHECK" if dist > THRESHOLD_KM else ""
        flagged += bool(flag)
        print("%-14s %-9.2f %-24s %.4f, %.4f%s" % (c["name"], dist, ("%s (%s)" % (best[0], best[3]))[:24], best[1], best[2], flag))
    print("\nMatched %d of %d cities; %d of the matched are more than %.0f km from the register point." % (matched, len(cities), flagged, THRESHOLD_KM))
    return 0 if matched else 1


if __name__ == "__main__":
    sys.exit(main())
