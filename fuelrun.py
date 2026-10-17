#!/usr/bin/env python3
"""fuelrun: will I make it, and will it be open when I get there?

Answers the question a sat nav doesn't. Routes A to B, works out where you run
dry, finds the fuel stops that are reachable BEFORE that, and checks whether
each one is actually open at the time you'd arrive.

Data: OSRM for routing, OpenStreetMap via Overpass for fuel stations and their
opening hours, Nominatim for place names. All free, no keys.

Usage:
  python3 fuelrun.py --from Inverness --to Thurso --tank 55 --mpg 42 --fuel 30
  python3 fuelrun.py --from "Inverness" --to "Thurso" --depart 2026-10-18T17:30
"""
#
#  fuelrun. Copyright (C) 2026 Erika Burnett.
#
#  This program is free software: you can redistribute it and/or modify it
#  under the terms of the GNU Affero General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or (at your
#  option) any later version.
#
#  This program is distributed in the hope that it will be useful, but
#  WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY
#  or FITNESS FOR A PARTICULAR PURPOSE. See the GNU Affero General Public
#  License for more details: https://www.gnu.org/licenses/
#

import argparse, json, math, re, subprocess, sys, time, urllib.parse
from datetime import datetime, timedelta
from pathlib import Path

UA = "fuelrun/0.1 (https://github.com/ ; prototype)"
# some public endpoints reject anything that doesn't look like a browser
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
CACHE = Path(__file__).parent / "cache"
CACHE.mkdir(exist_ok=True)

LITRES_PER_GALLON = {"uk": 4.54609, "us": 3.785411784}
MILES_PER_METRE = 1 / 1609.344


# ----------------------------------------------------------------- http

def get(url, tries=4, pause=3):
    """GET with a user agent and exponential backoff.

    Shells out to curl. The system Python on this Mac throws an SSLV3 handshake
    failure against router.project-osrm.org while curl connects to it happily,
    so curl it is.
    """
    last = None
    for n in range(tries):
        try:
            out = subprocess.run(
                ["curl", "-sL", "--max-time", "70", "-A", UA, url],
                capture_output=True, text=True, timeout=80)
            body = out.stdout
            if body.lstrip().startswith(("{", "[")):
                return json.loads(body)
            last = (body or out.stderr)[:200] or "empty response"
        except Exception as e:
            last = str(e)
        if n < tries - 1:
            wait = pause * (2 ** n)
            print(f"  retrying in {wait}s ({last[:60].strip()})", file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError(f"gave up after {tries} tries: {last}")


# ----------------------------------------------------------------- geo

OSRM_BASE = "https://router.project-osrm.org"   # overridden by --osrm
LEAKED = []                                     # calls that revealed a destination


def local_postcodes():
    """OS Code-Point Open, if it's been downloaded. Fully offline."""
    f = CACHE / "postcodes.csv"
    if not f.exists():
        return None
    idx = {}
    for line in f.read_text(errors="replace").splitlines():
        parts = line.split(",")
        if len(parts) >= 9 and parts[1].strip('"') == "live":
            try:
                idx[parts[0].strip('"').replace(" ", "").upper()] = (float(parts[7]), float(parts[8]))
            except (ValueError, IndexError):
                pass
    return idx


_PC_CACHE = None
POSTCODE_RE = re.compile(r"^[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}$", re.I)


def resolve(place, offline=False):
    """Turn user input into coordinates, leaking as little as possible.

    Order of preference:
      1. 'lat,lon' typed directly   -> no network at all
      2. UK postcode, local file    -> no network at all
      3. UK postcode, postcodes.io  -> sends a postcode
      4. place name, Nominatim      -> sends a place name
    """
    global _PC_CACHE
    s = place.strip()

    m = re.match(r"^\s*(-?\d+\.?\d*)\s*,\s*(-?\d+\.?\d*)\s*$", s)
    if m:
        return {"name": s, "lat": float(m.group(1)), "lon": float(m.group(2)), "how": "typed"}

    if POSTCODE_RE.match(s):
        if _PC_CACHE is None:
            _PC_CACHE = local_postcodes() or {}
        hit = _PC_CACHE.get(s.replace(" ", "").upper())
        if hit:
            return {"name": s.upper(), "lat": hit[0], "lon": hit[1], "how": "offline postcode file"}
        if offline:
            raise SystemExit(f"'{s}' not in the local postcode file, and --offline is set.")
        res = get(f"https://api.postcodes.io/postcodes/{urllib.parse.quote(s)}")
        r = res.get("result") or (res.get("terminated") if res.get("status") == 404 else None)
        if r:
            LEAKED.append(f"postcodes.io  <- {s.upper()}")
            return {"name": s.upper(), "lat": float(r["latitude"]), "lon": float(r["longitude"]),
                    "how": "postcodes.io"}

    if offline:
        raise SystemExit(f"'{s}' needs a geocoder and --offline is set. Use a postcode or lat,lon.")

    key = CACHE / f"geo_{re.sub(r'[^a-z0-9]+', '_', s.lower())}.json"
    if key.exists():
        return {**json.loads(key.read_text()), "how": "cached"}
    q = urllib.parse.urlencode({"q": s, "format": "json", "limit": 1})
    res = get(f"https://nominatim.openstreetmap.org/search?{q}")
    if not res:
        raise SystemExit(f"Couldn't find '{s}' on the map.")
    out = {"name": res[0]["display_name"], "lat": float(res[0]["lat"]), "lon": float(res[0]["lon"])}
    key.write_text(json.dumps(out))
    LEAKED.append(f"Nominatim     <- \"{s}\"")
    time.sleep(1)
    return {**out, "how": "Nominatim"}


def route(a, b, offline=False):
    ck = CACHE / f"route_{a['lat']:.4f}_{a['lon']:.4f}_{b['lat']:.4f}_{b['lon']:.4f}.json"
    if ck.exists():
        d = json.loads(ck.read_text())
        d["roads"] = set(d["roads"])
        d["cached"] = True
        return d
    if offline and "localhost" not in OSRM_BASE and "127.0.0.1" not in OSRM_BASE:
        raise SystemExit("Route not cached and --offline is set. Run once online, or use --osrm.")
    url = (f"{OSRM_BASE}/route/v1/driving/"
           f"{a['lon']},{a['lat']};{b['lon']},{b['lat']}"
           f"?overview=full&geometries=geojson&steps=true")
    res = get(url)
    if res.get("code") != "Ok":
        raise SystemExit(f"Routing failed: {res.get('message', res.get('code'))}")
    r = res["routes"][0]
    coords = [(c[1], c[0]) for c in r["geometry"]["coordinates"]]  # to (lat, lon)
    roads = set()
    for leg in r.get("legs", []):
        for step in leg.get("steps", []):
            for ref in (step.get("ref") or "").split(";"):
                ref = ref.strip().upper()
                if re.fullmatch(r"[AMB]\d{1,4}\(?M?\)?", ref):
                    roads.add(ref)
    idx = {}
    for leg in r.get("legs", []):
        for st in leg.get("steps", []):
            geo = (st.get("geometry") or {}).get("coordinates") or []
            pts = [[round(c[1], 5), round(c[0], 5)] for c in geo[::3]]
            if not pts:
                continue
            for ref in (st.get("ref") or "").split(";"):
                ref = ref.strip().upper()
                if re.fullmatch(r"[AMB]\d{1,4}\(?M?\)?", ref):
                    idx.setdefault(ref, []).extend(pts)
    out = {"miles": r["distance"] * MILES_PER_METRE,
           "hours": r["duration"] / 3600,
           "coords": coords, "roads": roads, "road_points": idx}
    ck.write_text(json.dumps({**out, "roads": sorted(roads)}))
    if "localhost" not in OSRM_BASE and "127.0.0.1" not in OSRM_BASE:
        LEAKED.append(f"OSRM          <- {a['lat']:.3f},{a['lon']:.3f} to {b['lat']:.3f},{b['lon']:.3f}")
    return out


def haversine_miles(p, q):
    R = 3958.8
    dlat, dlon = math.radians(q[0] - p[0]), math.radians(q[1] - p[1])
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(p[0])) * math.cos(math.radians(q[0])) * math.sin(dlon / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(a))


def cumulative_miles(coords):
    out, run = [0.0], 0.0
    for i in range(1, len(coords)):
        run += haversine_miles(coords[i - 1], coords[i])
        out.append(run)
    return out


# ----------------------------------------------------------------- fuel stops

def fuel_near_route(coords, radius_m=3000):
    """Overpass 'around' query along a thinned version of the route polyline."""
    step = max(1, len(coords) // 90)
    thin = coords[::step]
    poly = ",".join(f"{lat:.5f},{lon:.5f}" for lat, lon in thin)
    cache_key = CACHE / f"fuel_{abs(hash(poly)) % (10 ** 12)}.json"
    if cache_key.exists():
        print("  (fuel data from local cache)", file=sys.stderr)
        return json.loads(cache_key.read_text())

    q = (f'[out:json][timeout:60];'
         f'(node["amenity"="fuel"](around:{radius_m},{poly});'
         f' way["amenity"="fuel"](around:{radius_m},{poly}););'
         f'out center tags;')
    url = "https://overpass-api.de/api/interpreter?" + urllib.parse.urlencode({"data": q})
    res = get(url, tries=5, pause=5)

    stations = []
    for e in res.get("elements", []):
        t = e.get("tags", {})
        lat = e.get("lat") or (e.get("center") or {}).get("lat")
        lon = e.get("lon") or (e.get("center") or {}).get("lon")
        if lat is None:
            continue
        stations.append({
            "name": t.get("name") or t.get("brand") or "Unnamed fuel station",
            "brand": t.get("brand", ""),
            "hours": t.get("opening_hours"),
            "lat": lat, "lon": lon,
        })
    cache_key.write_text(json.dumps(stations))
    return stations


# ----------------------------------------------------------------- locating a closure

def route_points_on(road, steps_index):
    """Every coordinate on our route that belongs to a given road number."""
    return steps_index.get(road.upper(), [])


def build_steps_index(r):
    """road number -> the coordinates of our route that run along it."""
    return {k: [tuple(p) for p in v] for k, v in (r.get("road_points") or {}).items()}



def junction_index(route_coords, road_points):
    """(road, junction number) -> coordinates, from OpenStreetMap.

    English closures are almost always written as "M6 J12" or "M6 between
    J37 and J38". OSM tags motorway junctions with their number, so one
    query along the route turns those into places on the map.
    """
    key = CACHE / f"junc_{abs(hash(str(route_coords[::40]))) % (10**12)}.json"
    if key.exists():
        return {tuple(k.split("|")): tuple(v) for k, v in json.loads(key.read_text()).items()}

    step = max(1, len(route_coords) // 70)
    poly = ",".join(f"{a:.4f},{b:.4f}" for a, b in route_coords[::step])
    q = (f'[out:json][timeout:60];node["highway"="motorway_junction"]'
         f'["ref"](around:2500,{poly});out body;')
    try:
        res = get("https://overpass-api.de/api/interpreter?"
                  + urllib.parse.urlencode({"data": q}), tries=3, pause=4)
    except Exception:
        return {}

    idx = {}
    for e in res.get("elements", []):
        ref = (e.get("tags", {}).get("ref") or "").strip().upper()
        if not ref:
            continue
        here = (e["lat"], e["lon"])
        # which of our roads does this junction sit on?
        best_road, best_d = None, 1.2
        for road, pts in road_points.items():
            d = min((haversine_miles(here, pt) for pt in pts[::4]), default=99)
            if d < best_d:
                best_road, best_d = road, d
        if best_road:
            idx[(best_road, ref)] = here
    key.write_text(json.dumps({f"{k[0]}|{k[1]}": list(v) for k, v in idx.items()}))
    return idx


def locate_closure(inc, steps_index, route_coords, junctions=None):
    """Work out where on our route a closure actually bites.

    Feeds rarely say. Tried in order, best first:
      1. coordinates in the feed
      2. junction references, e.g. "between A47 and A605", matched against
         the points where our own route touches those roads
      3. a place name, geocoded and then snapped to the route
    Returns (lat, lon, how) or None, and None is reported honestly.
    """
    if inc.get("lat") is not None and inc.get("lon") is not None:
        return inc["lat"], inc["lon"], "feed gave coordinates"

    text = f"{inc.get('title','')} {inc.get('desc','')}"
    mine = {r for r in inc.get("roads", set()) if r in steps_index}
    if not mine:
        return None
    road = sorted(mine)[0]

    # 1b. junction numbers, which is how England writes nearly everything
    if junctions:
        js = re.findall(r"\b(?:J|JUNCTION\s*)(\d{1,2}[A-Z]?)\b", text.upper())
        pts = [junctions[(road, j)] for j in js if (road, j) in junctions]
        if pts:
            lat = sum(q[0] for q in pts) / len(pts)
            lon = sum(q[1] for q in pts) / len(pts)
            label = "J" + ", J".join(js[:2])
            return lat, lon, f"{road} at {label}"

    # 2. junctions: the closure names other roads, and our route meets them
    others = {x.upper() for x in re.findall(r"\b([AMB]\d{1,4}\(?M?\)?)\b", text.upper())}
    others -= inc.get("roads", set())
    anchors = []
    for o in others:
        if o in steps_index:
            here = set(map(tuple, steps_index[o]))
            for pt in steps_index[next(iter(mine))]:
                if any(haversine_miles(pt, h) < 0.6 for h in list(here)[::9]):
                    anchors.append(pt)
                    break
    if anchors:
        lat = sum(a[0] for a in anchors) / len(anchors)
        lon = sum(a[1] for a in anchors) / len(anchors)
        return lat, lon, f"where your route meets {', '.join(sorted(others))[:24]}"

    # 3. a place name, then snap it to the nearest point of our road
    place = re.sub(r"\b[AMB]\d{1,4}\(?M?\)?\b", " ", inc.get("title", ""))
    place = re.sub(r"\b(J\d+\w*|Junction|northbound|southbound|eastbound|westbound|"
                   r"slip|on|off|exit|access|between|and|to|the)\b", " ", place, flags=re.I)
    place = re.sub(r"[^\w\s'-]", " ", place)
    place = re.sub(r"\s+", " ", place).strip()
    if len(place) < 4:
        return None
    try:
        hit = resolve(place + ", UK")
    except SystemExit:
        return None
    except Exception:
        return None
    pts = steps_index[next(iter(mine))]
    best = min(pts, key=lambda c: haversine_miles((hit["lat"], hit["lon"]), c))
    if haversine_miles((hit["lat"], hit["lon"]), best) > 12:
        return None
    return best[0], best[1], f"geocoded '{place[:22]}' onto your route"



# ----------------------------------------------------------------- closures: pluggable by country

# Ordered, most specific first. A lane closure is a delay; a road closure is a
# wall. Matching "closure" loosely called 145 Northern Irish lane closures
# blockages, which would have sent people on diversions they did not need.
SEVERITY = [
    ("lane closure", 2), ("footway closure", 3), ("lanes closed", 2),
    ("road closure", 0), ("roadclosed", 0), ("carriagewayclosed", 0),
    ("closure", 0), ("closed", 0),
    ("accident", 1), ("collision", 1), ("obstruction", 1), ("vehicle fire", 1),
    ("roadworks", 1), ("maintenanceworks", 1), ("maintenance", 1), ("repair", 1),
    ("contraflow", 2), ("hazard", 2), ("authorityoperation", 2), ("weather", 2),
    ("abnormaltraffic", 3), ("queue", 3), ("congestion", 3), ("delay", 3),
]


def _rank(kind):
    k = re.sub(r"[^a-z ]", "", (kind or "").lower())
    for word, n in SEVERITY:
        if word in k:
            return n
    return 4


def _norm(source, title, kind, direction="", desc="", since="", roads=None, lat=None, lon=None):
    """One shape for every provider, whatever the country."""
    if roads is None:
        roads = set(re.findall(r"\b([AMBNERT]\d{1,4}\(?M?\)?)\b", (title or "").upper()))
    roads = {re.sub(r"^([A-Z]+)0+(\d)", r"\1\2", r) for r in (roads or set())}
    tidy = lambda s: re.sub(r"\s+", " ", (s or "")).strip()
    return {"source": source, "title": tidy(title), "type": tidy(kind),
            "direction": tidy(direction), "desc": tidy(desc), "start": tidy(since),
            "roads": roads, "lat": lat, "lon": lon, "rank": _rank(kind)}


# --- provider: Traffic Scotland (scraped; their DATEX II feed needs approval)

def p_traffic_scotland():
    out = subprocess.run(["curl", "-sL", "--max-time", "40", "-A", UA,
                          "https://www.traffic.gov.scot/traffic-information/incidents"],
                         capture_output=True, text=True, timeout=60)
    html = out.stdout
    if 'class="item"' not in html:
        raise RuntimeError("Traffic Scotland page returned nothing usable")
    items = []
    for block in re.split(r'<div class="item">', html)[1:]:
        h = re.search(r'<h2 class="qsf">(.*?)</h2>', block, re.S)
        if not h:
            continue
        clean = lambda s: re.sub(r"<[^>]+>", "", s).replace("&amp;", "&").strip()
        title = clean(h.group(1))
        def fld(label):
            m = re.search(rf'<span class="label">{label}:</span>(.*?)</p>', block, re.S)
            return clean(m.group(1)) if m else ""
        paras = re.findall(r"<p>(?!<span)(.*?)</p>", block, re.S)
        items.append(_norm("Traffic Scotland", title, fld("Incident type"),
                           fld("Direction"), clean(paras[0]) if paras else "",
                           fld("Start time")))
    return items


# --- provider: Transport Infrastructure Ireland. GraphQL behind their live map.

TII_QUERY = ("query MapFeatures($input: MapFeaturesArgs!) { mapFeaturesQuery(input: $input) "
             "{ mapFeatures { bbox title tooltip uri __typename "
             "features { id geometry properties type } ... on Event { priority } } "
             "error { message type } } }")


def p_tii_ireland():
    """Ireland publishes no DATEX II incident feed. Its National Access Point
    carries journey times and 2014 collision data.

    Their public map at traffic.tii.ie runs on GraphQL, so this asks the same
    question the map asks. Unofficial and undocumented, so it will break when
    they change it. Titles carry road numbers, features carry coordinates,
    which means both matching modes work.
    """
    body = json.dumps({
        "operationName": "MapFeatures",
        "query": TII_QUERY,
        "variables": {"input": {
            "north": 55.45, "south": 51.40, "east": -5.90, "west": -10.60,
            "zoom": 11, "layerSlugs": ["roadReports"],
            "nonClusterableUris": ["dashboard"]}},
    })
    out = subprocess.run(
        ["curl", "-s", "--max-time", "45", "-A", BROWSER_UA, "-X", "POST",
         "https://traffic.tii.ie/api/graphql",
         "-H", "Content-Type: application/json",
         "-H", "Origin: https://traffic.tii.ie",
         "-H", "Referer: https://traffic.tii.ie/",
         "-H", "Accept: application/json", "-d", body],
        capture_output=True, text=True, timeout=70).stdout
    try:
        d = json.loads(out)
    except Exception:
        raise RuntimeError(f"TII: no JSON ({out[:60].strip()})")
    q = (d.get("data") or {}).get("mapFeaturesQuery") or {}
    if q.get("error"):
        raise RuntimeError(f"TII: {str(q['error'])[:70]}")

    items = []
    for f in q.get("mapFeatures") or []:
        if f.get("__typename") != "Event":
            continue
        title = (f.get("title") or f.get("tooltip") or "").strip()
        if not title:
            continue
        lat = lon = None
        for sub in f.get("features") or []:
            g = sub.get("geometry") or {}
            if g.get("type") == "Point" and g.get("coordinates"):
                lon, lat = g["coordinates"][0], g["coordinates"][1]
                break
        if lat is None and f.get("bbox"):
            bb = f["bbox"]
            lon, lat = (bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2
        # the kind lives in the sentence, e.g. "M18/N18 northbound: Vehicle on fire."
        tail = title.split(":", 1)[1].strip() if ":" in title else title
        items.append(_norm("TII Ireland", title[:70], tail[:30] or "incident",
                           "", f.get("tooltip", "")[:170], "", None, lat, lon))
        if f.get("priority") is not None:
            items[-1]["rank"] = min(items[-1]["rank"], 1 if f["priority"] <= 2 else 2)
    return items



# --- provider: DATEX II, the European standard. One parser, most of a continent.

def p_datex2(name, url, headers=None, gzip_required=False):
    """Parses a DATEX II situation feed from any National Access Point.

    Every EU member state runs one under Directive 2010/40/EU and DATEX II
    is the reference format. The catch, learned the hard way: the standard
    fixes the envelope and leaves location referencing open. Some feeds give
    coordinates, some give road numbers, some give AlertC codes and nothing
    else. This pulls whatever is there and reports what it couldn't find.
    """
    cmd = ["curl", "-sL", "--max-time", "45", "-A", UA]
    if gzip_required:
        cmd.append("--compressed")
    for k, v in (headers or {}).items():
        cmd += ["-H", f"{k}: {v}"]
    xml = subprocess.run(cmd + [url], capture_output=True, text=True, timeout=70).stdout
    if "<" not in xml:
        raise RuntimeError(f"{name}: no XML returned ({xml[:60].strip()})")

    import xml.etree.ElementTree as ET
    root = ET.fromstring(xml)
    strip = lambda tag: tag.rsplit("}", 1)[-1]

    def first(el, *names):
        for n in names:
            for e in el.iter():
                if strip(e.tag) == n and e.text and e.text.strip():
                    return e.text.strip()
        return ""

    def num(el, *names):
        v = first(el, *names)
        try:
            return float(v)
        except ValueError:
            return None

    items = []
    for sit in root.iter():
        if strip(sit.tag) != "situationRecord":
            continue
        kind = first(sit, "roadOrCarriagewayOrLaneManagementType", "obstructionType",
                     "accidentType", "environmentalObstructionType",
                     "networkManagementType", "generalNetworkManagementType") or "incident"
        desc = first(sit, "value", "generalPublicComment", "comment")
        road = first(sit, "roadNumber", "roadIdentifier", "aRoadNumber")
        lat = num(sit, "latitude")
        lon = num(sit, "longitude")
        place = first(sit, "roadName", "descriptor", "areaName", "tpegDescriptor")
        title = " ".join(x for x in (road, place) if x).strip() or desc.split(".")[0][:60]
        roads = {road.upper()} if road else None
        items.append(_norm(name, title, kind, first(sit, "directionBound", "alertCDirection"),
                           desc, first(sit, "overallStartTime"), roads, lat, lon))
    return items



# --- provider: National Highways (England). Their own JSON, no key.

def p_national_highways():
    base = "https://nationalhighways.co.uk/trafficsearchapi/events"
    out, page = [], 1
    while page <= 5:
        raw = subprocess.run(["curl", "-sL", "--max-time", "35", "-A", UA,
                              f"{base}?limit=100&page={page}"],
                             capture_output=True, text=True, timeout=60).stdout
        try:
            d = json.loads(raw)
        except Exception:
            raise RuntimeError(f"National Highways: bad response ({raw[:50].strip()})")
        for e in d.get("data", []):
            if e.get("status") and e["status"].lower() not in ("active", "current"):
                continue
            road = (e.get("road") or "").upper()
            bits = [e.get("location") or "", e.get("lanesClosedText") or "",
                    e.get("timeToClearText") or ""]
            delay = e.get("delay") or 0
            if delay:
                bits.append(f"{delay} min delay")
            out.append(_norm("National Highways", e.get("title") or road,
                             e.get("type") or e.get("reason") or "incident",
                             e.get("direction") or "", ". ".join(b for b in bits if b),
                             (e.get("overallStartTime") or "")[:16],
                             {road} if road else None))
        pg = d.get("pagination") or {}
        if page >= (pg.get("totalPages") or 1):
            break
        page += 1
    return out


# --- provider: Traffic Wales. Server-rendered, colon-delimited titles.

def p_traffic_wales():
    html = subprocess.run(["curl", "-sL", "--max-time", "35", "-A", UA, "https://traffic.wales/"],
                          capture_output=True, text=True, timeout=60).stdout
    if "traffic-alert__title" not in html:
        raise RuntimeError("Traffic Wales page returned nothing usable")
    out, seen = [], set()
    for block in re.split(r'<div class="traffic-alert ', html)[1:]:
        m = re.search(r'traffic-alert__title[^>]*>(.*?)</a>', block, re.S)
        if not m:
            continue
        title = re.sub(r"<[^>]+>", "", m.group(1)).replace("&amp;", "&").strip()
        if title in seen:
            continue
        seen.add(title)
        # the class list reads "traffic-alert--featured traffic-alert--maintenanceworks",
        # so skip the presentational ones and take the real category
        classes = re.findall(r'traffic-alert--(\w+)', block[:220])
        kind = next((c for c in classes if c not in ("featured", "compact")), "incident")
        # titles read: ROAD : direction : place : work : detail : detail
        parts = [x.strip() for x in title.split(":") if x.strip()]
        start = re.search(r'traffic-alert__start[^>]*>\s*Start\s*([\d/]+)', block)
        out.append(_norm("Traffic Wales", title[:70], kind,
                         parts[1] if len(parts) > 1 else "",
                         " : ".join(parts[2:])[:160],
                         start.group(1) if start else ""))
    return out


# --- provider: TrafficwatchNI. Server-rendered table, 240-odd entries.

def p_trafficwatch_ni():
    html = subprocess.run(["curl", "-sL", "--max-time", "45", "-A", UA,
                           "https://www.trafficwatchni.com/twni/roadworks"],
                          capture_output=True, text=True, timeout=70).stdout
    rows = re.findall(r'<tr id="roadwork-.*?</tr>', html, re.S)
    if not rows:
        raise RuntimeError("TrafficwatchNI returned no rows")
    out = []
    for r in rows:
        sev = (re.search(r'roadwork-row-toggle (\w+)', r) or [None, "low"])[1] if re.search(
            r'roadwork-row-toggle (\w+)', r) else "low"
        txt = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " | ", r))
        cells = [c.strip() for c in txt.split("|") if c.strip()]
        if not cells:
            continue
        head = cells[0]
        kind = "roadworks"
        for c in cells[:3]:
            m = re.search(r"-\s*(Road closure|Lane closure|Footway closure|Contraflow)", c, re.I)
            if m:
                kind = m.group(1).lower()
                break
        loc = next((cells[i + 1] for i, c in enumerate(cells) if c == "Location" and i + 1 < len(cells)), "")
        desc = next((cells[i + 1] for i, c in enumerate(cells) if c == "Description" and i + 1 < len(cells)), "")
        out.append(_norm("TrafficwatchNI", head[:70], kind, "",
                         f"{loc}. {desc}".strip(". ")[:170], "",
                         None if "severity" in sev else None))
        if kind == "road closure" and sev == "low":
            out[-1]["rank"] = 1   # a low-severity full closure is still serious
    return out


def p_datex2_bulk(name, url, max_age_hours=6, gzipped=True):
    """DATEX II feeds too big to hold in memory.

    The Dutch national feed is 213 MB decompressed and carries 62,550 records,
    most of them roadworks planned months ahead. Streamed with iterparse,
    filtered to whatever is live now, and the extract cached as JSON so later
    runs cost nothing.
    """
    import gzip, io
    import xml.etree.ElementTree as ET
    from datetime import timezone

    slug = re.sub(r"[^a-z0-9]+", "_", name.lower())
    parsed = CACHE / f"bulk_{slug}.json"
    if parsed.exists() and (time.time() - parsed.stat().st_mtime) < max_age_hours * 3600:
        return [{**d, "roads": set(d.get("roads") or [])} for d in json.loads(parsed.read_text())]

    blob = CACHE / f"bulk_{slug}.raw"
    if not (blob.exists() and (time.time() - blob.stat().st_mtime) < max_age_hours * 3600):
        r = subprocess.run(["curl", "-sL", "--max-time", "300", "-A", UA, "-o", str(blob), url],
                           capture_output=True, timeout=360)
        if r.returncode or not blob.exists() or blob.stat().st_size < 1000:
            raise RuntimeError(f"{name}: download failed")

    raw = blob.read_bytes()
    if gzipped or raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)

    strip = lambda tag: tag.rsplit("}", 1)[-1]
    now = datetime.now(timezone.utc)
    out = []
    for _, el in ET.iterparse(io.BytesIO(raw), events=("end",)):
        if strip(el.tag) != "situationRecord":
            continue
        g = lambda nm: next((e.text.strip() for e in el.iter()
                             if strip(e.tag) == nm and e.text and e.text.strip()), "")
        end, start = g("overallEndTime"), g("overallStartTime")
        try:
            if end and datetime.fromisoformat(end.replace("Z", "+00:00")) < now:
                el.clear(); continue
            if start and datetime.fromisoformat(start.replace("Z", "+00:00")) > now:
                el.clear(); continue      # planned for later, not live
        except ValueError:
            pass
        lat, lon = g("latitude"), g("longitude")
        if not lat:
            el.clear(); continue
        kind = (g("causeType") or g("roadOrCarriagewayOrLaneManagementType")
                or g("obstructionType") or "incident")
        desc = g("generalPublicComment") or g("value") or ""
        out.append(_norm(name, (g("roadName") or desc or kind)[:60], kind,
                         g("directionBound"), desc, g("overallStartTime")[:16],
                         None, float(lat), float(lon)))
        el.clear()

    parsed.write_text(json.dumps([{**d, "roads": sorted(d["roads"])} for d in out]))
    return out



# --- registry: which provider covers which patch of the map
#
# "match" says how an incident gets tied to your route:
#   roads   the feed gives road numbers, match those against the route
#   coords  the feed gives lat/lon, match by distance to the route line
#   all     the feed gives neither, so show everything it reports and say so

PROVIDERS = [
    {"name": "Traffic Scotland", "box": (54.6, 61.0, -8.7, -0.7), "match": "roads",
     "fn": p_traffic_scotland, "note": "scraped public page"},

    {"name": "National Highways", "box": (49.8, 56.0, -6.5, 2.0), "match": "roads",
     "fn": p_national_highways, "note": "their own JSON API, no key, paginated"},

    {"name": "Traffic Wales", "box": (51.3, 53.5, -5.4, -2.6), "match": "roads",
     "fn": p_traffic_wales, "note": "scraped public page"},

    {"name": "TrafficwatchNI", "box": (54.0, 55.4, -8.2, -5.4), "match": "roads",
     "fn": p_trafficwatch_ni, "note": "scraped roadworks table"},

    {"name": "TII Ireland", "box": (51.3, 55.5, -10.7, -5.8), "match": "roads",
     "fn": p_tii_ireland, "note": "GraphQL behind their public map, undocumented"},

    {"name": "Netherlands", "box": (50.7, 53.6, 3.3, 7.3), "match": "coords",
     "fn": lambda: p_datex2_bulk("Netherlands",
         "https://opendata.ndw.nu/planningsfeed_wegwerkzaamheden_en_evenementen.xml.gz"),
     "note": "NDW open data, no key, 60k records streamed and filtered"},

    {"name": "Finland", "box": (59.5, 70.1, 19.0, 31.6), "match": "all",
     "fn": lambda: p_datex2("Finland",
         "https://tie.digitraffic.fi/api/traffic-message/v1/messages.datex2"
         "?situationType=TRAFFIC_ANNOUNCEMENT", gzip_required=True),
     "note": "Digitraffic, open, no key. AlertC locations, so no route matching."},

    # Ireland: its National Access Point is data.gov.ie, and as of this build
    # TII publishes journey times, traffic counts and historic collisions
    # there. No live incident feed. Their 2023 ITS progress report said
    # incidents would join the DATEX II feed "later this year" and it has not
    # appeared. The day it does, this is the whole change:
    #
    # {"name": "Ireland", "box": (51.4, 55.4, -10.6, -5.9), "match": "coords",
    #  "fn": lambda: p_datex2("Ireland", "<feed url>"), "note": "TII DATEX II"},
]


def providers_for(coords):
    lats = [c[0] for c in coords]; lons = [c[1] for c in coords]
    lo_la, hi_la, lo_lo, hi_lo = min(lats), max(lats), min(lons), max(lons)
    hits = []
    for pr in PROVIDERS:
        a, b, c, d = pr["box"]
        if not (hi_la < a or lo_la > b or hi_lo < c or lo_lo > d):
            hits.append(pr)
    return hits


def incidents_on_route(route_roads, coords, corridor_miles=4.0):
    """corridor_miles applies to road-number feeds. Coordinate feeds use a
    much tighter one, because countries that map every municipal pavement
    will otherwise bury the motorway closure you actually care about."""
    """Tie incidents to this route, by whatever the feed gives us."""
    provs = providers_for(coords)
    if not provs:
        return [], "no closure provider covers this route yet"

    step = max(1, len(coords) // 300)
    thin = coords[::step]
    hits, problems = [], []

    for pr in provs:
        try:
            found = pr["fn"]()
        except Exception as e:
            problems.append(f"{pr['name']}: {str(e)[:50]}")
            continue
        mode = pr.get("match", "roads")
        for i in found:
            if i["lat"] is not None and i["lon"] is not None:
                limit = 0.4 if mode == "coords" else corridor_miles
                d = min(haversine_miles((i["lat"], i["lon"]), c) for c in thin)
                if d <= limit:
                    hits.append({**i, "why": f"{d:.1f} mi from your route"})
            elif i["roads"] & route_roads:
                hits.append({**i, "why": "on " + ",".join(sorted(i["roads"] & route_roads))})
            elif mode == "all":
                hits.append({**i, "why": f"somewhere in {pr['name']}, feed gives no location"})

    hits.sort(key=lambda i: (i["rank"], "somewhere in" in i.get("why", "")))
    if len(hits) > 40:
        note = f"{len(hits) - 40} further minor items not shown"
        hits = hits[:40]
        problems.append(note)
    return hits, ("; ".join(problems) if problems and not hits else None)


# ----------------------------------------------------------------- Fuel Finder (UK)

FF_URL = "https://raw.githubusercontent.com/matthewgall/fuelfinder-archive/main/data.csv"
FF_DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
FUEL_GRADES = {"E10": "petrol", "E5": "super unleaded", "B7S": "diesel", "B7P": "premium diesel"}


def load_fuelfinder(max_age_hours=6):
    """The UK statutory forecourt dataset. Every petrol station in Britain is
    legally required to be in here, with opening hours and closure flags.
    Motor Fuel Price (Open Data) Regulations 2025, live since 2 February 2026.
    Open Government Licence v3.0."""
    import csv, io
    cache = CACHE / "fuelfinder.csv"
    fresh = cache.exists() and (time.time() - cache.stat().st_mtime) < max_age_hours * 3600
    if not fresh:
        print("  downloading UK Fuel Finder dataset...", file=sys.stderr)
        out = subprocess.run(["curl", "-sL", "--max-time", "90", "-A", UA, FF_URL],
                             capture_output=True, text=True, timeout=120)
        if out.stdout.count("\n") > 100:
            cache.write_text(out.stdout)
        elif not cache.exists():
            raise RuntimeError("Fuel Finder download failed and no cache available")
    rows = list(csv.DictReader(io.StringIO(cache.read_text())))
    out = []
    for r in rows:
        try:
            lat, lon = float(r["forecourts.location.latitude"]), float(r["forecourts.location.longitude"])
        except (ValueError, TypeError, KeyError):
            continue
        if r.get("forecourts.permanent_closure", "").lower() == "true":
            continue
        prices = {}
        for g in FUEL_GRADES:
            v = r.get(f"forecourts.fuel_price.{g}", "").strip()
            if v:
                try: prices[g] = float(v)
                except ValueError: pass
        hours = {}
        for d in FF_DAYS:
            k = f"forecourts.opening_times.usual_days.{d}"
            hours[d] = {"open": r.get(f"{k}.open_time", "").strip(),
                        "close": r.get(f"{k}.close_time", "").strip(),
                        "all_day": r.get(f"{k}.is_24_hours", "").lower() == "true"}
        out.append({
            "name": (r.get("forecourts.trading_name") or r.get("forecourts.brand_name") or "Unnamed").title(),
            "brand": r.get("forecourts.brand_name", ""),
            "lat": lat, "lon": lon,
            "phone": r.get("forecourts.public_phone_number", "").strip(),
            "temp_closed": r.get("forecourts.temporary_closure", "").lower() == "true",
            "motorway": r.get("forecourts.is_motorway_service_station", "").lower() == "true",
            "toilets": r.get("forecourts.amenities.customer_toilets", "").lower() == "true",
            "prices": prices, "ff_hours": hours, "source": "fuelfinder",
        })
    return out


def ff_open_at(st, when):
    """True, False or None. Uses the statutory per-day hours."""
    if st.get("temp_closed"):
        return False
    h = st["ff_hours"][FF_DAYS[when.weekday()]]
    if h["all_day"]:
        return True
    if not h["open"] or not h["close"]:
        return None
    try:
        o = [int(x) for x in h["open"].split(":")[:2]]
        c = [int(x) for x in h["close"].split(":")[:2]]
    except ValueError:
        return None
    now, start, end = when.hour * 60 + when.minute, o[0] * 60 + o[1], c[0] * 60 + c[1]
    if end <= start:
        return now >= start or now < end
    return start <= now < end


def ff_closes_at(st, when):
    h = st["ff_hours"][FF_DAYS[when.weekday()]]
    if h["all_day"] or not h["close"]:
        return None
    return h["close"][:5]


def fuelfinder_near_route(coords, corridor_miles=3.0):
    stations = load_fuelfinder()
    lats = [c[0] for c in coords]; lons = [c[1] for c in coords]
    pad = corridor_miles / 60.0
    box = (min(lats) - pad, max(lats) + pad, min(lons) - pad * 1.7, max(lons) + pad * 1.7)
    near = [s for s in stations if box[0] <= s["lat"] <= box[1] and box[2] <= s["lon"] <= box[3]]
    step = max(1, len(coords) // 400)
    thin = coords[::step]
    out = []
    for s in near:
        d = min(haversine_miles((s["lat"], s["lon"]), c) for c in thin)
        if d <= corridor_miles:
            s["off_route"] = d
            out.append(s)
    return out


# ----------------------------------------------------------------- opening hours

DAYS = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]


def parse_hours(spec, when):
    """Is this place open at `when`?  Returns True, False, or None for unknown.

    Handles the common OSM forms: 24/7, 'Mo-Fr 06:00-22:00', day lists,
    multiple rules split by ';', and 'off'. Anything stranger returns None,
    and None is reported honestly as unknown. Guessing here is how people
    end up at a locked petrol station.
    """
    if not spec:
        return None
    s = spec.strip()
    if s in ("24/7", "Mo-Su 00:00-24:00", "00:00-24:00"):
        return True

    today = DAYS[when.weekday()]
    minutes_now = when.hour * 60 + when.minute
    verdict = None

    for rule in s.split(";"):
        rule = rule.strip()
        if not rule:
            continue
        m = re.match(r"^([A-Za-z,\-]*)\s*(.*)$", rule)
        daypart, timepart = m.group(1).strip(), m.group(2).strip()

        if daypart:
            days = set()
            for chunk in daypart.split(","):
                chunk = chunk.strip()
                if "-" in chunk:
                    a, b = chunk.split("-", 1)
                    if a in DAYS and b in DAYS:
                        i, j = DAYS.index(a), DAYS.index(b)
                        days.update(DAYS[i:j + 1] if i <= j else DAYS[i:] + DAYS[:j + 1])
                    else:
                        return None
                elif chunk in DAYS:
                    days.add(chunk)
                else:
                    return None
            if today not in days:
                continue
        # no daypart means the rule applies every day

        if timepart.lower() in ("off", "closed"):
            verdict = False
            continue
        if timepart == "24/7":
            verdict = True
            continue

        windows = re.findall(r"(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})", timepart)
        if not windows:
            return None
        for h1, m1, h2, m2 in windows:
            start = int(h1) * 60 + int(m1)
            end = int(h2) * 60 + int(m2)
            if end <= start:  # crosses midnight
                if minutes_now >= start or minutes_now < end:
                    return True
            elif start <= minutes_now < end:
                return True
        if verdict is None:
            verdict = False

    return verdict


def closes_at(spec, when):
    """Best guess at today's closing time, for the warning line."""
    if not spec or spec.strip() == "24/7":
        return None
    today = DAYS[when.weekday()]
    for rule in spec.split(";"):
        rule = rule.strip()
        m = re.match(r"^([A-Za-z,\-]*)\s*(.*)$", rule)
        daypart, timepart = m.group(1).strip(), m.group(2).strip()
        if daypart:
            days = set()
            for chunk in daypart.split(","):
                if "-" in chunk:
                    a, b = chunk.split("-", 1)
                    if a in DAYS and b in DAYS:
                        i, j = DAYS.index(a), DAYS.index(b)
                        days.update(DAYS[i:j + 1] if i <= j else DAYS[i:] + DAYS[:j + 1])
                elif chunk.strip() in DAYS:
                    days.add(chunk.strip())
            if today not in days:
                continue
        w = re.findall(r"(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})", timepart)
        if w:
            return f"{w[-1][2]}:{w[-1][3]}"
    return None


# ----------------------------------------------------------------- phone handout

def write_plan(path, plan):
    """A single self-contained page for the passenger's phone.

    No network, no scripts, no fonts. Opens on a dead signal in Caithness.
    """
    args = plan["opts"]
    total_miles, total_hours = plan["total_miles"], plan["total_hours"]
    depart, range_miles = plan["depart"], plan["range_miles"]
    scored, hits = plan["scored"], plan["hits"]
    vol, usable_vol, roads = plan["vol"], plan["usable_vol"], plan["roads"]
    arrive = plan["arrive"]
    usable = [s for s in scored if s["open"] is True]
    pick = usable[0] if usable else None

    def card(s):
        if s["open"] is True:
            cls, status = "ok", (f"shuts {s['shuts']}" if s["shuts"] else "open 24 hours")
        elif s["open"] is False:
            cls, status = "no", ("temporarily closed" if s.get("temp_closed") else "shut by then")
        else:
            cls, status = "maybe", "hours unknown"
        bits = []
        if s.get("prices", {}).get("E10"):
            bits.append(f"{s['prices']['E10']/100:.2f}/L")
        if s.get("off_route", 0) > 0.6:
            bits.append(f"{s['off_route']:.1f} mi off route")
        if s.get("toilets"):
            bits.append("toilets")
        tel = (f'<a class="tel" href="tel:{s["phone"].replace(" ", "")}">{s["phone"]}</a>'
               if s.get("phone") else "")
        return (f'<div class="s {cls}"><div class="hd"><b>mile {s["along"]:.0f}</b>'
                f'<span class="eta">{s["eta"]:%H:%M}</span></div>'
                f'<div class="nm">{s["name"]}</div>'
                f'<div class="st">{status}</div>'
                f'<div class="x">{" &middot; ".join(bits)}</div>{tel}</div>')

    late = ""
    if pick:
        tol = []
        for s in usable[:6]:
            if s["shuts"]:
                hh, mm = (int(x) for x in s["shuts"].split(":")[:2])
                shut = s["eta"].replace(hour=hh % 24, minute=mm)
                slack = (shut - s["eta"]).total_seconds() / 60
                if slack > 0:
                    tol.append(f"<li><b>{s['name']}</b> is fine if you're up to "
                               f"<b>{int(slack // 60)}h {int(slack % 60)}m</b> late. After that it's shut.</li>")
            else:
                tol.append(f"<li><b>{s['name']}</b> is 24 hours. Late doesn't matter.</li>")
        late = ("<h2>If you're running late</h2><ul class='late'>" + "".join(tol) + "</ul>") if tol else ""

    inc = ""
    if hits:
        rows = "".join(f"<li><b>{i['type']}</b> &middot; {i['title']}<br><small>{i['direction']}"
                       f"{' &middot; ' + i['desc'][:90] if i['desc'] else ''}</small></li>" for i in hits[:6])
        inc = f"<h2>Live when this was made</h2><ul class='inc'>{rows}</ul>"

    doc = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Fuel plan</title><style>
:root{{--bg:#101418;--fg:#eef2f5;--mut:#9aa7b2;--ok:#2e7d4f;--no:#8c2f2f;--maybe:#7a6a2a;--card:#192027}}
*{{box-sizing:border-box}}
body{{margin:0;padding:16px;background:#101418;color:#eef2f5;
font:17px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif}}
h1{{font-size:21px;margin:0 0 2px}} h2{{font-size:15px;text-transform:uppercase;letter-spacing:.08em;
color:#9aa7b2;margin:26px 0 10px;font-weight:600}}
.sub{{color:#9aa7b2;font-size:15px;margin-bottom:18px}}
.big{{background:#1b232b;border-left:5px solid #2e7d4f;padding:14px 16px;border-radius:8px;margin:4px 0 8px}}
.big .t{{font-size:13px;color:#9aa7b2;text-transform:uppercase;letter-spacing:.08em}}
.big .v{{font-size:25px;font-weight:700;margin:4px 0}}
.s{{background:#192027;border-radius:8px;padding:11px 13px;margin-bottom:9px;border-left:5px solid #3a4550}}
.s.ok{{border-left-color:#2e7d4f}} .s.no{{border-left-color:#8c2f2f;opacity:.55}}
.s.maybe{{border-left-color:#7a6a2a}}
.hd{{display:flex;justify-content:space-between;font-size:14px;color:#9aa7b2}}
.nm{{font-weight:650;margin:3px 0 1px;font-size:17px}}
.st{{font-size:15px}} .x{{color:#9aa7b2;font-size:14px;margin-top:2px}}
.tel{{display:inline-block;margin-top:8px;padding:8px 14px;background:#273039;
color:#eef2f5;border-radius:7px;text-decoration:none;font-size:16px}}
ul{{padding-left:20px;margin:0}} li{{margin-bottom:9px}} small{{color:#9aa7b2}}
.foot{{margin-top:26px;padding-top:14px;border-top:1px solid #273039;color:#9aa7b2;font-size:13px}}
.warn{{background:#2a2317;border-left:5px solid #7a6a2a;padding:12px 14px;border-radius:8px;
font-size:15px;margin-bottom:12px}}
.roads{{background:#192027;border-radius:8px;padding:12px 14px;margin-bottom:12px}}
.roads .t{{font-size:13px;color:#9aa7b2;text-transform:uppercase;letter-spacing:.08em}}
.tel.wide{{display:block;margin:0 0 9px;text-align:center;padding:14px;font-size:17px;font-weight:600}}
.note{{color:#9aa7b2;font-size:14px;margin-top:10px}}
</style></head><body>
<h1>{args.origin} to {args.dest}</h1>
<div class="sub">{total_miles:.0f} miles &middot; leave {depart:%a %d %b %H:%M} &middot; arrive about {arrive:%H:%M}<br>
{args.fuel:.0f}% of {args.tank:.0f}{vol} gives you {range_miles:.0f} miles</div>
{'<div class="big"><div class="t">The plan</div><div class="v">' + pick['name'] + '</div>mile ' + f"{pick['along']:.0f}" + ', about ' + f"{pick['eta']:%H:%M}" + '</div>' if pick else '<div class="big" style="border-left-color:#8c2f2f"><div class="t">The plan</div><div class="v">Fuel before you leave</div>Nothing confirmed open within range.</div>'}
{late}
<h2>Every stop in range</h2>
{"".join(card(s) for s in scored[:14])}
{inc}
<h2>If you get a bar of signal</h2>
<div class="warn">Closures below were live at <b>{datetime.now():%H:%M on %d %b}</b>.
Anything that happened after that is not in here. On a long run, check once before you set off.</div>
<div class="roads"><span class="t">Your roads today</span><br><b>{" &middot; ".join(sorted(roads)) if roads else "see the route"}</b><br>
<small>These are the numbers to listen for on the radio.</small></div>
<a class="tel wide" href="tel:08000281414">Traffic Scotland &nbsp;0800 028 1414</a>
<a class="tel wide" href="tel:03001235000">National Highways &nbsp;0300 123 5000</a>
<a class="tel wide" href="sms:07380283600">Text roadside help &nbsp;0738 028 3600</a>
<div class="note">Both lines are 24 hours. The text number is for roadside assistance
when a phone call won't work. Emergencies are always 999.</div>
<div class="foot">Made {datetime.now():%d %b %Y, %H:%M}. Needs no signal.<br>
Fuel data: UK Fuel Finder, Open Government Licence v3.0. Closures: Traffic Scotland.<br>
Opening hours change. Ring ahead when it matters.</div>
</body></html>"""
    Path(path).write_text(doc)
    return path


# ----------------------------------------------------------------- rerouting

def closure_areas(points, radius_miles=0.45):
    """GeoJSON squares around each closure, for GraphHopper to refuse to enter."""
    feats = {}
    for n, (lat, lon) in enumerate(points):
        dlat = radius_miles / 69.0
        dlon = radius_miles / (69.0 * max(0.2, math.cos(math.radians(lat))))
        ring = [[lon - dlon, lat - dlat], [lon + dlon, lat - dlat],
                [lon + dlon, lat + dlat], [lon - dlon, lat + dlat],
                [lon - dlon, lat - dlat]]
        feats[f"shut{n}"] = {"type": "Feature", "id": f"shut{n}", "properties": {},
                             "geometry": {"type": "Polygon", "coordinates": [ring]}}
    return feats


def reroute_avoiding(a, b, points, gh_url, profile="car"):
    """Ask GraphHopper for a route that physically cannot enter the closures.

    Custom models go in the body of a POST and areas are GeoJSON polygons.
    Multiplying priority by zero inside an area removes it from the graph,
    so the answer genuinely avoids the closure instead of preferring not to.

    Needs a GraphHopper on your own machine. See setup-local-routing.sh.
    """
    if not points:
        return None
    areas = closure_areas(points)
    body = {
        "points": [[a["lon"], a["lat"]], [b["lon"], b["lat"]]],
        "profile": profile,
        "points_encoded": False,
        "instructions": True,
        "ch.disable": True,
        "custom_model": {
            "priority": [{"if": f"in_{k}", "multiply_by": "0"} for k in areas],
            "areas": {"type": "FeatureCollection", "features": list(areas.values())},
        },
    }
    out = subprocess.run(
        ["curl", "-sL", "--max-time", "60", "-A", UA, "-X", "POST",
         "-H", "Content-Type: application/json", "-d", json.dumps(body),
         f"{gh_url.rstrip('/')}/route"],
        capture_output=True, text=True, timeout=90).stdout
    try:
        d = json.loads(out)
    except Exception:
        raise RuntimeError(f"routing engine gave no JSON ({out[:70].strip()})")
    if d.get("message"):
        raise RuntimeError(d["message"][:90])
    paths = d.get("paths") or []
    if not paths:
        raise RuntimeError("no route avoids those closures")
    pth = paths[0]
    coords = [(c[1], c[0]) for c in pth["points"]["coordinates"]]
    return {"miles": pth["distance"] * MILES_PER_METRE,
            "hours": pth["time"] / 3600000.0,
            "coords": coords,
            "blocked": len(points)}



# ----------------------------------------------------------------- the plan

class Opts:
    """Plain options bag so the CLI and the server can share one code path."""
    def __init__(self, origin, dest, tank=55, mpg=42, fuel=50, reserve=10,
                 depart=None, detour=0.0, units="uk", offline=False, out=None,
                 verify=False, gh=None):
        self.origin, self.dest = origin, dest
        self.tank, self.mpg, self.fuel, self.reserve = float(tank), float(mpg), float(fuel), float(reserve)
        self.depart = depart
        self.detour, self.units, self.offline, self.out = float(detour), units, offline, out
        self.verify = verify
        self.gh = gh


def build_plan(o):
    """Everything, computed, nothing printed. The CLI and the server both call this."""
    depart = datetime.fromisoformat(o.depart) if o.depart else datetime.now()
    notes = []

    a, b = resolve(o.origin, o.offline), resolve(o.dest, o.offline)
    r = route(a, b, o.offline)

    total_miles = r["miles"] + o.detour
    total_hours = r["hours"] * (total_miles / r["miles"] if r["miles"] else 1)
    vol = "L" if o.units == "uk" else "US gal"
    per_unit = o.mpg if o.units == "us" else o.mpg / LITRES_PER_GALLON["uk"]
    usable_vol = o.tank * (o.fuel - o.reserve) / 100
    range_miles = usable_vol * per_unit

    plan = {"opts": o, "a": a, "b": b, "roads": r.get("roads", set()),
            "coords": r["coords"], "route_cached": bool(r.get("cached")),
            "depart": depart, "arrive": depart + timedelta(hours=total_hours),
            "total_miles": total_miles, "total_hours": total_hours,
            "vol": vol, "usable_vol": usable_vol, "range_miles": range_miles,
            "makes_it": range_miles >= total_miles,
            "spare": range_miles - total_miles, "notes": notes,
            "hits": [], "incident_error": None, "blocked": False,
            "scored": [], "source": None, "pick": None, "cheaper": None, "later": 0}

    hits, err = incidents_on_route(plan["roads"], r["coords"])
    plan["hits"], plan["incident_error"] = hits or [], err
    plan["blocked"] = any(i["rank"] <= 1 for i in (hits or []))

    # locate the serious ones, then route around them if an engine is available
    plan["located"], plan["reroute"], plan["reroute_error"] = [], None, None
    serious = [i for i in plan["hits"] if i["rank"] <= 1]
    if serious:
        idx = build_steps_index(r)
        jn = junction_index(r["coords"], idx) if idx else {}
        for i in serious:
            got = locate_closure(i, idx, r["coords"], jn)
            if got:
                i["where"] = got[2]
                plan["located"].append((got[0], got[1]))
        if plan["located"] and o.gh:
            try:
                plan["reroute"] = reroute_avoiding(a, b, plan["located"], o.gh)
            except Exception as e:
                plan["reroute_error"] = str(e)[:90]

    # a real diversion replaces the guessed one
    if plan["reroute"]:
        extra = plan["reroute"]["miles"] - r["miles"]
        if extra > 0.5:
            total_miles = plan["reroute"]["miles"]
            total_hours = plan["reroute"]["hours"]
            plan.update({"total_miles": total_miles, "total_hours": total_hours,
                         "arrive": depart + timedelta(hours=total_hours),
                         "makes_it": range_miles >= total_miles,
                         "spare": range_miles - total_miles,
                         "diversion_cost": extra})

    if plan["makes_it"]:
        return plan

    stations = []
    try:
        stations = fuelfinder_near_route(r["coords"])
        if stations:
            plan["source"] = "fuelfinder"
    except Exception as e:
        notes.append(f"Fuel Finder unavailable: {e}")
    if not stations:
        try:
            stations = fuel_near_route(r["coords"])
            plan["source"] = "osm"
        except RuntimeError as e:
            notes.append(f"No fuel data: {e}")
            return plan

    cum = cumulative_miles(r["coords"])
    scored = []
    for st in stations:
        best_i, best_d = 0, 1e9
        for i, c in enumerate(r["coords"]):
            d = haversine_miles((st["lat"], st["lon"]), c)
            if d < best_d:
                best_i, best_d = i, d
        along = cum[best_i]
        if along > range_miles or along < 1:
            continue
        eta = depart + timedelta(hours=total_hours * (along / total_miles))
        if st.get("source") == "fuelfinder":
            is_open, shuts = ff_open_at(st, eta), ff_closes_at(st, eta)
        else:
            is_open, shuts = parse_hours(st.get("hours"), eta), closes_at(st.get("hours"), eta)
        scored.append({**st, "along": along, "off_route": st.get("off_route", best_d),
                       "eta": eta, "open": is_open, "shuts": shuts})
    scored.sort(key=lambda s: -s["along"])
    plan["scored"] = scored
    if getattr(o, "verify", False) and plan["source"] == "fuelfinder":
        plan["verify"] = cross_check(scored, r["coords"])

    usable = [s for s in scored if s["open"] is True]
    if usable:
        pick = usable[0]
        plan["pick"] = pick
        plan["later"] = len([s for s in scored if s["along"] > pick["along"]])
        priced = [s for s in usable if s.get("prices", {}).get("E10")]
        if len(priced) > 1:
            best = min(priced, key=lambda s: s["prices"]["E10"])
            if best is not pick and best["prices"]["E10"] < pick["prices"].get("E10", 1e9) - 2:
                plan["cheaper"] = (best, pick["prices"]["E10"] - best["prices"]["E10"])
    return plan



def cross_check(stations, coords):
    """Second-source every stop against OpenStreetMap.

    Idea owed to a suggestion about satellite ground truth. The statutory
    register is authoritative for Great Britain, so anything OSM has that
    the register does not has very likely closed. Anything in both is as
    confirmed as it gets without driving there.
    """
    try:
        osm = fuel_near_route(coords, radius_m=2000)
    except Exception as e:
        return f"OpenStreetMap unavailable, no second source ({str(e)[:40]})"
    near = lambda s, lst: any(
        haversine_miles((s["lat"], s["lon"]), (o["lat"], o["lon"])) < 0.25 for o in lst)
    both = 0
    for s in stations:
        if near(s, osm):
            s["confirmed"] = True
            both += 1
        else:
            s["confirmed"] = False
    ghosts = [o["name"] for o in osm if not near(o, stations)]
    msg = f"{both} of {len(stations)} confirmed by OpenStreetMap as well"
    if ghosts:
        msg += (f". {len(ghosts)} place(s) OSM lists are absent from the statutory "
                f"register and may have closed: " + ", ".join(g[:28] for g in ghosts[:4]))
    return msg


def plan_json(plan):
    """Compact, serialisable. What the phone actually receives."""
    o = plan["opts"]
    def stop(s):
        return {"name": s["name"], "mile": round(s["along"], 1), "eta": s["eta"].strftime("%H:%M"),
                "open": s["open"], "shuts": s["shuts"], "phone": s.get("phone", ""),
                "off_route": round(s.get("off_route", 0), 1), "toilets": bool(s.get("toilets")),
                "price_e10": s.get("prices", {}).get("E10")}
    return {
        "from": o.origin, "to": o.dest,
        "miles": round(plan["total_miles"]), "hours": round(plan["total_hours"], 2),
        "depart": plan["depart"].isoformat(timespec="minutes"),
        "arrive": plan["arrive"].isoformat(timespec="minutes"),
        "range_miles": round(plan["range_miles"]), "makes_it": plan["makes_it"],
        "roads": sorted(plan["roads"]), "source": plan["source"],
        "blocked": plan["blocked"],
        "incidents": [{"type": i["type"], "title": i["title"], "direction": i["direction"],
                       "desc": i["desc"][:200], "since": i["start"], "rank": i["rank"],
                       "source": i["source"], "why": i.get("why", "")}
                      for i in plan["hits"][:12]],
        "pick": stop(plan["pick"]) if plan["pick"] else None,
        "stops": [stop(s) for s in plan["scored"][:14]],
        "notes": plan["notes"], "generated": datetime.now().isoformat(timespec="seconds"),
    }


def main():
    global OSRM_BASE
    p = argparse.ArgumentParser(description="Will I make it, and will it be open when I get there?")
    p.add_argument("--from", dest="origin", required=True)
    p.add_argument("--to", dest="dest", required=True)
    p.add_argument("--units", choices=["uk", "us"], default="uk")
    p.add_argument("--tank", type=float, default=55)
    p.add_argument("--mpg", type=float, default=42)
    p.add_argument("--fuel", type=float, default=50, help="fuel now, percent of tank")
    p.add_argument("--reserve", type=float, default=10)
    p.add_argument("--depart", default=None, help="ISO time, e.g. 2026-10-18T17:30")
    p.add_argument("--detour", type=float, default=0, help="extra miles from a diversion")
    p.add_argument("--osrm", default=OSRM_BASE)
    p.add_argument("--offline", action="store_true")
    p.add_argument("--privacy", action="store_true")
    p.add_argument("--out", default=None, help="write a page for your phone")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument("--verify", action="store_true",
                   help="second-source every stop against OpenStreetMap")
    p.add_argument("--gh", default=None,
                   help="GraphHopper URL for live rerouting, e.g. http://localhost:8989")
    args = p.parse_args()
    OSRM_BASE = args.osrm.rstrip("/")

    o = Opts(args.origin, args.dest, args.tank, args.mpg, args.fuel, args.reserve,
             args.depart, args.detour, args.units, args.offline, args.out, args.verify, args.gh)
    print("\nWorking...", file=sys.stderr)
    plan = build_plan(o)

    if args.json:
        print(json.dumps(plan_json(plan), indent=2))
        return

    W = "=" * 68
    print("\n" + W)
    print(f"  {o.origin.upper()}  to  {o.dest.upper()}")
    print(W)
    print(f"  Distance        {plan['total_miles']:.0f} miles"
          + (f"  (incl. {o.detour:.0f} mi diversion)" if o.detour else ""))
    print(f"  Driving time    {plan['total_hours']:.1f} hours")
    print(f"  Departing       {plan['depart']:%a %d %b, %H:%M}")
    print(f"  Arriving        {plan['arrive']:%a %d %b, %H:%M}")
    print(f"\n  In the tank     {o.fuel:.0f}% of {o.tank:.0f} {plan['vol']}")
    print(f"  Usable          {plan['usable_vol']:.1f} {plan['vol']} down to your {o.reserve:.0f}% reserve")
    print(f"  That's          {plan['range_miles']:.0f} miles at {o.mpg:.0f} mpg ({o.units.upper()})")

    if plan["incident_error"]:
        print(f"\n  Live incident check unavailable: {plan['incident_error'][:70]}")
    elif plan["hits"]:
        print("\n" + "-" * 68)
        where = ", ".join(sorted(plan["roads"]))[:40] or "this route"
        print(f"  LIVE ON YOUR ROUTE  ({len(plan['hits'])} on {where})")
        print("-" * 68)
        for i in plan["hits"][:8]:
            print(f"  [{i['type'].upper()[:9]:9}] {i['title'][:46]}")
            bits = [b for b in (i.get("why"), i["direction"],
                                f"since {i['start'][:16]}" if i["start"] else "") if b]
            print(f"              {'  |  '.join(bits)[:76]}")
            if i["desc"]:
                print(f"              {i['desc'][:78]}")
    else:
        print("\n  Live incidents: nothing reported on your roads right now.")

    if plan["makes_it"]:
        print(f"\n  VERDICT: you make it with {plan['spare']:.0f} miles to spare.")
        print("  No fuel stop needed. Enjoy the drive.")
        print(W + "\n")
        return

    print(f"\n  VERDICT: you run to reserve {-plan['spare']:.0f} miles short.")
    print(f"  You need fuel by mile {plan['range_miles']:.0f}.")
    for n in plan["notes"]:
        print(f"  {n}")

    scored = plan["scored"]
    print("\n" + "-" * 68)
    print(f"  REACHABLE STOPS, furthest first ({len(scored)} within range)")
    print("-" * 68)
    if not scored:
        print("  Nothing on the route within your range. Fuel before you set off.")
        print(W + "\n")
        return

    for s in scored[:12]:
        if s["open"] is True:
            flag, note = "OPEN  ", (f"shuts {s['shuts']}" if s["shuts"] else "24 hours")
        elif s["open"] is False:
            flag = "SHUT  "
            note = "temporarily closed" if s.get("temp_closed") else f"shut at {s['eta']:%H:%M}"
        else:
            flag, note = "UNKNOWN", "no hours recorded"
        price = ""
        if s.get("prices"):
            g, v = sorted(s["prices"].items(), key=lambda kv: (kv[0] != "E10", kv[0]))[0]
            price = f"  {v/100:.2f}/L {g}"
        print(f"  [{flag}] mile {s['along']:5.0f}  ETA {s['eta']:%H:%M}  "
              f"{s['name'][:28]:30}{price:16} {note}")
        extras = []
        if s["off_route"] > 0.6: extras.append(f"{s['off_route']:.1f} mi off route")
        if s.get("motorway"): extras.append("motorway services")
        if s.get("toilets"): extras.append("toilets")
        if extras:
            print(f"            {', '.join(extras)}")

    print("\n" + W)
    pick = plan["pick"]
    if pick:
        print(f"  PLAN: fill at {pick['name']}, mile {pick['along']:.0f}, around {pick['eta']:%H:%M}.")
        if pick["shuts"]:
            print(f"        It shuts at {pick['shuts']}. You have a margin, don't dawdle.")
        if pick.get("phone"):
            print(f"        Ring ahead: {pick['phone']}")
        if plan["cheaper"]:
            best, diff = plan["cheaper"]
            print(f"        Cheaper: {best['name']} at mile {best['along']:.0f}, {diff:.1f}p/L less.")
        if plan["later"]:
            print(f"        {plan['later']} station(s) further on are shut or unverified by then.")
        if pick.get("confirmed") is True:
            print("        Confirmed by two independent datasets.")
        elif pick.get("confirmed") is False:
            print("        Not in OpenStreetMap. Statutory register only. Ring ahead.")
    else:
        print("  PLAN: nothing confirmed open within range. Fuel before departure.")

    if plan.get("verify"):
        print(f"\n  CROSS-CHECK: {plan['verify']}")

    if plan.get("reroute"):
        n = plan["reroute"]["blocked"]
        cost = plan.get("diversion_cost", 0)
        if cost >= 0.5:
            print(f"\n  REROUTED around {n} closure(s).")
            print(f"     The diversion costs {cost:.0f} extra miles, and the fuel")
            print(f"     plan above already accounts for them.")
        else:
            print(f"\n  {n} closure(s) on your roads, and a route exists around them")
            print(f"     at no extra distance. Your journey is unaffected.")
    elif plan["blocked"]:
        print("\n  !! A closure or roadworks is live on your route.")
        if plan["located"]:
            print(f"     {len(plan['located'])} of them pinned to a point on your road:")
            for i in [x for x in plan["hits"] if x.get("where")][:3]:
                print(f"       {i['where']}")
        if plan.get("reroute_error"):
            print(f"     Rerouting failed: {plan['reroute_error']}")
        elif not o.gh:
            print("     Pass --gh http://localhost:8989 to route around them.")
        print("     Otherwise use --detour 40 to test the fuel plan by hand.")

    if o.out:
        write_plan(o.out, plan)
        print(f"\n  Phone handout written to {o.out}")
        print("  AirDrop or email it to yourself. It needs no signal.")

    if args.privacy or LEAKED:
        print("\n" + "-" * 68)
        if LEAKED:
            print("  LEFT THIS MACHINE THIS RUN:")
            for l in LEAKED:
                print(f"    {l}")
        else:
            print("  NOTHING ABOUT THIS JOURNEY LEFT THIS MACHINE.")
        print("-" * 68)
    print(W + "\n")


if __name__ == "__main__":
    main()
