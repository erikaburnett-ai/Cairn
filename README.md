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
        Confirmed by two independent datasets.
====================================================================
```

# fuelrun

Will I make it, and will it be open when I get there?

## The gap

The Motor Fuel Price (Open Data) Regulations 2025. Every forecourt in Great
Britain has been required since 2 February 2026 to register and publish:

```
forecourts.location.latitude            forecourts.fuel_price.E10
forecourts.location.longitude           forecourts.fuel_price.B7S
forecourts.opening_times.usual_days.monday.open_time
forecourts.opening_times.usual_days.monday.close_time
forecourts.opening_times.usual_days.monday.is_24_hours
forecourts.opening_times.bank_holiday.standard.open_time
forecourts.temporary_closure            forecourts.permanent_closure
forecourts.public_phone_number          forecourts.amenities.customer_toilets
```

8,139 forecourts. Open Government Licence v3.0. Almost nothing uses the hours.

Measured on one 110-mile stretch of the A9:

| Source | Stations found | With opening hours |
|---|---|---|
| OpenStreetMap | 13 | 3 |
| Fuel Finder | 14 | 14 |

## What it does

| | |
|---|---|
| Range | works out where the tank hits your reserve |
| Stops | every forecourt reachable before that, open at your arrival time |
| Closures | live, matched to the road numbers your route uses |
| Rerouting | locates a closure, blocks it, routes around it |
| Diversion | the extra miles feed back into the fuel plan |
| Offline | writes a page for your phone that needs no signal |
| Privacy | reports what left the machine, and can refuse to leak |

## Install

Python 3.8 or later. No dependencies.

```bash
git clone https://github.com/YOURNAME/fuelrun
cd fuelrun
python3 fuelrun.py --from "Inverness" --to "Thurso" --tank 55 --mpg 42 --fuel 30
```

UK postcodes, offline forever, one download:

```bash
./get-postcodes.sh
```

## Usage

```bash
# departure time matters: hours are checked against your real arrival
python3 fuelrun.py --from "G1 1AB" --to "KW14 8AA" \
    --tank 55 --mpg 45 --fuel 60 --depart 2026-10-18T07:00

# United States
python3 fuelrun.py --from "Flagstaff, Arizona" --to "Las Vegas" \
    --units us --tank 14 --mpg 28 --fuel 45

# route around live closures, on your own engine
python3 fuelrun.py --from "G1 1AB" --to "LL55 4NY" --fuel 85 \
    --gh http://localhost:8989

# a page for your phone
python3 fuelrun.py --from "G1 1AB" --to "KW14 8AA" --fuel 60 --out plan.html

# leak nothing
python3 fuelrun.py --from "G1 1AB" --to "KW14 8AA" --fuel 60 --offline --privacy
```

| Flag | |
|---|---|
| `--from` `--to` | place name, UK postcode, or `lat,lon` |
| `--tank` `--mpg` `--fuel` | size, consumption, percent remaining |
| `--reserve` | percent you refuse to drop below, default 10 |
| `--depart` | ISO time |
| `--detour` | extra miles, when rerouting is unavailable |
| `--units` | `uk` or `us` |
| `--gh` | GraphHopper URL for live rerouting |
| `--osrm` | your own routing server |
| `--offline` | refuse anything that would reveal a destination |
| `--privacy` | report what left the machine |
| `--verify` | second-source every stop against OpenStreetMap |
| `--out` | write the offline page |
| `--json` | machine-readable |

## Rerouting

Feeds say `M6 northbound between J37 and J38` and expect a human to know where
that is. Located in this order:

1. coordinates, when the feed gives them
2. junction numbers, against OSM `highway=motorway_junction` nodes near the route
3. other road numbers, triangulated against the route's own geometry
4. a place name, geocoded and snapped to the road

a 450-mile run the length of Britain: 34 of 37 located.

Blocking uses a GraphHopper custom model. Priority zero inside a polygon
removes that ground from the graph:

```json
"priority": [{"if": "in_shut0", "multiply_by": "0"}],
"areas": {"type": "FeatureCollection", "features": [...]}
```

Needs a GraphHopper on your own machine. See `setup-local-routing.sh`.

## Verifying a stop

```
PLAN: fill at Brora Fuel Express Automat, mile 57, around 13:02.
      Confirmed by two independent datasets.

CROSS-CHECK: 10 of 14 confirmed by OpenStreetMap as well. 10 place(s) OSM lists
are absent from the statutory register and may have closed: Bannerman, Edgemoor
Filling Station, Gleaner, Morrisons
```

Registration is a legal requirement. A forecourt OpenStreetMap lists that the
register does not has very likely shut.

## Privacy

```
LEFT THIS MACHINE THIS RUN:
  OSRM          <- 55.861,-4.244 to 58.595,-3.520
```

Run it again and the route comes from cache. With `--offline`:

```
NOTHING ABOUT THIS JOURNEY LEFT THIS MACHINE.
```

Fuel and closure data are plain downloads. Two calls can reveal a destination,
and both can be removed:

| Leak | Fix |
|---|---|
| Nominatim gets a place name | type a postcode or `lat,lon` |
| postcodes.io gets a postcode | `./get-postcodes.sh` |
| OSRM gets two coordinates | `./setup-local-routing.sh` |

## Serve mode

For a box at home. Polls on your own network, answers your phone over your own VPN.

```bash
python3 fuelrun-serve.py --bind 0.0.0.0 --osrm http://localhost:8989 --gh http://localhost:8989
```

| Endpoint | |
|---|---|
| `/live?roads=A9,A82` | 229 bytes of plain text. Loads on one bar. |
| `/plan?from=..&to=..&fuel=60` | JSON |
| `/plan.html?from=..&to=..` | the offline page |
| `/health` | uptime, poll times, per-provider health, rerouting state |

Put it behind Tailscale or WireGuard. Do not expose it to the open internet.

## Sources

| Country | Closures | Access |
|---|---|---|
| Scotland | Traffic Scotland | scraped |
| England | National Highways `trafficsearchapi` | their JSON, no key |
| Wales | Traffic Wales | scraped |
| Northern Ireland | TrafficwatchNI | scraped |
| Finland | Digitraffic | DATEX II, no key |

Fuel: UK Fuel Finder (OGL v3.0), OpenStreetMap elsewhere. Routing: OSRM or
GraphHopper. Geocoding: Nominatim, postcodes.io, or OS Code-Point Open.

### Adding a country

Directive 2010/40/EU requires every EU member state to run a National Access
Point, and DATEX II is the reference format. One entry:

```python
{"name": "Somewhere", "box": (lat_min, lat_max, lon_min, lon_max),
 "match": "coords",
 "fn": lambda: p_datex2("Somewhere", "https://nap.example/datex2")},
```

`match` says how an incident ties to a route:

| value | meaning |
|---|---|
| `roads` | the feed gives road numbers |
| `coords` | the feed gives coordinates, matched within four miles |
| `all` | neither, so everything is shown and labelled unmatched |

DATEX II standardises the envelope and leaves location referencing open.
Finland publishes AlertC codes with no road number and no coordinates. Check a
feed's contents before assuming route matching will work.

Ireland has no live incident feed. Its National Access Point is `data.gov.ie`,
where Transport Infrastructure Ireland publishes journey times, traffic counts
and 2014 collision data. A commented entry waits in `fuelrun.py`.

## Known limits

| | |
|---|---|
| Scraping | three of five providers parse web pages and will break on redesign |
| Data errors | Forecourt Trader found every supermarket site in Fuel Finder mislabelled |
| Snapshots | the offline page states when it was made |
| Consumption | one flat mpg figure, no hills, load, weather or speed |
| Hours outside GB | OpenStreetMap only, and it reports `no hours recorded` |

## Tests

```bash
python3 test_fuelrun.py          # logic only, no network, instant
python3 test_fuelrun.py --live   # also hits all seven live feeds
```

41 tests. The live ones break when a source changes its format, which is what
they are for.

## Provider health

A scraped page that gets redesigned does not raise an exception. It parses to
nothing, and the tool reports no incidents, which on a safety tool is worse
than crashing. Each provider's normal output is recorded and a collapse is
reported loudly:

```
!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
! Traffic Wales returned nothing. It normally gives about 8.
! Treat 'no incidents' from it as unknown until checked.
!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
```

## Contributing

Adding a country is the most useful thing anyone can do. Standard library only.

## Licence

GNU Affero General Public License v3.0. See `LICENSE`.

Use it, change it, sell it. Distribute a modified version or run one as a
network service and you must offer your users the source under the same terms.

Data keeps its own terms. UK Fuel Finder and OS Code-Point Open are OGL v3.0,
OpenStreetMap is ODbL, each traffic authority sets its own. Credit them.
