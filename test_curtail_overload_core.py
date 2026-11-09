"""Unit tests for the overload-curtailment math. No PSS/E required."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from curtail_overload_core import (  # noqa: E402
    Branch,
    ConstrainedLine,
    Generator,
    SwingLimits,
    build_joint_plan,
    loading_pct,
    mw_limit_for_mva,
    orient_factors,
    parse_shift_factor_report,
    q_exceeds_limit,
    relieving_effectiveness,
    select_candidates,
    share_mw,
    swing_correction_moves,
    violation_gen_rows,
)


def gen(bus, p, pmin, pmax, area=1, mid="1", name=""):
    return Generator(
        bus=bus, machine_id=mid, name=name or "G%s" % bus, area=area,
        p_mw=p, pmin_mw=pmin, pmax_mw=pmax, status=1, bus_type=2,
    )


def line(frm, to, ckt, p, q, mva, rating, limit_pct, violation=True):
    branch = Branch(frm, to, ckt, p, q, mva, rating, name="L")
    return ConstrainedLine(branch, limit_pct, violation)


class LoadingTests(unittest.TestCase):
    def test_loading_and_mw_limit(self):
        branch = Branch(1, 2, "1", 150.0, 0.0, 150.0, 100.0)
        self.assertAlmostEqual(loading_pct(branch), 150.0)
        branch.psse_loading_pct = 160.13
        self.assertAlmostEqual(loading_pct(branch), 160.13)
        self.assertAlmostEqual(mw_limit_for_mva(150, 0, 100, 100), 100.0)
        # Q = 60, target MVA 100 -> P limit 80
        self.assertAlmostEqual(mw_limit_for_mva(80, 60, 100, 100), 80.0)
        self.assertTrue(q_exceeds_limit(Branch(1, 2, "1", 10, 110, 110.4, 100), 100))

    def test_effectiveness_sign(self):
        # Positive SF and positive flow: curtailing reduces the flow.
        self.assertGreater(relieving_effectiveness(0.4, 100), 0.0)
        # Negative SF and negative flow: curtailing moves P toward zero.
        self.assertGreater(relieving_effectiveness(-0.4, -100), 0.0)
        # Positive SF and negative flow: curtailing makes the overload worse.
        self.assertLess(relieving_effectiveness(0.4, -100), 0.0)
        self.assertEqual(relieving_effectiveness(0.4, 0.0), 0.0)


class ShareTests(unittest.TestCase):
    def test_headroom_share(self):
        moves, left = share_mw({"a": 10.0, "b": 30.0}, 20.0)
        self.assertAlmostEqual(moves["a"], 5.0)
        self.assertAlmostEqual(moves["b"], 15.0)
        self.assertAlmostEqual(left, 0.0)

    def test_shortfall_when_room_runs_out(self):
        moves, left = share_mw({"a": 10.0, "b": 30.0}, 50.0)
        self.assertAlmostEqual(moves["a"], 10.0)
        self.assertAlmostEqual(moves["b"], 30.0)
        self.assertAlmostEqual(left, 10.0)

    def test_swing_correction_direction(self):
        # Swing is 30 MW high: other machines increase, inside their up room.
        moves, left = swing_correction_moves(530, 500, {"a": 100, "b": 100}, {"a": 50})
        self.assertAlmostEqual(sum(moves.values()), 30.0)
        self.assertGreater(moves["a"], 0.0)
        self.assertAlmostEqual(left, 0.0)
        # Swing is 20 MW low: other machines decrease.
        moves, left = swing_correction_moves(480, 500, {"a": 100}, {"a": 10, "b": 10})
        self.assertAlmostEqual(sum(moves.values()), -20.0)
        self.assertAlmostEqual(left, 0.0)
        # Not enough room to pull the swing down.
        moves, left = swing_correction_moves(40, 0, {"a": 5}, {"a": 0})
        self.assertAlmostEqual(sum(moves.values()), 5.0)
        self.assertAlmostEqual(left, 35.0)


class ShiftFactorParseTests(unittest.TestCase):
    REPORT = """
 SENSITIVITY FACTORS OF BRANCH FLOW (MW) ON 101 ALPHA 138.00 TO 202 BETA 138.00 CKT 1

 BUS
<----
  500 GENA 13.80 0.250
  600 GENB 13.80 -0.125

 SENSITIVITY FACTORS OF BRANCH FLOW (MW) ON 1261 LOU CR.7 138.00 TO 3WNDTR 809S901T * BUS T1 MORE THAN 0.03000

<------ GENERATOR BUS ------> PGEN(MW) PMAX(MW) PMIN(MW)  MW-SENS
561480     P2738_SOLAR 0.8000    75.17    92.00     0.00   0.0370

 SENSITIVITY FACTORS OF BRANCH FLOW (MW) ON 303 GAMMA 240.00 TO 101 ALPHA 138.00 CKT 2

<----
  500 GENA 13.80 0.800
"""

    def test_parse_and_orient(self):
        rows = parse_shift_factor_report(self.REPORT)
        self.assertEqual(len(rows), 3)
        factors = orient_factors(rows, [(101, 202, "1"), (101, 303, "2")])
        self.assertAlmostEqual(factors[(101, 202, "1")][500], 0.250)
        self.assertAlmostEqual(factors[(101, 202, "1")][600], -0.125)
        # Report was 303 -> 101. Case branch is 101 -> 303, so the factor flips.
        self.assertAlmostEqual(factors[(101, 303, "2")][500], -0.800)
        # Three-winding header has no to-bus, so its generator row is dropped.
        self.assertNotIn(561480, factors[(101, 202, "1")])
        self.assertNotIn(561480, factors[(101, 303, "2")])

    def test_orient_skips_missing_to_bus(self):
        factors = orient_factors(
            [{"from_bus": 101, "to_bus": None, "ckt": "1", "bus": 500, "factor": 0.2}],
            [(101, 202, "1")],
        )
        self.assertEqual(factors[(101, 202, "1")], {})


class JointPlanTests(unittest.TestCase):
    def test_curtail_and_hold_swing_with_pickup(self):
        overloaded = line(10, 20, "1", 150.0, 0.0, 150.0, 100.0, 100.0)
        gens = [
            gen(500, 200, 0, 200, name="A"),          # SF 0.5, curtail 100 MW
            gen(600, 100, 0, 400, name="D"),          # SF 0, pickup
        ]
        sf = {(10, 20, "1"): {500: 0.5, 600: 0.0}}
        swing = SwingLimits(500, 0, 1000, buses=[1520])
        plan = build_joint_plan(
            [overloaded], [overloaded], sf, gens, swing, swing_buses=[1520],
        )
        self.assertTrue(plan.success, plan.message + " " + str(plan.notes))
        by_bus = {mv.bus: mv for mv in plan.moves}
        self.assertAlmostEqual(by_bus[500].delta_mw, -100.0, delta=0.2)
        self.assertAlmostEqual(by_bus[600].delta_mw, 100.0, delta=0.2)
        self.assertAlmostEqual(plan.expected_swing_p_mw, 500.0, delta=0.2)
        self.assertGreater(by_bus[500].p_new_mw, -0.01)
        self.assertLess(by_bus[600].p_new_mw, 400.01)

    def test_uses_swing_room_when_nothing_can_pick_up(self):
        overloaded = line(10, 20, "1", 200.0, 0.0, 200.0, 100.0, 100.0)
        gens = [gen(500, 400, 0, 400, name="A")]  # already at Pmax, cannot pick up
        sf = {(10, 20, "1"): {500: 1.0}}
        # Swing can rise only 50 MW (P 100 -> Pmax 150), so only 50 MW can be curtailed.
        swing = SwingLimits(100, 0, 150, buses=[9])
        plan = build_joint_plan(
            [overloaded], [overloaded], sf, gens, swing, swing_buses=[9], max_neutral=0,
        )
        self.assertAlmostEqual(plan.curtail_mw, 50.0, delta=0.2)
        self.assertAlmostEqual(plan.slack_mw[(10, 20, "1")], 50.0, delta=0.2)
        self.assertFalse(plan.success)
        self.assertAlmostEqual(plan.moves[0].p_new_mw, 350.0, delta=0.2)

    def test_two_lines_use_their_gens_together(self):
        line1 = line(10, 20, "1", 150.0, 0.0, 150.0, 100.0, 100.0)
        line2 = line(30, 40, "1", 150.0, 0.0, 150.0, 100.0, 100.0)
        gens = [
            gen(501, 400, 0, 400, name="A"),  # helps line 1 only
            gen(502, 400, 0, 400, name="B"),  # helps line 2 only
            gen(503, 50, 0, 500, name="D"),   # neutral pickup
        ]
        sf = {
            (10, 20, "1"): {501: 0.5, 502: 0.0, 503: 0.0},
            (30, 40, "1"): {501: 0.0, 502: 0.5, 503: 0.0},
        }
        swing = SwingLimits(800, 0, 2000, buses=[1])
        plan = build_joint_plan(
            [line1, line2], [line1, line2], sf, gens, swing, swing_buses=[1],
        )
        self.assertTrue(plan.success, plan.notes)
        by_bus = {mv.bus: mv for mv in plan.moves}
        self.assertAlmostEqual(by_bus[501].delta_mw, -100.0, delta=0.2)
        self.assertAlmostEqual(by_bus[502].delta_mw, -100.0, delta=0.2)
        self.assertAlmostEqual(by_bus[503].delta_mw, 200.0, delta=0.2)
        # Both lines see both of their own top generators.
        self.assertIn(501, [row["bus"] for row in plan.rankings[(10, 20, "1")]])
        self.assertIn(502, [row["bus"] for row in plan.rankings[(30, 40, "1")]])

    def test_conflicting_gen_is_not_used_to_overload_the_other_line(self):
        line1 = line(10, 20, "1", 150.0, 0.0, 150.0, 100.0, 100.0)
        line2 = line(30, 40, "1", 100.0, 0.0, 100.0, 100.0, 100.0, violation=False)
        # Line 2 is at its rating. Gen A would relieve line 1 and overload line 2.
        # Gen C relieves line 1 and does not touch line 2.
        gens = [
            gen(501, 400, 0, 400, name="A"),
            gen(502, 400, 0, 400, name="C"),
            gen(503, 0, 0, 400, name="D"),
        ]
        # D has p_mw 0 so it is not eligible (not injecting). Give it a little P.
        gens[2] = gen(503, 20, 0, 400, name="D")
        sf = {
            (10, 20, "1"): {501: 0.5, 502: 0.5, 503: 0.0},
            (30, 40, "1"): {501: 0.5, 502: 0.0, 503: 0.0},
        }
        swing = SwingLimits(400, 0, 2000, buses=[1])
        plan = build_joint_plan(
            [line1], [line1, line2], sf, gens, swing, swing_buses=[1],
        )
        self.assertTrue(plan.success, plan.notes)
        by_bus = {mv.bus: mv.delta_mw for mv in plan.moves}
        self.assertAlmostEqual(by_bus.get(502, 0.0), -100.0, delta=0.2)
        self.assertGreater(by_bus.get(501, 0.0), -1.0)

    def test_swing_outside_its_range_is_pulled_back(self):
        overloaded = line(10, 20, "1", 150.0, 0.0, 150.0, 100.0, 100.0)
        gens = [
            gen(500, 200, 0, 200, name="A"),
            gen(600, 100, 0, 400, name="D"),
        ]
        sf = {(10, 20, "1"): {500: 0.5, 600: 0.0}}
        # Swing is 100 MW above Pmax. Target is 500. Curtail 100 and pick up 200.
        swing = SwingLimits(600, 0, 500, buses=[9])
        plan = build_joint_plan(
            [overloaded], [overloaded], sf, gens, swing, swing_buses=[9],
        )
        self.assertTrue(plan.success, plan.notes)
        self.assertAlmostEqual(plan.curtail_mw, 100.0, delta=0.2)
        self.assertAlmostEqual(plan.pickup_mw, 200.0, delta=0.2)
        self.assertAlmostEqual(plan.expected_swing_p_mw, 500.0, delta=0.2)

    def test_respects_pmin(self):
        overloaded = line(10, 20, "1", 180.0, 0.0, 180.0, 100.0, 100.0)
        # Only 30 MW of room above Pmin, SF = 1, swing can absorb 30.
        gens = [gen(500, 80, 50, 80, name="A")]
        sf = {(10, 20, "1"): {500: 1.0}}
        swing = SwingLimits(200, 0, 400, buses=[9])
        plan = build_joint_plan(
            [overloaded], [overloaded], sf, gens, swing, swing_buses=[9], max_neutral=0,
        )
        self.assertAlmostEqual(plan.curtail_mw, 30.0, delta=0.2)
        self.assertGreaterEqual(plan.moves[0].p_new_mw, 50.0 - 0.01)

    def test_swing_machine_is_not_a_candidate(self):
        overloaded = line(10, 20, "1", 150.0, 0.0, 150.0, 100.0, 100.0)
        gens = [
            gen(1520, 500, 0, 800, name="SW"),
            gen(500, 200, 0, 200, name="A"),
            gen(600, 100, 0, 300, name="D"),
        ]
        sf = {(10, 20, "1"): {1520: 0.9, 500: 0.5, 600: 0.0}}
        swing = SwingLimits(500, 0, 800, buses=[1520])
        plan = build_joint_plan(
            [overloaded], [overloaded], sf, gens, swing, swing_buses=[1520],
        )
        buses = [mv.bus for mv in plan.moves]
        self.assertNotIn(1520, buses)
        self.assertTrue(plan.success, plan.notes)

    def test_report_rows_put_gens_on_each_violation(self):
        line1 = line(10, 20, "1", 150.0, 0.0, 150.0, 100.0, 100.0)
        line2 = line(30, 40, "1", 160.0, 0.0, 160.0, 100.0, 100.0)
        gens = [
            gen(501, 400, 0, 400, area=23, name="A"),
            gen(502, 400, 0, 400, area=23, name="B"),
            gen(503, 50, 0, 500, area=23, name="D"),
        ]
        sf = {
            (10, 20, "1"): {501: 0.5, 502: 0.0, 503: 0.0},
            (30, 40, "1"): {501: 0.0, 502: 0.4, 503: 0.0},
        }
        swing = SwingLimits(100, 0, 5000, buses=[1])
        plan = build_joint_plan(
            [line1, line2], [line1, line2], sf, gens, swing, swing_buses=[1],
            curtail_areas=[23],
        )
        rows = violation_gen_rows("N-0", "BASE CASE", [line1, line2], plan.rankings, plan)
        # Worst line (160%) is listed before the 150% line.
        self.assertEqual(rows[0]["FromBus"], 30)
        line2_rows = [row for row in rows if row["FromBus"] == 30]
        self.assertEqual(line2_rows[0]["GenBus"], 502)
        self.assertEqual(line2_rows[0]["GenRank"], 1)
        self.assertAlmostEqual(line2_rows[0]["JointCurtailMW"], 150.0, delta=0.5)
        self.assertGreater(line2_rows[0]["MWToClearThisLineAlone"], 0.0)


class DispatchEligibilityTests(unittest.TestCase):
    def test_dispatch_direction_permissions_are_independent(self):
        overloaded = line(10, 20, "1", 150.0, 0.0, 150.0, 100.0, 100.0)
        wind = gen(501, 200, 0, 400, area=23, name="WIND")
        wind.technology = "WIND"
        wind.allow_dispatch_down = True
        wind.allow_dispatch_up = False
        gas = gen(502, 100, 0, 400, area=23, name="GAS")
        gas.technology = "CC"
        gas.allow_dispatch_down = False
        gas.allow_dispatch_up = True
        sf = {(10, 20, "1"): {501: 0.5, 502: 0.0}}

        down, up, _rankings = select_candidates(
            [overloaded], [overloaded], sf, [wind, gas],
            swing_buses=[], exclude_buses=[], curtail_areas=[23],
            balance_areas=[23], top_n=15, min_effectiveness=0.01,
            min_relief_mw=0.5, max_neutral=40,
        )

        self.assertIn(wind.key, down)
        self.assertNotIn(wind.key, up)
        self.assertNotIn(gas.key, down)
        self.assertIn(gas.key, up)


if __name__ == "__main__":
    unittest.main()
