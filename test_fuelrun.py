#!/usr/bin/env python3
"""Tests for fuelrun. Standard library unittest, no pytest, no network.

    python3 test_fuelrun.py              # the pure logic, offline, fast
    python3 test_fuelrun.py --live       # also hit every live provider

The offline tests use fixtures copied from real responses, so a provider's
HTML changing breaks the live test and leaves the logic tests honest.
"""

import json, sys, unittest
from datetime import datetime

import fuelrun as fr

LIVE = "--live" in sys.argv
if LIVE:
    sys.argv.remove("--live")


# ----------------------------------------------------------------- severity

class Severity(unittest.TestCase):
    """The bug that mattered most: 145 lane closures ranked as road closures."""

    def test_lane_closure_is_not_a_road_closure(self):
        self.assertGreater(fr._rank("lane closure"), fr._rank("road closure"))

    def test_road_closure_is_the_worst(self):
        self.assertEqual(fr._rank("road closure"), 0)
        self.assertEqual(fr._rank("Closure"), 0)
        self.assertEqual(fr._rank("roadClosed"), 0)

    def test_maintenance_is_serious_enough_to_reroute(self):
        for k in ("MaintenanceWorks", "maintenanceworks", "Roadworks", "roadMaintenance"):
            self.assertLessEqual(fr._rank(k), 1, k)

    def test_queues_are_not(self):
        for k in ("Queue", "AbnormalTraffic", "congestion"):
            self.assertGreaterEqual(fr._rank(k), 3, k)

    def test_footway_closure_is_minor(self):
        self.assertGreaterEqual(fr._rank("footway closure"), 3)

    def test_spacing_never_changes_the_rank(self):
        """Feeds write the same thing four ways. "laneClosures" must not fall
        through "lane closure" into "closure" and read as a shut motorway."""
        for a, b in [("lane closure", "laneClosures"), ("lane closure", "LaneClosed"),
                     ("road closure", "roadClosed"), ("roadworks", "RoadWorks"),
                     ("maintenance works", "MaintenanceWorks")]:
            self.assertEqual(fr._rank(a), fr._rank(b), f"{a} vs {b}")

    def test_the_five_feeds_that_got_this_wrong(self):
        """One case per country that shipped a wrong rank. Five so far, and
        every one of them was a closed lane reported as a closed road."""
        for kind, worst in [("A0002 lane closure", 1),      # TrafficwatchNI
                            ("minor roadworks", 2),          # autobahn.de
                            ("lorry restriction", 2),        # promet.si
                            ("laneClosures", 1),             # Bison Fute
                            ("ramp closed", 1)]:             # verkeerscentrum.be
            self.assertGreater(fr._rank(kind), worst, kind)

    def test_xsi_types_are_ranked(self):
        """Several publishers put severity only in xsi:type. Luxembourg's whole
        feed and 165 of Spain's 166 records were unranked until that was read."""
        for k in ("MaintenanceWorks", "ConstructionWorks", "EquipmentOrSystemFault",
                  "AbnormalTraffic", "VehicleObstruction", "flooding"):
            self.assertLess(fr._rank(k), 4, k)

    def test_unknown_types_sort_last(self):
        self.assertEqual(fr._rank("some new thing"), 4)
        self.assertEqual(fr._rank(""), 4)
        self.assertEqual(fr._rank(None), 4)


# ----------------------------------------------------------------- road codes

class RoadCodes(unittest.TestCase):
    """TrafficwatchNI writes A0002 where a router says A2."""

    def test_zero_padding_is_stripped(self):
        n = fr._norm("x", "A0002, LARNE ROAD - CARRICKFERGUS", "road closure")
        self.assertIn("A2", n["roads"])

    def test_motorway_padding_too(self):
        self.assertIn("M2", fr._norm("x", "M0002, BROUGHSHANE", "closure")["roads"])

    def test_unpadded_codes_survive(self):
        self.assertEqual(fr._norm("x", "A9 Tummel Off Slip", "roadworks")["roads"], {"A9"})

    def test_several_roads_in_one_title(self):
        self.assertEqual(fr._norm("x", "M18/N18 northbound", "incident")["roads"], {"M18", "N18"})

    def test_whitespace_is_collapsed(self):
        n = fr._norm("x", "A1", "roadworks", desc="line one\n\n  line two")
        self.assertEqual(n["desc"], "line one line two")


# ----------------------------------------------------------------- opening hours

class OpeningHours(unittest.TestCase):
    """Statutory per-day hours. Getting this wrong sends someone to a locked forecourt."""

    @staticmethod
    def station(**days):
        base = {d: {"open": "", "close": "", "all_day": False} for d in fr.FF_DAYS}
        base.update(days)
        return {"ff_hours": base, "temp_closed": False}

    def test_open_inside_the_window(self):
        s = self.station(friday={"open": "06:00:00", "close": "22:00:00", "all_day": False})
        self.assertIs(fr.ff_open_at(s, datetime(2026, 10, 16, 14, 0)), True)

    def test_shut_after_closing(self):
        s = self.station(friday={"open": "06:00:00", "close": "22:00:00", "all_day": False})
        self.assertIs(fr.ff_open_at(s, datetime(2026, 10, 16, 23, 30)), False)

    def test_shut_before_opening(self):
        s = self.station(friday={"open": "06:00:00", "close": "22:00:00", "all_day": False})
        self.assertIs(fr.ff_open_at(s, datetime(2026, 10, 16, 5, 0)), False)

    def test_24_hours(self):
        s = self.station(friday={"open": "", "close": "", "all_day": True})
        self.assertIs(fr.ff_open_at(s, datetime(2026, 10, 16, 3, 0)), True)

    def test_across_midnight(self):
        s = self.station(friday={"open": "22:00:00", "close": "06:00:00", "all_day": False})
        self.assertIs(fr.ff_open_at(s, datetime(2026, 10, 16, 23, 30)), True)
        self.assertIs(fr.ff_open_at(s, datetime(2026, 10, 16, 12, 0)), False)

    def test_missing_hours_is_unknown_never_open(self):
        self.assertIsNone(fr.ff_open_at(self.station(), datetime(2026, 10, 16, 12, 0)))

    def test_temporary_closure_beats_the_timetable(self):
        s = self.station(friday={"open": "", "close": "", "all_day": True})
        s["temp_closed"] = True
        self.assertIs(fr.ff_open_at(s, datetime(2026, 10, 16, 12, 0)), False)

    def test_the_right_day_is_used(self):
        s = self.station(sunday={"open": "10:00:00", "close": "16:00:00", "all_day": False})
        self.assertIs(fr.ff_open_at(s, datetime(2026, 10, 18, 12, 0)), True)   # Sunday
        self.assertIsNone(fr.ff_open_at(s, datetime(2026, 10, 16, 12, 0)))     # Friday


class BankHolidays(unittest.TestCase):
    """Every one of the 8,139 forecourts publishes a bank holiday row and
    1,912 of them differ from the matching weekday. At six on Christmas
    morning 1,168 answer differently once it is read."""

    HOL = {"scotland": {"2026-11-30": "St Andrew's Day", "2026-12-25": "Christmas Day"},
           "england-and-wales": {"2026-12-25": "Christmas Day"},
           "northern-ireland": {"2026-03-17": "St Patrick's Day", "2026-12-25": "Christmas Day"}}

    @staticmethod
    def station(lat, lon, usual, bh):
        h = {d: dict(usual) for d in fr.FF_DAYS}
        h["bank_holiday"] = bh
        return {"ff_hours": h, "temp_closed": False, "lat": lat, "lon": lon}

    def test_the_holiday_row_wins_on_a_bank_holiday(self):
        s = self.station(51.5, -0.1,
                         {"open": "06:00:00", "close": "22:00:00", "all_day": False},
                         {"open": "09:00:00", "close": "16:00:00", "all_day": False})
        xmas = datetime(2026, 12, 25, 7, 0)          # inside Friday, outside the holiday
        self.assertIs(fr.ff_open_at(s, xmas, self.HOL), False)
        self.assertIs(fr.ff_open_at(s, datetime(2026, 12, 18, 7, 0), self.HOL), True)

    def test_an_empty_holiday_row_falls_back(self):
        s = self.station(51.5, -0.1,
                         {"open": "06:00:00", "close": "22:00:00", "all_day": False},
                         {"open": "", "close": "", "all_day": False})
        self.assertIs(fr.ff_open_at(s, datetime(2026, 12, 25, 7, 0), self.HOL), True)

    def test_st_andrews_day_is_scottish_only(self):
        row = {"open": "06:00:00", "close": "22:00:00", "all_day": False}
        shut = {"open": "10:00:00", "close": "16:00:00", "all_day": False}
        nov30 = datetime(2026, 11, 30, 7, 0)
        self.assertIs(fr.ff_open_at(self.station(55.95, -3.19, row, shut), nov30, self.HOL), False)
        self.assertIs(fr.ff_open_at(self.station(51.48, -3.18, row, shut), nov30, self.HOL), True)

    def test_the_border_runs_where_the_border_runs(self):
        for place, lat, lon, div in [
                ("Gretna", 55.00, -3.06, "scotland"),
                ("Carlisle", 54.89, -2.93, "england-and-wales"),
                ("Berwick", 55.77, -2.00, "england-and-wales"),
                ("Coldstream", 55.65, -2.25, "scotland"),
                ("Campbeltown", 55.43, -5.61, "scotland"),
                ("Belfast", 54.60, -5.93, "northern-ireland")]:
            self.assertEqual(fr.uk_division(lat, lon), div, place)


class CrossBorder(unittest.TestCase):
    """A1 exists in Britain, the Netherlands, Germany, Austria and France.
    Amsterdam to Munich put an Austrian tunnel closure on the Dutch A1."""

    AT = (46.3, 49.1, 9.5, 17.2)

    def test_a_dutch_a1_is_not_an_austrian_a1(self):
        dutch = {"A1": [(52.2, 5.3), (52.1, 6.0)]}
        self.assertFalse(fr._road_runs_through({"A1"}, dutch, self.AT))

    def test_an_austrian_a1_still_matches(self):
        austrian = {"A1": [(48.2, 14.3), (48.0, 15.4)]}
        self.assertTrue(fr._road_runs_through({"A1"}, austrian, self.AT))

    def test_britain_does_not_claim_ireland(self):
        """England's box reaches 6.5W and Scotland's 8.7W, so both contain
        Dublin. A Dublin to Belfast run collected English A1 and M1 works."""
        dublin_belfast = [(53.35, -6.26), (53.9, -6.4), (54.2, -6.3), (54.60, -5.93)]
        names = {p["name"] for p in fr.providers_for(dublin_belfast)}
        self.assertNotIn("National Highways", names)
        self.assertNotIn("Traffic Scotland", names)
        self.assertIn("TrafficwatchNI", names)

    def test_britain_still_claims_britain(self):
        glasgow_london = [(55.86, -4.25), (54.5, -2.6), (53.0, -1.5), (51.51, -0.13)]
        names = {p["name"] for p in fr.providers_for(glasgow_london)}
        self.assertIn("National Highways", names)
        self.assertIn("Traffic Scotland", names)

    def test_an_irish_a1_is_not_an_english_a1(self):
        england = (49.8, 56.0, -6.5, 2.0)
        irish = {"A1": [(54.2, -6.30), (54.4, -6.15)]}
        self.assertFalse(fr._road_runs_through({"A1"}, irish, england, fr.IRELAND))
        english = {"A1": [(52.5, -0.45), (53.1, -0.80)]}
        self.assertTrue(fr._road_runs_through({"A1"}, english, england, fr.IRELAND))

    def test_european_road_numbers_survive_the_regex(self):
        """The old pattern allowed A, M and B only, so N154, D920, R132 and
        S1 were dropped before the matcher ever saw them."""
        import re
        for ref in ("A1", "M6", "A74(M)", "N154", "D920", "R132", "S1", "E40", "AP7"):
            self.assertTrue(re.fullmatch(fr.ROAD_RE, ref), ref)

    def test_no_index_keeps_the_old_behaviour(self):
        self.assertTrue(fr._road_runs_through({"A1"}, None, self.AT))


class OsmHours(unittest.TestCase):
    """The OSM opening_hours string, used everywhere outside Great Britain."""

    def test_always_open(self):
        self.assertIs(fr.parse_hours("24/7", datetime(2026, 10, 16, 3, 0)), True)

    def test_day_range(self):
        self.assertIs(fr.parse_hours("Mo-Fr 08:00-18:00", datetime(2026, 10, 16, 12, 0)), True)
        self.assertIs(fr.parse_hours("Mo-Fr 08:00-18:00", datetime(2026, 10, 17, 12, 0)), None)

    def test_several_rules(self):
        spec = "Mo-Fr 06:00-22:00; Sa-Su 08:00-20:00"
        self.assertIs(fr.parse_hours(spec, datetime(2026, 10, 17, 9, 0)), True)
        self.assertIs(fr.parse_hours(spec, datetime(2026, 10, 17, 21, 0)), False)

    def test_explicit_closed(self):
        self.assertIs(fr.parse_hours("Mo-Sa 09:00-17:00; Su off", datetime(2026, 10, 18, 12, 0)), False)

    def test_nonsense_returns_unknown(self):
        for spec in (None, "", "sunrise-sunset", "by appointment"):
            self.assertIsNone(fr.parse_hours(spec, datetime(2026, 10, 16, 12, 0)), spec)


# ----------------------------------------------------------------- geometry

class Geometry(unittest.TestCase):

    def test_known_distance(self):
        # Inverness to Thurso, about 75 miles as the crow flies
        d = fr.haversine_miles((57.4778, -4.2247), (58.5936, -3.5221))
        self.assertTrue(80 < d < 85, d)

    def test_zero_distance(self):
        self.assertAlmostEqual(fr.haversine_miles((55.0, -4.0), (55.0, -4.0)), 0.0)

    def test_cumulative_runs_forward(self):
        c = fr.cumulative_miles([(55.0, -4.0), (55.1, -4.0), (55.2, -4.0)])
        self.assertEqual(c[0], 0.0)
        self.assertLess(c[1], c[2])


# ----------------------------------------------------------------- health

class Health(unittest.TestCase):
    """A scraped page that gets redesigned parses to nothing and raises nothing."""

    def setUp(self):
        self.name = "__test_provider__"

    def tearDown(self):
        h = fr._health_load()
        h.pop(self.name, None)
        fr.HEALTH.write_text(fr.json.dumps(h))

    def test_first_run_never_warns(self):
        self.assertIsNone(fr._health_record(self.name, 0))

    def test_falling_to_zero_warns(self):
        fr._health_record(self.name, 40)
        w = fr._health_record(self.name, 0)
        self.assertIsNotNone(w)
        self.assertIn("40", w)

    def test_a_big_drop_warns(self):
        fr._health_record(self.name, 200)
        self.assertIsNotNone(fr._health_record(self.name, 5))

    def test_normal_variation_is_quiet(self):
        fr._health_record(self.name, 40)
        self.assertIsNone(fr._health_record(self.name, 33))


# ----------------------------------------------------------------- blocking

class Blocking(unittest.TestCase):

    def test_a_polygon_per_closure(self):
        a = fr.closure_areas([(57.0, -4.0), (56.0, -3.0)])
        self.assertEqual(len(a), 2)
        self.assertEqual(a["shut0"]["geometry"]["type"], "Polygon")

    def test_the_ring_closes(self):
        ring = fr.closure_areas([(57.0, -4.0)])["shut0"]["geometry"]["coordinates"][0]
        self.assertEqual(ring[0], ring[-1])

    def test_the_closure_sits_inside_its_own_box(self):
        ring = fr.closure_areas([(57.0, -4.0)])["shut0"]["geometry"]["coordinates"][0]
        lons = [p[0] for p in ring]
        lats = [p[1] for p in ring]
        self.assertTrue(min(lons) < -4.0 < max(lons))
        self.assertTrue(min(lats) < 57.0 < max(lats))

    def test_nothing_to_block_means_no_request(self):
        self.assertIsNone(fr.reroute_avoiding({}, {}, [], "http://localhost:1"))


class CachedSeverity(unittest.TestCase):
    """A cached incident stores the rank it was given when it was written.
    Improving the severity table has to reach those records too, or the fix
    does nothing until the cache expires."""

    def test_rank_is_recomputed_not_trusted(self):
        import re as _re
        src = open(fr.__file__).read()
        body = src[src.index("def p_datex2_bulk"):]
        head = body[:body.index("blob = CACHE")]
        self.assertIn('"rank": _rank(', head,
                      "cached incidents are returned with their stored rank")


class EveryExit(unittest.TestCase):
    """main() had two early returns above the handout and the privacy
    report. Asking for --out on a journey you had the fuel for wrote no
    file and said nothing, and --privacy reported nothing at all."""

    def test_no_return_in_main_skips_finish(self):
        src = open(fr.__file__).read()
        body = src[src.index("\ndef main():"):]
        lines = [l.strip() for l in body.splitlines()]
        for n, line in enumerate(lines):
            if line != "return":
                continue
            window = " ".join(lines[max(0, n - 8):n])
            self.assertTrue("finish(" in window or "json.dumps(out" in window,
                            f"a return in main() at offset {n} skips both "
                            f"finish() and the json exit: {window[-90:]}")

    def test_finish_writes_and_reports(self):
        src = open(fr.__file__).read()
        fin = src[src.index("def finish("):src.index("def main():")]
        self.assertIn("write_plan(", fin)
        self.assertIn("LEFT THIS MACHINE", fin)


class CacheKeys(unittest.TestCase):
    """Python randomises str.__hash__ per process. A cache key built from it
    changes every run, so the cache silently never hits and every invocation
    re-queries the upstream. This caught exactly that."""

    def test_keys_are_stable_across_processes(self):
        import subprocess as sp
        code = ("import sys; sys.path.insert(0,'.'); import fuelrun as f, hashlib; "
                "print(hashlib.sha1('abc'.encode()).hexdigest()[:16])")
        a = sp.run([sys.executable, "-c", code], capture_output=True, text=True).stdout
        b = sp.run([sys.executable, "-c", code], capture_output=True, text=True).stdout
        self.assertEqual(a, b)
        self.assertTrue(a.strip())

    def test_no_builtin_hash_in_cache_keys(self):
        src = (fr.__file__ and open(fr.__file__).read()) or ""
        for line in src.splitlines():
            if "CACHE /" in line and "hash(" in line and "hashlib" not in line:
                self.fail(f"unstable cache key: {line.strip()}")


class EmptyIsNotAnAnswer(unittest.TestCase):
    """overpass.osm.ch sat in the mirror list and holds Switzerland only. It
    answered a Norwegian query with zero results in 0.8 seconds, no error and
    no remark, and the tool printed "nothing on the route within your range".
    Zero fuel stations is the one answer that strands somebody, so it now has
    to be said twice by two different servers."""

    def setUp(self):
        self.real_get, self.real_mirrors = fr.get, fr.OVERPASS
        fr.OVERPASS = ["https://one.invalid/api", "https://two.invalid/api"]

    def tearDown(self):
        fr.get, fr.OVERPASS = self.real_get, self.real_mirrors

    def _answers(self, *bodies):
        seen = []
        def fake(url, **kw):
            seen.append(url)
            return bodies[min(len(seen) - 1, len(bodies) - 1)]
        fr.get = fake
        return seen

    def test_one_mirror_saying_nothing_is_not_nothing(self):
        seen = self._answers({"elements": []}, {"elements": [{"id": 1}]})
        res = fr.overpass("x")
        self.assertEqual(len(res["elements"]), 1)
        self.assertEqual(len(seen), 2, "it took the first empty answer at face value")

    def test_two_mirrors_agreeing_on_nothing_is_nothing(self):
        self._answers({"elements": []})
        self.assertEqual(fr.overpass("x")["elements"], [])

    def test_a_lone_empty_answer_raises_rather_than_reports_no_fuel(self):
        calls = []
        def fake(url, **kw):
            calls.append(url)
            if len(calls) == 1:
                return {"elements": []}
            raise RuntimeError("mirror down")
        fr.get = fake
        with self.assertRaises(RuntimeError) as e:
            fr.overpass("x")
        self.assertIn("unknown", str(e.exception))

    def test_a_timed_out_query_is_a_failure_not_a_result(self):
        self._answers({"elements": [], "remark": "runtime error: Query timed out"},
                      {"elements": [{"id": 2}]})
        self.assertEqual(len(fr.overpass("x")["elements"]), 1)

    def test_the_swiss_mirror_is_gone(self):
        self.assertNotIn("osm.ch", " ".join(self.real_mirrors),
                         "a Switzerland-only instance cannot answer for Europe")


class FuelCache(unittest.TestCase):
    """A cached empty station list is a permanent confident lie about where
    the fuel is. One was found on disk for Oslo to Trondheim."""

    def test_empty_results_are_never_written_to_disk(self):
        import tempfile, pathlib as pl
        real_cache, real_overpass = fr.CACHE, fr.overpass
        fr.CACHE = pl.Path(tempfile.mkdtemp())
        fr.overpass = lambda q, **kw: {"elements": []}
        try:
            coords = [(63.4, 10.4), (63.5, 10.5)]
            self.assertEqual(fr.fuel_near_route(coords), [])
            self.assertEqual(list(fr.CACHE.glob("fuel_*.json")), [],
                             "an empty answer was cached and will outlive the outage")
        finally:
            fr.CACHE, fr.overpass = real_cache, real_overpass

    def test_the_cache_expires(self):
        self.assertTrue(hasattr(fr, "FUEL_CACHE_DAYS"))
        src = open(fr.__file__).read()
        i = src.index("def fuel_near_route")
        self.assertIn("FUEL_CACHE_DAYS", src[i:i + 900],
                      "fuel_near_route caches forever")


class Norway(unittest.TestCase):
    """Norway's own map service, and the two traps in it."""

    def test_intermittent_closures_are_a_wait_not_a_wall(self):
        # 87 live Norwegian records carry this. It is a road shut for ten
        # minutes at a time while they blast. Any matcher looking for the
        # word "closure" calls it a shut motorway.
        self.assertEqual(fr._rank("intermittentShortTermClosures"), 2)
        self.assertEqual(fr._rank("delaysIntermittentShortTermClosures"), 2)
        self.assertLess(fr._rank("roadClosed"), fr._rank("intermittentShortTermClosures"))

    def test_county_road_spellings_both_survive(self):
        # The feed writes F773 and R92. OSM, and so the router, writes Fv773
        # and Rv92. Matching one spelling means never matching at all.
        i = fr._norm("Norway", "x", "roadworks", roads={"F773", "FV773", "R92", "RV92"})
        self.assertEqual(i["roads"], {"F773", "FV773", "R92", "RV92"})



class DanishAndSwedish(unittest.TestCase):
    """Denmark publishes in Danish, and the Swedish layer it relays keeps a
    Danish title with a Swedish body. Five languages have now had the same
    lane-versus-road mistake made in them."""

    def test_the_road_and_one_lane_of_it_are_different_sentences(self):
        self.assertEqual(fr._rank("road closure"), 0)
        self.assertEqual(fr._rank("lane closure"), 2)

    def test_a_swedish_closure_is_not_filed_as_danish_roadworks(self):
        # "Vejarbejde i gang" is the title on every record in that layer,
        # closure or not. The Swedish body is where the truth is.
        feat = {"properties": {"title": "Vejarbejde i gang",
                               "description": "<p>Väg 969 i Sk\u00e5ne l\u00e4n. "
                                              "V\u00e4gen avst\u00e4ngd. V\u00e4garbete.</p>",
                               "future": "false", "suspended": "false"},
                "geometry": {"type": "Point", "coordinates": [13.1, 55.6]}}
        got = self._parse(feat)
        self.assertEqual(got["type"], "road closure")
        self.assertNotEqual(got["title"], "Vejarbejde i gang",
                            "a closure printed as the Danish word for roadworks")

    def test_one_swedish_lane_is_not_the_whole_road(self):
        feat = {"properties": {"title": "Vejarbejde i gang",
                               "description": "Ett k\u00f6rf\u00e4lt avst\u00e4ngt.",
                               "future": "false", "suspended": "false"},
                "geometry": {"type": "Point", "coordinates": [13.1, 55.6]}}
        self.assertEqual(self._parse(feat)["type"], "lane closure")

    def test_planned_work_is_not_live_work(self):
        feat = {"properties": {"title": "Planlagt vejarbejde", "description": "x",
                               "future": "true", "suspended": "false"},
                "geometry": {"type": "Point", "coordinates": [13.1, 55.6]}}
        self.assertIsNone(self._parse(feat), "a future roadwork was reported as live")

    def _parse(self, feature):
        real = fr.get_text
        fr.get_text = lambda *a, **k: json.dumps({"features": [feature]})
        try:
            got = fr._dk_layer("current-roadwork", "Denmark")
        finally:
            fr.get_text = real
        return got[0] if got else None



class SpanishHours(unittest.TestCase):
    """1,165 distinct timetable strings across 11,429 forecourts, all of them
    in one small grammar. Spanish day letters are L M X J V S D, so X is
    Wednesday: reading M as Wednesday shifts half the week by a day."""

    def test_x_is_wednesday_and_m_is_tuesday(self):
        h = fr.parse_spanish_hours("X: 24H")
        self.assertTrue(h["wednesday"]["all_day"])
        self.assertFalse(h["tuesday"]["all_day"])
        self.assertTrue(fr.parse_spanish_hours("M: 24H")["tuesday"]["all_day"])

    def test_lunch(self):
        h = fr.parse_spanish_hours("L-V: 08:00-14:00 y 16:00-21:00")
        self.assertEqual(h["monday"]["windows"], [[480, 840], [960, 1260]])
        self.assertEqual(h["sunday"]["windows"], [])

    def test_a_shut_forecourt_at_three_is_not_open(self):
        st = {"source": "spain",
              "ff_hours": fr.parse_spanish_hours("L-V: 08:00-14:00 y 16:00-21:00")}
        at = lambda t: datetime.fromisoformat("2026-10-19T" + t)
        self.assertIs(fr.ff_open_at(st, at("09:00")), True)
        self.assertIs(fr.ff_open_at(st, at("15:00")), False)
        self.assertIs(fr.ff_open_at(st, at("17:00")), True)
        self.assertEqual(fr.ff_closes_at(st, at("09:00")), "14:00")

    def test_mixed_groups(self):
        h = fr.parse_spanish_hours("L-J: 06:00-22:00; V: 06:00-23:59; S: 24H; D: 00:00-22:00")
        self.assertEqual(h["wednesday"]["windows"], [[360, 1320]])
        self.assertTrue(h["saturday"]["all_day"])
        self.assertEqual(h["sunday"]["windows"], [[0, 1320]])

    def test_an_unparseable_string_says_unknown_not_open(self):
        st = {"source": "spain", "ff_hours": fr.parse_spanish_hours("sin horario")}
        self.assertIsNone(fr.ff_open_at(st, datetime(2026, 10, 19, 9, 0)))


class FrenchCardMachines(unittest.TestCase):
    """5,592 of France's 9,799 forecourts run a card machine around the
    clock behind a kiosk that shuts at eight. Reading the kiosk hours alone
    would send somebody past a working pump at two in the morning."""

    def test_the_kiosk_shutting_does_not_shut_the_pump(self):
        hours = {d: fr._hours(all_day=True) for d in fr.FF_DAYS}
        st = {"source": "france", "ff_hours": hours, "always_card": True}
        self.assertIs(fr.ff_open_at(st, datetime(2026, 10, 19, 3, 0)), True)

    def test_without_the_card_machine_the_kiosk_hours_rule(self):
        hours = {d: fr._hours(windows=[[480, 1200]]) for d in fr.FF_DAYS}
        st = {"source": "france", "ff_hours": hours}
        self.assertIs(fr.ff_open_at(st, datetime(2026, 10, 19, 3, 0)), False)
        self.assertIs(fr.ff_open_at(st, datetime(2026, 10, 19, 9, 0)), True)


class WhichRegister(unittest.TestCase):
    def test_every_register_declares_a_box_and_a_loader(self):
        for reg in fr.FUEL_REGISTERS:
            for k in ("name", "key", "box", "load", "note"):
                self.assertIn(k, reg, f"{reg.get('name')} is missing {k}")
            self.assertEqual(len(reg["box"]), 4)

    def test_a_station_from_a_register_is_not_read_with_the_osm_parser(self):
        # The test used to be the string "fuelfinder". The day France
        # arrived, 9,799 forecourts would have come back as hours unknown.
        src = open(fr.__file__).read()
        i = src.index("eta = depart + timedelta")
        self.assertIn('st.get("ff_hours")', src[i:i + 500])



class Money(unittest.TestCase):
    """Prices are held in the minor unit because Britain publishes pence.
    A Spanish saving printed as "31.0p/L" is the wrong currency on the
    wrong continent."""

    def test_pence_and_cents_are_not_the_same_mark(self):
        self.assertEqual(fr.money(31.0, "EUR"), "31.0c/L")
        self.assertEqual(fr.money(31.0, "GBP"), "31.0p/L")

    def test_over_a_unit_prints_as_one(self):
        self.assertEqual(fr.money(180, "EUR"), "\u20ac1.80/L")
        self.assertEqual(fr.money(140.9, "GBP"), "\u00a31.41/L")

    def test_an_unlabelled_price_does_not_crash(self):
        self.assertTrue(fr.money(100, None))



# ----------------------------------------------------------------- live

@unittest.skipUnless(LIVE, "pass --live to hit the real feeds")
class LiveProviders(unittest.TestCase):
    """These break when a source changes, which is the point of them."""

    def check(self, fn, least=1):
        items = fn()
        self.assertGreaterEqual(len(items), least)
        for i in items[:5]:
            self.assertTrue(i["title"], "an item with no title")
            self.assertIn("rank", i)
            self.assertIsInstance(i["roads"], set)

    def test_traffic_scotland(self):   self.check(fr.p_traffic_scotland)
    def test_national_highways(self):  self.check(fr.p_national_highways, 10)
    def test_traffic_wales(self):      self.check(fr.p_traffic_wales)
    def test_trafficwatch_ni(self):    self.check(fr.p_trafficwatch_ni, 10)
    def test_tii_ireland(self):        self.check(fr.p_tii_ireland, 0)
    def test_norway(self):             self.check(fr.p_vegvesen, 50)
    def test_denmark(self):            self.check(fr.p_vejdirektoratet, 20)
    def test_sweden_via_denmark(self): self.check(fr.p_swedish_via_denmark, 10)

    def test_france_register(self):
        rows = fr.load_france()
        self.assertGreater(len(rows), 8000)
        self.assertTrue(any(r['prices'] for r in rows))

    def test_spain_register(self):
        rows = fr.load_spain()
        self.assertGreater(len(rows), 9000)
        self.assertTrue(any(r['prices'] for r in rows))

    def test_fuel_finder_has_the_statutory_fields(self):
        rows = fr.load_fuelfinder()
        self.assertGreater(len(rows), 5000)
        s = rows[0]
        for k in ("name", "lat", "lon", "ff_hours", "prices"):
            self.assertIn(k, s)


if __name__ == "__main__":
    unittest.main(verbosity=2)
