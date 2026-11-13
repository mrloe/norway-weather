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


def candidates(name):
    req = urllib.request.Request(URL % urllib.parse.quote(name), headers={"User-Agent": "NorwayWeatherCoordCheck/1.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.load(r)
    out = []
    for n in data.get("navn", []):
        typ = str(n.get("navneobjekttype", "")).lower()
        pt = n.get("representasjonspunkt") or {}
        if "nord" not in pt or "øst" not in pt:
            continue
        names = [s.get("skrivemåte", "") for s in n.get("stedsnavn", [])]
        if not any(x.lower() == name.lower() for x in names):
            continue
        out.append((typ, pt["nord"], pt["øst"], ", ".join(k.get("kommunenavn", "") for k in n.get("kommuner", []))))
    return out


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    cities = json.load(open(os.path.join(here, "..", "cities.json"), encoding="utf-8"))
    flagged = 0
    print("%-14s %-9s %-24s %s" % ("city", "dist km", "nearest official place", "official point"))
    for c in cities:
        try:
            cand = [x for x in candidates(c["name"]) if x[0] in TOWNISH] or candidates(c["name"])
        except Exception as e:  # keep going, report per city
            print("%-14s ERROR %s" % (c["name"], e))
            continue
        if not cand:
            print("%-14s no match in register" % c["name"])
            continue
        best = min(cand, key=lambda x: km(c["lat"], c["lon"], x[1], x[2]))
        dist = km(c["lat"], c["lon"], best[1], best[2])
        flag = "  <-- CHECK" if dist > THRESHOLD_KM else ""
        flagged += bool(flag)
        print("%-14s %-9.2f %-24s %.4f, %.4f%s" % (c["name"], dist, ("%s (%s)" % (best[0], best[3]))[:24], best[1], best[2], flag))
    print("\n%d of %d cities more than %.0f km from the register point." % (flagged, len(cities), THRESHOLD_KM))
    return 0


if __name__ == "__main__":
    sys.exit(main())
