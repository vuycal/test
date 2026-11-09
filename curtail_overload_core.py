"""
Pure-Python core for overload curtailment.

PSS/E is not imported here. CurtailOverload.py reads the case, contingencies,
and shift factors, then calls this module to:

* rank the generators that relieve each overloaded line
* choose one joint set of those generators (point 5 of the request)
* solve a linear plan that curtails them together
* keep the swing machine inside its real Pmin/Pmax while holding its P
  when there is room to do so (point 6)

Shift-factor sign
    SF[line, bus] = d(P from->to) / d(P injected at the bus),
    with the mismatch picked up by the swing.
    Curtailing a machine by x MW is an injection change of -x.
    A positive "effectiveness" means curtailing that machine reduces |P|
    on that line.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field


NEAR_ZERO = 0.01


def normalize_ckt(ckt) -> str:
    return str(ckt).strip().strip("'").strip('"')


def branch_key(from_bus, to_bus, ckt):
    return (int(from_bus), int(to_bus), normalize_ckt(ckt))


@dataclass
class Generator:
    bus: int
    machine_id: str
    name: str = ""
    area: int = 0
    area_name: str = ""
    p_mw: float = 0.0
    pmin_mw: float = 0.0
    pmax_mw: float = 0.0
    q_mvar: float = 0.0
    qmin_mvar: float = 0.0
    qmax_mvar: float = 0.0
    status: int = 1
    bus_type: int = 2
    mpid: str = ""
    facility_name: str = ""
    technology: str = ""
    region: str = ""
    catalog_area: int = 0
    catalog_matched: bool = False
    allow_dispatch_down: bool = True
    allow_dispatch_up: bool = True

    @property
    def key(self):
        return (int(self.bus), str(self.machine_id).strip())

    @property
    def room_down_mw(self) -> float:
        """MW this machine can still decrease and stay at or above Pmin."""
        return max(0.0, float(self.p_mw) - float(self.pmin_mw))

    @property
    def room_up_mw(self) -> float:
        """MW this machine can still increase and stay at or below Pmax."""
        return max(0.0, float(self.pmax_mw) - float(self.p_mw))


@dataclass
class Branch:
    from_bus: int
    to_bus: int
    ckt: str
    p_mw: float
    q_mvar: float
    mva: float
    rating_mva: float
    name: str = ""
    rating_rate1_mva: float = 0.0
    rating_rate3_mva: float = 0.0
    from_name: str = ""
    to_name: str = ""
    from_kv: float = 0.0
    to_kv: float = 0.0
    # PSS/E PCTRATE for the selected rating. Negative means it was not supplied
    # and loading_pct falls back to MVA / rating.
    psse_loading_pct: float = -1.0

    @property
    def key(self):
        return branch_key(self.from_bus, self.to_bus, self.ckt)

    @property
    def label(self) -> str:
        name = (" " + self.name.strip()) if self.name else ""
        return "%s-%s-%s%s" % (self.from_bus, self.to_bus, normalize_ckt(self.ckt), name)


@dataclass
class ConstrainedLine:
    """A line the joint plan must respect.

    limit_pct is the loading the plan aims for. Violations use the tighter
    target (for example 98). Other heavy lines use the overload threshold
    (for example 100) so the plan does not create a new overload.
    """

    branch: Branch
    limit_pct: float
    is_violation: bool = False


@dataclass
class Move:
    bus: int
    machine_id: str
    name: str
    area: int
    area_name: str
    p_old_mw: float
    p_new_mw: float
    delta_mw: float
    role: str
    pmin_mw: float
    pmax_mw: float
    effectiveness: float = 0.0
    mpid: str = ""
    facility_name: str = ""
    technology: str = ""
    region: str = ""

    @property
    def key(self):
        return (int(self.bus), str(self.machine_id).strip())


@dataclass
class SwingLimits:
    """Aggregate room of the swing machine(s) that pick up the mismatch."""

    p_mw: float
    pmin_mw: float
    pmax_mw: float
    buses: list = field(default_factory=list)

    def __post_init__(self):
        self.p_mw = float(self.p_mw)
        self.pmin_mw = float(self.pmin_mw)
        self.pmax_mw = float(self.pmax_mw)
        if self.pmin_mw > self.pmax_mw:
            self.pmin_mw, self.pmax_mw = self.pmax_mw, self.pmin_mw
        self.buses = [int(bus) for bus in self.buses]

    @property
    def p_target_mw(self) -> float:
        return clip(self.p_mw, self.pmin_mw, self.pmax_mw)

    @property
    def s_target(self) -> float:
        """Required (pickup - curtailment) so the swing lands on its target."""
        return self.p_mw - self.p_target_mw

    @property
    def s_min(self) -> float:
        """Lowest allowed (pickup - curtailment). More negative means the swing may rise."""
        return self.p_mw - self.pmax_mw

    @property
    def s_max(self) -> float:
        """Highest allowed (pickup - curtailment). Positive means the swing may fall."""
        return self.p_mw - self.pmin_mw


@dataclass
class Plan:
    moves: list
    curtail_mw: float
    pickup_mw: float
    slack_mw: dict
    notes: list
    success: bool
    message: str
    expected_swing_p_mw: float = 0.0
    swing_target_mw: float = 0.0
    rankings: dict = field(default_factory=dict)

    def move_map(self):
        return {mv.key: mv for mv in self.moves}


def clip(value, low, high):
    if low > high:
        low, high = high, low
    return min(max(float(value), float(low)), float(high))


def loading_pct(branch: Branch) -> float:
    """PSS/E branch loading when available, otherwise MVA / rating.

    PSS/E PCTRATE is a current loading: the larger terminal current divided by
    the current represented by the MVA rating at nominal kV. It equals
    MVA / rating only when that terminal voltage is exactly 1.0 pu.
    """
    reported = float(getattr(branch, "psse_loading_pct", -1.0))
    if reported >= 0.0:
        return reported
    rating = float(branch.rating_mva)
    if rating <= NEAR_ZERO:
        return 0.0
    return abs(float(branch.mva)) / rating * 100.0


def mw_limit_for_mva(p_mw, q_mvar, rating_mva, limit_pct) -> float:
    """Largest |P| that keeps sqrt(P^2+Q^2) at or under the MVA limit.

    Q is treated as unchanged, which is the same assumption the old
    curtailment LP made when it converted a rating to MW with the power factor.
    Returns 0 when |Q| alone is already over the limit.
    """
    target_mva = float(rating_mva) * float(limit_pct) / 100.0
    if target_mva <= 0.0:
        return 0.0
    q_abs = abs(float(q_mvar))
    if q_abs >= target_mva:
        return 0.0
    return math.sqrt(target_mva * target_mva - q_abs * q_abs)


def q_exceeds_limit(branch: Branch, limit_pct: float) -> bool:
    target_mva = float(branch.rating_mva) * float(limit_pct) / 100.0
    return abs(float(branch.q_mvar)) >= target_mva > 0.0


def relieving_effectiveness(sf, p_mw) -> float:
    """Positive when curtailing this injection reduces |P| on the line."""
    if abs(float(p_mw)) < NEAR_ZERO:
        return 0.0
    sign = 1.0 if float(p_mw) > 0.0 else -1.0
    return float(sf) * sign


def share_mw(capacity, amount):
    """Share `amount` MW across keys in proportion to the room they have left.

    Same rule as DispatchGens.py SHARE_MODE = HEADROOM: a unit that fills up
    drops out and the remainder is shared again. Returns (moves, unserved).
    """
    room = {key: float(value) for key, value in capacity.items() if float(value) > 1.0e-4}
    moves = {key: 0.0 for key in room}
    left = max(0.0, float(amount))
    if left <= 1.0e-4 or not room:
        return moves, left
    active = dict(room)
    for _ in range(len(room) + 3):
        if left <= 1.0e-3 or not active:
            break
        weight = {key: active[key] - moves[key] for key in active}
        weight_sum = sum(weight.values())
        if weight_sum <= 1.0e-8:
            break
        left_before = left
        still = {}
        for key, w in weight.items():
            free = room[key] - moves[key]
            add = min(left_before * w / weight_sum, free)
            if add < 0.0:
                add = 0.0
            moves[key] += add
            left -= add
            if room[key] - moves[key] > 1.0e-3:
                still[key] = room[key]
        active = still
        if left_before - left <= 1.0e-6:
            break
    return moves, max(0.0, left)


def swing_correction_moves(swing_p_mw, swing_target_mw, room_up, room_down):
    """MW to move on other machines so the swing returns to its target.

    swing_p - target > 0 means the swing is high, so other machines must
    increase. The opposite means other machines must decrease. Every move
    stays inside the room given by the caller (Pmin/Pmax headroom).
    """
    error = float(swing_p_mw) - float(swing_target_mw)
    if error > NEAR_ZERO:
        moves, unserved = share_mw(room_up, error)
        return moves, unserved
    if error < -NEAR_ZERO:
        moves, unserved = share_mw(room_down, -error)
        return {key: -value for key, value in moves.items()}, unserved
    return {}, 0.0


def clip_setpoint(p_mw, pmin_mw, pmax_mw):
    """Setpoint that keeps a non-swing machine inside its real P range."""
    return clip(p_mw, pmin_mw, pmax_mw)


# ---------------------------------------------------------------------------
# Shift-factor report (same PSS/E text ShiftFactor_StandAlone.py reads)
# ---------------------------------------------------------------------------

_ON_RE = re.compile(r"\bON\s+(\d+)\b", re.IGNORECASE)
_TO_RE = re.compile(r"\bTO\s+(\d+)\b", re.IGNORECASE)
_CKT_RE = re.compile(r"\b(?:CKT|CIRCUIT)\s+'?([A-Za-z0-9]+)'?", re.IGNORECASE)
_FACTOR_RE = re.compile(
    r"^\s*(\d+)\b.*\s([+-]?\d+(?:\.\d+)?(?:[Ee][+-]?\d+)?)\s*$"
)


def parse_shift_factor_report(text: str):
    """Parse a PSS/E sensitivity-factor report.

    Returns a list of dicts:
        {from_bus, to_bus, ckt, bus, factor}
    `factor` is d(P from->to) / d(P injection) in the orientation printed
    on that report header. orient_factors() flips it onto the case branch.

    A three-winding transformer is printed as ``TO 3WNDTR <name>`` with no
    to-bus number. Those sections are skipped. The branch list used for
    curtailment is two-winding only, so there is no case key to attach
    them to.
    """
    rows = []
    from_bus = None
    to_bus = None
    ckt = ""
    reading = False
    for raw in text.splitlines():
        line = raw.rstrip()
        upper = line.upper()
        if "SENSITIVITY FACTORS OF BRANCH FLOW" in upper:
            reading = False
            from_bus = None
            to_bus = None
            ckt = ""
            on_match = _ON_RE.search(line)
            to_match = _TO_RE.search(line)
            ckt_match = _CKT_RE.search(line)
            # Both ends are required. ``TO 3WNDTR`` has no bus number.
            if on_match and to_match:
                from_bus = int(on_match.group(1))
                to_bus = int(to_match.group(1))
            if ckt_match:
                ckt = normalize_ckt(ckt_match.group(1))
            continue
        if from_bus is None or to_bus is None:
            continue
        if "<----" in line or "FACTOR" in upper:
            reading = "<----" in line or reading
            if "<----" in line:
                reading = True
            continue
        if not reading:
            continue
        if not line.strip():
            reading = False
            continue
        match = _FACTOR_RE.match(line)
        if not match:
            if line[:1].isalpha() or upper.startswith("SENSITIVITY"):
                reading = False
            continue
        rows.append({
            "from_bus": from_bus,
            "to_bus": to_bus,
            "ckt": ckt,
            "bus": int(match.group(1)),
            "factor": float(match.group(2)),
        })
    return rows


def orient_factors(parsed_rows, monitored_keys):
    """Map report rows onto monitored branch keys.

    monitored_keys: iterable of (from_bus, to_bus, ckt) in case orientation.
    Returns {branch_key: {injection_bus: sf}}.
    A report row printed in the reverse direction is multiplied by -1.
    """
    wanted = {}
    pairs = {}
    for key in monitored_keys:
        key = branch_key(key[0], key[1], key[2])
        wanted[key] = {}
        pairs.setdefault((key[0], key[1]), []).append(key)

    for row in parsed_rows:
        if row.get("from_bus") is None or row.get("to_bus") is None:
            continue
        forward = (int(row["from_bus"]), int(row["to_bus"]))
        ckt = normalize_ckt(row["ckt"])
        factor = float(row["factor"])
        bus = int(row["bus"])
        chosen = None
        sign = 1.0
        if ckt:
            direct = (forward[0], forward[1], ckt)
            reverse = (forward[1], forward[0], ckt)
            if direct in wanted:
                chosen, sign = direct, 1.0
            elif reverse in wanted:
                chosen, sign = reverse, -1.0
        else:
            candidates = []
            for end in (forward, (forward[1], forward[0])):
                for key in pairs.get(end, []):
                    candidates.append((key, 1.0 if end == forward else -1.0))
            if len(candidates) == 1:
                chosen, sign = candidates[0]
        if chosen is None:
            continue
        wanted[chosen][bus] = sign * factor
    return wanted


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------

def _eligible(gen: Generator, swing_buses, exclude_buses) -> bool:
    if int(gen.status) != 1:
        return False
    if int(gen.bus) in swing_buses or int(gen.bus) in exclude_buses:
        return False
    if float(gen.p_mw) <= NEAR_ZERO:
        return False
    return True


def rank_line_gens(line: ConstrainedLine, sf_by_bus, gens, top_n, min_effectiveness, min_relief_mw):
    """Generators that can relieve this line by curtailing, best first."""
    branch = line.branch
    phi = mw_limit_for_mva(branch.p_mw, branch.q_mvar, branch.rating_mva, line.limit_pct)
    needed = max(0.0, abs(float(branch.p_mw)) - phi)
    q_blocks = q_exceeds_limit(branch, line.limit_pct)
    ranked = []
    for gen in gens:
        sf = float(sf_by_bus.get(int(gen.bus), 0.0))
        eff = relieving_effectiveness(sf, branch.p_mw)
        room = gen.room_down_mw
        if eff < float(min_effectiveness) or room <= NEAR_ZERO:
            continue
        relief = eff * room
        if relief < float(min_relief_mw):
            continue
        alone = needed / eff if eff > 1.0e-6 and needed > 0.0 else 0.0
        ranked.append({
            "bus": int(gen.bus),
            "machine_id": gen.machine_id,
            "name": gen.name,
            "area": int(gen.area),
            "area_name": gen.area_name,
            "mpid": gen.mpid,
            "facility_name": gen.facility_name,
            "technology": gen.technology,
            "region": gen.region,
            "p_mw": float(gen.p_mw),
            "pmin_mw": float(gen.pmin_mw),
            "pmax_mw": float(gen.pmax_mw),
            "q_mvar": float(gen.q_mvar),
            "effectiveness": eff,
            "shift_factor": sf,
            "room_mw": room,
            "mw_to_clear_alone": alone,
            "can_clear_alone": (not q_blocks) and alone > 0.0 and room + 0.1 >= alone,
            "relief_at_full_curtail_mw": relief,
        })
    ranked.sort(key=lambda row: (-row["effectiveness"], -row["room_mw"], row["bus"], row["machine_id"]))
    kept = ranked[: int(top_n)] if int(top_n) > 0 else ranked
    for index, row in enumerate(kept, start=1):
        row["rank"] = index
    return kept


def _pickup_score(gen, violated_lines, sf_table):
    """How much increasing this machine helps the worst violated line."""
    best = 0.0
    worst_harm = 0.0
    for line in violated_lines:
        sf = float(sf_table.get(line.branch.key, {}).get(int(gen.bus), 0.0))
        eff = relieving_effectiveness(sf, line.branch.p_mw)
        # Increasing is the opposite of curtailing.
        best = max(best, -eff)
        worst_harm = max(worst_harm, eff)
    return best, worst_harm


def select_candidates(
    violated,
    constrained,
    sf_table,
    gens,
    swing_buses,
    exclude_buses,
    curtail_areas,
    balance_areas,
    top_n,
    min_effectiveness,
    min_relief_mw,
    max_neutral,
):
    """Union of the most effective machines across every violated line.

    Down set: top machines per violated line, restricted to curtail areas.
    Up set: machines whose increase relieves a violated line, plus a few
    nearly neutral machines that can pick up MW so the swing can stay put.
    A machine may be in both sets. The LP will not both raise and lower it.
    """
    swing_buses = set(int(b) for b in swing_buses)
    exclude_buses = set(int(b) for b in exclude_buses)
    curtail_areas = set(int(a) for a in curtail_areas)
    balance_areas = set(int(a) for a in balance_areas)

    usable = [g for g in gens if _eligible(g, swing_buses, exclude_buses)]
    curtailable = [
        g for g in usable
        if bool(g.allow_dispatch_down)
        and (not curtail_areas or int(g.area) in curtail_areas)
    ]
    rankings = {}
    down = {}
    for line in violated:
        sf_by_bus = sf_table.get(line.branch.key, {})
        ranked = rank_line_gens(
            line, sf_by_bus, curtailable, top_n, min_effectiveness, min_relief_mw
        )
        rankings[line.branch.key] = ranked
        for row in ranked:
            gen = _find_gen(curtailable, row["bus"], row["machine_id"])
            if gen is not None:
                down[gen.key] = gen

    if balance_areas:
        pickup_pool = [
            g for g in usable
            if bool(g.allow_dispatch_up)
            and (int(g.area) in balance_areas or g.key in down)
        ]
    else:
        pickup_pool = [
            g for g in usable
            if bool(g.allow_dispatch_up)
            and (not curtail_areas or int(g.area) in curtail_areas)
        ]

    up = {}
    helpful = []
    for gen in pickup_pool:
        if gen.room_up_mw <= NEAR_ZERO:
            continue
        score, harm = _pickup_score(gen, violated, sf_table)
        if score >= float(min_effectiveness) and score >= harm:
            helpful.append((score, gen.room_up_mw, gen))
        elif abs(score) < float(min_effectiveness) and harm < float(min_effectiveness):
            helpful.append((0.0, gen.room_up_mw, gen))
    helpful.sort(key=lambda item: (-item[0], -item[1], item[2].bus, item[2].machine_id))
    neutral_kept = 0
    for score, _room, gen in helpful:
        if score <= 0.0:
            if neutral_kept >= int(max_neutral):
                continue
            neutral_kept += 1
        up[gen.key] = gen

    # A curtailed machine may also be raised if another line needs that.
    for gen in down.values():
        if bool(gen.allow_dispatch_up) and gen.room_up_mw > NEAR_ZERO:
            up.setdefault(gen.key, gen)

    return down, up, rankings


def _find_gen(gens, bus, machine_id):
    mid = str(machine_id).strip()
    for gen in gens:
        if int(gen.bus) == int(bus) and str(gen.machine_id).strip() == mid:
            return gen
    return None


# ---------------------------------------------------------------------------
# Joint linear plan
# ---------------------------------------------------------------------------

def _solve_lp(c, a_ub, b_ub, a_eq, b_eq, bounds):
    import numpy as np
    from scipy.optimize import linprog

    kwargs = dict(
        c=np.array(c, dtype=float),
        A_ub=None if a_ub is None else np.array(a_ub, dtype=float),
        b_ub=None if b_ub is None else np.array(b_ub, dtype=float),
        A_eq=None if a_eq is None else np.array(a_eq, dtype=float),
        b_eq=None if b_eq is None else np.array(b_eq, dtype=float),
        bounds=bounds,
    )
    last_error = None
    for method in ("highs", "highs-ipm", "revised simplex", "interior-point", "simplex"):
        try:
            result = linprog(method=method, **kwargs)
        except (ValueError, TypeError) as exc:
            last_error = exc
            continue
        return result
    raise RuntimeError("scipy.optimize.linprog is not usable (%s)" % last_error)


def build_joint_plan(
    violated,
    constrained,
    sf_table,
    gens,
    swing: SwingLimits,
    swing_buses,
    exclude_buses=None,
    curtail_areas=None,
    balance_areas=None,
    top_n=15,
    min_effectiveness=0.01,
    min_relief_mw=0.5,
    max_neutral=20,
):
    """One curtailment plan for every violated line at once.

    The objective, in order, is:
        1. remove the overload (huge penalty on leftover MW)
        2. keep the swing inside Pmin/Pmax (large penalty)
        3. hold the swing at its current P when that P is already in range
        4. curtail as few MW as possible
        5. pick up only the MW required to do that
    """
    exclude_buses = exclude_buses or []
    curtail_areas = curtail_areas or []
    balance_areas = balance_areas or []
    constrained = list(constrained) if constrained else list(violated)
    notes = []

    for line in violated:
        if q_exceeds_limit(line.branch, line.limit_pct):
            notes.append(
                "%s: reactive flow is at or above the MVA target. "
                "Curtailing P cannot bring this line under the limit."
                % line.branch.label
            )

    down, up, rankings = select_candidates(
        violated,
        constrained,
        sf_table,
        gens,
        swing_buses,
        exclude_buses,
        curtail_areas,
        balance_areas,
        top_n,
        min_effectiveness,
        min_relief_mw,
        max_neutral,
    )

    if not violated:
        return Plan([], 0.0, 0.0, {}, notes, True, "No overloaded line.", swing.p_target_mw, swing.p_target_mw, rankings)

    down_list = [down[key] for key in sorted(down)]
    up_list = [up[key] for key in sorted(up)]
    lines = list(constrained)

    # Variable layout: x (curtail), y (pickup), slack high, slack low,
    # swing deviation above/below the target, swing range shortfalls.
    n_x = len(down_list)
    n_y = len(up_list)
    n_line = len(lines)
    n_var = n_x + n_y + 2 * n_line + 4
    i_shi = n_x + n_y
    i_slo = i_shi + n_line
    i_dpos = i_slo + n_line
    i_dneg = i_dpos + 1
    i_rlo = i_dneg + 1
    i_rhi = i_rlo + 1

    c = [0.0] * n_var
    for i in range(n_x):
        c[i] = 1.0
    for i in range(n_y):
        c[n_x + i] = 0.15
    for i in range(2 * n_line):
        c[i_shi + i] = 1.0e6
    c[i_dpos] = 50.0
    c[i_dneg] = 50.0
    # Range shortfall is priced above the thermal shortfall so a line is never
    # "cleared" by pushing the swing outside Pmin/Pmax. The hard pass below
    # forbids that move. This cost is only used if the hard pass is infeasible
    # (the swing is already outside its range and the other machines cannot
    # bring it back).
    c[i_rlo] = 1.0e7
    c[i_rhi] = 1.0e7

    bounds = []
    for gen in down_list:
        bounds.append((0.0, gen.room_down_mw))
    for gen in up_list:
        bounds.append((0.0, gen.room_up_mw))
    for _ in range(2 * n_line + 4):
        bounds.append((0.0, None))

    def sf_of(line, gen):
        return float(sf_table.get(line.branch.key, {}).get(int(gen.bus), 0.0))

    a_ub = []
    b_ub = []
    for li, line in enumerate(lines):
        phi = mw_limit_for_mva(
            line.branch.p_mw, line.branch.q_mvar, line.branch.rating_mva, line.limit_pct
        )
        p_flow = float(line.branch.p_mw)
        # sum SF*(y - x) - slack_hi <= phi - P
        row_hi = [0.0] * n_var
        row_lo = [0.0] * n_var
        for i, gen in enumerate(down_list):
            sf = sf_of(line, gen)
            row_hi[i] = -sf
            row_lo[i] = sf
        for i, gen in enumerate(up_list):
            sf = sf_of(line, gen)
            row_hi[n_x + i] = sf
            row_lo[n_x + i] = -sf
        row_hi[i_shi + li] = -1.0
        row_lo[i_slo + li] = -1.0
        a_ub.append(row_hi)
        b_ub.append(phi - p_flow)
        a_ub.append(row_lo)
        b_ub.append(phi + p_flow)

    # Swing range, soft: s_min - rlo <= (sum y - sum x) <= s_max + rhi
    row_s_hi = [0.0] * n_var
    row_s_lo = [0.0] * n_var
    for i in range(n_x):
        row_s_hi[i] = -1.0
        row_s_lo[i] = 1.0
    for i in range(n_y):
        row_s_hi[n_x + i] = 1.0
        row_s_lo[n_x + i] = -1.0
    row_s_hi[i_rhi] = -1.0
    row_s_lo[i_rlo] = -1.0
    a_ub.append(row_s_hi)
    b_ub.append(swing.s_max)
    a_ub.append(row_s_lo)
    b_ub.append(-swing.s_min)

    # (sum y - sum x) - dpos + dneg = s_target
    a_eq = [[0.0] * n_var]
    for i in range(n_x):
        a_eq[0][i] = -1.0
    for i in range(n_y):
        a_eq[0][n_x + i] = 1.0
    a_eq[0][i_dpos] = -1.0
    a_eq[0][i_dneg] = 1.0
    b_eq = [swing.s_target]

    if n_x == 0 and n_y == 0:
        notes.append("No generator with enough effectiveness and room was found.")
        slack = {line.branch.key: max(0.0, abs(line.branch.p_mw) - mw_limit_for_mva(
            line.branch.p_mw, line.branch.q_mvar, line.branch.rating_mva, line.limit_pct
        )) for line in violated}
        return Plan(
            [], 0.0, 0.0, slack, notes, False,
            "No candidate generator.", swing.p_mw, swing.p_target_mw, rankings,
        )

    hard_bounds = list(bounds)
    hard_bounds[i_rlo] = (0.0, 0.0)
    hard_bounds[i_rhi] = (0.0, 0.0)
    try:
        result = _solve_lp(c, a_ub, b_ub, a_eq, b_eq, hard_bounds)
        if result.x is None or not getattr(result, "success", False):
            result = _solve_lp(c, a_ub, b_ub, a_eq, b_eq, bounds)
    except Exception as exc:
        return Plan(
            [], 0.0, 0.0, {}, notes, False,
            "Linear plan failed: %s" % exc, swing.p_mw, swing.p_target_mw, rankings,
        )

    if result.x is None or not getattr(result, "success", False):
        message = "Linear plan was not feasible (%s)." % getattr(result, "message", "")
        return Plan(
            [], 0.0, 0.0, {}, notes, False, message, swing.p_mw, swing.p_target_mw, rankings,
        )

    x = result.x

    def _best_eff(gen):
        best = 0.0
        for line in violated:
            sf = sf_of(line, gen)
            best = max(best, relieving_effectiveness(sf, line.branch.p_mw))
        return best

    # Net the up and down variables of the same machine. The solver is priced
    # not to do both, but a net move is what must be applied to the case.
    net = {}
    order = []
    for i, gen in enumerate(down_list):
        net[gen.key] = net.get(gen.key, 0.0) - float(x[i])
        if gen.key not in order:
            order.append(gen.key)
    for i, gen in enumerate(up_list):
        net[gen.key] = net.get(gen.key, 0.0) + float(x[n_x + i])
        if gen.key not in order:
            order.append(gen.key)
    gen_by_key = {}
    for gen in down_list + up_list:
        gen_by_key[gen.key] = gen

    moves = []
    curtail_mw = 0.0
    pickup_mw = 0.0
    for key in order:
        delta = net[key]
        if abs(delta) < 0.01:
            continue
        gen = gen_by_key[key]
        delta = clip(delta, -gen.room_down_mw, gen.room_up_mw)
        if abs(delta) < 0.01:
            continue
        role = "Curtail" if delta < 0.0 else "Pickup"
        moves.append(Move(
            gen.bus, gen.machine_id, gen.name, gen.area, gen.area_name,
            gen.p_mw, gen.p_mw + delta, delta, role,
            gen.pmin_mw, gen.pmax_mw, _best_eff(gen),
            gen.mpid, gen.facility_name, gen.technology, gen.region,
        ))
        if delta < 0.0:
            curtail_mw += -delta
        else:
            pickup_mw += delta

    slack = {}
    for li, line in enumerate(lines):
        hi = max(0.0, float(x[i_shi + li]))
        lo = max(0.0, float(x[i_slo + li]))
        if line.is_violation and max(hi, lo) >= 0.05:
            slack[line.branch.key] = max(hi, lo)
            notes.append(
                "%s: plan leaves about %.2f MW of P-flow outside the target."
                % (line.branch.label, max(hi, lo))
            )

    s_net = pickup_mw - curtail_mw
    expected_swing = swing.p_mw - s_net
    range_gap = max(0.0, float(x[i_rlo])) + max(0.0, float(x[i_rhi]))
    if range_gap >= 0.05:
        notes.append(
            "Swing P cannot be held inside %.2f to %.2f MW with the generators "
            "that are allowed to move. Shortfall about %.2f MW."
            % (swing.pmin_mw, swing.pmax_mw, range_gap)
        )

    cleared = not slack
    if cleared and not any(q_exceeds_limit(line.branch, line.limit_pct) for line in violated):
        message = "Joint plan clears the overloaded lines in the linear model."
    elif not down_list:
        message = "No generator in the curtail areas is effective on these lines."
    else:
        message = "Joint plan reduces the overload. Some flow remains over the target."

    return Plan(
        moves=moves,
        curtail_mw=curtail_mw,
        pickup_mw=pickup_mw,
        slack_mw=slack,
        notes=notes,
        success=cleared and range_gap < 0.05,
        message=message,
        expected_swing_p_mw=expected_swing,
        swing_target_mw=swing.p_target_mw,
        rankings=rankings,
    )


def scale_moves(moves, fraction):
    """Scale every MW change. Used when a full step does not solve."""
    fraction = float(fraction)
    scaled = []
    for mv in moves:
        delta = mv.delta_mw * fraction
        if abs(delta) < 0.01:
            continue
        scaled.append(Move(
            mv.bus, mv.machine_id, mv.name, mv.area, mv.area_name,
            mv.p_old_mw, mv.p_old_mw + delta, delta, mv.role,
            mv.pmin_mw, mv.pmax_mw, mv.effectiveness,
            mv.mpid, mv.facility_name, mv.technology, mv.region,
        ))
    return scaled


def violation_gen_rows(study, contingency, violated, rankings, plan, extra=None):
    """One row per overloaded line per candidate generator.

    The line columns are repeated on every generator row so each violation
    shows its own mitigating generators beside it.
    """
    extra = extra or {}
    move_map = plan.move_map() if plan is not None else {}
    rows = []
    ordered = sorted(violated, key=lambda line: loading_pct(line.branch), reverse=True)
    for line in ordered:
        branch = line.branch
        phi = mw_limit_for_mva(branch.p_mw, branch.q_mvar, branch.rating_mva, line.limit_pct)
        ranked = list(rankings.get(branch.key, []))
        if not ranked:
            ranked = [{
                "rank": "",
                "bus": "",
                "machine_id": "",
                "name": "",
                "area": "",
                "area_name": "",
                "mpid": "",
                "facility_name": "",
                "technology": "",
                "region": "",
                "p_mw": "",
                "pmin_mw": "",
                "pmax_mw": "",
                "effectiveness": "",
                "room_mw": "",
                "mw_to_clear_alone": "",
                "can_clear_alone": "",
            }]
        for gen_row in ranked:
            key = (gen_row["bus"], str(gen_row["machine_id"]).strip()) if gen_row["bus"] != "" else None
            move = move_map.get(key) if key else None
            row = {
                "Study": study,
                "Contingency": contingency,
                "FromBus": branch.from_bus,
                "ToBus": branch.to_bus,
                "Ckt": normalize_ckt(branch.ckt),
                "Line": branch.label,
                "FlowMW": round(float(branch.p_mw), 3),
                "QMVAR": round(float(branch.q_mvar), 3),
                "MVA": round(abs(float(branch.mva)), 3),
                "RatingMVA": round(float(branch.rating_mva), 3),
                "LoadingPct": round(loading_pct(branch), 2),
                "TargetPct": line.limit_pct,
                "PLimitMW": round(phi, 3),
                "GenRank": gen_row["rank"],
                "GenBus": gen_row["bus"],
                "MachineID": gen_row["machine_id"],
                "GenName": gen_row["name"],
                "GenArea": gen_row["area"],
                "GenAreaName": gen_row["area_name"],
                "MPID": gen_row["mpid"],
                "FacilityName": gen_row["facility_name"],
                "Technology": gen_row["technology"],
                "Region": gen_row["region"],
                "PgenMW": gen_row["p_mw"] if gen_row["p_mw"] == "" else round(float(gen_row["p_mw"]), 3),
                "PminMW": gen_row["pmin_mw"] if gen_row["pmin_mw"] == "" else round(float(gen_row["pmin_mw"]), 3),
                "PmaxMW": gen_row["pmax_mw"] if gen_row["pmax_mw"] == "" else round(float(gen_row["pmax_mw"]), 3),
                "Effectiveness": gen_row["effectiveness"] if gen_row["effectiveness"] == "" else round(float(gen_row["effectiveness"]), 4),
                "RoomToCurtailMW": gen_row["room_mw"] if gen_row["room_mw"] == "" else round(float(gen_row["room_mw"]), 3),
                "MWToClearThisLineAlone": (
                    "" if gen_row["mw_to_clear_alone"] == ""
                    else round(float(gen_row["mw_to_clear_alone"]), 3)
                ),
                "CanClearAlone": gen_row["can_clear_alone"],
                "JointCurtailMW": round(-move.delta_mw, 3) if move is not None and move.delta_mw < 0 else 0.0,
                "JointPickupMW": round(move.delta_mw, 3) if move is not None and move.delta_mw > 0 else 0.0,
            }
            row.update(extra)
            rows.append(row)
    return rows


def plan_rows(plan: Plan, extra=None):
    extra = extra or {}
    rows = []
    for mv in plan.moves:
        row = {
            "GenBus": mv.bus,
            "MachineID": mv.machine_id,
            "GenName": mv.name,
            "GenArea": mv.area,
            "GenAreaName": mv.area_name,
            "MPID": mv.mpid,
            "FacilityName": mv.facility_name,
            "Technology": mv.technology,
            "Region": mv.region,
            "Action": mv.role,
            "PBeforeMW": round(mv.p_old_mw, 3),
            "PAfterMW": round(mv.p_new_mw, 3),
            "DeltaMW": round(mv.delta_mw, 3),
            "PminMW": round(mv.pmin_mw, 3),
            "PmaxMW": round(mv.pmax_mw, 3),
            "BestEffectiveness": round(mv.effectiveness, 4),
        }
        row.update(extra)
        rows.append(row)
    return rows
