```

====================================================================
  G1 1AB  to  KW14 8AA
====================================================================
  Distance        277 miles
  Driving time    5.7 hours
  Departing       Mon 19 Oct, 07:00  (London time)
  Arriving        Mon 19 Oct, 12:44

  In the tank     60% of 55 L
  Usable          27.5 L down to your 10% reserve

  VERDICT: you run to reserve 5 miles short.
  You need fuel by mile 272.

  LIVE ON YOUR ROUTE  (10 on A8, A803, A9, B9154, M8, M80, M9)
--------------------------------------------------------------------
  [CLOSURE  ] A78 Dunlop Street to A8 Ratho Street
              on A8  |  Both Directions  |  since 3rd of August 20
              The A78 Dunlop Street to A8 Ratho Street is currently closed in both direction

  REACHABLE STOPS, furthest first (89 within range)
--------------------------------------------------------------------
  [OPEN  ] mile   224  ETA 11:38  Brora Fuel Express Automat      £1.79/L E10    24 hours
  [OPEN  ] mile   223  ETA 11:38  Sutherland Arms Garage Ltd      £1.79/L E10    shuts 20:00
            toilets
  [OPEN  ] mile   208  ETA 11:18  Evelix Service Station          £1.80/L E10    shuts 20:00
            toilets

  PLAN: fill at Brora Fuel Express Automat, mile 224, around 11:38.
        Ring ahead: +44345 602 9528
        Cheaper: Mfg Morrisons Glasgow Auchinlea at mile 5, 8.8p/L less.
```

# Cairn

Will I make it, will it be open when I get there, and which way do I go if
the road is shut?

A cairn is a pile of stones on a hill path, left by somebody who walked it
before you. No power, no signal, no account. It is simply there when the
weather closes in and you cannot see the way.

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

8,138 forecourts, Open Government Licence v3.0, free to anybody who asks for
them. Most apps built on this dataset read the prices and leave the opening
hours alone. Fuel Finder UK is the exception found so far: since 14 October
2026 its route planner has estimated whether a station will be open when you
arrive. So the hours are in use elsewhere. What has not turned up anywhere
else is doing it offline, on hardware you own, alongside live closures from
eighteen national road authorities.

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
| Closures | live, from 19 national sources, matched to the roads you use |
| Fuel data | five national registers: Britain, France, Spain, Portugal, Italy. 53,965 forecourts |
| Bank holidays | the statutory holiday timetable, per nation, where it differs |
| Rerouting | locates a closure, blocks it, routes around it |
| Diversion | the extra miles feed back into the fuel plan |
| Offline | writes a page for your phone that needs no signal |
| Time | every clock is the clock where the driving happens, not this machine's |
| Privacy | reports what left the machine, and can refuse to leak |

## Install

Python 3.8 or later. No dependencies.

```bash
git clone https://github.com/YOURNAME/cairn
cd cairn
python3 cairn.py --from "Inverness" --to "Thurso" --tank 55 --mpg 42 --fuel 30
```

UK postcodes, offline forever, one download:

```bash
./get-postcodes.sh
```

## Usage

```bash
# departure time matters: hours are checked against your real arrival
python3 cairn.py --from "G1 1AB" --to "KW14 8AA" \
    --tank 55 --mpg 45 --fuel 60 --depart 2026-10-18T07:00

# United States
python3 cairn.py --from "Flagstaff, Arizona" --to "Las Vegas" \
    --units us --tank 14 --mpg 28 --fuel 45

# route around live closures, on your own engine
python3 cairn.py --from "G1 1AB" --to "LL55 4NY" --fuel 85 \
    --gh http://localhost:8989

# a page for your phone
python3 cairn.py --from "G1 1AB" --to "KW14 8AA" --fuel 60 --out plan.html

# leak nothing
python3 cairn.py --from "G1 1AB" --to "KW14 8AA" --fuel 60 --offline --privacy
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
| `--escapes N` | work out the way round a closure at N points, before you leave |
| `--osrm` | your own routing server |
| `--offline` | refuse anything that would reveal a destination |
| `--privacy` | report what left the machine |
| `--verify` | second-source every stop against OpenStreetMap |
| `--out` | write the offline page |
| `--json` | machine-readable |

## If the road ahead is shut

The plan answers "will this journey work". It does not answer the question
you have at eleven at night, on an unlit road, with a POLICE ROAD CLOSED sign
across it and no signal. Which way now.

`--escapes N` walks the route before you leave, pretends each of N points is
shut, works out the way round each one, and bakes the answers into the
offline page.

```bash
python3 cairn.py --from "Belfast" --to "Cork" --fuel 60 \
    --gh http://localhost:8991 --escapes 8 --out plan.html
```

```
If the road ahead is shut
  mile 174 on M8
  Come off at mile 159, rejoin at mile 189
  M7 -> R433 -> L5559
  +4 miles

  mile 206 on M8
  Come off at mile 191, rejoin at mile 221
  N24 -> R670 -> R665
  +9 miles
```

The page reads your phone's GPS, which works with no signal, and says which
marked point you are nearest to. No part of the page touches a network, so it
opens the same in Caithness as it does at home.

Two things took getting right. Blocking half a mile around a point lets a
parallel service road absorb it and the detour reads as nothing, so the block
is the length of a real closed section. And the diversion is routed from a
few miles before the closure to a few miles after, because routing origin to
destination around the point hands back a whole different journey when what
the driver wants is where to come off and where to rejoin. Irish and Highland
junctions can be fifteen miles apart, so the window widens until it finds one.

A point with no way round at any width is reported as exactly that. Being
told before you leave that a closure there leaves you nowhere to go is worth
more than any diversion.

Needs a GraphHopper on your own machine, because it is one routing query per
sample and nobody should aim that at a public server.

## Rerouting

Feeds say `M6 northbound between J37 and J38` and expect a human to know where
that is. Located in this order:

1. coordinates, when the feed gives them
2. junction numbers, against OSM `highway=motorway_junction` nodes near the route
3. other road numbers, triangulated against the route's own geometry
4. a place name, geocoded and snapped to the road

On one 450-mile run the length of Britain, 34 of 37 located.

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

## As an MCP server

So an agent can ask Cairn a question directly, without being handed a shell.

```json
{"mcpServers": {"cairn": {
   "command": "python3",
   "args": ["/path/to/cairn/cairn-mcp.py", "--gh", "http://localhost:8989"]}}}
```

| Tool | |
|---|---|
| `plan_journey` | the full answer: range, stops with opening state, closures, diversions |
| `road_closures` | live closures on named roads. Takes `near` so an Austrian A9 is not a Scottish one |
| `forecourts_near` | stations at a point, with hours, prices and amenities |

JSON-RPC 2.0 over stdio, standard library, no Node and no SDK. Every answer
carries `left_this_machine`, so an agent acting for somebody can report what
the answer cost them in privacy.

It also attaches as an upstream to a gateway such as
[ToolFunnel](https://github.com/Rendeverance/toolfunnel), which is
worth having if you run many tools behind one policy. Cairn does not need
one, and keeping it that way is deliberate: a dependency on a gateway would
end the no-dependencies promise above.

## Serve mode

For a box at home. Polls on your own network, answers your phone over your own VPN.

```bash
python3 cairn-serve.py --bind 0.0.0.0 --osrm http://localhost:8989 --gh http://localhost:8989
```

| Endpoint | |
|---|---|
| `/live?roads=A9,A82&near=56.8,-3.9` | 229 bytes of plain text. Loads on one bar. |
| `/plan?from=..&to=..&fuel=60` | JSON |
| `/plan.html?from=..&to=..` | the offline page |
| `/health` | uptime, poll times, per-provider health, rerouting state |

Put it behind Tailscale or WireGuard. Do not expose it to the open internet.

Every count in this file was measured on 18 October 2026. Registers change
daily as forecourts join and leave. Treat every figure here as a reading taken
on one morning.

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
| Croatia | HAK | their road status page, with their own diversions |
| Italy | Autostrade per l'Italia's own map files | no key, their network only |
| Netherlands | NDW | DATEX II, no key |
| Norway | the service behind vegvesen.no's own map | no key, undocumented |
| Denmark | Vejdirektoratet map layers | open bucket, no key |
| Sweden | the Skane roadworks Denmark relays | roadworks only, southern tip only |
| Finland | Digitraffic | DATEX II, no key |

About 10,000 live incidents across 14 EU member states, 52% of them, holding 74% of the EU's people, plus the four UK nations and Norway. 4.2% carry a type this tool cannot rank, and
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

This is not a preference. Several National Access Points will only register
a citizen or a company of their own member state. So "registration required" on a European feed means
permanently shut here, and a country is covered through its public channels
or it is not covered. Every provider in the table above needs no account. That is a hard
requirement here.

Routing: OSRM or GraphHopper. Geocoding: Nominatim, postcodes.io, or OS
Code-Point Open.

### The fuel registers

Britain is not the only country that made publishing compulsory, and the
other two are just as open.

| Country | Register | Forecourts | Hours | Prices |
|---|---|---|---|---|
| Great Britain | Fuel Finder, Motor Fuel Price (Open Data) Regulations 2025 | 8,138 | per day, plus a bank holiday row | 6 grades |
| France | prix-carburants, Licence Ouverte | 9,799 | per day, plus a 24-hour card flag | 5 grades |
| Spain | MITECO, Real Decreto-ley 6/2000 | 11,429 | per day, lunch closings included | 11 grades |
| Portugal | DGEG | 3,118 | weekday, Saturday, Sunday and a holiday row | 3 grades |
| Italy | MIMIT, through a daily mirror | 21,481 | none published | 2 grades |

32,485 forecourts with hours published by law, and 21,481 more in Italy
with prices and no hours at all. Everywhere else still falls
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

## What time is it there

This was found on a machine whose clock sat eight hours from the route, planning a run from
Lisbon. The tool read the machine's clock, got 17:48, checked Portuguese
opening hours against it, and called every shut forecourt open. It was
01:48 in Portugal.

So no time in this tool comes from the machine any more.

- Departure, when you do not give one, is now where the car is.
- Times printed stay on the driver's watch, the clock at the start of the run.
- Opening hours are checked where each forecourt stands. A Lisbon to Madrid
  journey puts an hour on somewhere round Badajoz, and the stations on the
  far side are judged in Spanish time.
- A forecourt that came out of a national register is in that nation,
  already established by the government that published it. Spain's register
  runs to the Canaries, which are an hour behind Madrid.
- Everything else falls to a table of boxes. Portugal and Spain are an hour
  apart across a 754 mile land border, so Portugal gets a rough outline
  instead: a box round it swallows Badajoz, Merida, Caceres, Huelva, Zamora
  and Vigo. Tested against 25 towns, it places 22 and misses three that sit
  within two miles of the line, where the border is the middle of a river.
- Norwegian Finnmark pokes into the Finnish box, and those two are an hour
  apart, so the Finnish entry carries two holes for it.

### The three countries that are shut

Recorded so the next person does not have to find out.

| Country | What stops it |
|---|---|
| Czechia | `dopravniinfo.cz` does not answer a connection from here. Not a 403, no SYN-ACK at all, on every address tried. |
| Poland | `drogi.gddkia.gov.pl` sits behind Incapsula bot protection, and getting round that is not something this tool is going to do. |
| Switzerland | The only incident feed is `api.opentransportdata.swiss`, token required. The EU access points want a member-state entity. A Swiss token looks obtainable with an email address. Not wired up. |

Italy's ministry host behaves like Czechia's, which is why the Italian fuel
data comes through a mirror and the closures come from the motorway operator.

## Known limits

| | |
|---|---|
| Scraping | 7 of 17 providers read a web page or an undocumented API and will break on redesign |
| Coverage | 12 EU member states of 27, plus the UK and Norway. Most of the rest put their feed behind an account |
| Sweden | only the Skane roadworks Denmark relays. Trafikverket's own feed needs an account, and that door is shut for good |
| Borders | road numbers repeat across countries, and bounding boxes overlap at the border |
| Data errors | Forecourt Trader found every supermarket site in Fuel Finder mislabelled |
| Snapshots | the offline page states when it was made |
| Consumption | one flat mpg figure, no hills, load, weather or speed |
| Border clocks | Miranda do Douro, Vila Real de Santo Antonio and Tui read as the wrong side, each about two miles from the line |
| Hours outside GB, France, Spain and Portugal | none. Italy's register has no hours field, and everywhere else falls to OpenStreetMap |
| Italy | Autostrade's network only, so no ANAS state roads and no other concessionaire. Fuel prices come through a volunteer's mirror |
| Zone labels | Munich prints as Vienna time and Nice as Rome time. Same clock to the minute, wrong city on the label |
| Holidays outside GB | no register outside Britain publishes a holiday timetable |
| Overpass | two mirrors left, both rate-limited. An empty answer from one is treated as a failure, not an answer |

## Tests

```bash
python3 test_cairn.py          # logic only, no network, instant
python3 test_cairn.py --live   # also hits every live feed
```

122 tests. The live ones break when a source changes its format, which is what
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

Adding a country is the most useful thing anyone can do. `CONTRIBUTING.md` has
the four checks to run on a feed before writing a parser, and the severity
mistake that five countries have already caused.

Standard library only, in both files. Read `CLA.md` and add your line to
`CONTRIBUTORS.md` before your first pull request.

## Credits

Written by Erika Burnett.

Data keeps its own terms and its own credit. See the Sources table above.

## Licence

GNU Affero General Public License v3.0. See `LICENSE`.

Use it, change it, sell it. Distribute a modified version or run one as a
network service and you must offer your users the source under the same terms.

Data keeps its own terms. UK Fuel Finder and OS Code-Point Open are OGL v3.0,
OpenStreetMap is ODbL, each traffic authority sets its own. Credit them.
