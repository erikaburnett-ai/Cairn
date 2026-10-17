#!/usr/bin/env python3
"""Tests for fuelrun. Standard library unittest, no pytest, no network.

    python3 test_fuelrun.py              # the pure logic, offline, fast
    python3 test_fuelrun.py --live       # also hit every live provider

The offline tests use fixtures copied from real responses, so a provider's
HTML changing breaks the live test and leaves the logic tests honest.
"""

import sys, unittest
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

    def test_fuel_finder_has_the_statutory_fields(self):
        rows = fr.load_fuelfinder()
        self.assertGreater(len(rows), 5000)
        s = rows[0]
        for k in ("name", "lat", "lon", "ff_hours", "prices"):
            self.assertIn(k, s)


if __name__ == "__main__":
    unittest.main(verbosity=2)
