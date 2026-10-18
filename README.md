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

8,139 forecourts, Open Government Licence v3.0, free to anybody who asks for
them, and the per-day opening hours sitting inside every one of those records
go almost entirely unread by the apps people actually use to find fuel.

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
| Closures | live, from 17 national sources, matched to the roads you use |
| Fuel data | four statutory registers: Britain, France, Spain, Portugal. 32,000 forecourts with published hours |
| Bank holidays | the statutory holiday timetable, per nation, where it differs |
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

```bash
./setup-local-routing.sh great-britain    # port 8989
./setup-local-routing.sh ireland          # port 8991, Ireland and NI
IMPORT_ONLY=1 ./setup-local-routing.sh ireland   # build the graph, do not serve
```

Each region gets its own port, config and graph folder, so two can be built
once and either served later. Great Britain is 2.2 GB of map and wants an 8 GB
heap. Ireland and Northern Ireland together are 400 MB and want 5 GB.

```
Dublin to Belfast, routed entirely on this machine
  102 miles, 1.7 h
  roads: A1, A12, B38, M1, M50, N1, R132
```

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

With a local GraphHopper the base route is computed there too, so `--offline`
works without Docker and without an admin password:

```
  Phone handout written to plan.html
--------------------------------------------------------------------
  NOTHING ABOUT THIS JOURNEY LEFT THIS MACHINE.
--------------------------------------------------------------------
```

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

## Bank holidays

All 8,138 registered forecourts publish a separate bank holiday timetable.
1,912 of them differ from the matching weekday. At six on Christmas morning,
1,168 give a different answer once it is read.

Three calendars, because the nations do not share one. Scotland keeps
2 January and St Andrew's Day. Northern Ireland keeps St Patrick's Day and
the Twelfth. The dates come from `gov.uk/bank-holidays.json`, free and
without a key, and which calendar a forecourt keeps is worked out from its
own coordinates.

```
  St Andrew's Day. Holiday opening hours are in use below.
  REACHABLE STOPS, furthest first (14 within range)
--------------------------------------------------------------------
  [OPEN  ] mile   57  ETA 08:14  Brora Fuel Express Automat  1.79/L  24 hours
            St Andrew's Day hours
  [SHUT  ] mile   35  ETA 07:45  Morangie Filling Station    1.80/L  shut at 07:45
            St Andrew's Day hours
```

That second one reads OPEN on its weekday row.

## Sources

| Country | Source | Access |
|---|---|---|
| Scotland | Traffic Scotland | scraped |
| England | National Highways `trafficsearchapi` | their JSON, no key |
| Wales | Traffic Wales | scraped |
| Northern Ireland | TrafficwatchNI | scraped |
| Ireland | Transport Infrastructure Ireland | GraphQL behind their map |
| Germany | autobahn.de | per road, no key |
| France | Bison Fute | one file per event, no key |
| Austria | ASFINAG public RSS | no key |
| Luxembourg | CITA | DATEX II v3.6, CC0 |
| Slovenia | DARS | Atom feed, no key |
| Spain | DGT | DATEX II, no key |
| Netherlands | NDW | DATEX II, no key |
| Norway | the service behind vegvesen.no's own map | no key, undocumented |
| Denmark | Vejdirektoratet map layers | open bucket, no key |
| Sweden | the Skane roadworks Denmark relays | roadworks only, southern tip only |
| Finland | Digitraffic | DATEX II, no key |

About 10,000 live incidents. 4.2% carry a type this tool cannot rank, and
nearly all of those are the Dutch feed's literal `other`.

### "Registration required" usually is not

Public directories list England, Scotland, Austria and Slovenia as needing an
account, and all four are wired up here without one, because the registration
attaches to the official DATEX II feed and not to the authority's own public
channels. Those are open. The same authority is often publishing the
same incidents on a web page, an RSS feed or the JSON behind their own map,
and those are open.

| Country | Official feed | What this uses instead |
|---|---|---|
| England | account | the JSON behind their traffic map |
| Scotland | account | the public incident page |
| Austria | `contentportal.asfinag.at`, account | `publiccontent.asfinag.at` RSS |
| Slovenia | account | the DARS B2B Atom feed |

Look for the public route before writing a country off.

Routing: OSRM or GraphHopper. Geocoding: Nominatim, postcodes.io, or OS
Code-Point Open.

### The fuel registers

Britain is not the only country that made publishing compulsory, and the
other two are just as open.

| Country | Register | Forecourts | Hours | Prices |
|---|---|---|---|---|
| Great Britain | Fuel Finder, Motor Fuel Price (Open Data) Regulations 2025 | 8,139 | per day, plus a bank holiday row | 6 grades |
| France | prix-carburants, Licence Ouverte | 9,799 | per day, plus a 24-hour card flag | 5 grades |
| Spain | MITECO, Real Decreto-ley 6/2000 | 11,429 | per day, lunch closings included | 11 grades |
| Portugal | DGEG | 3,118 | weekday, Saturday, Sunday and a holiday row | 3 grades |

32,485 forecourts with hours published by law. Everywhere else still falls
back to OpenStreetMap. That knows where the stations are. Three times in
four it has no opening hours for them.

Two things the other registers teach that Britain's does not.

France publishes `automate-24-24`. The kiosk shuts at eight and the card
machine runs all night, so 5,592 of its 9,799 forecourts are usable at three
in the morning while their published staffed hours say closed. Reading the
staffed hours alone drives somebody past a working pump.

Spain publishes lunch. 1,165 distinct timetable strings, many of the form
`L-V: 08:00-14:00 y 16:00-21:00`, so a day can have two windows with a hole
in the middle. Britain's register has one window a day and the model here had
to grow a second before Spain could be read at all. Spanish day letters run
L M X J V S D, so X is Wednesday.

Portugal keeps its hours one call deeper than its prices. Asking for all
three thousand would be a lot of calls for one journey, so the hours are
fetched only for the stations that make the shortlist on your route, and
kept once they are.

France and Spain publish no public holiday row. Britain and Portugal do, and
this tool reads both, which for Portugal means working out Easter, since
three of its thirteen national days move with it. Municipal holidays are not
in the data: Lisbon shuts for St Anthony on 13 June and Porto for St John on
the 24th, and no register has a field for that. On 1 November in Spain you
are on your own.

Opening hours now carry three states. Open, shut, and nobody said. 602 French forecourts are marked closed on a Sunday by the register
itself, and reporting those as "hours unknown" threw away the half of the
answer that was there.

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

Ireland's National Access Point carries journey times, traffic counts and
2014 collision data, with no incident publication at all, so the entry here
reads the GraphQL that sits behind Transport Infrastructure Ireland's own
public map. That is undocumented and can change without notice, and the
provider health check is what catches it when it does.

## Known limits

| | |
|---|---|
| Scraping | 7 of 17 providers read a web page or an undocumented API and will break on redesign |
| Coverage | 11 EU member states of 27, plus the UK and Norway. Most of the rest put their feed behind an account |
| Sweden | only the Skane roadworks Denmark relays. Trafikverket's own feed needs an account |
| Borders | road numbers repeat across countries, and bounding boxes overlap at the border |
| Data errors | Forecourt Trader found every supermarket site in Fuel Finder mislabelled |
| Snapshots | the offline page states when it was made |
| Consumption | one flat mpg figure, no hills, load, weather or speed |
| Hours outside GB, France and Spain | OpenStreetMap only, and it reports `no hours recorded` |
| Holidays outside GB | no register outside Britain publishes a holiday timetable |
| Overpass | two mirrors left, both rate-limited. An empty answer from one is treated as a failure, not an answer |

## Tests

```bash
python3 test_fuelrun.py          # logic only, no network, instant
python3 test_fuelrun.py --live   # also hits every live feed
```

60 tests. The live ones break when a source changes its format, which is what
they are for. Several exist because of a bug that shipped: five feeds reported
a coned-off lane as a shut road, two early returns swallowed the `--out` file
without saying so, and cached incidents kept a severity judgement that had
since been corrected.

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
