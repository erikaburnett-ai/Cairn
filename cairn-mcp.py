#!/usr/bin/env python3
"""Cairn as an MCP server, so an agent can ask it a question directly.

JSON-RPC 2.0 over stdio, hand-rolled on the standard library, because Cairn
has no dependencies and this should not be the thing that gives it one.

    python3 cairn-mcp.py

Point a client at it:

    {"mcpServers": {"cairn": {
       "command": "python3", "args": ["/path/to/cairn/cairn-mcp.py"]}}}

Pass --gh and --osrm through to route on your own machine:

    {"command": "python3",
     "args": ["/path/to/cairn-mcp.py", "--gh", "http://localhost:8989"]}

stdout carries the protocol and nothing else. Everything human goes to
stderr. A stray print to stdout corrupts the stream and the client sees a
parse error with no explanation, so Cairn's own logging already writes to
stderr and must keep doing so.

Standing alone like this means Cairn works as a CLI, as an MCP server, and
as an upstream attached to a gateway, without requiring any of the three.
"""
#
#  Cairn. Copyright (C) 2026 Erika Burnett.
#
#  This program is free software: you can redistribute it and/or modify it
#  under the terms of the GNU Affero General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or (at
#  your option) any later version. See https://www.gnu.org/licenses/
#

import argparse, json, sys, traceback
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import cairn as c

PROTOCOL = "2025-06-18"
NAME, VERSION = "cairn", "0.1.0"

JOURNEY = {
    "type": "object",
    "properties": {
        "origin": {"type": "string", "description":
                   "Where the journey starts. A place name, a UK postcode, "
                   "or 'lat,lon'. Coordinates reveal nothing to a geocoder."},
        "destination": {"type": "string", "description": "Same forms as origin."},
        "tank_litres": {"type": "number", "default": 55},
        "mpg": {"type": "number", "default": 42, "description":
                "Fuel consumption. UK gallons unless units is 'us'."},
        "fuel_percent": {"type": "number", "default": 50, "description":
                         "How full the tank is now, 0 to 100."},
        "reserve_percent": {"type": "number", "default": 10, "description":
                            "The level the driver refuses to go below."},
        "depart": {"type": "string", "description":
                   "ISO 8601 departure time. Defaults to now, in the time "
                   "zone where the journey starts. Opening hours are checked "
                   "against predicted arrival, so this changes the answer."},
        "units": {"type": "string", "enum": ["uk", "us"], "default": "uk"},
        "escapes": {"type": "integer", "default": 0, "description":
                    "Work out the way round a closure at this many points "
                    "along the route, before departure. Needs a local "
                    "routing engine. 0 disables it."},
        "offline": {"type": "boolean", "default": False, "description":
                    "Refuse any call that would reveal the destination to a "
                    "third party. Needs cached or local geocoding and routing."},
    },
    "required": ["origin", "destination"],
}

TOOLS = [
    {"name": "plan_journey",
     "description":
        "Answer three questions about a drive: will the tank reach the "
        "destination, will a filling station be open at the predicted "
        "arrival time, and which way round if a road is shut. Returns "
        "distance, time, range, every reachable forecourt with its opening "
        "state and prices, live closures on the roads used, and any "
        "diversion. Covers the UK, Ireland and 14 EU member states for "
        "closures, and five national fuel registers.",
     "inputSchema": JOURNEY},

    {"name": "road_closures",
     "description":
        "Live closures and incidents on specific numbered roads, from 19 "
        "national road authorities. Cheap and fast; use this when the "
        "question is only about the roads and not about fuel. Give a "
        "position too, because road numbers repeat across countries and an "
        "Austrian A9 is not a Scottish one.",
     "inputSchema": {
        "type": "object",
        "properties": {
            "roads": {"type": "array", "items": {"type": "string"},
                      "description": "Road numbers, for example ['A9','M80']."},
            "near": {"type": "string", "description":
                     "'lat,lon' to limit the answer to the countries covering "
                     "that point. Without it, results are tagged by country "
                     "and may include the same number elsewhere."},
        },
        "required": ["roads"]}},

    {"name": "forecourts_near",
     "description":
        "Filling stations near a point, with statutory opening hours, "
        "whether each is open at a given time, prices per grade, and "
        "amenities. Opening hours come from a national register where one "
        "exists and from OpenStreetMap otherwise, and the source is stated "
        "per station.",
     "inputSchema": {
        "type": "object",
        "properties": {
            "at": {"type": "string", "description": "'lat,lon'."},
            "when": {"type": "string", "description":
                     "ISO 8601. Defaults to now in that location's time zone."},
            "radius_miles": {"type": "number", "default": 5},
            "limit": {"type": "integer", "default": 12},
        },
        "required": ["at"]}},
]

ARGS = None


# ----------------------------------------------------------------- tools

def t_plan_journey(a):
    o = c.Opts(a["origin"], a["destination"],
               tank=a.get("tank_litres", 55), mpg=a.get("mpg", 42),
               fuel=a.get("fuel_percent", 50), reserve=a.get("reserve_percent", 10),
               depart=a.get("depart"), units=a.get("units", "uk"),
               offline=bool(a.get("offline")), gh=ARGS.gh,
               escapes=int(a.get("escapes", 0)))
    before = len(c.LEAKED)
    plan = c.build_plan(o)
    out = c.plan_json(plan)
    # An agent acting for somebody deserves to know what the answer cost
    # them in privacy, in the same payload as the answer.
    out["left_this_machine"] = list(c.LEAKED[before:])
    return out


def t_road_closures(a):
    roads = {str(r).strip().upper() for r in a.get("roads") or [] if str(r).strip()}
    if not roads:
        raise ValueError("give at least one road number")
    near = a.get("near")
    point = None
    if near:
        try:
            lat, lon = [float(x) for x in str(near).split(",")]
            point = (lat, lon)
        except ValueError:
            raise ValueError("near must be 'lat,lon'")

    found, problems = [], []
    for pr in c.PROVIDERS:
        if pr.get("per_road"):
            continue                       # needs a route to know what to ask
        if point and not c._inside(pr["box"], *point):
            continue
        nb = pr.get("not_box")
        if point and nb and c._inside(nb, *point):
            continue
        try:
            got = pr["fn"]()
        except Exception as e:
            problems.append(f"{pr['name']}: {str(e)[:60]}")
            continue
        warn = c._health_record(pr["name"], len(got))
        if warn:
            problems.append(warn)
        for i in got:
            if i["roads"] & roads:
                found.append({"country": pr["name"], "type": i["type"],
                              "title": i["title"], "roads": sorted(i["roads"]),
                              "direction": i["direction"], "since": i["start"],
                              "rank": i["rank"], "desc": i["desc"][:300]})
    found.sort(key=lambda i: i["rank"])
    return {"asked": sorted(roads), "near": near,
            "scoped_to_point": bool(point),
            "count": len(found), "closures": found[:40],
            "warnings": problems or None,
            "note": None if point else
                    "No position given, so road numbers may match the same "
                    "number in another country. Each item is tagged.",
            "checked_at": datetime.now().astimezone().isoformat(timespec="seconds")}


def t_forecourts_near(a):
    try:
        lat, lon = [float(x) for x in str(a["at"]).split(",")]
    except (ValueError, KeyError):
        raise ValueError("at must be 'lat,lon'")
    when = c.parse_when(a["when"]) if hasattr(c, "parse_when") and a.get("when") else None
    if a.get("when") and when is None:
        when = datetime.fromisoformat(a["when"])
    when = when or datetime.now()
    radius = float(a.get("radius_miles", 5))
    limit = int(a.get("limit", 12))

    # Same path build_plan uses: every national register whose box contains
    # the point, falling back to OpenStreetMap where none does. A point in
    # Spain gets the Spanish register, not a British one and not a guess.
    here = [(lat, lon)]
    stations, notes = [], []
    for reg in c.FUEL_REGISTERS:
        if not c._inside(reg["box"], lat, lon):
            continue
        try:
            rows = c._near(reg["load"](), here, corridor_miles=radius)
            if rows and reg.get("enrich"):
                rows = reg["enrich"](rows)
            stations += rows
        except Exception as e:
            notes.append(f"{reg['name']} unavailable: {str(e)[:60]}")
    if not stations:
        try:
            stations = c.fuel_near_route(here, radius_m=int(radius * 1609))
        except Exception as e:
            notes.append(f"OpenStreetMap unavailable: {str(e)[:60]}")
    hol = c.bank_holidays()
    out = []
    for s in stations:
        d = c.haversine_miles((lat, lon), (s["lat"], s["lon"]))
        if d > radius:
            continue
        if s.get("ff_hours"):
            is_open, shuts = c.ff_open_at(s, when, hol), c.ff_closes_at(s, when, hol)
            bh = c.ff_bank_holiday(s, when, hol)
        else:
            is_open, shuts = c.parse_hours(s.get("hours"), when), None
            bh = None
        out.append({"name": s["name"], "lat": s["lat"], "lon": s["lon"],
                    "miles_away": round(d, 2), "open": is_open, "shuts": shuts,
                    "bank_holiday": bh, "prices": s.get("prices") or {},
                    "phone": s.get("phone", ""), "toilets": bool(s.get("toilets")),
                    "motorway_services": bool(s.get("motorway")),
                    "hours_source": s.get("source", "openstreetmap")})
    out.sort(key=lambda s: s["miles_away"])
    return {"at": [lat, lon], "when": when.isoformat(timespec="minutes"),
            "radius_miles": radius, "count": len(out), "forecourts": out[:limit],
            "warnings": notes or None,
            "note": "open is true, false, or null meaning nobody published hours."}


HANDLERS = {"plan_journey": t_plan_journey,
            "road_closures": t_road_closures,
            "forecourts_near": t_forecourts_near}


# ----------------------------------------------------------------- protocol

def reply(msg_id, result=None, error=None):
    m = {"jsonrpc": "2.0", "id": msg_id}
    m["error" if error else "result"] = error or result
    sys.stdout.write(json.dumps(m) + "\n")
    sys.stdout.flush()


def handle(msg):
    mid, method = msg.get("id"), msg.get("method")
    params = msg.get("params") or {}

    if method == "initialize":
        return reply(mid, {
            "protocolVersion": PROTOCOL,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": NAME, "version": VERSION},
            "instructions":
                "Cairn answers whether a drive is possible on the fuel in the "
                "tank, whether the filling station will be open on arrival, "
                "and the way round a closure. Prefer road_closures when the "
                "question is only about roads; plan_journey does far more "
                "work. Give coordinates as 'lat,lon' when you have them, "
                "because a place name is sent to a geocoder and a postcode "
                "to a postcode service, and both reveal where somebody is "
                "going. Every answer reports what left the machine."})

    if method in ("notifications/initialized", "notifications/cancelled"):
        return                                        # notifications get no reply

    if method == "ping":
        return reply(mid, {})

    if method == "tools/list":
        return reply(mid, {"tools": TOOLS})

    if method == "tools/call":
        name = params.get("name")
        fn = HANDLERS.get(name)
        if not fn:
            return reply(mid, error={"code": -32602, "message": f"no tool named {name}"})
        try:
            result = fn(params.get("arguments") or {})
            text = json.dumps(result, indent=1, default=str)
            return reply(mid, {"content": [{"type": "text", "text": text}],
                               "isError": False})
        except SystemExit as e:
            # Cairn exits on a bad postcode or an offline refusal. That is an
            # answer, so it returns as one instead of killing the server.
            return reply(mid, {"content": [{"type": "text", "text": str(e)}],
                               "isError": True})
        except Exception as e:
            traceback.print_exc(file=sys.stderr)
            return reply(mid, {"content": [{"type": "text",
                                            "text": f"{type(e).__name__}: {e}"}],
                               "isError": True})

    if mid is not None:
        reply(mid, error={"code": -32601, "message": f"unknown method {method}"})


def main():
    global ARGS
    p = argparse.ArgumentParser(description="Cairn as an MCP server")
    p.add_argument("--gh", default=None,
                   help="GraphHopper on this machine, for routing and escapes")
    p.add_argument("--osrm", default=None, help="OSRM on this machine")
    ARGS = p.parse_args()
    if ARGS.osrm:
        c.OSRM_BASE = ARGS.osrm.rstrip("/")
    if ARGS.gh:
        c.GH_BASE = ARGS.gh.rstrip("/")

    print(f"{NAME} {VERSION} on stdio. "
          f"{len(c.PROVIDERS)} closure sources, {len(c.FUEL_REGISTERS)} fuel registers. "
          f"routing: {ARGS.gh or c.OSRM_BASE}", file=sys.stderr)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            reply(None, error={"code": -32700, "message": "parse error"})
            continue
        try:
            handle(msg)
        except Exception:
            traceback.print_exc(file=sys.stderr)
            if msg.get("id") is not None:
                reply(msg["id"], error={"code": -32603, "message": "internal error"})


if __name__ == "__main__":
    main()
