#!/usr/bin/env python3
"""fuelrun serve: the bit that lives on a box at home.

Runs on a machine you own. Polls the closure feeds on your own wifi, keeps
the fuel dataset warm in memory, and answers your phone over your own VPN.
No cloud, no subscription, nobody in the middle.

Standard library only. No pip install. Python 3.8 or later.

    python3 fuelrun-serve.py                  # localhost only
    python3 fuelrun-serve.py --bind 0.0.0.0   # reachable over your VPN

Endpoints:
    /                  status page, readable on a phone
    /live?roads=A9,A82 the car endpoint. Plain text, a few hundred bytes.
    /plan?from=..&to=..&fuel=60     full plan as JSON
    /plan.html?from=..&to=..        the offline handout
    /health            JSON, for checking it's alive
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

import argparse, json, sys, threading, time, traceback
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
import fuelrun as fr

STATE = {
    "closures": [],
    "closures_at": None,
    "closures_error": None,
    "fuel_count": 0,
    "fuel_at": None,
    "started": datetime.now(),
    "polls": 0,
    "serves": 0,
}
LOCK = threading.Lock()


# ----------------------------------------------------------------- poller

def poll_closures():
    found, problems = [], []
    for pr in fr.PROVIDERS:
        try:
            found += pr["fn"]()
        except Exception as e:
            problems.append(f"{pr['name']}: {str(e)[:60]}")
    with LOCK:
        if found or not STATE["closures"]:
            STATE["closures"] = found
            STATE["closures_at"] = datetime.now()
        STATE["closures_error"] = "; ".join(problems) or None
        STATE["polls"] += 1


def poll_fuel():
    try:
        rows = fr.load_fuelfinder(max_age_hours=6)
        with LOCK:
            STATE["fuel_count"] = len(rows)
            STATE["fuel_at"] = datetime.now()
    except Exception as e:
        print(f"  fuel refresh failed: {e}", file=sys.stderr)


def poller(closure_mins, fuel_hours):
    last_fuel = 0
    while True:
        try:
            poll_closures()
            if time.time() - last_fuel > fuel_hours * 3600:
                poll_fuel()
                last_fuel = time.time()
        except Exception:
            traceback.print_exc()
        time.sleep(closure_mins * 60)


# ----------------------------------------------------------------- handler

class H(BaseHTTPRequestHandler):
    server_version = "fuelrun/0.1"

    def log_message(self, fmt, *a):
        pass  # the default logger is noisy and leaks query strings

    def send(self, code, body, ctype="text/plain; charset=utf-8"):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        with LOCK:
            STATE["serves"] += 1
        try:
            if u.path == "/":
                self.send(200, self.status_page(), "text/html; charset=utf-8")
            elif u.path == "/health":
                self.send(200, json.dumps(self.health(), indent=2), "application/json")
            elif u.path == "/live":
                self.send(200, self.live(q))
            elif u.path in ("/plan", "/plan.html"):
                self.plan(u.path, q)
            else:
                self.send(404, "no such endpoint. try / for the list.\n")
        except Exception as e:
            traceback.print_exc()
            self.send(500, f"{type(e).__name__}: {e}\n")

    # ---- /live : the one that matters in a moving car

    def live(self, q):
        """Deliberately tiny. Plain text, no JSON, no markup.

        On one bar in the Highlands every byte is a second of waiting,
        so this returns a few hundred bytes and nothing else.
        """
        with LOCK:
            closures, at, err = list(STATE["closures"]), STATE["closures_at"], STATE["closures_error"]
        wanted = {r.strip().upper() for r in q.get("roads", "").split(",") if r.strip()}
        hits = [c for c in closures if (c["roads"] & wanted)] if wanted else closures
        hits.sort(key=lambda c: c["rank"])

        lines = [f"{at:%H:%M} {'(stale)' if at and (datetime.now() - at).seconds > 1800 else ''}".strip()]
        if err and not hits:
            lines.append(f"! {err[:60]}")
        if not hits:
            lines.append("clear" + (f" on {','.join(sorted(wanted))}" if wanted else ""))
        for c in hits[:10]:
            road = ",".join(sorted(c["roads"])) or "?"
            lines.append(f"{c['type'][:9].upper():9} {road:9} {c['title'][:34]}")
            if c["rank"] <= 1 and c["desc"]:
                lines.append(f"          {c['desc'][:60]}")
        return "\n".join(lines) + "\n"

    # ---- /plan and /plan.html

    def plan(self, path, q):
        if not q.get("from") or not q.get("to"):
            self.send(400, "need from= and to=\n")
            return
        o = fr.Opts(q["from"], q["to"],
                    tank=q.get("tank", 55), mpg=q.get("mpg", 42),
                    fuel=q.get("fuel", 50), reserve=q.get("reserve", 10),
                    depart=q.get("depart"), detour=q.get("detour", 0),
                    units=q.get("units", "uk"), offline=q.get("offline") == "1")
        plan = fr.build_plan(o)
        if path == "/plan":
            self.send(200, json.dumps(fr.plan_json(plan), indent=1), "application/json")
        else:
            import tempfile, pathlib
            f = pathlib.Path(tempfile.gettempdir()) / "fuelrun_plan.html"
            fr.write_plan(str(f), plan)
            self.send(200, f.read_bytes(), "text/html; charset=utf-8")

    # ---- status

    def health(self):
        with LOCK:
            s = dict(STATE)
        up = (datetime.now() - s["started"]).total_seconds()
        return {"ok": True, "uptime_seconds": int(up), "polls": s["polls"],
                "requests_served": s["serves"],
                "closures_known": len(s["closures"]),
                "closures_updated": s["closures_at"].isoformat(timespec="seconds") if s["closures_at"] else None,
                "closures_error": s["closures_error"],
                "fuel_stations": s["fuel_count"],
                "fuel_updated": s["fuel_at"].isoformat(timespec="seconds") if s["fuel_at"] else None,
                "providers": [p["name"] for p in fr.PROVIDERS]}

    def status_page(self):
        h = self.health()
        with LOCK:
            closures = sorted(STATE["closures"], key=lambda c: c["rank"])[:25]
        rows = "".join(
            f"<tr><td class='t{c['rank']}'>{c['type'][:14]}</td>"
            f"<td><b>{','.join(sorted(c['roads'])) or '?'}</b></td>"
            f"<td>{c['title'][:52]}</td></tr>" for c in closures) or \
            "<tr><td colspan=3>nothing reported</td></tr>"
        return f"""<!doctype html><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>fuelrun</title>
<style>body{{background:#101418;color:#eef2f5;font:16px/1.5 system-ui,sans-serif;margin:0;padding:18px}}
h1{{font-size:20px;margin:0 0 4px}} .m{{color:#9aa7b2;font-size:14px;margin-bottom:18px}}
table{{border-collapse:collapse;width:100%;font-size:14px}} td{{padding:7px 8px;border-bottom:1px solid #222b33;vertical-align:top}}
.t0{{color:#ff8080;font-weight:700}} .t1{{color:#ffc266}} .t2{{color:#ffe08a}} .t3,.t4{{color:#9aa7b2}}
code{{background:#192027;padding:2px 6px;border-radius:4px;font-size:13px}}
.e{{margin-top:20px;color:#9aa7b2;font-size:14px;line-height:2}}</style>
<h1>fuelrun</h1>
<div class=m>up {h['uptime_seconds'] // 3600}h {(h['uptime_seconds'] % 3600) // 60}m &middot;
{h['closures_known']} closures known &middot; {h['fuel_stations']} forecourts loaded &middot;
{h['polls']} polls &middot; {h['requests_served']} requests<br>
closures updated {h['closures_updated'] or 'never'}
{'<br><b style=color:#ff8080>' + h['closures_error'] + '</b>' if h['closures_error'] else ''}</div>
<table>{rows}</table>
<div class=e>
<code>/live?roads=A9,A82</code> plain text for the car<br>
<code>/plan?from=G1+1AB&amp;to=KW14+8AA&amp;fuel=60</code> JSON<br>
<code>/plan.html?from=...&amp;to=...</code> the offline handout<br>
<code>/health</code> JSON
</div>"""


# ----------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description="fuelrun, served from a box you own")
    ap.add_argument("--port", type=int, default=8847)
    ap.add_argument("--bind", default="127.0.0.1",
                    help="127.0.0.1 is local only. Use 0.0.0.0 to reach it over your VPN.")
    ap.add_argument("--closure-mins", type=float, default=4, help="how often to poll closures")
    ap.add_argument("--fuel-hours", type=float, default=6, help="how often to refresh fuel data")
    ap.add_argument("--osrm", default=None, help="your local routing server, e.g. http://localhost:5000")
    a = ap.parse_args()

    if a.osrm:
        fr.OSRM_BASE = a.osrm.rstrip("/")

    print(f"fuelrun serve")
    print(f"  listening   http://{a.bind}:{a.port}")
    print(f"  routing     {fr.OSRM_BASE}")
    print(f"  providers   {', '.join(p['name'] for p in fr.PROVIDERS) or 'none registered'}")
    print(f"  polling     closures every {a.closure_mins:g} min, fuel every {a.fuel_hours:g} h")
    if a.bind == "0.0.0.0":
        print("\n  Bound to all interfaces. Put this behind a VPN such as Tailscale")
        print("  or WireGuard. Do not expose it to the open internet.")
    print("\n  Warming up...")

    poll_fuel()
    poll_closures()
    with LOCK:
        print(f"  {STATE['fuel_count']} forecourts, {len(STATE['closures'])} live incidents")
        if STATE["closures_error"]:
            print(f"  note: {STATE['closures_error']}")

    threading.Thread(target=poller, args=(a.closure_mins, a.fuel_hours), daemon=True).start()
    srv = ThreadingHTTPServer((a.bind, a.port), H)
    print(f"\n  Ready. Ctrl-C to stop.\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.")


if __name__ == "__main__":
    main()
