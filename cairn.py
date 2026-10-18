#!/usr/bin/env python3
"""Cairn: will I make it, and will it be open when I get there?

Answers the question a sat nav doesn't. Routes A to B, works out where you run
dry, finds the fuel stops that are reachable BEFORE that, and checks whether
each one is actually open at the time you'd arrive.

Data: OSRM for routing, OpenStreetMap via Overpass for fuel stations and their
opening hours, Nominatim for place names. All free, no keys.

Usage:
  python3 cairn.py --from Inverness --to Thurso --tank 55 --mpg 42 --fuel 30
  python3 cairn.py --from "Inverness" --to "Thurso" --depart 2026-10-18T17:30
"""
#
#  Cairn. Copyright (C) 2026 Erika Burnett.
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

import argparse, hashlib, json, math, re, subprocess, sys, time, urllib.parse
from html import unescape
from datetime import date, datetime, timedelta
from pathlib import Path

UA = "cairn/0.1 (https://github.com/ ; prototype)"
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
    # Name the host in the retry line. A bare "retrying, empty response"
    # on a long European route tells you nothing about which of fourteen
    # sources has gone quiet, and the run can sit there for minutes.
    host = urllib.parse.urlparse(url).netloc or url[:40]
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
            print(f"  {host}: retrying in {wait}s ({last[:52].strip()})", file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError(f"gave up after {tries} tries: {last}")


def get_text(url, headers=None, ua=None, allow_empty=False):
    """Same idea as get(), for feeds that answer in XML or HTML.

    This used to return "" when the fetch failed. Every scraped provider
    then saw an empty string, found none of the markup it parses, and
    returned an empty list, so an unreachable site reported "no incidents"
    on a tool whose job is to say whether a road is shut. Nothing in the
    world looks more like an open road than no data about it.

    So a failed fetch raises. A provider's exception path then fires, the
    health check records it, and the driver is told the source went quiet
    instead of being told the road is clear. Pass allow_empty=True where a
    genuinely empty body is a valid answer.
    """
    cmd = ["curl", "-sL", "--fail", "--max-time", "45", "-A", ua or BROWSER_UA]
    for k, v in (headers or {}).items():
        cmd += ["-H", f"{k}: {v}"]
    host = urllib.parse.urlparse(url).netloc or url[:40]
    try:
        r = subprocess.run(cmd + [url], capture_output=True, text=True, timeout=60)
    except Exception as e:
        raise RuntimeError(f"{host} did not answer: {str(e)[:60]}")
    if r.returncode:
        raise RuntimeError(f"{host} did not answer (curl exit {r.returncode})")
    if not r.stdout.strip() and not allow_empty:
        raise RuntimeError(f"{host} answered with an empty body")
    return r.stdout


# overpass.osm.ch was in this list and should never have been. It is a Swiss
# regional instance: it holds Switzerland and nothing else, and it answers a
# query about Norway with zero results in under a second, no error, no remark.
# Tested 17 October 2026: 49 fuel stations around Bern, 0 around Trondheim,
# where the global instances find 38. Any route that fell through to it got a
# fast, confident, wrong answer that there is no fuel on the road.
#
# overpass.private.coffee, overpass.osm.jp, overpass.monicz.dev and
# overpass.nchc.org.tw were tested the same day and answer this tool's own
# query with an HTML error page or nothing at all.
#
# maps.mail.ru runs a working world instance and answered Norway correctly.
# It is not in this list because using it would send the shape of somebody's
# journey to a Russian server, and that is the user's call to make, not this
# file's.
OVERPASS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]


def overpass(query, tries=2, corroborate_empty=True):
    """Ask whichever public Overpass instance answers first.

    The main instance rate-limits, and on a five hundred mile corridor it
    returns an empty body and keeps returning one. A single endpoint with a
    long backoff turns that into a run that sits there for four minutes
    saying nothing useful, so this walks a list of mirrors and gives up
    quickly on each. Raises only when every one of them has refused.

    Zero results is the dangerous answer and gets treated as one. A timed-out
    query comes back 200 with an empty element list and the reason tucked in
    "remark"; a regional instance comes back 200 with an empty element list
    and nothing at all. Either way the tool reads it as a plain statement
    that there is no fuel on this road, which is the one wrong answer that
    leaves somebody stood on a verge. So an empty answer has to be said twice
    by two different servers before it counts. One mirror saying nothing, on
    its own, is a failure and raises.
    """
    last, empties = None, 0
    for base in OVERPASS:
        try:
            res = get(base + "?" + urllib.parse.urlencode({"data": query}),
                      tries=tries, pause=3)
            remark = res.get("remark") or ""
            if remark and not res.get("elements"):
                raise RuntimeError(f"said: {remark[:90]}")
            if corroborate_empty and not res.get("elements"):
                empties += 1
                if empties < 2:
                    raise RuntimeError("answered empty, asking somebody else")
            return res
        except Exception as e:
            last = e
            print(f"  {urllib.parse.urlparse(base).netloc} {str(e)[:48]}, trying another",
                  file=sys.stderr)
    if empties and empties < 2:
        raise RuntimeError(
            "only one Overpass mirror answered and it found nothing. That is "
            "not the same as there being nothing: treat fuel as unknown here.")
    raise RuntimeError(f"every Overpass mirror refused: {last}")


# ----------------------------------------------------------------- geo

OSRM_BASE = "https://router.project-osrm.org"   # overridden by --osrm
GH_BASE = None                                  # set by --gh, routes locally

# A1, M6, A74(M), N154, D920, R0, S1, E40, AP7, SS1. The old pattern allowed
# A, M and B only, so every road number outside Britain was dropped before
# the closure matcher ever saw it, and a French or Irish route matched nothing.
ROAD_RE = r"[A-Z]{1,2}\d{1,4}(?:\(M\))?"

# The island of Ireland. A rectangle around Great Britain cannot avoid it:
# England's box reaches to 6.5W, which contains Dublin, and Scotland's reaches
# to 8.7W, which contains all of it. So a Dublin to Belfast run collected
# English A1 and M1 roadworks, correctly matched on the number and nine
# hundred miles away. The British providers carry this as an exclusion.
IRELAND = (51.3, 55.45, -10.7, -5.35)


def _inside(box, lat, lon):
    la0, la1, lo0, lo1 = box
    return la0 <= lat <= la1 and lo0 <= lon <= lo1
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
        # Anything shaped like a postcode stops here. It used to fall through
        # to the place-name search, where Nominatim matched the non-existent
        # G1 1AA to a street in Hanoi and the tool planned 7,700 miles from
        # Glasgow to Thurso without a word of complaint. One mistyped letter
        # should never return a confident answer on the wrong continent.
        raise SystemExit(
            f"'{s.upper()}' looks like a UK postcode and is not a real one. "
            f"Check it, or give coordinates as lat,lon.")

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


def route_graphhopper(a, b, gh_url):
    """Base routing against a GraphHopper on this machine.

    Without this the only engine was OSRM, so anybody who took the Java
    route got local rerouting and still sent the journey itself to a public
    server. The setup script promises journeys never leave the machine, and
    on the Java path that promise was not kept.
    """
    url = (f"{gh_url.rstrip('/')}/route?point={a['lat']},{a['lon']}"
           f"&point={b['lat']},{b['lon']}&profile=car&points_encoded=false"
           f"&instructions=false&details=street_ref&ch.disable=true")
    res = get(url, tries=2)
    paths = res.get("paths") or []
    if not paths:
        raise SystemExit(f"Local routing failed: {res.get('message', 'no path found')}")
    pth = paths[0]
    pts = pth["points"]["coordinates"]                    # [lon, lat]
    coords = [(c[1], c[0]) for c in pts]
    roads, idx = set(), {}
    for lo, hi, ref in (pth.get("details") or {}).get("street_ref", []):
        for one in re.split(r"[;,/ ]+", (ref or "").upper()):
            if not re.fullmatch(ROAD_RE, one):
                continue
            roads.add(one)
            seg = [[round(c[1], 5), round(c[0], 5)] for c in pts[lo:hi + 1:3]]
            if seg:
                idx.setdefault(one, []).extend(seg)
    return {"miles": pth["distance"] * MILES_PER_METRE,
            "hours": pth["time"] / 3600000.0,
            "coords": coords, "roads": roads, "road_points": idx}


def route(a, b, offline=False):
    ck = CACHE / f"route_{a['lat']:.4f}_{a['lon']:.4f}_{b['lat']:.4f}_{b['lon']:.4f}.json"
    if ck.exists():
        d = json.loads(ck.read_text())
        d["roads"] = set(d["roads"])
        d["cached"] = True
        return d
    local_gh = GH_BASE and ("localhost" in GH_BASE or "127.0.0.1" in GH_BASE)
    local_osrm = "localhost" in OSRM_BASE or "127.0.0.1" in OSRM_BASE
    if GH_BASE and not local_osrm:
        out = route_graphhopper(a, b, GH_BASE)
        ck.write_text(json.dumps({**out, "roads": sorted(out["roads"])}))
        return out
    if offline and not (local_osrm or local_gh):
        raise SystemExit("Route not cached and --offline is set. Run once online, "
                         "or point --osrm or --gh at a server on this machine.")
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
                if re.fullmatch(ROAD_RE, ref):
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
                if re.fullmatch(ROAD_RE, ref):
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

FUEL_CACHE_DAYS = 7


def fuel_near_route(coords, radius_m=3000):
    """Overpass 'around' query along a thinned version of the route polyline."""
    step = max(1, len(coords) // 90)
    thin = coords[::step]
    poly = ",".join(f"{lat:.5f},{lon:.5f}" for lat, lon in thin)
    # hashlib, because Python randomises str.__hash__ per process and a cache
    # key that changes every run is not a cache
    cache_key = CACHE / f"fuel_{hashlib.sha1(poly.encode()).hexdigest()[:16]}.json"
    if cache_key.exists() and (time.time() - cache_key.stat().st_mtime) < FUEL_CACHE_DAYS * 86400:
        print("  (fuel data from local cache)", file=sys.stderr)
        return json.loads(cache_key.read_text())

    q = (f'[out:json][timeout:60];'
         f'(node["amenity"="fuel"](around:{radius_m},{poly});'
         f' way["amenity"="fuel"](around:{radius_m},{poly}););'
         f'out center tags;')
    res = overpass(q)

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
    # Never write an empty answer to disk. Overpass times out often enough
    # on a long corridor, and a cached [] is a permanent, confident lie
    # about where the fuel is. An empty result is a failed lookup until a
    # mirror proves otherwise.
    if stations:
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
    seed = str(route_coords[::40]).encode()
    key = CACHE / f"junc_{hashlib.sha1(seed).hexdigest()[:16]}.json"
    if key.exists():
        return {tuple(k.split("|")): tuple(v) for k, v in json.loads(key.read_text()).items()}

    step = max(1, len(route_coords) // 70)
    poly = ",".join(f"{a:.4f},{b:.4f}" for a, b in route_coords[::step])
    q = (f'[out:json][timeout:60];node["highway"="motorway_junction"]'
         f'["ref"](around:2500,{poly});out body;')
    try:
        res = overpass(q)
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
    ("minor roadworks", 3), ("lane restriction", 3), ("lorry restriction", 3),
    ("lane closure", 2), ("lane closed", 2), ("ramp closed", 2),
    ("ramp closure", 2), ("slip road closed", 2), ("footway closure", 3),
    ("footway closed", 3), ("lanes closed", 2),
    ("intermittent short term closures", 2),
    ("road closure", 0), ("roadclosed", 0), ("carriagewayclosed", 0),
    ("closure", 0), ("closed", 0),
    ("accident", 1), ("collision", 1), ("obstruction", 1), ("vehicle fire", 1),
    ("roadwork", 1), ("maintenancework", 1), ("constructionwork", 1),
    ("maintenance", 1), ("repair", 1),
    ("contraflow", 2), ("flooding", 2), ("hazard", 2),
    ("peopleonroadway", 2), ("disturbance", 2), ("demonstration", 2),
    ("incident", 2), ("publicevent", 3), ("transitinformation", 3),
    ("reroutingmanagement", 3), ("authorityoperation", 2), ("weather", 2),
    ("equipmentorsystemfault", 3), ("signalfault", 3),
    ("abnormaltraffic", 3), ("queue", 3), ("congestion", 3), ("delay", 3),
]


def clip(s, n):
    """Truncate on a word boundary and say so. A hard slice cut "3rd of
    August 2026" into "3rd of August 20", which reads as a broken date
    rather than a shortened one, in the first block of the README."""
    s = (s or "").strip()
    if len(s) <= n:
        return s
    cut = s[:n].rsplit(" ", 1)[0]
    return (cut or s[:n]) + "..."


def _rank(kind):
    """Feeds write the same thing four ways: "lane closure", "laneClosures",
    "LaneClosed", "lane_closure". Matching on the spaced form alone means
    "laneClosures" misses "lane closure", falls through to "closure", and a
    coned-off lane is reported as a shut motorway. So spaces come out of both
    sides before comparing. Four countries have been ranked wrong by this."""
    k = re.sub(r"[^a-z]", "", (kind or "").lower())
    for word, n in SEVERITY:
        if word.replace(" ", "") in k:
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


# ----------------------------------------------------------------- provider health

HEALTH = CACHE / "provider_health.json"


def _health_load():
    try:
        return json.loads(HEALTH.read_text())
    except Exception:
        return {}


def _health_record(name, count):
    """Remember what each provider normally returns.

    A scraped page that gets redesigned does not raise. It parses to nothing
    and the tool reports no incidents, which on a safety tool is worse than
    crashing. This keeps a rolling record of normal and complains when a
    provider falls off a cliff.

    Returns a warning string, or None.
    """
    h = _health_load()
    rec = h.get(name) or {"best": 0, "runs": 0, "last": None}
    warn = None
    if count == 0 and rec["best"] >= 3:
        warn = (f"{name} returned nothing. It normally gives about {rec['best']}. "
                f"Treat 'no incidents' from it as unknown until checked.")
    elif rec["best"] >= 20 and count < rec["best"] * 0.15:
        warn = (f"{name} returned {count}, far below its usual {rec['best']}. "
                f"The source may have changed.")
    rec["best"] = max(rec["best"], count) if count else rec["best"]
    rec["last"] = count
    rec["runs"] = rec.get("runs", 0) + 1
    h[name] = rec
    try:
        HEALTH.write_text(json.dumps(h, indent=1))
    except Exception:
        pass
    return warn


def provider_health():
    """What each provider has been doing lately."""
    return _health_load()



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
    cmd = ["curl", "-sL", "--max-time", "45", "-A", BROWSER_UA]
    if gzip_required:
        cmd.append("--compressed")
    for k, v in (headers or {}).items():
        cmd += ["-H", f"{k}: {v}"]
    xml = subprocess.run(cmd + [url], capture_output=True, text=True, timeout=70).stdout
    if "<" not in xml:
        raise RuntimeError(f"{name}: no XML returned ({xml[:60].strip()})")
    return datex2_parse(name, xml)


def datex2_parse(name, xml):
    """The DATEX II half of p_datex2, split out because not every publisher
    serves one file. France serves one file per event, so it fetches many
    and parses each with this."""
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
        # Several publishers put the only severity information in the XSI
        # type attribute, not in a child element: xsi:type="sit:MaintenanceWorks".
        # Reading the children alone ranks their whole feed as unknown.
        xsi = sit.get("{http://www.w3.org/2001/XMLSchema-instance}type", "")
        kind = (first(sit, "roadOrCarriagewayOrLaneManagementType", "obstructionType",
                      "accidentType", "environmentalObstructionType",
                      "networkManagementType", "generalNetworkManagementType")
                or xsi.split(":")[-1] or "incident")
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
        # Rank again from the stored type rather than trusting the stored
        # rank. Severity is a judgement this tool keeps improving, and a
        # cached file written an hour ago would otherwise pin yesterday's
        # judgement to 8,577 Dutch records until it expired.
        return [{**d, "roads": set(d.get("roads") or []), "rank": _rank(d.get("type"))}
                for d in json.loads(parsed.read_text())]

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

# ----------------------------------------------------------------- Germany

AUTOBAHN = "https://verkehr.autobahn.de/o/autobahn"


def p_autobahn(route_roads):
    """Germany publishes per road, not per country.

    There is no national file to download. You ask for A7 and you get A7.
    Seeing the whole network means about 120 requests, which nobody should
    make, and seeing the roads you are actually driving means two. So this
    provider gets handed the route's own road numbers and asks about those.

    The records carry a road number, a coordinate and a future flag, so the
    scheduled-works problem that bit the Dutch feed is answered in the data.
    """
    from concurrent.futures import ThreadPoolExecutor
    want = sorted(r for r in route_roads if re.fullmatch(r"A\d{1,3}", r))[:10]
    if not want:
        return []

    jobs = [(r, k) for r in want for k in ("closure", "roadworks", "warning")]

    def fetch(job):
        road, kind = job
        try:
            return road, kind, (get(f"{AUTOBAHN}/{road}/services/{kind}", tries=2) or {})
        except Exception:
            return road, kind, {}

    out = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        for road, kind, d in pool.map(fetch, jobs):
            for r in d.get(kind, []) or []:
                if r.get("future"):           # scheduled, not happening yet
                    continue
                # impact.symbols is a lane diagram, not a severity flag: it
                # reads CLOSED once per closed lane, so a line-painting job
                # with one lane coned off carries the same word as a shut
                # motorway. isBlocked is the only field that means shut.
                sym = (r.get("impact") or {}).get("symbols") or []
                blocked = str(r.get("isBlocked", "")).lower() == "true"
                takes_lanes = "CLOSED" in sym
                if blocked:
                    typ = "road closure"
                elif kind == "closure":
                    typ = "lane closure"
                elif kind == "roadworks":
                    typ = "roadworks" if takes_lanes else "minor roadworks"
                else:
                    typ = "hazard"
                c = r.get("coordinate") or {}
                try:
                    lat, lon = float(c.get("lat")), float(c.get("long"))
                except (TypeError, ValueError):
                    lat = lon = None
                desc = " ".join(x for x in (r.get("description") or []) if x)
                out.append(_norm("Germany", r.get("title") or road, typ,
                                 direction=(r.get("subtitle") or "").strip(),
                                 desc=desc[:300], roads={road}, lat=lat, lon=lon))
    return out


# ----------------------------------------------------------------- Belgium

def p_verkeerscentrum():
    """The Flemish traffic centre's DATEX II endpoint is dead and its map is
    a single-page app. Their textual situation page is neither, and it names
    the type in an icon filename and the road in its own field, so it parses
    without guessing. Flanders only: Wallonia and Brussels run separately.
    """
    x = get_text("https://www.verkeerscentrum.be/tekstuele-verkeerssituatie")
    if "event-tcc" not in (x or ""):
        return []
    kinds = {"ongeval": "accident", "file": "queue", "wegenwerken": "roadworks",
             "afgesloten": "road closure", "gesloten": "road closure",
             "hinder": "obstruction", "incident": "incident",
             "wind": "weather", "sneeuw": "weather", "mist": "weather"}
    out = []
    for art in re.split(r"<article class=\"node node--event-tcc", x)[1:]:
        icon = (re.search(r"/icons/([A-Za-z_]+?)_?\d*\.svg", art) or [None, ""])[1].lower()
        road = (re.search(r'field--field-tcc-road-name.*?<div class="item">(.*?)</div>', art, re.S)
                or [None, ""])[1]
        title = (re.search(r"<h2>\s*(.*?)\s*</h2>", art, re.S) or [None, ""])[1]
        title = unescape(re.sub(r"<[^>]+>", " ", title)).strip()
        if not title:
            continue
        typ = kinds.get(icon) or next((v for k, v in kinds.items() if k in title.lower()), "incident")
        # Every authority separates a closed lane from a closed road, and
        # every one of them does it in its own language. "Een rijstrook
        # afgesloten" is one lane coned off. "Weg afgesloten" is the road.
        # Reading the shared word and stopping there shuts the Brussels ring.
        low = title.lower()
        if "weg afgesloten" in low:
            typ = "road closure"
        elif "rijstrook" in low or "rijstroken" in low:
            typ = "lane closure"
        elif "oprit afgesloten" in low or "afrit afgesloten" in low:
            typ = "ramp closed"
        elif "afgesloten" in low or "gesloten" in low:
            typ = "road closure"
        roads = set(re.findall(r"\b([ARNE]\d{1,3})\b", (unescape(road) + " " + title).upper()))
        out.append(_norm("Belgium", title, typ, desc=unescape(road).strip(), roads=roads))
    return out


# ----------------------------------------------------------------- France

BISON = "https://tipi.bison-fute.gouv.fr/bison-fute-ouvert/publicationsDIR/Evenementiel-DIR/grt/RRN"


def p_bison_fute(limit=80):
    """France publishes one SOAP-wrapped DATEX II file per event, about 1,600
    of them in an Apache directory, with no combined feed. Downloading the lot
    is 8 MB and 1,600 requests, so this reads the index newest-first and takes
    the freshest few dozen. Older files are long-running works that have not
    been touched in months.
    """
    from concurrent.futures import ThreadPoolExecutor
    index = get_text(f"{BISON}/?C=M;O=D")
    names = re.findall(r'href="(\d+\.xml)"', index or "")
    if not names:
        return []

    def one(n):
        try:
            return datex2_parse("France", get_text(f"{BISON}/{n}"))
        except Exception:
            return []

    out = []
    with ThreadPoolExecutor(max_workers=12) as pool:
        for got in pool.map(one, names[:limit]):
            out += got
    return out


# ----------------------------------------------------------------- Austria

def p_asfinag():
    """ASFINAG's content portal needs an account. Their public RSS does not,
    and it carries the messages that matter: closures, tunnel closures, ramp
    closures. Six to twenty entries, already filtered, no noise to wade through.
    """
    x = get_text("https://publiccontent.asfinag.at/rss/feed/de/trafficmessages")
    if "<entry>" not in (x or ""):
        return []
    kinds = {"tunnelsperre": "road closure", "rampensperre": "lane closure",
             "sperre": "road closure", "gesperrt": "road closure",
             "baustelle": "roadworks", "unfall": "accident", "stau": "queue",
             "behinderung": "obstruction", "gefahr": "hazard",
             "routenempfehlung": "delay", "schnee": "weather", "glatt": "weather"}
    out = []
    for e in re.findall(r"<entry>(.*?)</entry>", x, re.S):
        pick = lambda tag: unescape((re.search(rf"<{tag}>(.*?)</{tag}>", e, re.S) or [None, ""])[1]).strip()
        title, body = pick("title"), pick("content")
        low = (title + " " + body).lower()
        typ = next((v for k, v in kinds.items() if k in low), "incident")
        # A8, S1 (Schnellstrasse), B17. The shared regex misses the S roads.
        roads = set(re.findall(r"\b([AS]\d{1,3})\b", body.upper())) | \
                set(re.findall(r"\bB\d{1,3}\b", body.upper()))
        out.append(_norm("Austria", f"{title}: {body[:70]}", typ,
                         desc=body[:300], roads=roads, since=pick("published")[:10]))
    return out


# ----------------------------------------------------------------- Slovenia

def p_promet_si():
    """DARS publish an Atom feed in Slovenian. Road numbers, no coordinates."""
    x = get_text("https://www.promet.si/dc/b2b.dogodki.rss")
    if not x:
        return []
    # DARS categorise every entry, and the category is far more reliable than
    # the prose. Almost every roadworks summary contains the word "zapora",
    # because coning off one lane is a zapora too. Reading closure out of the
    # text alone ranks the whole country as shut.
    cats = {"delo": "roadworks", "ovir": "obstruction", "nesre": "accident",
            "zasto": "queue", "vreme": "weather", "tovornjak": "lorry restriction",
            "mejni": "border delay"}
    out = []
    for e in re.findall(r"<entry>(.*?)</entry>", x, re.S):
        pick = lambda tag: (re.search(rf"<{tag}>(.*?)</{tag}>", e, re.S) or [None, ""])[1]
        title = unescape(pick("title")).strip()
        summary = unescape(pick("summary")).strip()
        cat = unescape((re.search(r'category term="([^"]*)"', e) or [None, ""])[1]).lower()
        blob = (title + " " + summary).lower()
        typ = next((v for k, v in cats.items() if k in cat), "incident")
        if "popolna zapora" in blob or "cesta je zaprta" in blob:
            typ = "road closure"          # popolna: complete, as opposed to delna
        elif typ == "roadworks" and "zaprt" in blob:
            typ = "lane closure"
        # A1, H4, R2-430, G1-4: take the leading code, drop the section number
        roads = {m.group(1) for m in re.finditer(r"\b([AHRG]\d{1,3})(?:-\d+)?\b", title.upper())}
        out.append(_norm("Slovenia", title, typ, desc=summary, roads=roads))
    return out


# ----------------------------------------------------------------- Norway

NO_TRAFFIC = "https://traffic-info.atlas.vegvesen.no/traffic-information/messages"


def p_vegvesen():
    """Norway's DATEX II feed wants an account. Their own traffic map does not.

    The map talks to a service that publishes its OpenAPI spec openly at
    /v3/api-docs, and the spec says the thing out loud: for internal use,
    changes may occur at any time. So treat it the way the Irish GraphQL is
    treated. It works today, it is nobody's promise, and the registry entry
    says so.

    Two traps in the data. 335 of 1,143 messages are scheduled for a date
    that has not arrived and another 334 have finished, so the feed has to
    be filtered to activityStatus before anything else: that is the Dutch
    bug wearing a Norwegian hat. And "intermittentShortTermClosures" is a
    road shut for ten minutes at a time while they blast, not a road shut.
    It reads as a closure to any matcher that looks for the word.
    """
    body = get_text(NO_TRAFFIC + "?lang=en",
                    headers={"X-System-ID": "Cairn",
                             "Accept": "application/vnd.svv.v2+json;charset=utf-8"})
    try:
        msgs = json.loads(body).get("trafficMessages") or []
    except Exception:
        return []

    shut = {"roadClosed", "roadClosedForWinter"}
    out = []
    for m in msgs:
        if m.get("activityStatus") not in ("active", "overdue"):
            continue
        loc = m.get("location") or {}
        regs = {r.get("type") for r in (m.get("trafficRegulations") or [])}
        ev = (m.get("trafficEvent") or {}).get("trafficEventType") or ""
        impact = m.get("trafficImpact") or ""

        if m.get("trafficManagement") in shut or regs & shut:
            typ = "road closure"
        elif regs & {"intermittentShortTermClosures", "delaysIntermittentShortTermClosures"}:
            typ = "intermittent short term closures"
        elif regs & {"oneLaneClosures", "moreLaneClosures"}:
            typ = "lane closure"
        elif "contraflow" in regs:
            typ = "contraflow"
        elif ev and ev not in ("roadworks", "mobileRoadworks", "resurfacingWork"):
            typ = ev
        elif impact in ("none", "small"):
            # Their own judgement of how much this costs you. A resurfacing
            # job with traffic lights is not an accident, and ranking all
            # 1,037 roadworks alike buries the ones that are.
            typ = "minor roadworks"
        else:
            typ = "roadworks"

        # The feed writes county and national roads as F773 and R92. OSM,
        # and so the router, writes Fv773 and Rv92. Same road, and a number
        # match would quietly never fire. Carry both spellings.
        roads = set()
        for r in loc.get("roads") or []:
            num, cat = (r.get("number") or "").upper(), (r.get("category") or "").upper()
            if not re.fullmatch(r"[A-Z]{1,2}\d{1,4}", num):
                continue
            roads.add(num)
            if cat in ("F", "R", "K") and num.startswith(cat):
                roads.add(cat + "V" + num[1:])

        pos = ((m.get("icon") or {}).get("position") or {}).get("coordinates") or []
        lat = lon = None
        if len(pos) == 2:
            lon, lat = float(pos[0]), float(pos[1])

        d = m.get("locationDescriptionDetails") or {}
        title = d.get("simpleLocationDescription") or (m.get("trafficEvent") or {}).get(
            "trafficEventDescription") or typ
        desc = (m.get("trafficStatus") or {}).get("description") or ""
        if m.get("activityStatus") == "overdue":
            desc = (desc + ". Past its stated end time and still listed").strip(". ")
        out.append(_norm("Norway", title[:70], typ, desc=desc[:200],
                         since=(m.get("startTime") or "")[:16],
                         roads=roads, lat=lat, lon=lon))
    return out


# ----------------------------------------------------------------- Croatia

def p_hak():
    """HAK publish the state of the roads as a plain page, in Croatian.

    No access point, no key, no JSON. A bold heading names the road and the
    list items under it are its incidents, so the road carries down the page
    until the next heading replaces it.

    Worth the trouble for one thing nobody else gives: HAK print their own
    recommended diversion, as "Obilazak: cvor Bisko (A1)-DC220-ZC6260-cvor
    Split (A1)". That is the road authority's answer to the question this
    tool spent the morning computing, written out by the people who closed
    the road.
    """
    x = get_text("https://www.hak.hr/info/stanje-na-cestama")
    if "<li>" not in (x or ""):
        return []

    # "zatvoren" is the word for shut, and it is the word for a shut lane
    # too. Croatian makes the distinction with "trak", the lane. Reading the
    # closure word alone and stopping there shuts the whole A1.
    def kind(s):
        s = s.lower()
        scheduled = bool(re.search(r"zatvarat ce se|zatvarat \u0107e se", s)) or \
                    bool(re.match(r"\s*\d{1,2}\.\s*/\s*\d{1,2}\.", s))
        if "nesre" in s:
            return "accident", scheduled
        if "odron" in s:
            return "obstruction", scheduled
        if any(w in s for w in ("bura", "snijeg", "poledic", "magla", "vjetar")):
            return "weather", scheduled
        if "zatvor" in s or "zabran" in s:
            if "trak" in s or "traka" in s:
                return "lane closure", scheduled
            if "dionica" in s or "cesta" in s or "most" in s or "tunel" in s:
                return "road closure", scheduled
            return "lane closure", scheduled
        if "radov" in s:
            return "roadworks", scheduled
        if "usporen" in s or "koloni" in s or "zasto" in s:
            return "queue", scheduled
        if "pojacan" in s or "poja\u010dan" in s:
            return "congestion", scheduled
        return "incident", scheduled

    out, road = [], ""
    # bold headings and motorway spans both set the road for what follows
    for m in re.finditer(r'<span class="motorway">(.*?)</span>|<b>(.*?)</b>|<li>(.*?)</li>', x, re.S):
        span, bold, item = m.group(1), m.group(2), m.group(3)
        if span or bold:
            head = unescape(re.sub(r"<[^>]+>", " ", span or bold)).strip()
            found = re.findall(r"\b((?:DC|ZC|A|D)\s?\d{1,4})\b", head.upper().replace("\u017d", "Z"))
            if found:
                road = re.sub(r"\s", "", found[0])
            continue
        if not item:
            continue
        txt = unescape(re.sub(r"<[^>]+>", " ", item))
        txt = re.sub(r"\s+", " ", txt).strip()
        if len(txt) < 12:
            continue
        low = txt.lower()
        # HAK announce reopenings on the same page, in the same shape, and
        # one of them carried a leftover diversion and read as a closure.
        if re.match(r"\s*otvoren", low):
            continue
        typ, scheduled = kind(txt)
        own = re.findall(r"\b((?:DC|A|D)\s?\d{1,3})\b", txt.upper())
        # The page's own navigation is list items too: "Prohodnost cesta",
        # "Vremenski uvjeti", "Ostali dogadaji". They carry no road, no date
        # and no incident word, and 141 of them were being filed as incidents.
        if typ == "incident" and not own and not re.search(r"\d{1,2}[.:]", txt):
            continue
        if scheduled:
            typ = "roadworks" if typ in ("road closure", "lane closure") else typ
        roads = {road} if road else set()
        roads |= {re.sub(r"\s", "", r) for r in
                  re.findall(r"\b((?:DC|A|D)\s?\d{1,3})\b", txt.upper())}
        detour = (re.search(r"Obilazak:\s*([^.]{0,160})", txt) or [None, ""])[1].strip()
        out.append(_norm("Croatia", f"{road + ': ' if road else ''}{txt[:90]}", typ,
                         desc=(f"Diversion: {detour}. " if detour else "") + txt[:260],
                         roads={r for r in roads if r}))
    return out


# ----------------------------------------------------------------- Denmark, and Sweden through it

DK_GEOJSON = "https://storage.googleapis.com/trafikkort-data/geojson"

DK_LAYERS = ["current-roadwork", "current-blocking-roadwork", "current-events",
             "current-blocking-events", "current-other-traffic-announcements",
             "current-strong-wind-traffic-announcements"]


def _dk_layer(name, source):
    """One Danish map layer. They publish every layer twice, .point and
    .line, with the same features in both, so only the points are read."""
    import xml.sax.saxutils as sx
    try:
        d = json.loads(get_text(f"{DK_GEOJSON}/{name}.point.json"))
    except Exception:
        return []
    out = []
    for f in d.get("features") or []:
        pr = f.get("properties") or {}
        if pr.get("future") == "true" or pr.get("suspended") == "true":
            continue
        desc = unescape(re.sub(r"<[^>]+>", " ", pr.get("description") or ""))
        desc = re.sub(r"\s+", " ", unescape(desc)).strip()
        title = unescape(pr.get("title") or "")
        low = (title + " " + desc).lower()
        klass = (pr.get("TrafficMan2_Type") or "").rsplit(".", 1)[-1]

        # Danish separates the two the same way English does and this tool
        # has now got it wrong in five languages. "Vejen er spærret" is the
        # road. "Spor spærret" is one lane of it, coned off, with the rest
        # of the carriageway running.
        # The Swedish layer the Danes relay keeps its Danish title and its
        # Swedish body text, so both get read and both languages are listed.
        if "körfält avstäng" in low or "spor spærret" in low or "farbart spor" in low:
            typ = "lane closure"
        elif ("blocking" in name or "vejen er spærret" in low or "vej spærret" in low
                or "vägen avstängd" in low or "väg avstängd" in low):
            typ = "road closure"
        elif klass == "Accident" or "uheld" in low or "olycka" in low:
            typ = "accident"
        elif klass == "PoorEnvironmentConditions" or "blæst" in low:
            typ = "weather"
        elif klass == "AnimalPresenceObstruction" or "dyr på vejen" in low:
            typ = "obstruction"
        elif klass == "MaintenanceWorks" or "vejarbejde" in low or "vägarbete" in low \
                or "arbete" in low:
            typ = "roadworks"
        else:
            typ = "incident"

        g = f.get("geometry") or {}
        c = g.get("coordinates") or []
        lat = lon = None
        if g.get("type") == "Point" and len(c) == 2:
            lon, lat = float(c[0]), float(c[1])

        # The Swedish layer has no header and its title is the Danish word
        # for roadworks, so a closure on the E4 printed as "Vejarbejde i
        # gang" and told you nothing about where it was. The description
        # opens with the road and the two places it runs between.
        head = unescape(pr.get("header") or "") or desc[:70] or title or typ
        out.append(_norm(source, head[:70], typ, pr.get("direction") or "",
                         desc[:200], (pr.get("beginPeriod") or "")[:16],
                         None, lat, lon))
    return out


def p_vejdirektoratet():
    """Denmark's own traffic map reads flat GeoJSON off a public bucket.

    No key, no registration, no account. The bucket refuses a listing, so
    the layer names come from their map's own bundle. Every layer is
    published twice, as points and as lines, carrying the same features.
    """
    out = []
    for name in DK_LAYERS:
        out += _dk_layer(name, "Denmark")
    return out


def p_swedish_via_denmark():
    """Sweden's own feed, Trafikverket, wants an account, and this tool does
    not make accounts. The Danes publish the Swedish roadworks nearest them
    on their own map, so this is the Oresund corner of Skane and nothing
    else: roadworks only, no accidents, no live closures. It is better than
    a blank map on the drive from Copenhagen to Malmo, and worse than
    Sweden thinking it has coverage, so the registry note says both.
    """
    return _dk_layer("current-swedish-roadworks", "Sweden (relayed by Denmark)")


# ----------------------------------------------------------------- Italy, closures

ASPI = "https://viabilita.autostrade.it/json"


def p_autostrade():
    """Autostrade per l'Italia publish three flat JSON files, no key.

    The ministry's own host will not take a connection from here, and the
    national access point is a registration wall, so this is the motorway
    operator's own map data. Coverage is their network, roughly 3,000 km
    of Italy's 7,000. Autovie Venete, Milano Serravalle, CAV and every
    ANAS state road are not in here, and the registry note says so.

    Two filters worth keeping. 22 of the 66 live events on a quiet Sunday
    were "Camper Service chiuso", a motorhome waste point out of order at
    a service area, which is not a road condition and has no business in a
    list a driver reads at speed. And the roadworks file carries works
    that start next month, so it is filtered on its own timestamps.
    """
    now_ms = time.time() * 1000
    out = []

    def pull(name):
        try:
            d = json.loads(get_text(f"{ASPI}/{name}.json"))
        except Exception:
            return []
        if isinstance(d, list):
            return d
        return next((v for v in d.values() if isinstance(v, list)), [])

    def road_of(r):
        c = (r.get("c_str") or "").upper()
        return {c} if re.fullmatch(r"[A-Z]{1,2}\d{1,3}", c) else set()

    for r in pull("incidenti"):
        lat, lon = r.get("n_crd_lat_ini"), r.get("n_crd_lon_ini")
        out.append(_norm("Italy", (r.get("t_des_it") or "Incidente")[:70], "accident",
                         r.get("t_des_dir_it") or "", "", "", road_of(r),
                         float(lat) if lat else None, float(lon) if lon else None))

    for r in pull("eventi"):
        fra = (r.get("t_des_fra_it") or "").strip()
        low = fra.lower()
        if "camper service" in low:
            continue
        if "chiusura pesanti" in low:
            typ = "lorry restriction"
        elif "mancanza di" in low:
            typ = "fuel shortage at services"
        elif any(w in low for w in ("entrata chiusa", "uscita chiusa", "allacciamento chiuso")):
            typ = "ramp closure"
        elif "chiusa" in low or "chiuso" in low:
            typ = "road closure"
        elif "coda" in low or "rallentato" in low:
            typ = "queue"
        elif any(w in low for w in ("pioggia", "neve", "nebbia", "vento", "ghiaccio")):
            typ = "weather"
        else:
            typ = "incident"
        lat, lon = r.get("n_crd_lat_ini"), r.get("n_crd_lon_ini")
        title = (r.get("t_des_en") or r.get("t_des_it") or fra)[:70]
        out.append(_norm("Italy", title, typ, r.get("t_des_dir_it") or "",
                         fra, "", road_of(r),
                         float(lat) if lat else None, float(lon) if lon else None))

    for r in pull("cantieri"):
        start, end = r.get("d_ora_nst"), r.get("d_ora_rmz_pre")
        if start and start > now_ms:
            continue                      # starts next month
        if end and end < now_ms:
            continue                      # finished
        sgn = (r.get("t_des_sgn") or "").strip()
        low = sgn.lower()
        if "corsia unica" in low or "1 corsia" in low:
            typ = "lane closure"
        elif "riduzione" in low or "flesso" in low or "deviazione" in low:
            typ = "roadworks"
        else:
            typ = "minor roadworks"
        lat, lon = r.get("n_lat"), r.get("n_lon")
        where = (r.get("t_des_trt") or r.get("t_des") or "").strip().title()
        out.append(_norm("Italy", f"{(r.get('c_str') or '').upper()} {where}"[:70], typ,
                         r.get("t_des_dir") or "", sgn, "", road_of(r),
                         float(lat) if lat else None, float(lon) if lon else None))
    return out


PROVIDERS = [
    {"name": "Traffic Scotland", "not_box": IRELAND, "box": (54.6, 61.0, -8.7, -0.7), "match": "roads",
     "fn": p_traffic_scotland, "note": "scraped public page"},

    {"name": "National Highways", "not_box": IRELAND, "box": (49.8, 56.0, -6.5, 2.0), "match": "roads",
     "fn": p_national_highways, "note": "their own JSON API, no key, paginated"},

    {"name": "Traffic Wales", "not_box": IRELAND, "box": (51.3, 53.5, -5.4, -2.6), "match": "roads",
     "fn": p_traffic_wales, "note": "scraped public page"},

    {"name": "TrafficwatchNI", "box": (54.0, 55.4, -8.2, -5.4), "match": "roads",
     "fn": p_trafficwatch_ni, "note": "scraped roadworks table"},

    {"name": "TII Ireland", "box": (51.3, 55.5, -10.7, -5.8), "match": "roads",
     "fn": p_tii_ireland, "note": "GraphQL behind their public map, undocumented"},

    {"name": "Germany", "box": (47.2, 55.1, 5.8, 15.1), "match": "roads",
     "per_road": True, "fn": p_autobahn,
     "note": "autobahn.de, queried per road so only your own motorways are fetched"},

    {"name": "Belgium", "box": (49.4, 51.6, 2.5, 5.95), "match": "roads",
     "fn": p_verkeerscentrum, "note": "Flemish traffic centre, textual page, Flanders only"},

    {"name": "France", "box": (41.3, 51.1, -5.2, 9.6), "match": "coords",
     "fn": p_bison_fute,
     "note": "Bison Fute, one file per event, freshest 80 read each run"},

    {"name": "Austria", "box": (46.3, 49.1, 9.5, 17.2), "match": "roads",
     "fn": p_asfinag, "note": "ASFINAG public RSS, already filtered to what matters"},

    {"name": "Luxembourg", "box": (49.4, 50.2, 5.7, 6.6), "match": "coords",
     "fn": lambda: p_datex2("Luxembourg",
         "https://cita.lu/info_trafic/datex/situationrecord36"),
     "note": "CITA, DATEX II v3.6, CC0"},

    {"name": "Croatia", "box": (42.3, 46.6, 13.4, 19.5), "match": "roads",
     "fn": p_hak, "note": "HAK road status page, in Croatian, with their own diversions"},

    {"name": "Slovenia", "box": (45.4, 46.9, 13.3, 16.6), "match": "roads",
     "fn": p_promet_si, "note": "DARS Atom feed, road numbers only, no coordinates"},

    {"name": "Spain", "box": (35.9, 43.9, -9.4, 4.4), "match": "roads",
     "fn": lambda: p_datex2("Spain",
         "https://infocar.dgt.es/datex2/sct/SituationPublication/all/content.xml"),
     "note": "DGT open DATEX II, no key"},

    {"name": "Italy", "box": (35.4, 47.1, 6.6, 18.6), "match": "coords",
     "fn": p_autostrade,
     "note": "Autostrade per l'Italia's own map files. Their network only, "
             "so no ANAS state roads and no other concessionaire"},

    {"name": "Netherlands", "box": (50.7, 53.6, 3.3, 7.3), "match": "coords",
     "fn": lambda: p_datex2_bulk("Netherlands",
         "https://opendata.ndw.nu/planningsfeed_wegwerkzaamheden_en_evenementen.xml.gz"),
     "note": "NDW open data, no key, 60k records streamed and filtered"},

    {"name": "Norway", "box": (57.8, 71.3, 4.0, 31.2), "match": "coords",
     "fn": p_vegvesen,
     "note": "the service behind vegvesen.no's own map, undocumented and unpromised"},

    {"name": "Denmark", "box": (54.5, 57.8, 8.0, 15.2), "match": "coords",
     "fn": p_vejdirektoratet, "note": "vejdirektoratet's own map layers, open bucket, no key"},

    {"name": "Sweden (relayed by Denmark)", "box": (55.3, 56.6, 12.6, 14.4), "match": "coords",
     "fn": p_swedish_via_denmark,
     "note": "Skane only, roadworks only. Trafikverket's own feed needs an account."},

    {"name": "Finland", "box": (59.5, 70.1, 19.0, 31.6), "match": "all",
     "fn": lambda: p_datex2("Finland",
         "https://tie.digitraffic.fi/api/traffic-message/v1/messages.datex2"
         "?situationType=TRAFFIC_ANNOUNCEMENT", gzip_required=True),
     "note": "Digitraffic, open, no key. AlertC locations, so no route matching."},

    # Ireland's National Access Point is data.gov.ie, and as of this build TII
    # publish journey times, traffic counts and historic collisions there, with
    # no incident publication at all. So the live Ireland entry above reads the
    # GraphQL behind their own public map, which is undocumented and can be
    # withdrawn without warning. Their 2023 ITS progress report said incidents
    # would join the DATEX II feed "later this year" and it has not appeared.
    # The day it does, swap the entry above for this one and delete the scrape:
    #
    # {"name": "Ireland", "box": (51.4, 55.4, -10.6, -5.9), "match": "coords",
    #  "fn": lambda: p_datex2("Ireland", "<feed url>"), "note": "TII DATEX II"},
]


def _road_runs_through(roads, road_points, box, not_box=None):
    """A1 exists in Britain, the Netherlands, Germany, Austria and France.
    Matching on the number alone put an Austrian tunnel closure onto a Dutch
    motorway on an Amsterdam to Munich run. So the stretch of this route that
    carries the number has to lie inside the reporting country before the
    match counts. Bounding boxes overlap at borders, so this narrows the
    error without closing it.
    """
    if not road_points:
        return True
    for r in roads:
        for lat, lon in road_points.get(r, ()):
            if _inside(box, lat, lon) and not (not_box and _inside(not_box, lat, lon)):
                return True
    return False


def providers_for(coords):
    """Which countries this route actually enters.

    Tests real route points, not the route's own bounding box. A box around
    a Dublin to Belfast run overlaps half of Europe, and the route is in two
    countries.
    """
    step = max(1, len(coords) // 400)
    thin = coords[::step] + [coords[-1]]
    hits = []
    for pr in PROVIDERS:
        nb = pr.get("not_box")
        if any(_inside(pr["box"], la, lo) and not (nb and _inside(nb, la, lo))
               for la, lo in thin):
            hits.append(pr)
    return hits


def incidents_on_route(route_roads, coords, corridor_miles=4.0, road_points=None):
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
            found = pr["fn"](route_roads) if pr.get("per_road") else pr["fn"]()
        except Exception as e:
            problems.append(f"{pr['name']} FAILED: {str(e)[:60]}")
            _health_record(pr["name"], 0)
            continue
        warn = _health_record(pr["name"], len(found))
        if warn:
            problems.append(warn)
        mode = pr.get("match", "roads")
        for i in found:
            if i["lat"] is not None and i["lon"] is not None:
                limit = 0.4 if mode == "coords" else corridor_miles
                d = min(haversine_miles((i["lat"], i["lon"]), c) for c in thin)
                if d <= limit:
                    hits.append({**i, "why": f"{d:.1f} mi from your route"})
            elif i["roads"] & route_roads:
                shared = i["roads"] & route_roads
                if not _road_runs_through(shared, road_points, pr["box"],
                                          pr.get("not_box")):
                    continue        # same number, different country
                hits.append({**i, "why": "on " + ",".join(sorted(shared))})
            elif mode == "all":
                hits.append({**i, "why": f"somewhere in {pr['name']}, feed gives no location"})

    hits.sort(key=lambda i: (i["rank"], "somewhere in" in i.get("why", "")))
    if len(hits) > 40:
        note = f"{len(hits) - 40} further minor items not shown"
        hits = hits[:40]
        problems.append(note)
    return hits, ("; ".join(problems) if problems else None)


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
        bh = "forecourts.opening_times.bank_holiday.standard"
        hours["bank_holiday"] = {
            "open": r.get(f"{bh}.open_time", "").strip(),
            "close": r.get(f"{bh}.close_time", "").strip(),
            "all_day": r.get(f"{bh}.is_24_hours", "").lower() == "true"}
        out.append({
            "name": (r.get("forecourts.trading_name") or r.get("forecourts.brand_name") or "Unnamed").title(),
            "brand": r.get("forecourts.brand_name", ""),
            "lat": lat, "lon": lon,
            "phone": r.get("forecourts.public_phone_number", "").strip(),
            "temp_closed": r.get("forecourts.temporary_closure", "").lower() == "true",
            "motorway": r.get("forecourts.is_motorway_service_station", "").lower() == "true",
            "toilets": r.get("forecourts.amenities.customer_toilets", "").lower() == "true",
            "prices": prices, "currency": "GBP",
            "ff_hours": hours, "source": "fuelfinder",
        })
    return out


# ----------------------------------------------------------------- France and Spain: the other statutory registers

# Britain is not the only country that made publishing compulsory. France has
# run prix-carburants since 2007 under the Code de commerce, and Spain runs
# its own under Real Decreto-ley 6/2000, both free, both without a key, both
# carrying opening hours as well as prices. Until tonight this tool fell back
# to OpenStreetMap the moment it left Britain. That gives a name and a dot on
# a map. Three times in four it has no opening hours at all.
#
# Hours here are a list of windows per day, in minutes past midnight, because
# a Spanish forecourt shuts for lunch and the one-window model used for
# Britain cannot say so.

FR_URL = "https://donnees.roulez-eco.fr/opendata/instantane"
ES_URL = ("https://sedeaplicaciones.minetur.gob.es/ServiciosRESTCarburantes"
          "/PreciosCarburantes/EstacionesTerrestres/")


def _hours(all_day=False, windows=None, closed=False):
    """One day of a timetable. windows is a list of (from, to) in minutes.

    Three states, because two is not enough. Open, shut, and nobody said.
    A French forecourt marked ferme on a Sunday is shut and the register
    says so; one with no row at all is unknown, and telling a driver
    "hours unknown" when the register plainly said "closed" throws away
    the half of the answer that was there.
    """
    return {"all_day": bool(all_day), "windows": list(windows or []),
            "closed": bool(closed), "open": "", "close": ""}


def _hhmm(text, sep=":"):
    """'07:30', '7.30', '7:30' to minutes past midnight, or None."""
    m = re.match(r"\s*(\d{1,2})[.:](\d{2})", text or "")
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if h > 24 or mi > 59:
        return None
    return h * 60 + mi


def load_france(max_age_hours=6):
    """prix-carburants, the French statutory register. 9,799 forecourts.

    A zipped XML in ISO-8859-1, which Python will not guess, and
    coordinates in hundred-thousandths of a degree rather than degrees.
    The flag that matters is automate-24-24: the kiosk shuts at eight and
    the card machine does not, so a station listed closed all week is
    open all night to anyone with a card. Getting that wrong strands
    somebody at two in the morning next to a working pump.
    """
    import io, zipfile
    import xml.etree.ElementTree as ET

    parsed = CACHE / "fuel_france.json"
    if parsed.exists() and (time.time() - parsed.stat().st_mtime) < max_age_hours * 3600:
        return json.loads(parsed.read_text())

    blob = CACHE / "fuel_france.zip"
    if not (blob.exists() and (time.time() - blob.stat().st_mtime) < max_age_hours * 3600):
        r = subprocess.run(["curl", "-sL", "--max-time", "120", "-A", UA, "-o", str(blob), FR_URL],
                           capture_output=True, timeout=180)
        if r.returncode or not blob.exists() or blob.stat().st_size < 10000:
            raise RuntimeError("prix-carburants download failed")
    try:
        with zipfile.ZipFile(blob) as z:
            name = next(n for n in z.namelist() if n.endswith(".xml"))
            raw = z.read(name).decode("iso-8859-1")
    except (zipfile.BadZipFile, StopIteration, KeyError):
        # A half-downloaded zip is still a file. Bin it so the next run
        # fetches rather than failing the same way for six hours.
        blob.unlink(missing_ok=True)
        raise RuntimeError("prix-carburants download was incomplete")

    grades = {"Gazole": "B7S", "SP95": "E5", "SP98": "E5P", "E10": "E10", "E85": "E85"}
    out = []
    for pdv in ET.fromstring(raw):
        try:
            lat = float(pdv.get("latitude")) / 100000.0
            lon = float(pdv.get("longitude")) / 100000.0
        except (TypeError, ValueError):
            continue
        if not lat:
            continue
        hz = pdv.find("horaires")
        always = bool(hz is not None and hz.get("automate-24-24") == "1")
        hours = {}
        days = {str(i + 1): d for i, d in enumerate(FF_DAYS)}
        for d in FF_DAYS:
            hours[d] = _hours(all_day=always)
        for jour in (hz if hz is not None else []):
            day = days.get(jour.get("id"))
            if not day:
                continue
            if jour.get("ferme") == "1":
                # Shut, unless the card machine runs on without the staff.
                hours[day] = _hours(all_day=always, closed=not always)
                continue
            wins = []
            for hr in jour.findall("horaire"):
                o, c = _hhmm(hr.get("ouverture")), _hhmm(hr.get("fermeture"))
                if o is not None and c is not None and c != o:
                    wins.append([o, c])
            hours[day] = _hours(all_day=always, windows=wins)
        prices = {}
        for pr in pdv.findall("prix"):
            g = grades.get(pr.get("nom"))
            try:
                v = float(pr.get("valeur"))
            except (TypeError, ValueError):
                continue
            if g:
                prices[g] = v * 100          # pence-equivalent, as the UK rows are
        town = (pdv.findtext("ville") or "").strip()
        addr = (pdv.findtext("adresse") or "").strip()
        services = [x.text for x in pdv.findall("./services/service") if x.text]
        out.append({
            # The register has no brand field, so four Total garages in one
            # town all came out as "Saint-Jean-De-Luz". The street is the
            # only thing separating them.
            "name": ", ".join(x for x in (town.title(), addr.title()) if x) or "Station",
            "brand": "", "lat": lat, "lon": lon, "phone": "",
            "temp_closed": False, "motorway": "autoroute" in addr.lower(),
            "toilets": any("oilettes" in x for x in services),
            "always_card": always, "addr": addr,
            "prices": prices, "currency": "EUR",
            "ff_hours": hours, "source": "france",
        })
    parsed.write_text(json.dumps(out))
    return out


ES_DAYS = {"L": 0, "M": 1, "X": 2, "J": 3, "V": 4, "S": 5, "D": 6}


def parse_spanish_hours(text):
    """'L-V: 08:00-14:00 y 16:00-21:00; S: 08:00-14:00' to seven days.

    1,165 distinct strings across 11,429 forecourts and they all follow
    this grammar: semicolon between groups, a day or a day range, then
    either 24H or one or two windows joined by 'y'. Days run L M X J V S D,
    so X is Wednesday and M is Tuesday. Reading M as Wednesday would shift
    half the week by a day.
    """
    out = {d: _hours() for d in FF_DAYS}
    for part in (text or "").split(";"):
        if ":" not in part:
            continue
        spec, times = part.split(":", 1)
        spec, times = spec.strip().upper(), times.strip()
        if "-" in spec and len(spec) == 3:
            a, b = ES_DAYS.get(spec[0]), ES_DAYS.get(spec[2])
            if a is None or b is None:
                continue
            idx = list(range(a, b + 1)) if a <= b else list(range(a, 7)) + list(range(0, b + 1))
        else:
            if spec not in ES_DAYS:
                continue
            idx = [ES_DAYS[spec]]
        if times.upper().startswith("24H"):
            for i in idx:
                out[FF_DAYS[i]] = _hours(all_day=True)
            continue
        wins = []
        for chunk in re.split(r"\s+Y\s+", times, flags=re.I):
            m = re.match(r"\s*(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})", chunk)
            if not m:
                continue
            o, c = _hhmm(m.group(1)), _hhmm(m.group(2))
            if o is not None and c is not None:
                wins.append([o, c])
        if wins:
            for i in idx:
                out[FF_DAYS[i]] = _hours(windows=wins)
    return out


def load_spain(max_age_hours=6):
    """Spain's Ministry register. 11,429 forecourts, prices and hours.

    Decimal commas throughout, in the coordinates as well as the prices.
    """
    parsed = CACHE / "fuel_spain.json"
    if parsed.exists() and (time.time() - parsed.stat().st_mtime) < max_age_hours * 3600:
        return json.loads(parsed.read_text())

    # 12 MB in one response and the server is slow, so a short timeout
    # leaves a truncated file on disk that is the right size and will not
    # parse. The first run of this against Bordeaux to Madrid died on
    # "unterminated string at char 10389766". Parse before keeping it.
    raw = CACHE / "fuel_spain_raw.json"
    d = None
    if raw.exists() and (time.time() - raw.stat().st_mtime) < max_age_hours * 3600:
        try:
            d = json.loads(raw.read_text())
        except ValueError:
            raw.unlink(missing_ok=True)
    for attempt in range(2):
        if d is not None:
            break
        subprocess.run(["curl", "-sL", "--max-time", "240", "-A", UA, "-o", str(raw), ES_URL],
                       capture_output=True, timeout=300)
        try:
            d = json.loads(raw.read_text())
        except (ValueError, OSError):
            raw.unlink(missing_ok=True)
    if d is None:
        raise RuntimeError("Spanish fuel register download was truncated twice")

    num = lambda v: float(str(v).replace(",", ".")) if str(v).strip() else None
    grades = {"Precio Gasoleo A": "B7S", "Precio Gasolina 95 E5": "E5",
              "Precio Gasolina 98 E5": "E5P", "Precio Gasolina 95 E10": "E10"}
    out = []
    for r0 in d.get("ListaEESSPrecio") or []:
        try:
            lat, lon = num(r0.get("Latitud")), num(r0.get("Longitud (WGS84)"))
        except ValueError:
            continue
        if lat is None or lon is None:
            continue
        prices = {}
        for k, g in grades.items():
            try:
                v = num(r0.get(k))
            except ValueError:
                v = None
            if v:
                prices[g] = v * 100
        # Rotulo is the brand, except where an independent has filed a
        # licence number instead. "Nº 10.935" on a sign in the dark tells
        # a driver nothing, so those fall back to the street.
        rot = (r0.get("Rótulo") or "").strip()
        place = (r0.get("Municipio") or r0.get("Localidad") or "").strip().title()
        if not rot or re.fullmatch(r"N[ºo°]?\s*[\d.,]+", rot, re.I):
            name = ", ".join(x for x in (place, (r0.get("Dirección") or "").strip().title()) if x)
        else:
            name = ", ".join(x for x in (rot.title(), place) if x)
        name = name or "Estación"
        out.append({
            "name": name, "brand": (r0.get("Rótulo") or "").strip(),
            "lat": lat, "lon": lon, "phone": "",
            "temp_closed": False, "motorway": False, "toilets": False,
            "addr": f"{r0.get('Dirección', '')}, {r0.get('Municipio', '')}".strip(", "),
            "prices": prices, "currency": "EUR",
            "ff_hours": parse_spanish_hours(r0.get("Horario")),
            "source": "spain",
        })
    parsed.write_text(json.dumps(out))
    return out



# ----------------------------------------------------------------- bank holidays

BH_URL = "https://www.gov.uk/bank-holidays.json"


def bank_holidays(max_age_days=30):
    """GOV.UK publish the official list, free and without a key.

    Three divisions, because the nations do not share a calendar. Scotland
    keeps 2 January and St Andrew's Day. Northern Ireland keeps St Patrick's
    Day and the Twelfth. England and Wales keep neither. A forecourt that
    runs bank holiday hours on 30 November is a Scottish problem only.
    """
    f = CACHE / "bank_holidays.json"
    fresh = f.exists() and (time.time() - f.stat().st_mtime) < max_age_days * 86400
    if not fresh:
        try:
            f.write_text(json.dumps(get(BH_URL, tries=2)))
        except Exception:
            if not f.exists():
                return {}
    try:
        raw = json.loads(f.read_text())
    except Exception:
        return {}
    return {k: {e["date"]: e["title"] for e in (v.get("events") or [])}
            for k, v in raw.items()}


def uk_division(lat, lon):
    """Which bank holiday calendar a forecourt keeps. Approximate, from its
    coordinates, because the statutory record gives no other location.

    The Anglo-Scottish border runs from about 54.99N at 3.05W in the west to
    55.80N at 2.03W in the east, so a straight line through those two points
    puts Gretna in Scotland and Carlisle in England, and Berwick in England
    and Coldstream in Scotland. It is wrong for anyone filling up on the very
    tip of the Mull of Kintyre, where no forecourt exists.
    """
    if lon < -5.45 and lat < 55.35:
        return "northern-ireland"
    if lat >= 54.99 + (lon + 3.05) * 0.7941:
        return "scotland"
    return "england-and-wales"


def easter(year):
    """Western Easter Sunday. The anonymous Gregorian algorithm."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    g = (8 * b + 13) // 25
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def pt_holidays(year):
    """Portugal's national holidays, which the register answers for.

    Ten fixed dates and three that move with Easter. Municipal holidays
    are not here: Lisbon shuts for St Anthony on 13 June and Porto for
    St John on the 24th, and the register has no field for that.
    """
    e = easter(year)
    out = {
        date(year, 1, 1): "Ano Novo",
        e - timedelta(days=2): "Sexta-feira Santa",
        e: "P\u00e1scoa",
        date(year, 4, 25): "Dia da Liberdade",
        date(year, 5, 1): "Dia do Trabalhador",
        e + timedelta(days=60): "Corpo de Deus",
        date(year, 6, 10): "Dia de Portugal",
        date(year, 8, 15): "Assun\u00e7\u00e3o",
        date(year, 10, 5): "Implanta\u00e7\u00e3o da Rep\u00fablica",
        date(year, 11, 1): "Todos os Santos",
        date(year, 12, 1): "Restaura\u00e7\u00e3o da Independ\u00eancia",
        date(year, 12, 8): "Imaculada Concei\u00e7\u00e3o",
        date(year, 12, 25): "Natal",
    }
    return out


def ff_bank_holiday(st, when, holidays=None):
    """The name of the bank holiday in force where this forecourt stands,
    or None. Separate from the hours lookup so it can be reported."""
    if holidays is None:
        holidays = bank_holidays()
    if not holidays or st.get("lat") is None:
        return None
    div = uk_division(st["lat"], st["lon"])
    return (holidays.get(div) or {}).get(when.strftime("%Y-%m-%d"))


def ff_row(st, when, holidays=None):
    """Which row of the statutory timetable applies on this date.

    Operators publish a separate bank holiday row and most of them use it.
    Reading the weekday row on Christmas Day sends someone to a locked
    forecourt with the tank on the pin, so the holiday row wins when there
    is one and it says something.
    """
    # Only the British register publishes a separate bank holiday row, and
    # the gate is the presence of that row, not the name of the source. An
    # earlier version keyed off the source string and quietly stopped
    # applying Christmas hours to any station that did not carry the label.
    if st["ff_hours"].get("bank_holiday") and ff_bank_holiday(st, when, holidays):
        bh = st["ff_hours"].get("bank_holiday") or {}
        if bh.get("all_day") or (bh.get("open") and bh.get("close")):
            return bh
    # Portugal publishes a holiday row too, and it is the only other
    # register that does. Thirteen national dates, three of them moving
    # with Easter.
    if st.get("pt_holiday_hours") and when.date() in pt_holidays(when.year):
        return st["pt_holiday_hours"]
    return st["ff_hours"][FF_DAYS[when.weekday()]]


def ff_open_at(st, when, holidays=None):
    """True, False or None. Uses the statutory per-day hours."""
    if st.get("temp_closed"):
        return False
    h = ff_row(st, when, holidays)
    if h["all_day"]:
        return True
    if h.get("closed"):
        return False
    # France and Spain publish days with two windows, because the place
    # shuts for lunch. Britain's register has one window a day and the
    # original code could only read that shape, so a Spanish forecourt
    # open 08:00 to 14:00 and 16:00 to 21:00 would have read as open
    # straight through the three hours it is shut.
    if h.get("windows"):
        now = when.hour * 60 + when.minute
        for o, c in h["windows"]:
            if (now >= o or now < c) if c <= o else (o <= now < c):
                return True
        return False
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


def ff_closes_at(st, when, holidays=None):
    h = ff_row(st, when, holidays)
    if h["all_day"]:
        return None
    if h.get("windows"):
        now = when.hour * 60 + when.minute
        for o, c in h["windows"]:
            if (now >= o or now < c) if c <= o else (o <= now < c):
                return f"{c // 60 % 24:02d}:{c % 60:02d}"
        return None
    if not h["close"]:
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

    No network and no fonts. Opens on a dead signal in Caithness.

    One inline script, for the escape routes: it reads the phone's GPS,
    which works with no signal, and says which marked point you are nearest
    to. Everything on the page is readable without it.
    """
    args = plan["opts"]
    total_miles, total_hours = plan["total_miles"], plan["total_hours"]
    # Escape routes. The one part of this page that is for the moment you
    # are already stopped at a closed road, so it comes before everything
    # else and it uses the phone's GPS, which works with no signal at all.
    esc = plan.get("escapes") or []
    escapes_html = ""
    if esc:
        rows = []
        for n, e in enumerate(esc):
            if e.get("trapped"):
                rows.append(
                    f'<div class="esc bad" data-lat="{e["lat"]}" data-lon="{e["lon"]}">'
                    f'<b>mile {e["mile"]:.0f}{" on " + e["road"] if e["road"] else ""}</b>'
                    f'<div class="no">No way round this one. Turn back, or wait it out.</div></div>')
                continue
            via = " &rarr; ".join(e["via"]) or "unnumbered local roads"
            rows.append(
                f'<div class="esc" data-lat="{e["lat"]}" data-lon="{e["lon"]}">'
                f'<b>mile {e["mile"]:.0f}{" on " + e["road"] if e["road"] else ""}</b>'
                f'<div class="go">Come off at mile {e["leave_mile"]:.0f}, '
                f'rejoin at mile {e["rejoin_mile"]:.0f}</div>'
                f'<div class="via">{via}</div>'
                f'<div class="cost">+{e["extra_miles"]:.0f} miles</div></div>')
        escapes_html = (
            '<h2>If the road ahead is shut</h2>'
            '<div class="warn">Worked out before you left. These need no signal. '
            'Tap the button and the phone will say which one you are nearest to.</div>'
            '<button id="whereami" onclick="locate()">Where am I?</button>'
            '<div id="near"></div>'
            + "".join(rows)
            + '<script>function locate(){var n=document.getElementById("near");'
              'if(!navigator.geolocation){n.textContent="This phone will not give a position.";return;}'
              'n.textContent="Looking...";'
              'navigator.geolocation.getCurrentPosition(function(p){'
              'var la=p.coords.latitude,lo=p.coords.longitude,best=null,bd=1e9;'
              'document.querySelectorAll(".esc").forEach(function(d){'
              'var y=(d.dataset.lat-la)*69,x=(d.dataset.lon-lo)*69*Math.cos(la*Math.PI/180);'
              'var m=Math.sqrt(x*x+y*y);d.classList.remove("here");'
              'if(m<bd){bd=m;best=d;}});'
              'if(best){best.classList.add("here");best.scrollIntoView({block:"center"});'
              'n.textContent="Nearest marked point: "+bd.toFixed(1)+" miles away.";}'
              '},function(){n.textContent="No position. GPS can take a minute outdoors.";},'
              '{enableHighAccuracy:true,timeout:30000});}</script>')

    depart, range_miles = plan["depart"], plan["range_miles"]
    scored, hits = plan["scored"], plan["hits"]
    vol, usable_vol, roads = plan["vol"], plan["usable_vol"], plan["roads"]
    arrive = plan["arrive"]
    usable = [s for s in scored if s["open"] is True]
    pick = usable[0] if usable else None

    # The handout used to print two Scottish phone numbers and "Closures:
    # Traffic Scotland" on every page, including a run from Dublin to
    # Belfast or Oslo to Trondheim. A card in a glovebox is only worth
    # anything if the number on it rings somebody who can come out.
    in_uk = any(n in plan.get("providers", [])
                for n in ("Traffic Scotland", "National Highways",
                          "Traffic Wales", "TrafficwatchNI"))
    if in_uk:
        phones = ('<a class="tel wide" href="tel:08000281414">Traffic Scotland '
                  '&nbsp;0800 028 1414</a>\n'
                  '<a class="tel wide" href="tel:03001235000">National Highways '
                  '&nbsp;0300 123 5000</a>\n'
                  '<a class="tel wide" href="sms:07380283600">Text roadside help '
                  '&nbsp;0738 028 3600</a>\n'
                  '<div class="note">Both lines are 24 hours. The text number is for '
                  'roadside assistance when a phone call won\'t work. '
                  'Emergencies are 999, and 112 also works.</div>')
    else:
        phones = ('<a class="tel wide" href="tel:112">Emergency &nbsp;112</a>\n'
                  '<div class="note">112 reaches police, fire and ambulance in every '
                  'country on this route, from any phone, in English. This tool has no '
                  'breakdown number for the roads you are on today: put your own '
                  'recovery service in your phone before you go.</div>')

    # A printed page in a glovebox has no clock of its own, so it says
    # whose time it is written in.
    tzname = (plan.get("tz") or "").split("/")[-1].replace("_", " ")
    tzline = f" &middot; all times {tzname} time" if tzname else ""
    if plan.get("end_tz") and plan.get("tz") and plan["end_tz"] != plan["tz"]:
        tzline += (" &middot; you cross into "
                   + plan["end_tz"].split("/")[-1].replace("_", " ") + " time")

    used = sorted({i["source"] for i in hits}) or plan.get("providers", [])
    sources = ("Fuel: UK Fuel Finder, Open Government Licence v3.0."
               if plan.get("source") == "fuelfinder" else
               "Fuel: OpenStreetMap, Open Database Licence."
               if plan.get("source") else "Fuel: lookup failed for this route.")
    sources += "<br>Closures: " + (", ".join(used) if used else "none covering this route")

    def card(s):
        if s["open"] is True:
            cls, status = "ok", (f"shuts {s['shuts']}" if s["shuts"] else "open 24 hours")
        elif s["open"] is False:
            cls, status = "no", ("temporarily closed" if s.get("temp_closed") else "shut by then")
        else:
            cls, status = "maybe", "hours unknown"
        bits = []
        if s.get("prices", {}).get("E10"):
            bits.append(money(s["prices"]["E10"], s.get("currency")))
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

    warn_block = ""
    if plan.get("incident_error"):
        ws = "".join(f"<li>{w}</li>" for w in plan["incident_error"].split("; "))
        warn_block = f"<h2>Trust this less</h2><ul class='late'>{ws}</ul>"

    if plan.get("unverified_hours"):
        stops_block = ('<div class="warn">No register published opening hours for '
                       'these. They are sorted by distance and none is confirmed '
                       'open. Ring ahead where you can.</div>'
                       + "".join(card(s) for s in scored[:14]))
    elif plan.get("source") is None:
        stops_block = ('<div class="warn">The fuel lookup failed when this page was '
                       'made, so there are no stops on it. That is not the same as '
                       'there being none on the road. Fill up before you set off.</div>')
    elif not scored:
        stops_block = ('<div class="warn">Nothing within range on this route. '
                       'Fuel before you set off.</div>')
    else:
        stops_block = "".join(card(s) for s in scored[:14])

    rr = ""
    if plan.get("reroute"):
        cost = plan.get("diversion_cost", 0)
        if cost >= 0.5:
            rr = (f"<div class='warn'><b>Rerouted around "
                  f"{plan['reroute']['blocked']} closure(s).</b> The diversion adds "
                  f"{cost:.0f} miles, and the fuel plan here already counts them.</div>")
        else:
            rr = (f"<div class='warn'><b>{plan['reroute']['blocked']} closure(s) on your "
                  f"roads.</b> A way round exists at no extra distance.</div>")
    elif plan.get("blocked"):
        rr = ("<div class='warn'><b>A closure is live on your route.</b> "
              "No rerouting engine was available, so these miles assume no diversion.</div>")

    inc = ""
    if hits:
        rows = "".join(f"<li><b>{i['type']}</b> &middot; {i['title']}<br><small>{i['direction']}"
                       f"{' &middot; ' + i['desc'][:90] if i['desc'] else ''}</small></li>" for i in hits[:6])
        inc = f"<h2>Live when this was made</h2><ul class='inc'>{rows}</ul>"

    doc = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Cairn</title><style>
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
.esc{{background:#192027;border-left:5px solid #5a7a9a;border-radius:8px;padding:11px 14px;
margin-bottom:9px}}
.esc.bad{{border-left-color:#ff8080}}
.esc.here{{background:#20303c;border-left-color:#ffd24a;box-shadow:0 0 0 2px #ffd24a}}
.esc b{{font-size:15px;color:#9fc6e8}}
.esc.bad b{{color:#ff8080}}
.go{{margin-top:4px;font-size:15px}}
.via{{margin-top:3px;font-size:17px;font-weight:700;letter-spacing:.3px}}
.cost{{margin-top:3px;font-size:13px;color:#9aa7b2}}
.no{{margin-top:4px;font-size:15px;color:#ffd0d0}}
#whereami{{width:100%;padding:16px;font-size:18px;font-weight:700;background:#2b4a63;
color:#eef2f5;border:0;border-radius:10px;margin-bottom:10px}}
#near{{font-size:14px;color:#9aa7b2;margin-bottom:10px;min-height:1em}}
.roads .t{{font-size:13px;color:#9aa7b2;text-transform:uppercase;letter-spacing:.08em}}
.tel.wide{{display:block;margin:0 0 9px;text-align:center;padding:14px;font-size:17px;font-weight:600}}
.note{{color:#9aa7b2;font-size:14px;margin-top:10px}}
</style></head><body>
<h1>{args.origin} to {args.dest}</h1>
<div class="sub">{total_miles:.0f} miles &middot; leave {depart:%a %d %b %H:%M} &middot; arrive about {arrive:%H:%M}{tzline}<br>
{args.fuel:.0f}% of {args.tank:.0f}{vol} gives you {range_miles:.0f} miles</div>
{'<div class="big"><div class="t">The plan</div><div class="v">' + pick['name'] + '</div>mile ' + f"{pick['along']:.0f}" + ', about ' + f"{pick['eta']:%H:%M}" + '</div>' if pick else '<div class="big" style="border-left-color:#8c2f2f"><div class="t">The plan</div><div class="v">Fuel before you leave</div>Nothing confirmed open within range.</div>'}
{rr}
{late}
<h2>Every stop in range</h2>
{stops_block}
{inc}
{escapes_html}
<h2>If you get a bar of signal</h2>
<div class="warn">Closures below were live at <b>{datetime.now():%H:%M on %d %b}</b>.
Anything that happened after that is not in here. On a long run, check once before you set off.</div>
<div class="roads"><span class="t">Your roads today</span><br><b>{" &middot; ".join(sorted(roads)) if roads else "see the route"}</b><br>
<small>These are the numbers to listen for on the radio.</small></div>
{phones}
<div class="foot">Made {datetime.now():%d %b %Y, %H:%M}. Needs no signal.<br>
{sources}<br>
Opening hours change. Ring ahead when it matters.</div>
</body></html>"""
    Path(path).write_text(doc)
    return path


# Prices are held in the minor unit everywhere, because the British register
# publishes pence. So a Spanish price of 1.80 euro is 180 here, and printing
# a difference as "31.0p/L" on a road outside Jerez is simply wrong.
CURRENCY = {"GBP": ("p", "\u00a3"), "EUR": ("c", "\u20ac")}


def money(minor, currency="GBP", per_litre=True):
    """A price or a difference, in the currency it was published in."""
    minor_name, major = CURRENCY.get(currency or "GBP", CURRENCY["GBP"])
    tail = "/L" if per_litre else ""
    if minor < 100:
        return f"{minor:.1f}{minor_name}{tail}"
    return f"{major}{minor / 100:.2f}{tail}"


# ----------------------------------------------------------------- Italy

# MIMIT runs the Osservatorio Prezzi Carburanti and publishes two CSVs a
# day, and their host will not take a connection from here at all. Not a
# 403, not a redirect: no answer to a TCP handshake, tried on three
# addresses over an hour. Other Italian government hosts answer fine, so
# it is that one.
#
# So this reads a third-party daily mirror, the same arrangement already
# used for the British register, and says so out loud. Two things to know
# about it. It is one volunteer's GitHub Action, and if it stops the
# provider health check is what notices. And the Italian register carries
# no opening hours at all, so every Italian stop reports its hours as
# unknown, which is the truth rather than a shrug.
IT_SNAPSHOT = "https://raw.githubusercontent.com/stexcx/fuelmap-dati/main/snapshot.json"


def load_italy(max_age_hours=6):
    """21,528 forecourts with prices. No hours in the source."""
    parsed = CACHE / "fuel_italy.json"
    if parsed.exists() and (time.time() - parsed.stat().st_mtime) < max_age_hours * 3600:
        return json.loads(parsed.read_text())

    raw = CACHE / "fuel_italy_raw.json"
    d = None
    if raw.exists() and (time.time() - raw.stat().st_mtime) < max_age_hours * 3600:
        try:
            d = json.loads(raw.read_text())
        except ValueError:
            raw.unlink(missing_ok=True)
    if d is None:
        subprocess.run(["curl", "-sL", "--compressed", "--max-time", "120",
                        "-A", UA, "-o", str(raw), IT_SNAPSHOT],
                       capture_output=True, timeout=180)
        try:
            d = json.loads(raw.read_text())
        except (ValueError, OSError):
            raw.unlink(missing_ok=True)
            raise RuntimeError("Italian mirror download failed")

    # The mirror says when the ministry last extracted. A price three days
    # old is still worth having and the age belongs on the screen.
    stamp = (d.get("updatedAt") or "")[:10]
    grades = {"benzina": "E5", "diesel": "B7S"}
    out = []
    for r in d.get("stations") or []:
        try:
            lat, lon = float(r["lat"]), float(r["lng"])
        except (TypeError, ValueError, KeyError):
            continue
        prices = {}
        for k, g in grades.items():
            v = (r.get("prices") or {}).get(k)
            if isinstance(v, (int, float)) and v > 0:
                prices[g] = float(v) * 100
        brand = (r.get("brand") or "").strip()
        city = (r.get("city") or "").strip().title()
        out.append({
            "name": ", ".join(x for x in (brand, city) if x) or (r.get("name") or "Distributore"),
            "brand": brand, "lat": lat, "lon": lon, "phone": "",
            "temp_closed": False, "motorway": False, "toilets": False,
            "addr": (r.get("address") or "").strip().title(),
            # Their own flag for a coordinate they do not trust. 47 of
            # 21,528. Sending somebody to a pump that is not there is the
            # one thing worse than sending them nowhere.
            "misplaced": bool(r.get("misplaced")),
            "prices": prices, "currency": "EUR", "priced_on": stamp,
            "ff_hours": {d2: _hours() for d2 in FF_DAYS}, "source": "italy",
        })
    out = [r for r in out if not r["misplaced"]]
    if not out:
        raise RuntimeError("Italian mirror had no usable stations")
    parsed.write_text(json.dumps(out))
    return out



# ----------------------------------------------------------------- Portugal

PT_API = "https://precoscombustiveis.dgeg.gov.pt/api/PrecoComb"
PT_GRADES = {2101: "B7S", 3201: "E5", 3400: "E5P"}


def load_portugal(max_age_hours=6):
    """DGEG's register. 3,077 forecourts, prices by grade.

    Their list call carries price and position and no hours at all. Hours
    live one call per station deeper, so they are fetched later, for the
    few stations that actually end up on a route, rather than three
    thousand times for one journey.
    """
    parsed = CACHE / "fuel_portugal.json"
    if parsed.exists() and (time.time() - parsed.stat().st_mtime) < max_age_hours * 3600:
        return json.loads(parsed.read_text())

    by_id = {}
    for code, grade in PT_GRADES.items():
        body = get_text(f"{PT_API}/PesquisarPostos?idsTiposComb={code}"
                        f"&qtdPorPagina=20000&pagina=1")
        try:
            rows = json.loads(body).get("resultado") or []
        except Exception:
            continue
        for r in rows:
            try:
                lat, lon = float(r["Latitude"]), float(r["Longitude"])
            except (TypeError, ValueError, KeyError):
                continue
            st = by_id.setdefault(r["Id"], {
                "name": ", ".join(x for x in ((r.get("Marca") or "").title(),
                                              (r.get("Municipio") or "").title()) if x)
                        or (r.get("Nome") or "Posto"),
                "brand": r.get("Marca") or "", "lat": lat, "lon": lon, "phone": "",
                "temp_closed": False, "motorway": "auto-estrada" in (r.get("TipoPosto") or "").lower(),
                "toilets": False, "pt_id": r["Id"],
                "addr": ", ".join(x for x in (r.get("Morada"), r.get("Localidade")) if x),
                "prices": {}, "currency": "EUR", "ff_hours": None, "source": "portugal",
            })
            m = re.search(r"([\d,.]+)", r.get("Preco") or "")
            if m:
                try:
                    st["prices"][grade] = float(m.group(1).replace(",", ".")) * 100
                except ValueError:
                    pass
    out = list(by_id.values())
    if not out:
        raise RuntimeError("Portuguese register returned nothing")
    parsed.write_text(json.dumps(out))
    return out


def _pt_window(text):
    """'Aberto 24 horas', '06:30-22:00', 'Encerrado', or nothing at all."""
    t = (text or "").strip()
    if not t:
        return None
    low = t.lower()
    if "24" in low and "abert" in low:
        return _hours(all_day=True)
    if "encerrad" in low or "fechad" in low:
        return _hours(closed=True)
    m = re.match(r"(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})", t)
    if not m:
        return None
    o, c = _hhmm(m.group(1)), _hhmm(m.group(2))
    return _hours(windows=[[o, c]]) if o is not None and c is not None else None


def enrich_portugal(rows, workers=8, max_age_days=7):
    """Fill in opening hours for the Portuguese stations on this route.

    One call per station, so it runs only on the shortlist and keeps what
    it learns. DGEG publish four rows: weekdays, Saturday, Sunday and a
    holiday row, which is one more than France and Spain manage.
    """
    from concurrent.futures import ThreadPoolExecutor
    store = CACHE / "fuel_portugal_hours.json"
    try:
        known = json.loads(store.read_text())
    except Exception:
        known = {}
    fresh = time.time() - max_age_days * 86400
    want = [r for r in rows
            if str(r.get("pt_id")) not in known
            or (known[str(r["pt_id"])].get("at", 0) < fresh)]

    def fetch(r):
        body = get_text(f"{PT_API}/GetDadosPosto?id={r['pt_id']}")
        try:
            return r["pt_id"], (json.loads(body)["resultado"] or {}).get("HorarioPosto") or {}
        except Exception:
            return r["pt_id"], {}

    if want:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for pid, h in pool.map(fetch, want):
                known[str(pid)] = {"at": time.time(), "h": h}
        try:
            store.write_text(json.dumps(known))
        except OSError:
            pass

    for r in rows:
        h = (known.get(str(r.get("pt_id"))) or {}).get("h") or {}
        week = _pt_window(h.get("DiasUteis"))
        sat = _pt_window(h.get("Sabado")) or week
        sun = _pt_window(h.get("Domingo")) or week
        hol = _pt_window(h.get("Feriado"))
        r["ff_hours"] = {d: (week or _hours()) for d in FF_DAYS[:5]}
        r["ff_hours"]["saturday"] = sat or _hours()
        r["ff_hours"]["sunday"] = sun or _hours()
        if hol:
            r["pt_holiday_hours"] = hol
    return rows



# ----------------------------------------------------------------- which register covers where

def _near(stations, coords, corridor_miles=3.0):
    """Trim a national register to the stations beside this route."""
    lats = [c[0] for c in coords]; lons = [c[1] for c in coords]
    pad = corridor_miles / 60.0
    box = (min(lats) - pad, max(lats) + pad, min(lons) - pad * 1.7, max(lons) + pad * 1.7)
    near = [s for s in stations if box[0] <= s["lat"] <= box[1] and box[2] <= s["lon"] <= box[3]]
    step = max(1, len(coords) // 400)
    thin = coords[::step]
    out = []
    for st in near:
        d = min(haversine_miles((st["lat"], st["lon"]), c) for c in thin)
        if d <= corridor_miles:
            out.append({**st, "off_route": d})
    return out


# Same idea as the closure registry: a box, a loader, and a note saying what
# the country actually promises. Three countries compel their forecourts to
# publish hours and prices. Everywhere else still falls back to OpenStreetMap,
# which knows where the stations are and usually not when they are open.
FUEL_REGISTERS = [
    {"name": "UK Fuel Finder", "key": "fuelfinder", "box": (49.8, 61.0, -8.7, 2.0),
     "load": load_fuelfinder,
     "note": "Motor Fuel Price (Open Data) Regulations 2025, Open Government Licence v3.0"},

    {"name": "prix-carburants", "key": "france", "box": (41.3, 51.1, -5.2, 9.6),
     "load": load_france,
     "note": "French statutory register, Licence Ouverte"},

    {"name": "Spanish Ministry register", "key": "spain", "box": (35.9, 43.9, -9.4, 4.4),
     "load": load_spain,
     "note": "Real Decreto-ley 6/2000, published by MITECO"},

    {"name": "MIMIT Italy, via mirror", "key": "italy", "box": (35.4, 47.1, 6.6, 18.6),
     "load": load_italy,
     "note": "prices only, no hours published. Ministry host unreachable, so a daily mirror"},

    {"name": "DGEG Portugal", "key": "portugal", "box": (36.9, 42.2, -9.6, -6.1),
     "load": load_portugal, "enrich": enrich_portugal,
     "note": "hours come one station at a time, so only the shortlist is asked"},
]


def statutory_near_route(coords):
    """Every compulsory register that this route passes through, merged.

    Returns the stations and a list of problems. A route from Cardiff to
    Madrid crosses three of them and each one stops at its own border, so
    they get merged and the gaps are somebody else's problem.
    """
    step = max(1, len(coords) // 300)
    thin = coords[::step] + [coords[-1]]
    found, used, problems = [], [], []
    for reg in FUEL_REGISTERS:
        if not any(_inside(reg["box"], la, lo) for la, lo in thin):
            continue
        try:
            rows = _near(reg["load"](), coords)
        except Exception as e:
            problems.append(f"{reg['name']} unavailable: {str(e)[:70]}")
            continue
        if rows and reg.get("enrich"):
            # Some registers keep opening hours one call deeper. Those are
            # fetched here, for the handful of stations beside this route,
            # rather than for the whole country.
            try:
                rows = reg["enrich"](rows)
            except Exception as e:
                problems.append(f"{reg['name']} hours unavailable: {str(e)[:60]}")
        if rows:
            found += rows
            used.append(reg["key"])
    return found, used, problems



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


def reroute_avoiding(a, b, points, gh_url, profile="car", radius_miles=0.45):
    """Ask GraphHopper for a route that physically cannot enter the closures.

    Custom models go in the body of a POST and areas are GeoJSON polygons.
    Multiplying priority by zero inside an area removes it from the graph,
    so the answer genuinely avoids the closure instead of preferring not to.

    Needs a GraphHopper on your own machine. See setup-local-routing.sh.
    """
    if not points:
        return None
    areas = closure_areas(points, radius_miles)
    body = {
        "points": [[a["lon"], a["lat"]], [b["lon"], b["lat"]]],
        "profile": profile,
        "points_encoded": False,
        "instructions": True,
        "ch.disable": True,
        "details": ["street_ref"],
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
    roads = []
    for lo, hi, ref in (pth.get("details") or {}).get("street_ref", []):
        for one in re.split(r"[;,/ ]+", (ref or "").upper()):
            if re.fullmatch(ROAD_RE, one) and one not in roads:
                roads.append(one)
    return {"miles": pth["distance"] * MILES_PER_METRE,
            "hours": pth["time"] / 3600000.0,
            "coords": coords, "roads": roads,
            "blocked": len(points)}



# ----------------------------------------------------------------- escape routes

def escape_routes(route, gh_url, samples=10, radius_miles=1.6):
    """Work out, before you leave, where a closure would actually trap you.

    The plan this tool writes answers "will this journey work". It does not
    answer the question you have at eleven at night on an unlit road with a
    POLICE ROAD CLOSED sign across it and no signal: which way now.

    So this walks the route, pretends each sampled point is shut, and asks
    the local engine for the way round. The answers are baked into the
    offline page before you leave, so on the road the page can say the A9 is
    shut at Dunkeld, come off at mile 71, rejoin at mile 81, via the A822.

    Two things that took a while to get right. Blocking half a mile lets a
    parallel service road absorb it and the detour reads as nothing, so the
    block is the length of a real closed section. And the diversion is
    routed from a few miles before to a few miles after, because routing
    origin to destination around the point answers a different question and
    returns a whole alternative journey.

    Needs a GraphHopper on this machine. It is one routing query per sample
    and nobody should point that at a public server.
    """
    from concurrent.futures import ThreadPoolExecutor
    # Irish and Highland motorway junctions can be fifteen miles apart, so a
    # diversion often cannot start and finish inside a ten mile window. Widen
    # until one is found. Needing the widest is itself worth reporting.
    WINDOWS = (5.0, 15.0, 30.0)
    coords = route["coords"]
    if len(coords) < 40 or not gh_url:
        return []
    cum = cumulative_miles(coords)
    total = cum[-1]
    if total < 20:
        return []

    at = lambda m: min(range(len(cum)), key=lambda k: abs(cum[k] - m))
    edge = max(6.0, min(12.0, total * 0.12))
    span = total - 2 * edge
    wanted = [edge + i * span / max(1, samples - 1) for i in range(samples)]
    picks = []
    for m in wanted:
        i = at(m)
        if not picks or abs(cum[i] - cum[picks[-1]]) > 2.0:
            picks.append(i)

    def road_at(i):
        """Which numbered road the route is on at this point."""
        here, best, best_d = coords[i], "", 9e9
        for ref, pts in (route.get("road_points") or {}).items():
            for q in pts:
                d = haversine_miles(here, (q[0], q[1]))
                if d < best_d:
                    best, best_d = ref, d
        return best if best_d < 0.6 else ""

    def one(i):
        here = coords[i]
        on = road_at(i)
        base = {"mile": round(cum[i], 1), "lat": round(here[0], 5),
                "lon": round(here[1], 5), "road": on}
        last = ""
        for gap in WINDOWS:
            lo, hi = at(max(0.0, cum[i] - gap)), at(min(total, cum[i] + gap))
            if lo == hi:
                continue
            leg = cum[hi] - cum[lo]
            try:
                alt = reroute_avoiding({"lat": coords[lo][0], "lon": coords[lo][1]},
                                       {"lat": coords[hi][0], "lon": coords[hi][1]},
                                       [here], gh_url, radius_miles=radius_miles)
            except Exception as e:
                last = str(e)[:70]
                continue
            if not alt:
                continue
            via = [r for r in alt.get("roads", []) if r != on][:4]
            return {**base, "trapped": False,
                    "extra_miles": round(max(alt["miles"] - leg, 0.0), 1),
                    "leave_mile": round(cum[lo], 1), "rejoin_mile": round(cum[hi], 1),
                    "detour_miles": round(alt["miles"], 1),
                    "wide": gap >= WINDOWS[-1], "via": via}

        # No route at any width is the most useful answer this can give. It
        # means a closure here leaves you with nowhere to go, and a driver
        # wants that written down before they set off.
        return {**base, "trapped": True, "extra_miles": None, "via": [],
                "why": last or "no route avoids it at any width"}

    with ThreadPoolExecutor(max_workers=4) as pool:
        got = [r for r in pool.map(one, picks) if r]
    got.sort(key=lambda r: r["mile"])
    return got


# ----------------------------------------------------------------- what time is it where you are driving

# This machine's clock is whatever room it is sitting in. The question this
# tool answers is whether a forecourt in Spain will be open when you reach
# it, and that is a question about Spanish time. Planning a Lisbon run from
# a machine eight hours from the route checked Portuguese hours against a
# clock eight hours out and called every shut station open.
#
# Boxes, in the same shape as the closure registry, because mapping a
# coordinate to a zone properly needs a shapefile and this does not.
# zoneinfo handles the summer time switch, which is the part worth getting
# from a library rather than guessing.
# Ordered narrowest first, because every one of these boxes overlaps a
# neighbour. Ireland before Britain or Dublin reads as London. Finland
# before Norway or Helsinki reads as Oslo, and those two are an hour
# apart. The third item is a list of holes: Norwegian Finnmark pokes into
# the Finnish box at the top of the map and keeps Norwegian time.
ZONES = [
    ((36.9, 42.2, -9.6, -6.1), "Europe/Lisbon", []),
    # Belfast lands in the Dublin box and Malmo in the Copenhagen one.
    # Both are wrong on the map and right on the clock, to the minute,
    # all year, which is the only thing this table is for.
    ((51.3, 55.5, -10.7, -5.8), "Europe/Dublin", []),
    ((49.8, 61.1, -8.7, 2.1), "Europe/London", []),
    ((35.9, 43.9, -9.4, 4.4), "Europe/Madrid", []),
    ((49.4, 50.2, 5.7, 6.6), "Europe/Luxembourg", []),
    ((49.4, 51.6, 2.5, 6.0), "Europe/Brussels", []),
    ((50.7, 53.6, 3.3, 7.3), "Europe/Amsterdam", []),
    ((45.83, 47.81, 5.96, 10.50), "Europe/Zurich", []),
    # Italy before France, or Milan and Turin read as Paris. The hole is
    # the French side of the Alps and the Riviera, which the Italian box
    # reaches over. Every country in this stretch keeps the same clock, so
    # the stakes here are a label on one line, not an hour.
    ((35.4, 47.1, 6.6, 18.6), "Europe/Rome", [(43.0, 46.5, 5.0, 7.55)]),
    ((41.3, 51.1, -5.2, 9.6), "Europe/Paris", []),
    ((46.3, 49.1, 9.5, 17.2), "Europe/Vienna", []),
    ((45.4, 46.9, 13.3, 16.6), "Europe/Ljubljana", []),
    ((47.2, 55.1, 5.8, 15.1), "Europe/Berlin", []),
    ((54.5, 57.8, 8.0, 15.2), "Europe/Copenhagen", []),
    ((59.7, 70.1, 20.5, 31.6), "Europe/Helsinki",
     [(69.3, 71.3, 15.0, 25.6),      # Troms and west Finnmark
      (69.4, 71.3, 28.6, 31.3)]),    # Sor-Varanger, round Kirkenes
    ((55.3, 69.1, 11.0, 24.2), "Europe/Stockholm", []),
    ((57.8, 71.3, 4.0, 31.2), "Europe/Oslo", []),
]


def zone_for(lat, lon):
    """The IANA zone covering this point, or None if it is off the map.

    Ordered narrowest first: Portugal before Spain, Ireland before Britain,
    Norway before Sweden, because the boxes overlap at every border.
    """
    if lat is None or lon is None:
        return None
    for box, name, holes in ZONES:
        if name == "Europe/Lisbon" and not _in_polygon(PT_OUTLINE, lat, lon):
            continue
        if _inside(box, lat, lon) and not any(_inside(h, lat, lon) for h in holes):
            try:
                from zoneinfo import ZoneInfo
                return ZoneInfo(name)
            except Exception:
                return None
    return None


# Portugal and Spain are an hour apart and share a 754 mile land border that
# no rectangle can follow. A box round Portugal swallows Badajoz, Merida,
# Caceres, Huelva, Zamora and Vigo, all of them Spanish and all of them an
# hour out. So the frontier gets a rough polygon. Tested against 25 towns on
# both sides: it places 22 correctly and misses three that sit within about
# two miles of the line, Miranda do Douro, Vila Real de Santo Antonio and
# Tui, where the border is the middle of a river.
PT_OUTLINE = [
    (42.15, -8.88), (42.05, -8.20), (41.95, -7.20), (41.87, -6.55), (41.68, -6.12),
    (41.52, -6.17), (41.40, -6.50), (41.03, -6.80), (40.62, -6.80), (40.35, -6.86),
    (40.26, -7.00), (39.95, -6.88), (39.67, -7.02), (39.70, -7.55), (39.37, -7.25),
    (39.17, -7.14), (38.98, -7.14), (38.87, -7.05), (38.45, -7.22), (38.20, -7.00),
    (38.03, -7.10), (37.95, -7.40), (37.52, -7.44), (37.25, -7.41), (37.15, -7.44),
    (37.00, -7.52), (36.95, -8.20), (37.02, -8.99), (38.00, -8.90), (38.70, -9.50),
    (39.40, -9.40), (40.20, -8.90), (41.00, -8.80), (41.80, -8.87),
]


def _in_polygon(poly, lat, lon):
    """Ray casting. Points exactly on an edge may fall either way."""
    inside, j = False, len(poly) - 1
    for i, (yi, xi) in enumerate(poly):
        yj, xj = poly[j]
        if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


# A forecourt that came out of a national register is in that nation, which
# its own government has already established. No geometry needed.
REGISTER_ZONES = {"fuelfinder": "Europe/London", "france": "Europe/Paris",
                  "spain": "Europe/Madrid", "portugal": "Europe/Lisbon",
                  "italy": "Europe/Rome"}


def zone_for_station(st, fallback=None):
    """The zone a forecourt keeps its hours in."""
    key = REGISTER_ZONES.get(st.get("source"))
    if key:
        # The Spanish register runs to the Canaries, which are an hour
        # behind the mainland and 600 miles out into the Atlantic.
        if st.get("source") == "spain" and (st.get("lon") or 0) < -12:
            key = "Atlantic/Canary"
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(key)
        except Exception:
            return fallback
    return zone_for(st.get("lat"), st.get("lon")) or fallback


def local_now(tz):
    """Now, on the clock where the driving happens, with no zone attached.

    The rest of this tool works in naive datetimes that mean local time at
    the forecourt, so the zone is used to get the number right and then
    dropped.
    """
    from datetime import timezone as _tz
    if tz is None:
        return datetime.now()
    return datetime.now(_tz.utc).astimezone(tz).replace(tzinfo=None)


def shift_between(when, from_tz, to_tz):
    """Read a wall clock in one zone and say what it reads in another."""
    if from_tz is None or to_tz is None or from_tz is to_tz:
        return when
    try:
        return when.replace(tzinfo=from_tz).astimezone(to_tz).replace(tzinfo=None)
    except Exception:
        return when


# ----------------------------------------------------------------- the plan

class Opts:
    """Plain options bag so the CLI and the server can share one code path."""
    def __init__(self, origin, dest, tank=55, mpg=42, fuel=50, reserve=10,
                 depart=None, detour=0.0, units="uk", offline=False, out=None,
                 verify=False, gh=None, escapes=0):
        self.origin, self.dest = origin, dest
        self.tank, self.mpg, self.fuel, self.reserve = float(tank), float(mpg), float(fuel), float(reserve)
        self.depart = depart
        self.detour, self.units, self.offline, self.out = float(detour), units, offline, out
        self.verify = verify
        self.gh = gh
        self.escapes = int(escapes or 0)


def build_plan(o):
    """Everything, computed, nothing printed. The CLI and the server both call this."""
    notes = []

    a, b = resolve(o.origin, o.offline), resolve(o.dest, o.offline)
    r = route(a, b, o.offline)

    # Every time in this plan is wall clock time at the start of the
    # journey. A departure the user typed is read that way, and "now"
    # means now where the car is, not where the laptop is.
    start_tz = zone_for(a["lat"], a["lon"])
    end_tz = zone_for(b["lat"], b["lon"])
    depart = datetime.fromisoformat(o.depart) if o.depart else local_now(start_tz)

    total_miles = r["miles"] + o.detour
    total_hours = r["hours"] * (total_miles / r["miles"] if r["miles"] else 1)
    vol = "L" if o.units == "uk" else "US gal"
    per_unit = o.mpg if o.units == "us" else o.mpg / LITRES_PER_GALLON["uk"]
    usable_vol = o.tank * (o.fuel - o.reserve) / 100
    range_miles = usable_vol * per_unit

    plan = {"opts": o, "a": a, "b": b, "roads": r.get("roads", set()),
            "tz": getattr(start_tz, "key", None), "end_tz": getattr(end_tz, "key", None),
            "coords": r["coords"], "route_cached": bool(r.get("cached")),
            "depart": depart, "arrive": depart + timedelta(hours=total_hours),
            "total_miles": total_miles, "total_hours": total_hours,
            "vol": vol, "usable_vol": usable_vol, "range_miles": range_miles,
            "makes_it": range_miles >= total_miles,
            "spare": range_miles - total_miles, "notes": notes,
            "hits": [], "incident_error": None, "blocked": False,
            "scored": [], "source": None, "pick": None, "cheaper": None, "later": 0}

    plan["providers"] = [pr["name"] for pr in providers_for(r["coords"])]
    hits, err = incidents_on_route(plan["roads"], r["coords"],
                                   road_points=r.get("road_points"))
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

    # Escape routes are worked out for the whole journey, whether or not
    # anything is shut today, because the point of them is the closure that
    # appears after you have left and out of reach of any signal.
    plan["escapes"] = []
    if getattr(o, "escapes", 0) and o.gh:
        try:
            plan["escapes"] = escape_routes(r, o.gh, samples=o.escapes)
        except Exception as e:
            plan["reroute_error"] = (plan.get("reroute_error") or "") + f" escapes: {str(e)[:60]}"

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

    stations, used, problems = statutory_near_route(r["coords"])
    notes += problems
    if stations:
        plan["source"] = ",".join(used)
    else:
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
        # The clock on the dashboard says one thing and the clock on the
        # forecourt door says another. The arrival time printed stays the
        # driver's, and the opening hours are checked against the
        # forecourt's own.
        here_tz = zone_for_station(st, start_tz)
        local_eta = shift_between(eta, start_tz, here_tz)
        # Any station from a statutory register carries ff_hours. The test
        # used to be the word "fuelfinder", so the day France and Spain
        # arrived every one of their 21,000 forecourts would have been read
        # with the OpenStreetMap parser, against an "hours" field they do
        # not have, and come back as hours unknown.
        if st.get("ff_hours"):
            hol = plan.setdefault("_holidays", bank_holidays()) \
                if st.get("source") == "fuelfinder" else None
            bh = ff_bank_holiday(st, local_eta, hol) if st.get("source") == "fuelfinder" else None
            is_open = ff_open_at(st, local_eta, hol)
            shuts = ff_closes_at(st, local_eta, hol)
        else:
            bh = None
            is_open = parse_hours(st.get("hours"), local_eta)
            shuts = closes_at(st.get("hours"), local_eta)

        scored.append({**st, "along": along, "off_route": st.get("off_route", best_d),
                       "eta": eta, "open": is_open, "shuts": shuts, "bank_holiday": bh})
    scored.sort(key=lambda s: -s["along"])
    plan["scored"] = scored
    if getattr(o, "verify", False) and plan["source"] == "fuelfinder":
        plan["verify"] = cross_check(scored, r["coords"])

    usable = [s for s in scored if s["open"] is True]
    # Italy's register publishes prices and no hours at all, so on a Milan
    # to Rome run every one of 21,000 forecourts reads as unknown and the
    # plan was "nothing confirmed open, fuel before departure" on a 350
    # mile drive. That is worse than no advice. Where nothing can be
    # confirmed, pick the furthest one that is not known to be shut and
    # say plainly that the hours were never published.
    plan["unverified_hours"] = False
    if not usable:
        maybe = [s for s in scored if s["open"] is None]
        if maybe:
            usable = maybe
            plan["unverified_hours"] = True
            srcs = sorted({s.get("source") for s in maybe if s.get("source")})
            if srcs == ["italy"]:
                notes.append("Italy's register publishes prices and no opening hours, "
                             "so no stop below can be confirmed open. Ring ahead.")
            else:
                notes.append("No stop in range has published hours. The times below "
                             "are arrival times, not opening times.")
    if usable:
        pick = usable[0]
        plan["pick"] = pick
        plan["later"] = len([s for s in scored if s["along"] > pick["along"]])
        # Countries do not all sell the same grade. Britain's register
        # leads on E10, Spain's on E5, and comparing a price against a
        # grade half the forecourts do not stock compares nothing.
        grade = next((g for g in ("E10", "E5", "B7S")
                      if sum(1 for s in usable if s.get("prices", {}).get(g)) > 1), None)
        priced = [s for s in usable if s.get("prices", {}).get(grade)] if grade else []
        # The stop this tool picked has to carry a price in the same grade,
        # or there is nothing to compare. One version defaulted a missing
        # price to a billion when testing and to zero when subtracting,
        # and printed a saving of minus 199.9 cents a litre outside Lisbon.
        here = pick.get("prices", {}).get(grade) if grade else None
        if len(priced) > 1 and here:
            best = min(priced, key=lambda s: s["prices"][grade])
            if best is not pick and best["prices"][grade] < here - 2:
                plan["cheaper"] = (best, here - best["prices"][grade])
                plan["cheaper_grade"] = grade
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
                "bank_holiday": s.get("bank_holiday"),
                "off_route": round(s.get("off_route", 0), 1), "toilets": bool(s.get("toilets")),
                "price_e10": s.get("prices", {}).get("E10")}
    return {
        "from": o.origin, "to": o.dest,
        "miles": round(plan["total_miles"]), "hours": round(plan["total_hours"], 2),
        "timezone": plan.get("tz"),
        "depart": plan["depart"].isoformat(timespec="minutes"),
        "arrive": plan["arrive"].isoformat(timespec="minutes"),
        "range_miles": round(plan["range_miles"]), "makes_it": plan["makes_it"],
        "roads": sorted(plan["roads"]), "source": plan["source"],
        "blocked": plan["blocked"],
        "escapes": plan.get("escapes") or [],
        "incidents": [{"type": i["type"], "title": i["title"], "direction": i["direction"],
                       "desc": i["desc"][:200], "since": i["start"], "rank": i["rank"],
                       "source": i["source"], "why": i.get("why", "")}
                      for i in plan["hits"][:12]],
        "pick": stop(plan["pick"]) if plan["pick"] else None,
        "stops": [stop(s) for s in plan["scored"][:14]],
        "notes": plan["notes"], "generated": datetime.now().isoformat(timespec="seconds"),
    }


def finish(o, args, plan, W):
    """The handout and the privacy report.

    These used to sit at the bottom of the printout, after two early returns.
    So asking for --out plan.html on a journey you had the fuel for wrote no
    file and said nothing about it, and --privacy reported nothing at all.
    Every exit runs through here now.
    """
    if o.out:
        write_plan(o.out, plan)
        print(f"\n  Phone handout written to {o.out}")
        print("  AirDrop or email it to yourself. It needs no signal.")
    if getattr(args, "privacy", False) or LEAKED:
        print("\n" + "-" * 68)
        if LEAKED:
            print("  LEFT THIS MACHINE THIS RUN:")
            for l in LEAKED:
                print(f"    {l}")
        else:
            print("  NOTHING ABOUT THIS JOURNEY LEFT THIS MACHINE.")
        print("-" * 68)
    print(W + "\n")


def main():
    global OSRM_BASE, GH_BASE
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
    p.add_argument("--escapes", type=int, default=0, metavar="N",
                   help="work out the way round a closure at N points along the route, "
                        "before you leave, and bake them into the offline page. Needs --gh.")
    p.add_argument("--out", default=None, help="write a page for your phone")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument("--verify", action="store_true",
                   help="second-source every stop against OpenStreetMap")
    p.add_argument("--gh", default=None,
                   help="GraphHopper URL for live rerouting, e.g. http://localhost:8989")
    args = p.parse_args()
    OSRM_BASE = args.osrm.rstrip("/")
    GH_BASE = args.gh.rstrip("/") if args.gh else None

    o = Opts(args.origin, args.dest, args.tank, args.mpg, args.fuel, args.reserve,
             args.depart, args.detour, args.units, args.offline, args.out, args.verify,
             args.gh, args.escapes)
    print("\nWorking...", file=sys.stderr)
    plan = build_plan(o)

    if args.json:
        # --out still has to write the file, and the privacy accounting goes
        # into the document instead of onto stdout, so the JSON stays JSON.
        if o.out:
            write_plan(o.out, plan)
        out = plan_json(plan)
        out["handout"] = o.out or None
        out["left_this_machine"] = list(LEAKED)
        print(json.dumps(out, indent=2))
        return

    W = "=" * 68
    print("\n" + W)
    print(f"  {o.origin.upper()}  to  {o.dest.upper()}")
    print(W)
    print(f"  Distance        {plan['total_miles']:.0f} miles"
          + (f"  (incl. {o.detour:.0f} mi diversion)" if o.detour else ""))
    print(f"  Driving time    {plan['total_hours']:.1f} hours")
    tz = plan.get("tz")
    zone = f"  ({tz.split('/')[-1].replace('_', ' ')} time)" if tz else ""
    print(f"  Departing       {plan['depart']:%a %d %b, %H:%M}{zone}")
    print(f"  Arriving        {plan['arrive']:%a %d %b, %H:%M}")
    if tz and plan.get("end_tz") and plan["end_tz"] != tz:
        # Lisbon to Madrid puts an hour on the clock somewhere round
        # Badajoz. Times here stay on the driver's watch, and each
        # forecourt is judged on its own.
        print(f"  Clocks          you cross into {plan['end_tz'].split('/')[-1]} time. "
              f"Times above are {tz.split('/')[-1]} time;")
        print("                  opening hours are checked where each station stands.")
    print(f"\n  In the tank     {o.fuel:.0f}% of {o.tank:.0f} {plan['vol']}")
    print(f"  Usable          {plan['usable_vol']:.1f} {plan['vol']} down to your {o.reserve:.0f}% reserve")
    print(f"  That's          {plan['range_miles']:.0f} miles at {o.mpg:.0f} mpg ({o.units.upper()})")

    if plan["incident_error"]:
        print("\n  " + "!" * 68)
        for w in plan["incident_error"].split("; "):
            print(f"  ! {w[:100]}")
        print("  " + "!" * 68)
    if plan["hits"]:
        print("\n" + "-" * 68)
        where = ", ".join(sorted(plan["roads"]))[:40] or "this route"
        print(f"  LIVE ON YOUR ROUTE  ({len(plan['hits'])} on {where})")
        print("-" * 68)
        for i in plan["hits"][:8]:
            print(f"  [{i['type'].upper()[:9]:9}] {clip(i['title'], 46)}")
            bits = [b for b in (i.get("why"), i["direction"],
                                f"since {i['start']}" if i["start"] else "") if b]
            print(f"              {'  |  '.join(bits)[:76]}")
            if i["desc"]:
                print(f"              {clip(i['desc'], 78)}")
    elif not plan["incident_error"]:
        print("\n  Live incidents: nothing reported on your roads right now.")

    if plan["makes_it"]:
        print(f"\n  VERDICT: you make it with {plan['spare']:.0f} miles to spare.")
        print("  No fuel stop needed. Enjoy the drive.")
        finish(o, args, plan, W)
        return

    print(f"\n  VERDICT: you run to reserve {-plan['spare']:.0f} miles short.")
    print(f"  You need fuel by mile {plan['range_miles']:.0f}.")
    for n in plan["notes"]:
        print(f"  {n}")

    scored = plan["scored"]
    print("\n" + "-" * 68)
    # "Nothing within range" and "we could not find out" are different
    # sentences and only one of them is safe to print as a fact. The source
    # is None when every fuel lookup failed, and the old code printed the
    # confident one either way, two lines under a note saying the lookup
    # had failed.
    if plan["source"] is None:
        print("  FUEL STOPS: NOT KNOWN")
        print("-" * 68)
        print("  The fuel lookup failed for this route, so this run cannot tell")
        print("  you where the stations are. That is not the same as there being")
        print("  none. Fill up before you set off and try again on a connection.")
        finish(o, args, plan, W)
        return
    bh = {s["bank_holiday"] for s in scored if s.get("bank_holiday")}
    if bh:
        print(f"  {' and '.join(sorted(bh))}. Holiday opening hours are in use below.")
    print(f"  REACHABLE STOPS, furthest first ({len(scored)} within range)")
    print("-" * 68)
    if not scored:
        print("  Nothing on the route within your range. Fuel before you set off.")
        finish(o, args, plan, W)
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
            price = f"  {money(v, s.get('currency'))} {g}"
        print(f"  [{flag}] mile {s['along']:5.0f}  ETA {s['eta']:%H:%M}  "
              f"{s['name'][:28]:30}{price:16} {note}")
        extras = []
        if s["off_route"] > 0.6: extras.append(f"{s['off_route']:.1f} mi off route")
        if s.get("motorway"): extras.append("motorway services")
        if s.get("bank_holiday"): extras.append(f"{s['bank_holiday']} hours")
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
            print(f"        Cheaper: {best['name']} at mile {best['along']:.0f}, "
                  f"{money(diff, best.get('currency'))} less.")
        if plan["later"]:
            print(f"        {plan['later']} station(s) further on are shut or unverified by then.")
        if pick.get("confirmed") is True:
            print("        Confirmed by two independent datasets.")
        elif pick.get("confirmed") is False:
            print("        Not in OpenStreetMap. Statutory register only. Ring ahead.")
        if plan.get("unverified_hours"):
            print("        Nobody published hours for this one. It may be shut.")
    else:
        print("  PLAN: nothing within range that is not known to be shut.")
        print("        Fuel before departure.")

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

    finish(o, args, plan, W)


if __name__ == "__main__":
    main()
