# Contributing to fuelrun

Adding a country is the most useful thing anyone can do.

## Before your first pull request

Read `CLA.md` and add your line to `CONTRIBUTORS.md`. It is short, it leaves
your copyright with you, and it keeps the project able to change its own
licence terms later.

## Adding a country

One entry in `PROVIDERS` in `fuelrun.py`:

```python
{"name": "Somewhere", "box": (lat_min, lat_max, lon_min, lon_max),
 "match": "coords",
 "fn": lambda: p_datex2("Somewhere", "https://nap.example/datex2")},
```

Before you write it, check four things about the feed:

1. Does it parse, and how many records does it return? Zero is dead.
2. How many records carry a road number? How many carry coordinates? A feed
   with neither cannot be matched to a route.
3. Are the dates current? Read the whole distribution before you decide; one sample proves nothing. Long
   roadworks legitimately carry old dates.
4. **Does it tell a closed lane apart from a closed road?** Read the next
   section before you answer.

### The mistake five countries have already caused

Every authority distinguishes a coned-off lane from a shut carriageway, and
every one of them does it in its own words. `laneClosures`. `Eén rijstrook
afgesloten`. `A0002 LANE CLOSURE`. `rijstrook` versus `weg`. Match on the
shared word, stop there, and the tool tells a driver the Brussels ring is
closed when one lane has cones on it.

Severity is the part of this project that has shipped wrong most often. Check
your mapping against real records from your feed, and add a test naming your
country in `test_fuelrun.py`.

### "Registration required" usually is not

Public directories list England, Scotland, Austria and Slovenia as needing an
account. All four are wired up here without one. The restriction attaches to
the official DATEX II feed. The same authority is often publishing the same
incidents on a web page, an RSS feed, or the JSON behind their own map. Look
for the public route before writing a country off.

## House rules

- **Standard library only.** No dependencies, in either file.
- **Tests pass.** `python3 test_fuelrun.py` before you open the pull request.
  `--live` hits the real feeds and is expected to break when a source changes.
- **A silent failure is worse than a crash.** A scraped page that gets
  redesigned parses to nothing and raises nothing, and the tool then reports
  no incidents, which on a safety tool is the worst outcome available. If your
  provider can fail quietly, make it fail loudly.
- **No em dashes in prose.** House style.
