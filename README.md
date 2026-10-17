# fuelrun

**Will I make it, and will it be open when I get there?**

Satnavs will route you to a petrol station that shut an hour ago. They don't know how much fuel is in your tank, they don't know what time places close, and they don't know you have a ferry to catch. In a city that hardly matters. On a single-carriageway trunk road at nine at night it matters a great deal.

fuelrun answers one question before you set off, and it tells you plainly when it doesn't know.

```
====================================================================
  G1 1AB  to  KW14 8AA
====================================================================
  Distance        287 miles
  In the tank     60% of 55 L
  That's          254 miles at 45 mpg

  VERDICT: you run to reserve 33 miles short.

--------------------------------------------------------------------
  LIVE ON YOUR ROUTE  (4 on A9, M8, M80, M9)
--------------------------------------------------------------------
  [ROADWORKS] A9 Tummel Off Slip
              on A9  |  Northbound  |  since 9th of September
              The A9 northbound off-slip at Tummel is closed.

--------------------------------------------------------------------
  REACHABLE STOPS, furthest first
--------------------------------------------------------------------
  [OPEN  ] mile  233  ETA 15:33  Brora Fuel Express       1.79/L  24 hours
  [OPEN  ] mile  211  ETA 14:58  Tain Superstore          1.76/L  24 hours   toilets
  [SHUT  ] mile  185  ETA 14:30  Frasers Filling Station  1.80/L  shut at 14:30

====================================================================
  PLAN: fill at Brora Fuel Express Automat, mile 233, around 15:33.
        Ring ahead: +44345 602 9528
        Cheaper: Mfg Morrisons Alness at mile 197, 4.8p/L less.
====================================================================
```

## Why it exists

Every petrol station in Great Britain has been legally required to publish its location, **opening hours**, prices and amenities since 2 February 2026, under the Motor Fuel Price (Open Data) Regulations 2025. 8,000-odd forecourts. Free, under the Open Government Licence.

Almost nothing uses the opening hours.

OpenStreetMap knows where every petrol station on earth is. On a 110-mile stretch of the A9 it had opening hours for three stations out of thirteen. The statutory dataset had fourteen out of fourteen, with prices and phone numbers.

That gap is the whole project.

## What it does

- Routes A to B and works out where you hit your reserve
- Finds every fuel stop you can reach before that, **and whether it's open when you'd arrive**
- Flags live road closures and roadworks on the roads you actually drive
- Warns that a diversion invalidates your fuel plan, and recalculates with `--detour`
- Writes an offline page for your phone that needs no signal
- Tells you what left your machine, and can refuse to leak anything at all

## Install

Python 3.8 or later. No dependencies. Clone it and run it.

```bash
git clone https://github.com/YOURNAME/fuelrun
cd fuelrun
python3 fuelrun.py --from "Inverness" --to "Thurso" --tank 55 --mpg 42 --fuel 30
```

Optional, for UK users, one download and postcodes resolve offline forever:

```bash
./get-postcodes.sh
```

## Usage

```bash
# A journey, with a departure time so opening hours are checked against your real arrival
python3 fuelrun.py --from "G1 1AB" --to "KW14 8AA" \
    --tank 55 --mpg 45 --fuel 60 --depart 2026-10-18T07:00

# United States units
python3 fuelrun.py --from "Flagstaff, Arizona" --to "Las Vegas" \
    --units us --tank 14 --mpg 28 --fuel 45

# A closure forced a diversion. Does the fuel plan survive?
python3 fuelrun.py --from "Inverness" --to "Thurso" --fuel 30 --detour 60

# Write a page for your phone that works with no signal
python3 fuelrun.py --from "G1 1AB" --to "KW14 8AA" --fuel 60 --out plan.html

# Leak nothing. Needs the postcode file and a previously cached route.
python3 fuelrun.py --from "G1 1AB" --to "KW14 8AA" --fuel 60 --offline --privacy
```

| Flag | Meaning |
|---|---|
| `--from` `--to` | place name, UK postcode, or `lat,lon` |
| `--tank` `--mpg` `--fuel` | tank size, consumption, percent remaining |
| `--reserve` | percent you refuse to drop below (default 10) |
| `--depart` | ISO time. Opening hours are checked against your arrival, so this matters |
| `--detour` | extra miles from a diversion |
| `--units` | `uk` (litres, UK mpg) or `us` (US gallons, US mpg) |
| `--osrm` | your own routing server, e.g. `http://localhost:5000` |
| `--offline` | refuse any request that would reveal your destination |
| `--privacy` | report exactly what left the machine |
| `--out` | write the offline phone page |
| `--verify` | second-source every stop against OpenStreetMap |
| `--json` | machine-readable output |

## Verifying a stop

`--verify` cross-references every recommended stop against OpenStreetMap.

```
PLAN: fill at Brora Fuel Express Automat, mile 57, around 13:02.
      Confirmed by two independent datasets.

CROSS-CHECK: 10 of 14 confirmed by OpenStreetMap as well. 10 place(s) OSM lists
are absent from the statutory register and may have closed: Bannerman, Edgemoor
Filling Station, Gleaner, Morrisons
```

Every forecourt in Great Britain is legally required to be in the statutory
register. A place OpenStreetMap lists that the register does not has very
likely shut. On one test route, OSM was missing 29 real registered forecourts
and listed 3 that no longer appear anywhere official.

## Privacy

The tool reports on itself:

```
LEFT THIS MACHINE THIS RUN:
  OSRM          <- 55.861,-4.244 to 58.595,-3.520
```

Run the same journey again and it comes from cache, so nothing is sent. With `--offline` it refuses anything that would leak and says so.

Fuel and closure data are plain downloads. They reveal nothing about you. The two calls that can reveal a destination are geocoding and routing, and both can be eliminated:

- Type a postcode or `lat,lon`, which skips the geocoder entirely
- Run `./get-postcodes.sh` once for offline UK postcode lookup
- Run `./setup-local-routing.sh` for a routing engine on your own machine

Do all three and nothing about your journey leaves the building.

## Serve mode

For a box at home. Polls the feeds on your own network, keeps the data warm, answers your phone over your own VPN.

```bash
python3 fuelrun-serve.py --bind 0.0.0.0 --osrm http://localhost:5000
```

| Endpoint | What it gives you |
|---|---|
| `/live?roads=A9,A82` | **229 bytes of plain text.** Loads on one bar of signal. |
| `/plan?from=..&to=..&fuel=60` | the full plan as JSON |
| `/plan.html?from=..&to=..` | the offline page |
| `/health` | uptime, poll times, provider status |
| `/` | status page, readable on a phone |

Put it behind Tailscale or WireGuard. Do not expose it to the open internet.

## Data sources

| Country | Closures | Access |
|---|---|---|
| Scotland | Traffic Scotland | scraped public page |
| England | National Highways `trafficsearchapi` | their own JSON, no key |
| Wales | Traffic Wales | scraped public page |
| Northern Ireland | TrafficwatchNI | scraped roadworks table |
| Finland | Digitraffic DATEX II | open, no key |

Fuel stations and opening hours come from the **UK Fuel Finder** dataset (Open Government Licence v3.0) where available, and **OpenStreetMap** everywhere else. Routing is **OSRM**. Geocoding is **Nominatim**, **postcodes.io**, or the offline Ordnance Survey postcode file.

### Adding a country

Closures are a registry keyed by bounding box. A provider is a function returning normalised records and one entry in `PROVIDERS`.

Every EU member state is required to run a National Access Point under Directive 2010/40/EU, and DATEX II is the reference format. A generic DATEX II parser is included, so for most of Europe a country is one entry:

```python
{"name": "Somewhere", "box": (lat_min, lat_max, lon_min, lon_max), "match": "coords",
 "fn": lambda: p_datex2("Somewhere", "https://nap.example/datex2"), "note": "NAP feed"},
```

`match` says how an incident gets tied to a route:

- `roads` the feed gives road numbers
- `coords` the feed gives coordinates, matched within four miles of the route
- `all` the feed gives neither, so everything is shown with a label saying it could not be matched to your route

**A hard-won warning.** DATEX II standardises the envelope and leaves location referencing open. Finland publishes AlertC codes with no road number and no coordinates. Check what a feed actually contains before assuming route matching will work.

**Ireland** has no live incident feed. Its National Access Point is `data.gov.ie`, where TII publishes journey times, traffic counts and historic collision data. Their 2023 ITS progress report said incidents would join the DATEX II feed "later this year". A commented entry is waiting in `fuelrun.py`.

## Known limits

- **Scraped sources break.** Three of the five providers parse public web pages. The day a site is redesigned, that provider stops working. Each one fails loudly and the rest carry on.
- **Opening hours can be wrong.** Forecourt Trader found the Fuel Finder dataset had every supermarket site mislabelled. Ring ahead when it matters. The tool prints the phone number for that reason.
- **A closure snapshot is a snapshot.** The offline page states the time it was made and tells you to check before setting off.
- **Fuel consumption is a flat figure.** No modelling of hills, load, weather or speed.
- **UK only for statutory fuel hours.** Elsewhere it falls back to OpenStreetMap, where hours are patchy. It reports `no hours recorded` and leaves the guessing to you.

## Contributing

Adding a country is the most useful thing anyone can do. Open an issue with the feed URL and what it contains, or send a provider function and a registry entry.

Please keep it dependency-free. Standard library only, so it runs on anything with Python 3.

## Licence

**GNU Affero General Public License v3.0.** See `LICENSE`.

You may use, modify and sell this. What you may not do is take it private. If
you distribute a modified version, or run one as a network service, you must
offer your users the source under the same terms. The network clause is the
reason for AGPL over plain GPL: the obvious commercial form of this tool is a
website, and running a website is not "distributing" under GPL.

Data remains under its own terms. UK Fuel Finder and Ordnance Survey Code-Point Open are Open Government Licence v3.0, OpenStreetMap is ODbL, and each traffic authority sets its own. Credit them.
