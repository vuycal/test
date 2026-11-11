"""
Curtail generators to relieve overloaded lines.

This is the case-driven companion to GenCurtailmentOptimization.py.
Electrical generator data comes from the saved case (same fields as
GetGenInfo.py). MPID, technology, region, and dispatch eligibility come from
Merged_MPID_Facility_List.xlsx.
Effectiveness comes from PSS/E branch-flow shift factors, the same
sensitivity report that ShiftFactor_StandAlone.py reads. The arithmetic
lives in curtail_overload_core.py so it can be checked without PSS/E.

How the seven requested points are handled
    1. Shift factors. For the lines that are overloaded (and other lines
       already close to their rating) the script builds a small subsystem
       and runs sensitivity_flows. Effectiveness is d(line MW) / d(gen MW)
       with the swing picking up the mismatch.
    2. N-0 and N-1. Every overloaded line is written with the generators
       that can relieve it: rank, Pgen, Pmin, Pmax, effectiveness, room,
       and the MW that machine would have to drop to clear that line by
       itself. The facility-list workbook controls which types may dispatch
       up/down; no GenerationMapping workbook is required.
    3. N-0 free case. Set MAKE_N0_FREE_CASE = True to apply the plan, solve,
       and save a case. The workbook then lists anything still overloaded.
    4. N-1-1. For each first outage the script clears the overloads on that
       N-1 case (the effective generators of every overloaded line together),
       saves that case, and only then runs the second outages.
    5. Joint plan. The top generators of every overloaded line are one set.
       One linear plan moves that set together, so a unit that helps one
       line and overloads another is limited by both lines.
    6. Swing. SWING_BUSES is the list of bus numbers whose Pgen is held.
       An empty list means every in-service type-3 swing bus. Those
       machines are never curtailed. Their P is held at the current value
       when that value is inside Pmin/Pmax, and it is pulled back inside
       that range when it is not. Other generators pick up the MW.
    7. The N-1 screen and the N-1-1 second level use the same ACCC flow as
       n_1.py and N-1-1.py. Dispatch uses the same Pmin/Pmax headroom share
       as DispatchGens.py.

Run
    Edit the USER SETTINGS block, then run this file with any Python on the
    PSS/E machine. It relaunches itself inside pssecmd35.exe.

    Needs pandas, openpyxl, and scipy in that PSS/E Python.
"""

import os
import sys
import re
import json
import logging
import dataclasses
import shutil
import time
import subprocess
from datetime import datetime
from pathlib import Path
import curtail_overload_core as core
import generator_catalog as gen_catalog
# ---------------------------------------------------------------------------
# USER SETTINGS
# ---------------------------------------------------------------------------

PSSBIN_PATH = r"C:\Program Files\PTI\PSSE35\35.4\PSSBIN"
PSSE_BUSES = 50000

# Saved case to study. The N-0 free case and the N-1 cases are written
# separately; this file is not overwritten.
#CASE_FILE = r"D:\ToolAI\N-1-1-AESO\S5_NW_2029_SP_HG_Post.sav"
CASE_FILE = r"D:\ToolAI\N-1-1-AESO\N0_free.sav"
# Contingency file. Used for N-1 and N-1-1. Ignored for a pure N-0 run.
CON_FILE = r"D:\ToolAI\N-1-1-AESO\test.con"

# Contingency sources for N-1 and N-1-1. Turn either one on, or both:
#   USE_SPECIAL_CON     = read the CONTINGENCY blocks in CON_FILE.
#   USE_SINGLE_LINE_CON = generate one outage for every in-service line or
#                         two-winding transformer in STUDY_AREAS inside the
#                         kV range below (MONITOR_TIES applies to the area test).
# With both on, the two lists are merged. A generated outage that repeats a
# special contingency made of that one line is dropped. Each generated
# contingency is named with the PSS/E branch name, for example
# TL_1097L_356S_611S.
USE_SPECIAL_CON = True
USE_SINGLE_LINE_CON = True
SINGLE_LINE_MIN_KV = 69.0 # Examples: 138.0, 69.0, or 25.0
SINGLE_LINE_MAX_KV = 999.0

# "N-0", "N-1", "N-1-1", or "ALL".
STUDY = "N-1-1"

# N-1-1 only.
#   "COMMON"     = build ONE generator dispatch on the base case that clears
#                  the first-level (N-1) overloads of all contingencies, save it
#                  as COMMON_SAVE_CASE, then apply every second outage on each
#                  first outage and only REPORT what is still overloaded.
#   "PER_OUTAGE" = old behavior: redispatch separately for each first outage.
N11_MODE = "COMMON"
COMMON_SAVE_CASE = r"D:\ToolAI\N-1-1-AESO\N1_free_common.sav"
# The worst contingencies are mitigated together, the result is checked against
# every contingency, and the step is kept only when the totals improve.
COMMON_MAX_ROUNDS = 20          # per phase
COMMON_WORST_PER_ROUND = 3      # contingencies tried one at a time when the joint step fails
# Acceptance rule. A dispatch is judged by the total overload, summed over all
# contingencies, counted only above (limit + MARGINAL_PCT). A line a hair over
# the limit no longer outweighs a large improvement elsewhere. A second pass
# then works on the small leftovers with no tolerance. Any newly
# non-converged contingency still rejects a step.
MARGINAL_PCT = 2.0
# A step that makes things worse is retried at these fractions of the moves.
COMMON_STEP_FRACTIONS = [1.0, 0.5, 0.25]
# True = one linear plan covers all contingencies at once. The worst
# contingencies are targeted, and the heavy lines of the others (and of the
# base case) are held inside their limits. False = older method: mitigate each
# contingency on its own and carry the moves to the base case.
COMMON_COUPLING = True
COMMON_COUPLED_TARGETS = 8      # worst contingencies targeted in one plan
COMMON_COUPLED_PROTECT = 10     # further contingencies whose heavy lines are protected
COMMON_WATCH_PCT = 85.0         # lines at or above this loading are protected
# Violated lines are planned down to this loading (a little under the limit).
COMMON_TARGET_PCT = 97.0
# The ACCC second-level screen flags pairs at or above (limit - margin). Each
# flagged pair is then solved explicitly so the loading is PSS/E PCTRATE.
SECOND_LEVEL_SCREEN_MARGIN_PCT = 5.0
# "AUTO"     = solve every (first, second) pair when there are at most
#              SECOND_LEVEL_EXPLICIT_MAX_PAIRS of them, otherwise use ACCC.
# "EXPLICIT" = always solve every pair. "ACCC" = always screen with ACCC.
SECOND_LEVEL_METHOD = "ACCC"
SECOND_LEVEL_EXPLICIT_MAX_PAIRS = 30

# kV range of the SECOND outages (N-1-1 only). The first outages use
# SINGLE_LINE_MIN_KV / SINGLE_LINE_MAX_KV. Example: screen first outages at
# 69 kV and above, but only 240 kV and above as the second outage. Special
# contingencies (USE_SPECIAL_CON) are always kept in both lists. Leave both
# equal to the single-line range to use one list for both levels.
SECOND_LEVEL_MIN_KV = SINGLE_LINE_MIN_KV
SECOND_LEVEL_MAX_KV = SINGLE_LINE_MAX_KV

# Parallel runs. MAX_WORKERS separate PSS/E processes share the independent
# power flows (contingency screening and the second level). 1 = no parallel run.
# Parallel runs start only when the work is large enough to repay the start-up
# time of each process. If a worker cannot start (for example, no licence for
# another PSS/E session) the study continues in this process.
MAX_WORKERS = 12
PARALLEL_MIN_SCREEN = 200   # contingencies in one screening pass
PARALLEL_MIN_PAIRS = 400   # first/second outage pairs

# N-0 only. False writes the report (each overload, and the generators that
# can relieve it) and does not change the case. True curtails and saves
# SAVE_CASE, aiming for no line at or above THERMAL_LIMIT_PCT.
MAKE_N0_FREE_CASE = True
SAVE_CASE = r"D:\ToolAI\N-1-1-AESO\out_curtail\N0_free.sav"

# N-1 only. False is the report. True also saves one mitigated case per
# contingency that has an overload (under WORK_DIR).
MITIGATE_N1 = False
# N-1 only.
#   "COMMON" = one generator dispatch that clears all N-1 violations, saved to
#              COMMON_SAVE_CASE. The report has the same four sheets as N-0
#              (Summary, Thermal Violations, Generator Dispatch, N-1 Free Case).
#   "ACCC"   = the older ACCC report (see MITIGATE_N1).
N1_MODE = "COMMON"

# 0 means no cap. Use a small number while trying a new contingency file.
MAX_N1_CONTINGENCIES = 0
MAX_N11_FIRST_OUTAGES = 0

WORK_DIR = r"D:\ToolAI\N-1-1-AESO\out_curtail"

# Lines to watch. Empty = the whole case (still filtered by MIN_KV / MAX_KV).
# A line is inside the study when both ends are in one of these areas.
# MONITOR_TIES = True also watches lines with only one end in the areas.
STUDY_AREAS = [17, 18, 19, 21, 23, 26]
MONITOR_TIES = True
MIN_KV = 69.0
MAX_KV = 999.0

# Generator information and dispatch permissions. Values are matched without
# regard to capitalization. An empty technology list means every technology
# in the workbook. Renewable units are allowed down but excluded from pickup
# by the default DISPATCH_UP_TECHNOLOGIES list.
GENERATOR_CATALOG_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "Merged_MPID_Facility_List.xlsx",
)
GENERATOR_CATALOG_SHEET = "Merged MPID Data"
# The case contains newer generators that are not yet in the workbook.
# Curtailing an unmapped unit is allowed; increasing one is not, because its
# technology (renewable or dispatchable) is unknown.
ALLOW_UNMAPPED_DISPATCH_DOWN = True
ALLOW_UNMAPPED_DISPATCH_UP = False

# Workbook values: CC, COGEN, GAS_CtG, HYDRO, IMPORTS, OTHER, SC, SOLAR,
# STORAGE, and WIND.
DISPATCH_DOWN_TECHNOLOGIES = []  # all mapped types, including WIND and SOLAR  "STORAGE",
DISPATCH_UP_TECHNOLOGIES = [
    "CC", "COGEN", "GAS_CtG", "HYDRO", "OTHER", "SC",
]

# Areas allowed to dispatch down/up. Empty down = STUDY_AREAS (or the whole
# case when STUDY_AREAS is empty). Empty up = use the dispatch-down areas.
DISPATCH_DOWN_AREAS = [57, 6, 42, 36, 37, 32, 13, 56, 28, 30, 35, 34, 38, 39, 40, 60, 31, 20, 24, 22, 25, 27, 33, 43, 48, 47, 52, 54, 4, 44, 45, 46, 49, 53, 55]
DISPATCH_UP_AREAS = [57, 6, 42, 36, 37, 32, 13, 56, 28, 30, 35, 34, 38, 39, 40, 60, 31, 20, 24, 22, 25, 27, 33, 43, 48, 47, 52, 54, 4, 44, 45, 46, 49, 53, 55]

# Internal aliases retained for the existing optimization API.
CURTAIL_AREAS = DISPATCH_DOWN_AREAS
BALANCE_AREAS = DISPATCH_UP_AREAS

# Buses whose Pgen is maintained, inside each machine's real Pmin and Pmax.
# Empty = every in-service type-3 swing bus in the case.
# Example: SWING_BUSES = [1520]  or  [1520, 13000]
SWING_BUSES = []

# Generators that must not be moved (same idea as the old prescribed list).
EXCLUDE_GEN_BUSES = []

# A line at or above this percent of RATING is an overload.
# The N-1 / N-1-1 scripts in this folder use 70; 100 is the thermal rating.
THERMAL_LIMIT_PCT = 100.0
# After curtailment the plan aims for this percent (a little under the limit).
TARGET_LOADING_PCT = 90.0
# Lines already this heavy are constrained so the plan does not overload them.
WATCH_PCT = 100.0
# Season / rating set.
#   "SP" = RATE1 (normal, used for the overload test) and RATE3 (emergency).
#   "WP" = RATE2 (normal, used for the overload test) and RATE4 (emergency).
SEASON = "SP"
_SEASON_RATES = {"SP": ("RATE1", "RATE3"), "WP": ("RATE2", "RATE4")}
RATING, EMERGENCY_RATING = _SEASON_RATES.get(
    str(SEASON).strip().upper(), ("RATE1", "RATE3")
)
# Report column titles follow the season.
COL_NORMAL = "RatingMVA (%s)" % RATING
COL_EMERGENCY = "RatingMVA (%s)" % EMERGENCY_RATING
COL_RATING_LONG = "Rating MVA (%s)" % RATING
COL_EMERGENCY_LONG = "Emergency Rating (%s) MVA" % EMERGENCY_RATING

# How many of the most effective generators to keep per overloaded line.
# The joint plan uses the union of those lists.
TOP_N_GENS = 0            # 0 = retain every effective generator
MIN_EFFECTIVENESS = 0.0001
MIN_RELIEF_MW = 0.01
MAX_NEUTRAL_GENS = 200
SF_REPORT_MIN = 0.0001    # PSS/E sensitivity rows below this are not reported

# Passed to machine_chng. PF keeps the Q/P ratio, HOLD keeps Q.
Q_MODE = "PF"

MAX_ITERS = 20
SWING_TOL_MW = 5.0
FNSL_STUDY = [0,0,0,1,2,0,99,0]

FNSL_CURTAIL = [0, 0, 0, 0, 2, 0, 0, 0]

# ---------------------------------------------------------------------------
# PSS/E: relaunch inside pssecmd35.exe when someone double-clicks this file
# ---------------------------------------------------------------------------

PSSECMD_EXE = os.path.join(PSSBIN_PATH, "pssecmd35.exe")
INSIDE_PSSECMD = os.environ.get("RUN_INSIDE_PSSECMD") == "1"


def run_in_pssecmd(script):
    script = os.path.abspath(script)
    bootstrap = (
        "import os, sys, traceback\n"
        "code = 0\n"
        "try:\n"
        f"    exec(compile(open({script!r}).read(), {script!r}, 'exec'),"
        f" {{'__name__': '__main__', '__file__': {script!r}}})\n"
        "except BaseException:\n"
        "    traceback.print_exc()\n"
        "    code = 1\n"
        "sys.stdout.flush()\n"
        "sys.stderr.flush()\n"
        "os._exit(code)\n"
    )
    commands = "PYTHON\nexec(%r)\n" % bootstrap
    kwargs = {}
    if sys.platform.startswith("win"):
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.Popen(
        [PSSECMD_EXE, "-buses", str(PSSE_BUSES)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
        env=dict(os.environ, RUN_INSIDE_PSSECMD="1"),
        **kwargs,
    )
    proc.stdin.write(commands)
    proc.stdin.close()
    for line in proc.stdout:
        print(line, end="", flush=True)
    return proc.wait()


if __name__ == "__main__" and not INSIDE_PSSECMD:
    sys.exit(run_in_pssecmd(__file__))

import psspy
import pssarrays

try:
    import pandas as pd
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter
except ImportError as exc:
    print("PSSE_RESULT:" + json.dumps({
        "success": False,
        "error": "Missing library in the PSS/E Python: %s. Install pandas and openpyxl." % exc,
    }), flush=True)
    raise

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
import curtail_overload_core as core

_i = psspy.getdefaultint()
_f = psspy.getdefaultreal()
_s = psspy.getdefaultchar()

SOLVED_OK = {0, 10, 11, 12}
SOLVED_MEANINGS = {
    0: "Met convergence tolerance",
    1: "Iteration limit exceeded (did not solve)",
    2: "Blown up",
    3: "Terminated by non-divergent option",
    4: "Terminated by console interrupt",
    5: "Singular Jacobian matrix or voltage of 0.0 detected",
    6: "Inertial power flow dispatch error (INLF)",
    7: "OPF solution met convergence tolerance (NOPF)",
    9: "Solution not attempted",
    10: "RSOL converged with Phase shift locked",
    11: "RSOL converged with TOLN increased",
    12: "RSOL converged with Y load conversion due to low voltage",
}

WORK = Path(WORK_DIR)
WORK.mkdir(parents=True, exist_ok=True)
RUN_ID = datetime.now().strftime("%Y%m%d_%H%M%S")
IS_WORKER = bool(os.environ.get("CURTAIL_JOB"))
LOG_PATH = WORK / ("CurtailOverload_%s.log" % RUN_ID)
if IS_WORKER:
    LOG_PATH = Path(os.environ["CURTAIL_JOB"]).with_suffix(".worker.log")
PROGRESS_PATH = WORK / ("CurtailOverload_%s_progress.txt" % RUN_ID)
EXCEL_PATH = WORK / ("CurtailOverload_%s_%s.xlsx" % (STUDY.replace("+", ""), RUN_ID))
ACTIVE_CON_FILE = ""          # merged contingency file written by build_contingency_set()
CON_DISPLAY = {}              # optional report alias; single-line names are the branch name
CON_SOURCE_COUNTS = {"special": 0, "single_line": 0}
GENERATOR_CATALOG = {}
_CATALOG_WARNING_KEYS = set()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(str(LOG_PATH), encoding="utf-8"), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("curtail")


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def check(ierr, message):
    if ierr != 0:
        log.warning("%s failed. ierr = %s", message, ierr)
    return ierr


def solved_meaning(code):
    return SOLVED_MEANINGS.get(code, "Unknown code %s" % code)


def convergence_report_value(issue):
    """Short value used in loading columns when no percentage is available."""
    text = str(issue or "Not converged")
    upper = text.upper()
    if "BLOWN" in upper or "DIVERG" in upper:
        return "Blown Up"
    if "ITERATION" in upper:
        return "Iteration Limit"
    return text


def is_solved(code=None):
    if code is None:
        code = psspy.solved()
    return code in SOLVED_OK


def solve_case(options, label):
    ierr = psspy.fnsl(list(options))
    code = psspy.solved()
    if not is_solved(code):
        log.warning("%s did not solve (%s). fnsl ierr = %s", label, solved_meaning(code), ierr)
    else:
        log.info("%s solved (%s).", label, solved_meaning(code))
    return is_solved(code), code


def safe_name(text):
    cleaned = re.sub(r"[^A-Za-z0-9_]+", "_", str(text).strip())
    return cleaned[:80] or "case"


def q_for_new_p(p_old, q_old, p_new, qmin, qmax):
    if Q_MODE.upper() == "HOLD" or abs(p_old) < core.NEAR_ZERO:
        q_new = q_old
    else:
        q_new = q_old * (p_new / p_old)
    return min(max(q_new, qmin), qmax)


def set_machine_p(bus, machine_id, p_new, q_new):
    ierr = psspy.machine_chng_3(
        int(bus),
        str(machine_id),
        [_i, _i, _i, _i, _i, _i, _i],
        [float(p_new), float(q_new), _f, _f, _f, _f, _f, _f, _f, _f, _f, _f, _f, _f, _f, _f, _f],
    )
    if ierr > 0:
        log.warning("machine_chng_3 failed for %s %s ierr=%s", bus, machine_id, ierr)
    return ierr


def areas_or_blank(areas):
    return [int(a) for a in areas]


def curtail_areas():
    if CURTAIL_AREAS:
        return areas_or_blank(CURTAIL_AREAS)
    return areas_or_blank(STUDY_AREAS)


def load_generator_catalog():
    catalog = gen_catalog.load_generator_catalog(
        GENERATOR_CATALOG_FILE,
        GENERATOR_CATALOG_SHEET,
    )
    technologies = sorted({
        record["technology"]
        for records in catalog.values()
        for record in records
        if record["technology"]
    })
    for label, selection in (
        ("DISPATCH_DOWN_TECHNOLOGIES", DISPATCH_DOWN_TECHNOLOGIES),
        ("DISPATCH_UP_TECHNOLOGIES", DISPATCH_UP_TECHNOLOGIES),
    ):
        unknown = sorted({
            gen_catalog.normalize_choice(value)
            for value in selection
            if gen_catalog.normalize_choice(value) not in technologies
        })
        if unknown:
            raise RuntimeError(
                "%s contains values not found in the generator catalog: %s. "
                "Available values: %s"
                % (label, ", ".join(unknown), ", ".join(technologies))
            )
    log.info(
        "Generator catalog: %s unique buses, technologies %s.",
        len(catalog), technologies,
    )
    return catalog


def catalog_fields(bus, generator_name, case_area):
    metadata = gen_catalog.resolve_generator(
        GENERATOR_CATALOG, bus, generator_name
    )
    down_allowed = gen_catalog.technology_is_allowed(
        metadata, DISPATCH_DOWN_TECHNOLOGIES,
        ALLOW_UNMAPPED_DISPATCH_DOWN,
    )
    up_allowed = gen_catalog.technology_is_allowed(
        metadata, DISPATCH_UP_TECHNOLOGIES,
        ALLOW_UNMAPPED_DISPATCH_UP,
    )

    warning_key = (int(bus), str(generator_name).strip())
    if not metadata["matched"] and warning_key not in _CATALOG_WARNING_KEYS:
        _CATALOG_WARNING_KEYS.add(warning_key)
        log.warning(
            "Generator %s at bus %s is not in %s; dispatch down=%s, up=%s.",
            generator_name, bus, GENERATOR_CATALOG_FILE,
            ALLOW_UNMAPPED_DISPATCH_DOWN, ALLOW_UNMAPPED_DISPATCH_UP,
        )
    elif metadata["matched"]:
        catalog_area = int(metadata.get("catalog_area") or 0)
        if (
            catalog_area
            and int(case_area) != catalog_area
            and warning_key not in _CATALOG_WARNING_KEYS
        ):
            _CATALOG_WARNING_KEYS.add(warning_key)
            log.warning(
                "Generator %s at bus %s is area %s in the case and area %s "
                "in the catalog. The case area controls dispatch eligibility.",
                generator_name, bus, case_area, catalog_area,
            )
        if metadata.get("ambiguous") and warning_key not in _CATALOG_WARNING_KEYS:
            _CATALOG_WARNING_KEYS.add(warning_key)
            log.warning(
                "Bus %s has ambiguous catalog metadata (%s). Eligibility is "
                "conservative because the workbook has no machine ID.",
                bus, metadata.get("technology", ""),
            )
    return metadata, down_allowed, up_allowed


# ---------------------------------------------------------------------------
# Case data
# ---------------------------------------------------------------------------

def read_generators(areas):
    """Machines in `areas`. Empty = the whole case. Same fields as GetGenInfo."""
    if not areas:
        sid = -1
    else:
        sid = 1
        psspy.bsys(sid, 0, [0.0, 999.0], len(areas), list(areas), 0, [], 0, [], 0, [])
    flag = 4  # in service and out of service
    ierr, gen_int = psspy.amachint(sid, flag, ["NUMBER", "STATUS"])
    ierr, gen_char = psspy.amachchar(sid, flag, ["ID", "NAME"])
    if gen_int is None or gen_char is None:
        log.warning("PSS/E did not return the machine list.")
        return []
    real = {}
    for name in ("PGEN", "PMIN", "PMAX", "QGEN", "QMIN", "QMAX"):
        ierr, values = psspy.amachreal(sid, flag, name)
        if values is None:
            log.warning("PSS/E did not return %s (ierr %s).", name, ierr)
            return []
        real[name] = values[0]
    ierr, bus_int = psspy.abusint(sid, 2, ["NUMBER", "AREA", "TYPE"])
    if bus_int is None:
        log.warning("PSS/E did not return bus data.")
        return []
    bus_area = dict(zip(bus_int[0], bus_int[1]))
    bus_type = dict(zip(bus_int[0], bus_int[2]))
    ierr, area_num = psspy.aareaint(-1, 1, ["NUMBER"])
    ierr, area_name = psspy.aareachar(-1, 1, ["AREANAME"])
    area_names = {}
    if area_num is not None and area_name is not None:
        area_names = dict(zip(area_num[0], area_name[0]))
    gens = []
    for i, bus in enumerate(gen_int[0]):
        area = bus_area.get(bus, 0)
        generator_name = str(gen_char[1][i]).strip()
        metadata, allow_down, allow_up = catalog_fields(
            bus, generator_name, area
        )
        gens.append(core.Generator(
            bus=int(bus),
            machine_id=str(gen_char[0][i]).strip(),
            name=generator_name,
            area=int(area) if area is not None else 0,
            area_name=str(area_names.get(area, "")).strip(),
            p_mw=float(real["PGEN"][i]),
            pmin_mw=float(real["PMIN"][i]),
            pmax_mw=float(real["PMAX"][i]),
            q_mvar=float(real["QGEN"][i]),
            qmin_mvar=float(real["QMIN"][i]),
            qmax_mvar=float(real["QMAX"][i]),
            status=int(gen_int[1][i]),
            bus_type=int(bus_type.get(bus, 0) or 0),
            mpid=metadata["mpid"],
            facility_name=metadata["facility_name"],
            technology=metadata["technology"],
            region=metadata["region"],
            catalog_area=int(metadata["catalog_area"] or 0),
            catalog_matched=bool(metadata["matched"]),
            allow_dispatch_down=bool(allow_down),
            allow_dispatch_up=bool(allow_up),
        ))
    if areas:
        psspy.bsysdef(sid, 0)
    return gens


def read_bus_info():
    ierr, ints = psspy.abusint(-1, 2, ["NUMBER", "AREA", "TYPE"])
    ierr, reals = psspy.abusreal(-1, 2, ["BASE"])
    ierr, chars = psspy.abuschar(-1, 2, ["NAME"])
    if ints is None or reals is None:
        return {}, {}, {}, {}
    area = dict(zip(ints[0], ints[1]))
    bus_type = dict(zip(ints[0], ints[2]))
    base_kv = dict(zip(ints[0], reals[0]))
    bus_name = {}
    if chars is not None:
        bus_name = {
            int(bus): str(name).strip()
            for bus, name in zip(ints[0], chars[0])
        }
    return area, bus_type, base_kv, bus_name


def _percent_fields_for_rating(rating_name):
    """PSS/E percent-loading fields corresponding to a branch rating."""
    fields = {
        "RATEA": ("PCTRATEA", "PCTRATE1"),
        "RATE1": ("PCTRATE1", "PCTRATEA"),
        "RATEB": ("PCTRATEB", "PCTRATE2"),
        "RATE2": ("PCTRATE2", "PCTRATEB"),
        "RATEC": ("PCTRATEC", "PCTRATE3"),
        "RATE3": ("PCTRATE3", "PCTRATEC"),
    }
    return fields.get(str(rating_name).upper(), ("PCTRATEA", "PCTRATE1"))


def _abrn_vector(kind, name, ties):
    api = {"int": psspy.abrnint, "char": psspy.abrnchar, "real": psspy.abrnreal}[kind]
    # FLAG=3: in-service non-transformer branches plus two-winding
    # transformers. ENTRY=1: return each element once.
    ierr, values = api(-1, 1, ties, 3, 1, [name])
    if ierr != 0 or not values:
        return None
    return values[0]


def read_branches(min_kv=None, max_kv=None, require_rating=True):
    """In-service two-winding branches inside the study window."""
    min_kv = float(MIN_KV if min_kv is None else min_kv)
    max_kv = float(MAX_KV if max_kv is None else max_kv)
    area_of, _bus_type, kv_of, name_of = read_bus_info()
    # Area and tie filtering is done below so a tie can be kept when
    # MONITOR_TIES is on.
    ties = 1
    frm = _abrn_vector("int", "FROMNUMBER", ties)
    to = _abrn_vector("int", "TONUMBER", ties)
    ckt = _abrn_vector("char", "ID", ties)
    p_mw = _abrn_vector("real", "P", ties)
    q_mvar = _abrn_vector("real", "Q", ties)
    # MAXMVA is the larger terminal power flow. PSS/E's displayed line loading
    # is not this value divided by the rating: PCTRATE uses terminal current
    # and the rating converted at nominal kV.
    mva = _abrn_vector("real", "MAXMVA", ties)
    rate1 = _abrn_vector("real", RATING, ties)
    if rate1 is None and RATING == "RATE1":
        rate1 = _abrn_vector("real", "RATEA", ties)
    rate3 = _abrn_vector("real", EMERGENCY_RATING, ties)
    if rate3 is None and EMERGENCY_RATING == "RATE3":
        rate3 = _abrn_vector("real", "RATEC", ties)
    rating = _abrn_vector("real", RATING, ties)
    rating_used = RATING
    if rating is None:
        rating_used = "RATEA"
        rating = _abrn_vector("real", "RATEA", ties)
    psse_pct = None
    for pct_name in _percent_fields_for_rating(rating_used):
        psse_pct = _abrn_vector("real", pct_name, ties)
        if psse_pct is not None:
            break
    if psse_pct is None:
        log.warning(
            "PSS/E did not return percent loading for %s. "
            "MVA / rating will be used.",
            rating_used,
        )
    if frm is None or to is None or ckt is None or p_mw is None or rating is None:
        log.warning("Could not read branch flows. rating=%s", rating_used)
        return [], rating_used
    if q_mvar is None:
        q_mvar = [0.0] * len(frm)
    if mva is None:
        mva = [0.0] * len(frm)
    study = set(int(a) for a in STUDY_AREAS)
    branches = []
    for i in range(len(frm)):
        fbus = int(frm[i])
        tbus = int(to[i])
        rate = float(rating[i])
        if require_rating and rate <= core.NEAR_ZERO:
            continue
        if study:
            in_from = int(area_of.get(fbus, -1)) in study
            in_to = int(area_of.get(tbus, -1)) in study
            if MONITOR_TIES:
                if not (in_from or in_to):
                    continue
            elif not (in_from and in_to):
                continue
        from_kv = float(kv_of.get(fbus, 0.0) or 0.0)
        to_kv = float(kv_of.get(tbus, 0.0) or 0.0)
        # Transmission-only screen: both terminals must be inside the kV
        # window. A 138/25 kV or 138/0.6 kV distribution transformer is not
        # monitored merely because its high side is above MIN_KV.
        if (
            from_kv < min_kv or to_kv < min_kv
            or from_kv > max_kv or to_kv > max_kv
        ):
            continue
        p_val = float(p_mw[i])
        q_val = float(q_mvar[i])
        mva_val = abs(float(mva[i]))
        if mva_val <= core.NEAR_ZERO:
            mva_val = (p_val ** 2 + q_val ** 2) ** 0.5
        # Query the name by branch key. BRANCHNAME from a separate bulk call
        # can have a different row sequence when the subsystem contains a mix
        # of lines and transformers, which can attach a valid name to the
        # wrong outage.
        cid = core.normalize_ckt(ckt[i])
        name_result = psspy.brnnam(fbus, tbus, cid)
        if isinstance(name_result, tuple) and len(name_result) >= 2:
            name_ierr, name_value = name_result[0], name_result[1]
            label = str(name_value).strip() if name_ierr == 0 else ""
        else:
            label = ""
        branches.append(core.Branch(
            from_bus=fbus,
            to_bus=tbus,
            ckt=cid,
            p_mw=p_val,
            q_mvar=q_val,
            mva=mva_val,
            rating_mva=rate,
            name=label,
            rating_rate1_mva=float(rate1[i]) if rate1 is not None else rate,
            rating_rate3_mva=float(rate3[i]) if rate3 is not None else 0.0,
            from_name=str(name_of.get(fbus, "")).strip(),
            to_name=str(name_of.get(tbus, "")).strip(),
            from_kv=float(kv_of.get(fbus, 0.0) or 0.0),
            to_kv=float(kv_of.get(tbus, 0.0) or 0.0),
            psse_loading_pct=(
                float(psse_pct[i]) if psse_pct is not None else -1.0
            ),
        ))
    return branches, rating_used


def split_lines(branches):
    """Violations (at or above the thermal limit) and the lines the plan must respect."""
    target = min(float(TARGET_LOADING_PCT), float(THERMAL_LIMIT_PCT))
    violated = []
    constrained = []
    seen = set()
    for branch in branches:
        pct = core.loading_pct(branch)
        if pct + 1.0e-6 < float(WATCH_PCT) and pct + 1.0e-6 < float(THERMAL_LIMIT_PCT):
            continue
        is_violation = pct + 1.0e-6 >= float(THERMAL_LIMIT_PCT)
        limit = target if is_violation else float(THERMAL_LIMIT_PCT)
        line = core.ConstrainedLine(branch, limit, is_violation)
        if branch.key in seen:
            continue
        seen.add(branch.key)
        constrained.append(line)
        if is_violation:
            violated.append(line)
    violated.sort(key=lambda line: core.loading_pct(line.branch), reverse=True)
    return violated, constrained


def worst_of(violated):
    if not violated:
        return 0.0, 0
    return max(core.loading_pct(line.branch) for line in violated), len(violated)


# ---------------------------------------------------------------------------
# Swing
# ---------------------------------------------------------------------------

def classify_swing(gens):
    """Return (maintain buses, electrical sink buses, warning text)."""
    type3 = sorted({int(g.bus) for g in gens if int(g.bus_type) == 3 and int(g.status) == 1})
    requested = [int(b) for b in SWING_BUSES]
    if requested:
        maintain = requested
    else:
        maintain = list(type3)
    known = {int(g.bus) for g in gens}
    missing = [b for b in maintain if b not in known]
    sink = [b for b in maintain if b in type3]
    if not sink:
        sink = list(type3) or maintain[:1]
    note = ""
    if missing:
        note = "These swing buses have no machine in the case: %s." % missing
        log.warning(note)
    if not sink:
        note = "No swing bus found. Set SWING_BUSES to the type-3 bus."
        log.warning(note)
    else:
        log.info("Maintained buses %s. Mismatch is picked up by %s.", maintain, sink)
    return maintain, sink, note


def swing_limits_from(gens, sink_buses):
    rows = [g for g in gens if int(g.bus) in set(sink_buses) and int(g.status) == 1]
    if not rows:
        return core.SwingLimits(0.0, 0.0, 0.0, buses=list(sink_buses))
    return core.SwingLimits(
        p_mw=sum(g.p_mw for g in rows),
        pmin_mw=sum(g.pmin_mw for g in rows),
        pmax_mw=sum(g.pmax_mw for g in rows),
        buses=list(sink_buses),
    )


def pull_locked_buses_inside_range(maintain, sink):
    """Non-swing buses in the maintain list are setpoints. Clamp them to Pmin/Pmax."""
    sink_set = set(int(b) for b in sink)
    gens = read_generators([])
    moved = 0
    for gen in gens:
        if int(gen.bus) not in maintain or int(gen.bus) in sink_set or int(gen.status) != 1:
            continue
        target = core.clip_setpoint(gen.p_mw, gen.pmin_mw, gen.pmax_mw)
        if abs(target - gen.p_mw) < 0.05:
            continue
        q_new = q_for_new_p(gen.p_mw, gen.q_mvar, target, gen.qmin_mvar, gen.qmax_mvar)
        if set_machine_p(gen.bus, gen.machine_id, target, q_new) == 0:
            moved += 1
            log.info(
                "Bus %s machine %s P moved from %.2f to %.2f to stay inside Pmin/Pmax.",
                gen.bus, gen.machine_id, gen.p_mw, target,
            )
    if moved:
        solve_case(FNSL_CURTAIL, "After clamping maintained non-swing machines")
    return moved


def swing_targets(gens, maintain):
    targets = {}
    for gen in gens:
        if int(gen.bus) not in set(int(b) for b in maintain) or int(gen.status) != 1:
            continue
        targets[gen.key] = core.clip_setpoint(gen.p_mw, gen.pmin_mw, gen.pmax_mw)
    return targets


def correct_swing(maintain, sink, targets, protect_keys):
    """Move other machines so the swing P returns to its target, inside Pmin/Pmax."""
    maintain_set = set(int(b) for b in maintain)
    down_areas = set(curtail_areas())
    up_areas = set(int(a) for a in BALANCE_AREAS) or down_areas
    for _pass in range(4):
        gens = read_generators([])
        sink_rows = [g for g in gens if int(g.bus) in set(sink) and int(g.status) == 1]
        if not sink_rows:
            return 0.0
        swing_p = sum(g.p_mw for g in sink_rows)
        swing_target = 0.0
        for gen in sink_rows:
            swing_target += targets.get(gen.key, core.clip_setpoint(gen.p_mw, gen.pmin_mw, gen.pmax_mw))
        if abs(swing_p - swing_target) <= SWING_TOL_MW:
            # Still flag an individual machine sitting outside its own range.
            outside = [
                g for g in sink_rows
                if g.p_mw < g.pmin_mw - SWING_TOL_MW or g.p_mw > g.pmax_mw + SWING_TOL_MW
            ]
            if not outside:
                return 0.0
        room_up = {}
        room_down = {}
        lookup = {}
        for gen in gens:
            if int(gen.bus) in maintain_set or int(gen.status) != 1:
                continue
            if gen.key in protect_keys:
                continue
            up = gen.room_up_mw
            down = gen.room_down_mw
            if (
                up > 0.05
                and bool(gen.allow_dispatch_up)
                and (not up_areas or int(gen.area) in up_areas)
            ):
                room_up[gen.key] = up
            if (
                down > 0.05
                and bool(gen.allow_dispatch_down)
                and (not down_areas or int(gen.area) in down_areas)
            ):
                room_down[gen.key] = down
            lookup[gen.key] = gen
        moves, unserved = core.swing_correction_moves(swing_p, swing_target, room_up, room_down)
        if not any(abs(v) >= 0.05 for v in moves.values()):
            if unserved > 1.0:
                log.warning(
                    "Swing is %.2f MW from its target and the other machines have no room left.",
                    swing_p - swing_target,
                )
            return unserved
        for key, delta in moves.items():
            if abs(delta) < 0.05:
                continue
            gen = lookup[key]
            p_new = core.clip(gen.p_mw + delta, gen.pmin_mw, gen.pmax_mw)
            q_new = q_for_new_p(gen.p_mw, gen.q_mvar, p_new, gen.qmin_mvar, gen.qmax_mvar)
            set_machine_p(gen.bus, gen.machine_id, p_new, q_new)
        solve_case(FNSL_CURTAIL, "Swing correction")
    gens = read_generators([])
    sink_rows = [g for g in gens if int(g.bus) in set(sink) and int(g.status) == 1]
    swing_p = sum(g.p_mw for g in sink_rows) if sink_rows else 0.0
    swing_target = sum(
        targets.get(g.key, core.clip_setpoint(g.p_mw, g.pmin_mw, g.pmax_mw)) for g in sink_rows
    )
    return abs(swing_p - swing_target)


def swing_rows(gens, maintain, sink, targets, stage):
    rows = []
    interesting = set(int(b) for b in list(maintain) + list(sink))
    for gen in gens:
        if int(gen.bus) not in interesting:
            continue
        target = targets.get(gen.key, "")
        inside = gen.pmin_mw - 0.1 <= gen.p_mw <= gen.pmax_mw + 0.1
        rows.append({
            "Stage": stage,
            "Bus": gen.bus,
            "MachineID": gen.machine_id,
            "Name": gen.name,
            "MPID": gen.mpid,
            "FacilityName": gen.facility_name,
            "Technology": gen.technology,
            "Region": gen.region,
            "BusType": gen.bus_type,
            "Role": "Swing" if int(gen.bus) in set(sink) else "Maintained",
            "PminMW": round(gen.pmin_mw, 3),
            "PmaxMW": round(gen.pmax_mw, 3),
            "PNowMW": round(gen.p_mw, 3),
            "PTargetMW": "" if target == "" else round(float(target), 3),
            "InsideRange": "Yes" if inside else "No",
        })
    return rows


# ---------------------------------------------------------------------------
# Shift factors
# ---------------------------------------------------------------------------

def write_text(path, text):
    with open(path, "w", newline="\n") as handle:
        handle.write(text)


def compute_shift_factors(constrained, source_buses, sink_buses, tag):
    """SF of each constrained line to an injection at each source bus.

    The sink subsystem is the swing, matching a curtailment that the swing
    picks up. Returns ({branch_key: {bus: sf}}, message).
    """
    source_buses = sorted({int(b) for b in source_buses if int(b) not in set(sink_buses)})
    sink_buses = sorted({int(b) for b in sink_buses})
    if not constrained or not source_buses or not sink_buses:
        return {}, "Shift factors were not run (no line, no source bus, or no swing)."
    monitor_buses = sorted({
        int(bus)
        for line in constrained
        for bus in (line.branch.from_bus, line.branch.to_bus)
    })
    folder = WORK / "sf"
    folder.mkdir(exist_ok=True)
    sub = folder / ("%s.sub" % tag)
    mon = folder / ("%s.mon" % tag)
    con = folder / ("%s.con" % tag)
    dfx = folder / ("%s.dfx" % tag)
    report = folder / ("%s.sf" % tag)
    sub_lines = ["COM", "COM curtailment shift-factor subsystems", "COM", "SUBSYSTEM 'SOURCE'"]
    sub_lines += [" BUS %d" % bus for bus in source_buses]
    sub_lines += ["END", "SUBSYSTEM 'SINK'"]
    sub_lines += [" BUS %d" % bus for bus in sink_buses]
    sub_lines += ["END", "SUBSYSTEM 'MONITOR'"]
    sub_lines += [" BUS %d" % bus for bus in monitor_buses]
    sub_lines += ["END", "END", ""]
    write_text(sub, "\n".join(sub_lines))
    write_text(mon, "\n".join([
        "COM",
        "COM branches whose flow sensitivity is required",
        "COM",
        "MONITOR BRANCHES IN SUBSYSTEM 'MONITOR'",
        "END",
        "",
    ]))
    write_text(con, "\n".join([
        "COM",
        "CONTINGENCY 'SFBASE'",
        "END",
        "END",
        "",
    ]))
    for path in (dfx, report):
        if path.exists():
            try:
                path.unlink()
            except OSError:
                pass
    psspy.report_output(2, str(report), [0, 0])
    ierr = psspy.dfax_2([1, 0, 0], str(sub), str(mon), str(con), str(dfx))
    if ierr != 0:
        psspy.close_report()
        return {}, "DFAX for shift factors failed (ierr %s)." % ierr
    ierr = psspy.sensitivity_flows(
        [0, 0],
        [0, 1, 0, 0, 0, 0, 0, 0, 0],
        [0.5, float(SF_REPORT_MIN)],
        ["SOURCE", "", "MONITOR"],
        str(dfx),
    )
    psspy.close_report()
    if ierr != 0:
        return {}, "sensitivity_flows failed (ierr %s)." % ierr
    try:
        text = report.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {}, "Could not read the shift-factor report (%s)." % exc
    parsed = core.parse_shift_factor_report(text)
    factors = core.orient_factors(parsed, [line.branch.key for line in constrained])
    nonempty = sum(1 for value in factors.values() if value)
    log.info("Shift factors: %s monitored lines, %s with at least one injection.", len(factors), nonempty)
    if nonempty == 0:
        preview = "\n".join(text.splitlines()[:25])
        log.warning("Shift-factor report had no usable rows. First lines:\n%s", preview)
        return factors, "Shift-factor report contained no injection rows."
    try:
        dfx.unlink()
    except OSError:
        pass
    return factors, ""


def source_buses_for(gens, maintain):
    blocked = set(int(b) for b in list(maintain) + list(EXCLUDE_GEN_BUSES))
    areas = set(curtail_areas())
    balance = set(int(a) for a in BALANCE_AREAS)
    buses = []
    for gen in gens:
        if int(gen.status) != 1 or int(gen.bus) in blocked:
            continue
        if gen.p_mw <= core.NEAR_ZERO:
            continue
        in_curtail = not areas or int(gen.area) in areas
        in_balance = (int(gen.area) in balance) if balance else in_curtail
        allowed_down = in_curtail and bool(gen.allow_dispatch_down)
        allowed_up = in_balance and bool(gen.allow_dispatch_up)
        if allowed_down or allowed_up:
            buses.append(int(gen.bus))
    return sorted(set(buses))


# ---------------------------------------------------------------------------
# One operating point: rank generators, optionally apply the joint plan
# ---------------------------------------------------------------------------

def analyze_and_maybe_apply(label, do_apply, study_name, contingency):
    """Rank mitigating generators for the case that is already solved.

    When do_apply is True, curtail and re-solve until the overloads are
    gone, the swing is out of room, or MAX_ITERS is reached.
    """
    result = {
        "violation_rows": [],
        "plan_rows": [],
        "applied_rows": [],
        "swing_rows": [],
        "remaining_rows": [],
        "notes": [],
        "cleared": False,
        "curtail_mw": 0.0,
        "mitigation": "Not required",
    }
    gens = read_generators([])
    maintain, sink, swing_note = classify_swing(gens)
    if swing_note:
        result["notes"].append(swing_note)
    # Apply the user's absolute first-level rule: if the solved operating point
    # has any screened line at or above the thermal limit, mitigation is
    # attempted. If it has none, pass it to the next contingency level unchanged.
    present = split_lines(read_branches()[0])[0]
    has_violation = bool(present)
    if do_apply and has_violation:
        pull_locked_buses_inside_range(maintain, sink)
        gens = read_generators([])
    targets = swing_targets(gens, maintain)
    result["swing_rows"].extend(swing_rows(gens, maintain, sink, targets, "%s before" % label))
    before = {g.key: g for g in gens}

    def _one_plan(iteration):
        branches, _rating = read_branches()
        if iteration == 1 and not branches:
            result["notes"].append(
                "%s: no in-service branch was found in the study window. Check STUDY_AREAS and MIN_KV."
                % label
            )
        violated, constrained = split_lines(branches)
        if not violated:
            return None, violated, 0.0, 0
        # sensitivity_flows can disturb the working case. Hold the solved
        # case and put it back before the plan is applied.
        hold = WORK / ("_hold_%s.sav" % safe_name(label))
        held = psspy.save(str(hold)) == 0
        sf, sf_note = compute_shift_factors(
            constrained,
            source_buses_for(read_generators([]), maintain),
            sink,
            safe_name("%s_%s" % (label, iteration)),
        )
        if held:
            check(psspy.case(str(hold)), "Restoring the case after shift factors")
        if sf_note:
            result["notes"].append(sf_note)
        gens_now = read_generators([])
        swing = swing_limits_from(gens_now, sink)
        plan = core.build_joint_plan(
            violated,
            constrained,
            sf,
            gens_now,
            swing,
            swing_buses=maintain,
            exclude_buses=EXCLUDE_GEN_BUSES,
            curtail_areas=curtail_areas(),
            balance_areas=BALANCE_AREAS,
            top_n=TOP_N_GENS,
            min_effectiveness=MIN_EFFECTIVENESS,
            min_relief_mw=MIN_RELIEF_MW,
            max_neutral=MAX_NEUTRAL_GENS,
        )
        worst, count = worst_of(violated)
        return plan, violated, worst, count

    plan, violated, worst, count = _one_plan(1)
    if plan is None:
        message = (
            "%s: no screened line is at or above %.1f%%. Mitigation was not applied."
            % (label, THERMAL_LIMIT_PCT)
        )
        log.info(message)
        result["cleared"] = True
        result["mitigation"] = "Not required"
        result["notes"].append(message)
        result["swing_rows"].extend(swing_rows(read_generators([]), maintain, sink, targets, "%s after" % label))
        return result
    result["mitigation"] = "Attempted" if do_apply else "Report only"

    extra = {"Label": label, "Iteration": 1}
    result["violation_rows"].extend(
        core.violation_gen_rows(study_name, contingency, violated, plan.rankings, plan, extra)
    )
    result["plan_rows"].extend(core.plan_rows(plan, extra))
    result["notes"].extend(plan.notes)
    result["notes"].append("%s: %s" % (label, plan.message))
    log.info(
        "%s iteration 1: %s overloads, worst %.1f%%, curtail %.1f MW, pickup %.1f MW. %s",
        label, count, worst, plan.curtail_mw, plan.pickup_mw, plan.message,
    )
    if not do_apply:
        result["notes"].append("%s: report only. The saved case was not modified." % label)
        result["swing_rows"].extend(swing_rows(read_generators([]), maintain, sink, targets, "%s after" % label))
        return result

    applied_any = False
    for iteration in range(1, MAX_ITERS + 1):
        if iteration > 1:
            plan, violated, worst, count = _one_plan(iteration)
            if plan is None:
                result["cleared"] = True
                break
            extra = {"Label": label, "Iteration": iteration}
            result["plan_rows"].extend(core.plan_rows(plan, extra))
            result["notes"].extend(plan.notes)
            log.info(
                "%s iteration %s: %s overloads, worst %.1f%%, curtail %.1f MW.",
                label, iteration, count, worst, plan.curtail_mw,
            )
        if plan.curtail_mw < 0.5 and plan.pickup_mw < 0.5:
            result["notes"].append("%s: the effective generators have no room left." % label)
            break
        snap = WORK / ("_snap_%s.sav" % safe_name(label))
        if psspy.save(str(snap)) != 0:
            result["notes"].append("%s: could not save a snapshot before moving generators." % label)
            break
        applied_ok = False
        fraction = 1.0
        for _attempt in range(3):
            psspy.case(str(snap))
            moves = core.scale_moves(plan.moves, fraction)
            live = {g.key: g for g in read_generators([])}
            for move in moves:
                gen = live.get(move.key)
                if gen is None or int(gen.status) != 1:
                    continue
                p_new = core.clip(gen.p_mw + move.delta_mw, gen.pmin_mw, gen.pmax_mw)
                q_new = q_for_new_p(gen.p_mw, gen.q_mvar, p_new, gen.qmin_mvar, gen.qmax_mvar)
                set_machine_p(gen.bus, gen.machine_id, p_new, q_new)
                log.info(
                    "  %s %s %s P %.2f -> %.2f MW",
                    move.role, move.bus, move.machine_id, gen.p_mw, p_new,
                )
            ok, _code = solve_case(FNSL_CURTAIL, "%s apply x%.2f" % (label, fraction))
            if not ok:
                fraction *= 0.5
                continue
            protected = {move.key for move in moves if move.delta_mw < -0.05}
            correct_swing(maintain, sink, targets, protected)
            ok, _code = solve_case(FNSL_CURTAIL, "%s after swing correction" % label)
            if not ok:
                fraction *= 0.5
                continue
            new_violated, _constrained = split_lines(read_branches()[0])
            new_worst, new_count = worst_of(new_violated)
            if new_worst > worst + 1.0 and new_count >= count:
                log.warning("%s step %.2f made the worst overload higher. Trying a smaller step.", label, fraction)
                fraction *= 0.5
                continue
            applied_ok = True
            applied_any = True
            result["curtail_mw"] += sum(-mv.delta_mw for mv in moves if mv.delta_mw < 0)
            log.info("%s after the move: %s overloads, worst %.1f%%.", label, new_count, new_worst)
            break
        if not applied_ok:
            psspy.case(str(snap))
            result["notes"].append(
                "%s: the power flow did not solve after the generator move. The last solved case was kept."
                % label
            )
            break
        if new_count == 0:
            result["cleared"] = True
            break

    result["mitigation"] = (
        "Applied" if applied_any else "Attempted - no acceptable move"
    )

    after = {g.key: g for g in read_generators([])}
    for key, old in before.items():
        new = after.get(key)
        if new is None:
            continue
        # The N-0 dispatch sheet is for machines that joined the active-power
        # redispatch. Reactive-only power-flow changes are not dispatch moves.
        if abs(new.p_mw - old.p_mw) < 0.05:
            continue
        result["applied_rows"].append({
            "Label": label,
            "Study": study_name,
            "Contingency": contingency,
            "Bus": old.bus,
            "MachineID": old.machine_id,
            "Name": old.name,
            "Area": old.area,
            "MPID": old.mpid,
            "FacilityName": old.facility_name,
            "Technology": old.technology,
            "Region": old.region,
            "PBeforeMW": round(old.p_mw, 3),
            "PAfterMW": round(new.p_mw, 3),
            "DeltaPMW": round(new.p_mw - old.p_mw, 3),
            "PminMW": round(old.pmin_mw, 3),
            "PmaxMW": round(old.pmax_mw, 3),
            "QBeforeMVAR": round(old.q_mvar, 3),
            "QAfterMVAR": round(new.q_mvar, 3),
            "InsideRange": "Yes" if old.pmin_mw - 0.1 <= new.p_mw <= old.pmax_mw + 0.1 else "No",
        })
    remaining, _constrained = split_lines(read_branches()[0])
    for line in remaining:
        branch = line.branch
        result["remaining_rows"].append({
            "Label": label,
            "Study": study_name,
            "Contingency": contingency,
            "FromBus": branch.from_bus,
            "ToBus": branch.to_bus,
            "Ckt": branch.ckt,
            "Line": branch.label,
            "LoadingPct": round(core.loading_pct(branch), 2),
            "FlowMW": round(branch.p_mw, 3),
            "MVA": round(branch.mva, 3),
            "RatingMVA": round(branch.rating_mva, 3),
            "QMVAR": round(branch.q_mvar, 3),
            "QBlocksClearing": "Yes" if core.q_exceeds_limit(branch, line.limit_pct) else "No",
        })
    result["cleared"] = not remaining
    result["swing_rows"].extend(swing_rows(list(after.values()), maintain, sink, targets, "%s after" % label))
    if result["cleared"]:
        log.info("%s is within %.1f%%.", label, THERMAL_LIMIT_PCT)
    else:
        log.info("%s still has %s overloaded lines.", label, len(remaining))
    return result


# ---------------------------------------------------------------------------
# Contingencies, DFAX, ACCC  (same pattern as n_1.py and N-1-1.py)
# ---------------------------------------------------------------------------

def read_con(path):
    contingencies = []
    with open(path, "r", errors="replace") as handle:
        lines = handle.readlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.upper().startswith("CONTINGENCY"):
            match = re.search(r"'([^']+)'|\"([^\"]+)\"", line)
            if match:
                name = match.group(1) or match.group(2)
            else:
                name = line.replace("CONTINGENCY", "").strip()
            actions = []
            i += 1
            while i < len(lines) and not lines[i].strip().upper().startswith("END"):
                action = lines[i].strip()
                if action and not action.upper().startswith("COM"):
                    actions.append(action)
                i += 1
            contingencies.append({"name": name, "actions": actions})
        i += 1
    return contingencies


def write_con(contingencies, path):
    lines = ["COM", "COM contingency file written by CurtailOverload.py", "COM"]
    for con in contingencies:
        lines.append("CONTINGENCY '%s'" % con["name"])
        for action in con["actions"]:
            lines.append(" " + action)
        lines.append("END")
        lines.append("")
    lines.append("END")
    lines.append("")
    write_text(path, "\n".join(lines))


_OPEN_BRANCH_RE = re.compile(
    r"FROM\s+BUS\s+'?(\d+)'?\s+TO\s+BUS\s+'?(\d+)'?\s+CKT\s+'?([^'\s]+)'?",
    re.IGNORECASE,
)


def _canonical_branch(from_bus, to_bus, ckt):
    return (
        min(int(from_bus), int(to_bus)),
        max(int(from_bus), int(to_bus)),
        core.normalize_ckt(ckt),
    )


def single_line_outage_of(contingency):
    """Canonical branch when the contingency is exactly one OPEN LINE/BRANCH, else None."""
    actions = [a for a in contingency["actions"] if a.strip() and not a.strip().startswith("/")]
    if len(actions) != 1:
        return None
    text = actions[0].strip()
    upper = text.upper()
    if not ("OPEN BRANCH" in upper or "OPEN LINE" in upper or "OPEN TRANSFORMER" in upper):
        return None
    match = _OPEN_BRANCH_RE.search(text)
    if not match:
        return None
    return _canonical_branch(match.group(1), match.group(2), match.group(3))


def contingency_name_for_branch(branch, taken_names):
    """PSS/E branch name, for example TL_1097L_356S_611S."""
    raw = str(branch.name).strip().replace("'", "")
    if not raw:
        raw = "TL_%s_%s_%s" % (
            core.normalize_ckt(branch.ckt), branch.from_bus, branch.to_bus
        )
        log.warning(
            "Branch %s-%s-%s has no branch name. Using %s.",
            branch.from_bus, branch.to_bus, branch.ckt, raw,
        )
    candidate = raw[:40]
    suffix = 2
    while candidate.casefold() in taken_names:
        # A special contingency can already use this branch name, for example
        # a double-circuit outage named TL_989L_77S_310P. Keep the branch name
        # and add the circuit so the single-line event stays distinct.
        extra = "_%s" % core.normalize_ckt(branch.ckt)
        if suffix > 2:
            extra = "_%s_%s" % (core.normalize_ckt(branch.ckt), suffix)
        candidate = raw[:40 - len(extra)] + extra
        suffix += 1
    taken_names.add(candidate.casefold())
    return candidate


def generate_single_line_contingencies(skip_branches, taken_names, min_kv=None, max_kv=None):
    """One OPEN LINE contingency per eligible branch, named from its branch name."""
    load_base()
    branches, _rating_name = read_branches(
        min_kv=SINGLE_LINE_MIN_KV if min_kv is None else min_kv,
        max_kv=SINGLE_LINE_MAX_KV if max_kv is None else max_kv,
        require_rating=False,
    )
    contingencies = []
    seen = set(skip_branches)
    for branch in branches:
        canonical = _canonical_branch(branch.from_bus, branch.to_bus, branch.ckt)
        if canonical in seen:
            continue
        seen.add(canonical)
        name = contingency_name_for_branch(branch, taken_names)
        action = "OPEN LINE FROM BUS %s TO BUS %s CKT %s" % (
            branch.from_bus, branch.to_bus, core.normalize_ckt(branch.ckt)
        )
        contingencies.append({"name": name, "actions": [action]})
    return contingencies


def build_contingency_set():
    """Merge the special and single-line contingencies into one explicit file.

    Returns (contingencies, path). Both N-1 and N-1-1 read this file, so the
    ACCC screen and the detailed passes always use the same list.
    """
    special = read_con(CON_FILE) if USE_SPECIAL_CON else []
    taken = {str(c["name"]).strip().casefold() for c in special}
    covered = set()
    for con in special:
        outage = single_line_outage_of(con)
        if outage is not None:
            covered.add(outage)
    generated = []
    if USE_SINGLE_LINE_CON:
        generated = generate_single_line_contingencies(covered, taken)
    contingencies = special + generated
    CON_SOURCE_COUNTS["special"] = len(special)
    CON_SOURCE_COUNTS["single_line"] = len(generated)
    destination = WORK / ("contingencies_%s.con" % RUN_ID)
    write_con(contingencies, destination)
    log.info(
        "Contingency set: %s special + %s single-line (%.1f-%.1f kV) = %s. File: %s",
        len(special), len(generated), SINGLE_LINE_MIN_KV, SINGLE_LINE_MAX_KV,
        len(contingencies), destination,
    )
    return contingencies, str(destination)


def build_second_level_set(first_contingencies):
    """Second-outage list for N-1-1, with its own kV range.

    The special contingencies are kept as they are. Single-line outages are
    generated again for SECOND_LEVEL_MIN_KV .. SECOND_LEVEL_MAX_KV. When that
    range equals the first-level range the first list is reused.
    """
    same_range = (
        float(SECOND_LEVEL_MIN_KV) == float(SINGLE_LINE_MIN_KV)
        and float(SECOND_LEVEL_MAX_KV) == float(SINGLE_LINE_MAX_KV)
    )
    if same_range or not USE_SINGLE_LINE_CON:
        return list(first_contingencies)
    special = read_con(CON_FILE) if USE_SPECIAL_CON else []
    taken = {str(c["name"]).strip().casefold() for c in special}
    covered = set()
    for con in special:
        outage = single_line_outage_of(con)
        if outage is not None:
            covered.add(outage)
    generated = generate_single_line_contingencies(
        covered, taken, SECOND_LEVEL_MIN_KV, SECOND_LEVEL_MAX_KV
    )
    seconds = special + generated
    destination = WORK / ("second_level_contingencies_%s.con" % RUN_ID)
    write_con(seconds, destination)
    log.info(
        "Second-level outage set: %s special + %s single-line (%.1f-%.1f kV) = %s. File: %s",
        len(special), len(generated), SECOND_LEVEL_MIN_KV, SECOND_LEVEL_MAX_KV,
        len(seconds), destination,
    )
    return seconds


def apply_actions(actions):
    """Apply every action and return True only when all actions succeed."""
    all_ok = True
    for action in actions:
        text = action.strip()
        upper = text.upper()
        if "OPEN BRANCH" in upper or "OPEN LINE" in upper or "OPEN TRANSFORMER" in upper:
            match = re.search(
                r"FROM\s+BUS\s+'?(\d+)'?\s+TO\s+BUS\s+'?(\d+)'?\s+CKT\s+'?([^'\s]+)'?",
                text,
                re.IGNORECASE,
            )
            if not match:
                log.warning("Could not parse branch outage: %s", text)
                all_ok = False
                continue
            fbus = int(match.group(1))
            tbus = int(match.group(2))
            ckt = match.group(3).strip()
            ierr = psspy.branch_chng_3(
                fbus,
                tbus,
                ckt,
                [0, _i, _i, _i, _i, _i],
                [_f] * 12,
                [_f] * 12,
                _s,
            )
            if ierr != 0:
                # Two-winding transformers are not modified by branch_chng_3.
                # Contingency files commonly describe them as OPEN LINE, so
                # fall back to the transformer API when no branch was found.
                tx_result = psspy.two_winding_chng_6(
                    fbus,
                    tbus,
                    ckt,
                    [0] + [_i] * 15,
                    [_f] * 21,
                    [_f] * 12,
                    _s,
                    _s,
                )
                tx_ierr = tx_result[0] if isinstance(tx_result, tuple) else tx_result
                if tx_ierr == 0:
                    log.info("Opened two-winding transformer %s-%s-%s.", fbus, tbus, ckt)
                else:
                    log.warning(
                        "Opening %s-%s-%s failed as a branch (ierr %s) "
                        "and as a two-winding transformer (ierr %s).",
                        fbus, tbus, ckt, ierr, tx_ierr,
                    )
                    all_ok = False
        elif "DISCONNECT BUS" in upper:
            match = re.search(r"DISCONNECT\s+BUS\s+'?(\d+)'?", text, re.IGNORECASE)
            if not match:
                log.warning("Could not parse bus disconnect: %s", text)
                all_ok = False
                continue
            ierr = psspy.dscn(int(match.group(1)))
            if ierr != 0:
                check(ierr, "Disconnect bus %s" % match.group(1))
                all_ok = False
        elif "REMOVE MACHINE" in upper:
            match = re.search(
                r"REMOVE\s+MACHINE\s+'?(\d+)'?\s+['\"]?([^'\"]+)['\"]?",
                text,
                re.IGNORECASE,
            )
            if not match:
                log.warning("Could not parse machine outage: %s", text)
                all_ok = False
                continue
            ierr = psspy.machine_chng_2(
                int(match.group(1)),
                match.group(2).strip(),
                [0, _i, _i, _i, _i, _i],
                [_f] * 17,
            )
            if ierr != 0:
                check(ierr, "Remove machine %s %s" % (match.group(1), match.group(2).strip()))
                all_ok = False
        else:
            log.warning("Unsupported contingency action: %s", text)
            all_ok = False
    return all_ok


def handle_islands(label):
    ierr, buses = psspy.tree(1, 0)
    if ierr != 0:
        log.warning("TREE failed after %s. ierr = %s", label, ierr)
        return False
    while buses > 0:
        log.warning("Swingless island after %s (%s buses). Disconnecting it.", label, buses)
        ierr, buses = psspy.tree(2, 1)
        if ierr != 0:
            log.warning("TREE disconnect failed after %s. ierr = %s", label, ierr)
            return False
    return True


def prepare_outage(actions, label):
    """Apply and solve an outage. Return (success, failure description)."""
    if not apply_actions(actions):
        log.warning("%s was not applied completely; skipping this contingency.", label)
        return False, "Outage action failed"
    if not handle_islands(label):
        return False, "Island processing failed"
    ok, code = solve_case(FNSL_STUDY, label)
    if not ok:
        return False, solved_meaning(code)
    if not handle_islands(label + " after solve"):
        return False, "Island processing failed"
    ok, code = solve_case(FNSL_STUDY, label + " after island check")
    return ok, "" if ok else solved_meaning(code)


def load_base():
    ierr = psspy.case(str(CASE_FILE))
    if ierr != 0:
        raise RuntimeError("Could not open the case (ierr %s): %s" % (ierr, CASE_FILE))
    ok, code = solve_case(FNSL_STUDY, "Base case")
    return ok, code


def create_study_mon_sub(tag=""):
    sub = WORK / ("study%s.sub" % tag)
    mon = WORK / ("study%s.mon" % tag)
    lines = ["COM", "COM study areas", "COM", "SUBSYSTEM 'STUDY_AREA'"]
    if STUDY_AREAS:
        for area in STUDY_AREAS:
            lines.append(" AREA %d" % int(area))
    else:
        lines.append(" KVRANGE %.3f %.3f" % (MIN_KV, MAX_KV))
    lines += ["END", "END", ""]
    write_text(sub, "\n".join(lines))
    write_text(mon, "\n".join([
        "COM",
        "COM monitor",
        "COM",
        "MONITOR BRANCHES IN SUBSYSTEM 'STUDY_AREA'",
        "MONITOR TIES FROM SUBSYSTEM 'STUDY_AREA'",
        "MONITOR VOLTAGE RANGE SUBSYSTEM 'STUDY_AREA' 0.90 1.10",
        "END",
        "",
    ]))
    return sub, mon


def generate_dfax(con_file, dfx_file, sub, mon):
    if Path(dfx_file).exists():
        try:
            Path(dfx_file).unlink()
        except OSError:
            return 999
    ierr = psspy.dfax_2([1, 1, 1], str(sub), str(mon), str(con_file), str(dfx_file))
    check(ierr, "DFAX")
    if ierr != 0:
        return ierr
    if not Path(dfx_file).exists() or Path(dfx_file).stat().st_size == 0:
        return 998
    return 0


def run_accc(acc_file, dfx_file):
    if Path(acc_file).exists():
        try:
            Path(acc_file).unlink()
        except OSError as exc:
            log.warning("Could not replace %s (%s).", acc_file, exc)
            return 999
    ierr = psspy.accc_with_dsp_3(
        0.5,
        [0, 0, 0, 1, 1, 1, 0, 0, 0, 0, 0],
        "",
        str(dfx_file),
        str(acc_file),
        "",
        "",
        "",
    )
    check(ierr, "ACCC")
    return ierr


def rating_lookup():
    lookup = {}
    frm = _abrn_vector("int", "FROMNUMBER", 1)
    to = _abrn_vector("int", "TONUMBER", 1)
    ckt = _abrn_vector("char", "ID", 1)
    rate = _abrn_vector("real", RATING, 1)
    name = RATING
    if rate is None:
        name = "RATEA"
        rate = _abrn_vector("real", "RATEA", 3)
    if frm is None or rate is None:
        return lookup, name
    for fbus, tbus, cid, value in zip(frm, to, ckt, rate):
        key = core.branch_key(fbus, tbus, cid)
        rev = core.branch_key(tbus, fbus, cid)
        lookup[key] = float(value)
        lookup[rev] = float(value)
    return lookup, name


def branch_rating_lookup(rating_name):
    """Return a both-directions lookup for one PSS/E branch rating field."""
    lookup = {}
    frm = _abrn_vector("int", "FROMNUMBER", 1)
    to = _abrn_vector("int", "TONUMBER", 1)
    ckt = _abrn_vector("char", "ID", 1)
    values = _abrn_vector("real", rating_name, 1)
    if frm is None or to is None or ckt is None or values is None:
        return lookup
    for fbus, tbus, cid, value in zip(frm, to, ckt, values):
        key = core.branch_key(fbus, tbus, cid)
        reverse = core.branch_key(tbus, fbus, cid)
        lookup[key] = float(value)
        lookup[reverse] = float(value)
    return lookup


def branch_key_from_label(element):
    parts = str(element).split()
    if len(parts) < 7:
        return None
    try:
        from_bus = int(parts[0])
    except ValueError:
        return None
    to_bus = None
    for token in parts[1:]:
        if re.fullmatch(r"\d+", token):
            to_bus = int(token)
            break
    if to_bus is None:
        return None
    return core.branch_key(from_bus, to_bus, parts[-1])


def parse_acc_thermal(acc_file, ratings, first_outage=None, limit_pct=None):
    """Thermal rows from an ACC file. first_outage is set for an N-1-1 pass."""
    screen_limit = float(THERMAL_LIMIT_PCT if limit_pct is None else limit_pct)
    rows = []
    non_converged = []
    try:
        summary = pssarrays.accc_summary(str(acc_file))
        labels = list(summary["colabel"])
        elements = list(summary["melement"])
    except Exception as exc:
        non_converged.append({
            "Contingency": "",
            "FirstOutage": first_outage or "",
            "Issue": "Could not read ACC file: %s" % exc,
        })
        return rows, non_converged
    for label in labels:
        if first_outage is None and str(label).strip().upper() == "BASE CASE":
            continue
        try:
            sol = pssarrays.accc_solution(str(acc_file), label)
        except Exception as exc:
            non_converged.append({
                "Contingency": str(label),
                "FirstOutage": first_outage or "",
                "Issue": str(exc),
            })
            continue
        if sol.get("ierr", 1) != 0 or not sol.get("cnvflag", False):
            non_converged.append({
                "Contingency": str(label),
                "FirstOutage": first_outage or "",
                "Issue": str(sol.get("cnvcond", "not converged")),
            })
            continue
        for element, flow in zip(elements, list(sol.get("ampflow", []))):
            key = branch_key_from_label(element)
            if key is None:
                continue
            rating = ratings.get(key)
            if not rating or rating <= 0:
                continue
            try:
                mva = abs(float(flow))
            except (TypeError, ValueError):
                continue
            pct = mva / rating * 100.0
            if pct < screen_limit:
                continue
            rows.append({
                "Contingency": str(label),
                "FirstOutage": first_outage or "",
                "Element": str(element),
                "FromBus": key[0],
                "ToBus": key[1],
                "Ckt": key[2],
                "LoadingPct": round(pct, 2),
                "MVA": round(mva, 3),
                "RatingMVA": round(rating, 3),
            })
    return rows, non_converged


def find_contingency(contingencies, name):
    target = str(name).strip().casefold()
    for con in contingencies:
        if str(con["name"]).strip().casefold() == target:
            return con
    return None


# ---------------------------------------------------------------------------
# Studies
# ---------------------------------------------------------------------------

def n0_line_description(branch):
    return "%s %s %.2f - %s %s %.2f - %s" % (
        branch.from_bus,
        branch.from_name,
        branch.from_kv,
        branch.to_bus,
        branch.to_name,
        branch.to_kv,
        core.normalize_ckt(branch.ckt),
    )


def n0_loading_pct(branch):
    """Use the same PSS/E percent that determines whether a line is overloaded."""
    return core.loading_pct(branch)


def n0_rating_pct_value(branch):
    """MVA represented by LoadingPct x RATE1 / 100, as in the requested report."""
    rating = float(branch.rating_rate1_mva or branch.rating_mva)
    return n0_loading_pct(branch) * rating / 100.0


def n0_thermal_row(branch):
    return {
        "Study": "N-0",
        "Contingency": "BASE CASE",
        "BranchName": branch.name,
        "LineViolation": n0_line_description(branch),
        "FromBus": branch.from_bus,
        "ToBus": branch.to_bus,
        "Ckt": core.normalize_ckt(branch.ckt),
        "Line": branch.label,
        "FlowMW": round(float(branch.p_mw), 3),
        "QMVAR": round(float(branch.q_mvar), 3),
        # PSS/E prints this terminal's own MVA beside P and Q. The branch
        # loading percent below is PSS/E PCTRATE, which uses both terminals.
        "MVA": round((float(branch.p_mw) ** 2 + float(branch.q_mvar) ** 2) ** 0.5, 3),
        COL_EMERGENCY: (
            round(float(branch.rating_rate3_mva), 3)
            if float(branch.rating_rate3_mva) > core.NEAR_ZERO else ""
        ),
        COL_NORMAL: round(
            float(branch.rating_rate1_mva or branch.rating_mva), 3
        ),
        "RatingPct": round(n0_rating_pct_value(branch), 3),
        "LoadingPct": round(n0_loading_pct(branch), 2),
    }


def n0_free_case_row(before, after):
    row = n0_thermal_row(before)
    if after is None:
        row["Post RatingPct"] = ""
        row["Post LoadingPct"] = ""
    else:
        row["Post RatingPct"] = round(n0_rating_pct_value(after), 3)
        row["Post LoadingPct"] = round(n0_loading_pct(after), 2)
    return row


def run_n0():
    load_base()
    log.info("N-0. Free case = %s.", MAKE_N0_FREE_CASE)
    before_branches = read_branches()[0]
    before_violated = split_lines(before_branches)[0]
    result = analyze_and_maybe_apply("N-0", MAKE_N0_FREE_CASE, "N-0", "BASE CASE")
    result["n0_thermal_rows"] = [
        n0_thermal_row(line.branch) for line in before_violated
    ]
    if MAKE_N0_FREE_CASE:
        after_by_key = {branch.key: branch for branch in read_branches()[0]}
        result["n0_free_rows"] = [
            n0_free_case_row(line.branch, after_by_key.get(line.branch.key))
            for line in before_violated
        ]
    saved = ""
    if MAKE_N0_FREE_CASE:
        destination = Path(SAVE_CASE) if SAVE_CASE else WORK / "N0_free.sav"
        destination.parent.mkdir(parents=True, exist_ok=True)
        ierr = psspy.save(str(destination))
        if ierr == 0:
            saved = str(destination)
            log.info("Saved N-0 case to %s.", destination)
        else:
            result["notes"].append("Could not save the N-0 case (ierr %s)." % ierr)
    result["saved_case"] = saved
    return result


def run_n1():
    if str(N1_MODE).upper() == "COMMON":
        return run_n11_common("N-1", second_level=False)
    return run_n1_accc()


def run_n1_accc():
    contingencies = read_con(ACTIVE_CON_FILE)
    by_name = {str(c["name"]).strip().casefold(): c for c in contingencies}
    log.info(
        "N-1. %s contingencies in %s. Mitigate = %s.",
        len(contingencies), ACTIVE_CON_FILE, MITIGATE_N1,
    )
    load_base()
    sub, mon = create_study_mon_sub()
    dfx = WORK / ("n1_%s.dfx" % RUN_ID)
    acc = WORK / ("n1_%s.acc" % RUN_ID)
    ierr = generate_dfax(ACTIVE_CON_FILE, dfx, sub, mon)
    if ierr != 0:
        raise RuntimeError("N-1 DFAX failed (ierr %s)." % ierr)
    ierr = run_accc(acc, dfx)
    if ierr != 0:
        raise RuntimeError("N-1 ACCC failed (ierr %s)." % ierr)
    # Ratings from the base case, not from the last contingency left in memory.
    load_base()
    ratings, _name = rating_lookup()
    thermal, non_converged = parse_acc_thermal(acc, ratings)
    log.info("N-1 ACCC: %s overload rows, %s non-converged.", len(thermal), len(non_converged))
    names = []
    seen = set()
    for row in sorted(thermal, key=lambda item: item["LoadingPct"], reverse=True):
        key = row["Contingency"].strip().casefold()
        if key not in seen:
            seen.add(key)
            names.append(row["Contingency"])
    if MAX_N1_CONTINGENCIES and len(names) > MAX_N1_CONTINGENCIES:
        log.info("Capping the detailed N-1 pass at %s contingencies.", MAX_N1_CONTINGENCIES)
        names = names[:MAX_N1_CONTINGENCIES]
    combined = _empty_result()
    combined["non_converged"] = non_converged
    for index, name in enumerate(names, start=1):
        con = by_name.get(name.strip().casefold()) or find_contingency(contingencies, name)
        if con is None:
            combined["notes"].append("ACCC contingency '%s' was not found in the con file." % name)
            continue
        log.info("N-1 detail %s/%s: %s", index, len(names), con["name"])
        load_base()
        outage_ok, outage_issue = prepare_outage(con["actions"], con["name"])
        if not outage_ok:
            combined["non_converged"].append({
                "Contingency": con["name"],
                "FirstOutage": "",
                "Issue": outage_issue or "Outage did not solve when reapplied for generator ranking.",
            })
            continue
        piece = analyze_and_maybe_apply(
            con["name"], MITIGATE_N1, "N-1", con["name"]
        )
        _merge(combined, piece)
        if MITIGATE_N1:
            destination = WORK / ("N1_%s_%s.sav" % (index, safe_name(con["name"])))
            ierr = psspy.save(str(destination))
            if ierr == 0:
                combined["notes"].append(
                    "Saved N-1 case %s (mitigation: %s)."
                    % (destination, piece.get("mitigation", ""))
                )
            else:
                combined["notes"].append("Could not save N-1 case for %s." % con["name"])
    if not names:
        combined["notes"].append("N-1 ACCC found no thermal overload at %.1f%%." % THERMAL_LIMIT_PCT)
    combined["saved_case"] = ""
    return combined


def run_n11_per_outage():
    contingencies = read_con(ACTIVE_CON_FILE)
    log.info("N-1-1. %s first outages. Each one is mitigated, then the second level is run.", len(contingencies))
    load_base()
    sub, mon = create_study_mon_sub()
    chosen = contingencies
    if MAX_N11_FIRST_OUTAGES:
        chosen = contingencies[:MAX_N11_FIRST_OUTAGES]
    combined = _empty_result()
    combined["second_level"] = []
    for index, first in enumerate(chosen, start=1):
        log.info("N-1-1 first outage %s/%s: %s", index, len(chosen), first["name"])
        load_base()
        outage_ok, outage_issue = prepare_outage(first["actions"], first["name"])
        if not outage_ok:
            combined["non_converged"].append({
                "Contingency": first["name"],
                "FirstOutage": first["name"],
                "Issue": outage_issue or "First outage did not solve.",
            })
            combined["thermal_violation"].append({
                "First Level Contingency": first["name"],
                "Second Level Contingency": "",
                "Violated Element": "",
                "Branch Key": "",
                "Rating MVA": None,
                COL_EMERGENCY_LONG: None,
                "First Level Loading (%)": convergence_report_value(outage_issue),
                "Mitigated Loading (%)": None,
                "Second Level Loading (%)": None,
            })
            continue
        pre_mitigation = {}
        outage_branches, _outage_rating = read_branches()
        first_level_violations = split_lines(outage_branches)[0]
        for branch in outage_branches:
            pre_mitigation[branch.key] = branch
            reverse = core.branch_key(branch.to_bus, branch.from_bus, branch.ckt)
            pre_mitigation[reverse] = branch
        piece = analyze_and_maybe_apply(
            first["name"], True, "N-1-1", first["name"]
        )
        _merge(combined, piece)
        saved = WORK / ("N11_mitigated_%s_%s.sav" % (index, safe_name(first["name"])))
        ierr = psspy.save(str(saved))
        if ierr != 0:
            combined["notes"].append("Could not save the mitigated case for %s." % first["name"])
            continue
        log.info(
            "First-level case saved to %s (mitigation: %s). Running second outages.",
            saved, piece.get("mitigation", ""),
        )
        seconds = [c for c in contingencies if c["name"] != first["name"]]
        second_con = WORK / ("second_%s_%s.con" % (index, safe_name(first["name"])))
        write_con(seconds, second_con)
        dfx = WORK / ("n11_%s_%s.dfx" % (index, safe_name(first["name"])))
        acc = WORK / ("n11_%s_%s.acc" % (index, safe_name(first["name"])))
        # DFAX must be built on the saved first-level case, after mitigation
        # when mitigation was required and an acceptable move was available.
        ierr = generate_dfax(second_con, dfx, sub, mon)
        if ierr != 0:
            combined["notes"].append("Second-level DFAX failed for %s." % first["name"])
            continue
        ierr = run_accc(acc, dfx)
        if ierr != 0:
            combined["non_converged"].append({
                "Contingency": "",
                "FirstOutage": first["name"],
                "Issue": "Second-level ACCC failed (ierr %s)." % ierr,
            })
            continue
        # Rating lookup has to see branch data. Reload the mitigated case;
        # the ACC file is already written.
        psspy.case(str(saved))
        mitigated = {}
        mitigated_branches, _rating_name = read_branches()
        for branch in mitigated_branches:
            mitigated[branch.key] = branch
            reverse = core.branch_key(branch.to_bus, branch.from_bus, branch.ckt)
            mitigated[reverse] = branch
        ratings, _name = rating_lookup()
        emergency_ratings = branch_rating_lookup(EMERGENCY_RATING)
        # First-level violations are report rows in their own right. Previously
        # this sheet only contained elements returned by the second-level ACCC,
        # which hid first-level violations that did not also appear at level 2.
        for line in first_level_violations:
            branch = line.branch
            key = branch.key
            mitigated_branch = mitigated.get(key)
            combined["thermal_violation"].append({
                "First Level Contingency": first["name"],
                "Second Level Contingency": "",
                "Violated Element": branch.label,
                "Branch Key": "%s-%s-%s" % (
                    branch.from_bus, branch.to_bus, core.normalize_ckt(branch.ckt)
                ),
                "Rating MVA": round(float(branch.rating_mva), 3),
                COL_EMERGENCY_LONG: (
                    round(float(emergency_ratings[key]), 3)
                    if key in emergency_ratings else None
                ),
                "First Level Loading (%)": round(float(core.loading_pct(branch)), 2),
                "Mitigated Loading (%)": (
                    round(float(core.loading_pct(mitigated_branch)), 2)
                    if mitigated_branch is not None else None
                ),
                "Second Level Loading (%)": None,
                "Mitigation": piece.get("mitigation", ""),
            })
        thermal, non_converged = parse_acc_thermal(acc, ratings, first_outage=first["name"])
        for row in thermal:
            row["Study"] = "N-1-1"
            row["MitigatedCase"] = str(saved)
            key = core.branch_key(row["FromBus"], row["ToBus"], row["Ckt"])
            outage_branch = pre_mitigation.get(key)
            mitigated_branch = mitigated.get(key)
            second_name = str(row["Contingency"]).strip()
            if second_name.upper() == "BASE CASE":
                second_name = ""
            combined["thermal_violation"].append({
                "First Level Contingency": first["name"],
                "Second Level Contingency": second_name,
                "Violated Element": row["Element"],
                "Branch Key": "%s-%s-%s" % (
                    row["FromBus"], row["ToBus"], core.normalize_ckt(row["Ckt"])
                ),
                "Rating MVA": row["RatingMVA"],
                COL_EMERGENCY_LONG: (
                    round(float(emergency_ratings[key]), 3)
                    if key in emergency_ratings else None
                ),
                "First Level Loading (%)": (
                    round(float(core.loading_pct(outage_branch)), 2)
                    if outage_branch is not None else None
                ),
                "Mitigated Loading (%)": (
                    round(float(core.loading_pct(mitigated_branch)), 2)
                    if mitigated_branch is not None else None
                ),
                "Second Level Loading (%)": row["LoadingPct"],
                "Mitigation": piece.get("mitigation", ""),
            })
        for failure in non_converged:
            second_name = str(failure.get("Contingency", "")).strip()
            status = convergence_report_value(failure.get("Issue", "Not converged"))
            is_mitigated_base = second_name.upper() == "BASE CASE"
            if is_mitigated_base:
                second_name = ""
            combined["thermal_violation"].append({
                "First Level Contingency": first["name"],
                "Second Level Contingency": second_name,
                "Violated Element": "",
                "Branch Key": "",
                "Rating MVA": None,
                COL_EMERGENCY_LONG: None,
                "First Level Loading (%)": None,
                "Mitigated Loading (%)": status if is_mitigated_base else None,
                "Second Level Loading (%)": None if is_mitigated_base else status,
                "Mitigation": piece.get("mitigation", ""),
            })
        if not thermal and not non_converged and piece.get("mitigation") == "Not required":
            combined["thermal_violation"].append({
                "First Level Contingency": first["name"],
                "Second Level Contingency": "",
                "Violated Element": "No first-level overload. Mitigation was not applied.",
                "Branch Key": "",
                "Rating MVA": None,
                COL_EMERGENCY_LONG: None,
                "First Level Loading (%)": None,
                "Mitigated Loading (%)": None,
                "Second Level Loading (%)": None,
                "Mitigation": "Not required",
            })
        combined["second_level"].extend(thermal)
        combined["non_converged"].extend(non_converged)
        log.info(
            "Second level after %s: %s overload rows, %s non-converged.",
            first["name"], len(thermal), len(non_converged),
        )
    combined["saved_case"] = str(WORK)
    return combined


# ---------------------------------------------------------------------------
# N-1-1 with ONE common dispatch
# ---------------------------------------------------------------------------

class _QuietLog:
    """Hide INFO lines while thousands of power flows run. Warnings still print."""

    def __enter__(self):
        self.level = log.level
        log.setLevel(logging.WARNING)

    def __exit__(self, *_args):
        log.setLevel(self.level)
        return False


def _pack_keys(keys):
    return [[int(k[0]), int(k[1]), str(k[2])] for k in sorted(keys)]


def _unpack_keys(rows):
    return {(int(r[0]), int(r[1]), str(r[2])) for r in rows}


def _pack_loading(loading):
    return [[int(k[0]), int(k[1]), str(k[2]), float(v)] for k, v in loading.items()]


def _unpack_loading(rows):
    return {(int(r[0]), int(r[1]), str(r[2])): float(r[3]) for r in rows}


def _worker_count(item_count, minimum_items):
    workers = int(MAX_WORKERS or 1)
    if IS_WORKER or workers <= 1 or item_count < int(minimum_items) or item_count < 2:
        return 1
    return max(1, min(workers, item_count))


def _kill_tree(proc):
    """Stop a worker. A PSS/E process can stay alive after its script is done."""
    try:
        proc.kill()
    except Exception as exc:
        log.warning("proc.kill failed for worker %s: %s", proc.pid, exc)
    if sys.platform.startswith("win"):
        taskkill = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "taskkill.exe")
        try:
            subprocess.run(
                [taskkill, "/PID", str(proc.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30,
            )
        except Exception as exc:
            log.warning("taskkill failed for worker %s: %s", proc.pid, exc)
    try:
        proc.wait(timeout=10)
        return
    except Exception:
        pass
    if sys.platform.startswith("win"):
        command = (
            "Get-CimInstance Win32_Process -Filter 'ProcessId=%d' | "
            "Invoke-CimMethod -MethodName Terminate | Out-Null" % proc.pid
        )
        try:
            subprocess.run(
                ["powershell", "-NoProfile", "-Command", command],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60,
            )
            proc.wait(timeout=10)
            return
        except Exception:
            pass
    log.warning("Worker %s is still running.", proc.pid)


_WORKER_BOOTSTRAP = (
    "import os, sys, traceback\n"
    "try:\n"
    "    exec(compile(open({script!r}).read(), {script!r}, 'exec'),"
    " {{'__name__': '__main__', '__file__': {script!r}}})\n"
    "except BaseException:\n"
    "    traceback.print_exc()\n"
    "sys.stdout.flush()\n"
    "sys.stderr.flush()\n"
    "os._exit(1)\n"
)


def run_worker_jobs(jobs, tag, timeout_s=6 * 3600):
    """Run each job in its own PSS/E process. Returns the result dicts, or None
    when any worker could not start or did not finish."""
    script = os.path.abspath(__file__)
    commands = "PYTHON\nexec(%r)\n" % _WORKER_BOOTSTRAP.format(script=script)
    started = []
    failed = False
    for k, job in enumerate(jobs):
        stem = str(WORK / ("_job_%s_%s_%s" % (RUN_ID, tag, k)))
        job_path = Path(stem + ".json")
        out_path = Path(stem + "_out.json")
        job["out"] = str(out_path)
        job["progress"] = stem + "_progress.txt"
        if out_path.exists():
            out_path.unlink()
        with open(job_path, "w", encoding="utf-8") as handle:
            json.dump(job, handle)
        try:
            logfile = open(stem + ".log", "w")
            kwargs = {}
            if sys.platform.startswith("win"):
                kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            proc = subprocess.Popen(
                [PSSECMD_EXE, "-buses", str(PSSE_BUSES)],
                stdin=subprocess.PIPE,
                stdout=logfile,
                stderr=subprocess.STDOUT,
                text=True,
                env=dict(os.environ, RUN_INSIDE_PSSECMD="1", CURTAIL_JOB=str(job_path)),
                **kwargs,
            )
            proc.stdin.write(commands)
            proc.stdin.close()
        except OSError as exc:
            log.warning("Could not start worker %s (%s).", k, exc)
            failed = True
            break
        started.append((proc, out_path, logfile, job_path, stem))
    results = [None] * len(started)
    pending = set(range(len(started)))
    deadline = time.time() + timeout_s
    while pending and not failed and time.time() < deadline:
        for k in list(pending):
            proc, out_path = started[k][0], started[k][1]
            if out_path.exists():
                try:
                    with open(out_path, "r", encoding="utf-8") as handle:
                        results[k] = json.load(handle)
                except (OSError, ValueError):
                    failed = True
                pending.discard(k)
            elif proc.poll() is not None:
                if out_path.exists():
                    continue
                log.warning("Worker %s ended without a result. See %s.log", k, started[k][4])
                failed = True
                pending.discard(k)
        time.sleep(0.3)
    if pending:
        failed = True
    for proc, _out, logfile, _job, _stem in started:
        if proc.poll() is None:
            time.sleep(0.5)
        if proc.poll() is None:
            _kill_tree(proc)
        try:
            logfile.close()
        except OSError:
            pass
    for result in results:
        if result is None or "error" in result:
            if result is not None:
                log.warning("Worker error: %s", result["error"])
            failed = True
    if failed:
        return None
    for _proc, out_path, _logfile, job_path, stem in started:
        for path in (
            job_path, out_path, Path(stem + ".log"), Path(stem + ".worker.log"),
            Path(stem + "_progress.txt"),
        ):
            try:
                path.unlink()
            except OSError:
                pass
    return results


def monitored_branch_keys():
    """Canonical keys of every monitored transmission branch, from the loaded case."""
    keys = set()
    for branch in read_branches()[0]:
        keys.add(_canonical_branch(branch.from_bus, branch.to_bus, branch.ckt))
    return keys


def read_loading_map(monitored, keep_all=False):
    """PSS/E percent loading of monitored branches in the solved case.

    By default only lines at or above (limit - 20) are kept, enough to rank
    and compare without holding every line for every contingency. keep_all
    returns every monitored line.
    """
    frm = _abrn_vector("int", "FROMNUMBER", 1)
    to = _abrn_vector("int", "TONUMBER", 1)
    ckt = _abrn_vector("char", "ID", 1)
    percent = None
    for name in _percent_fields_for_rating(RATING):
        percent = _abrn_vector("real", name, 1)
        if percent is not None:
            break
    if frm is None or to is None or ckt is None or percent is None:
        return {}
    floor = float("-inf") if keep_all else float(THERMAL_LIMIT_PCT) - 20.0
    loading = {}
    for fbus, tbus, cid, value in zip(frm, to, ckt, percent):
        key = _canonical_branch(fbus, tbus, cid)
        if key in monitored and float(value) >= floor:
            loading[key] = float(value)
    return loading


def _pct_cell(loading, key):
    """Loading for a report cell. Empty when the line has no value."""
    if key in loading:
        return round(loading[key], 2)
    return None


def violations_of(loading):
    limit = float(THERMAL_LIMIT_PCT)
    return {key: pct for key, pct in loading.items() if pct + 1.0e-6 >= limit}


def _screen_items(items, case_path, monitored):
    """Solve each item ({"name", "actions"}; actions None = base case)."""
    results = {}
    with _QuietLog():
        for item in items:
            name = item["name"]
            ierr = psspy.case(str(case_path))
            if ierr != 0:
                raise RuntimeError("Could not open %s (ierr %s)." % (case_path, ierr))
            if item["actions"] is None:
                ok, code = solve_case(FNSL_STUDY, "Base case")
                issue = "" if ok else solved_meaning(code)
            else:
                ok, issue = prepare_outage(item["actions"], name)
            results[name] = {
                "ok": bool(ok),
                "issue": issue,
                "pct": read_loading_map(monitored) if ok else {},
            }
    return results


def screen_contingencies(contingencies, case_path, monitored, label):
    """Apply BASE CASE and every contingency to case_path and read PSS/E loading."""
    started = time.time()
    items = [{"name": "BASE CASE", "actions": None}] + [
        {"name": con["name"], "actions": con["actions"]} for con in contingencies
    ]
    results = None
    workers = _worker_count(len(items), PARALLEL_MIN_SCREEN)
    if workers > 1:
        jobs = [
            {
                "kind": "screen",
                "case": str(case_path),
                "items": items[k::workers],
                "monitored": _pack_keys(monitored),
            }
            for k in range(workers)
        ]
        done = run_worker_jobs(jobs, "screen")
        if done is None:
            log.warning("%s: parallel run failed. Running in this process.", label)
        else:
            merged = {}
            for part in done:
                for name, entry in part["results"].items():
                    merged[name] = {
                        "ok": entry["ok"],
                        "issue": entry["issue"],
                        "pct": _unpack_loading(entry["pct"]),
                    }
            results = {item["name"]: merged[item["name"]] for item in items if item["name"] in merged}
    if results is None:
        workers = 1
        results = _screen_items(items, case_path, monitored)
    pairs, excess, failed = screen_totals(results)
    log.info(
        "%s: %s contingencies screened in %.0f s with %s process(es). "
        "%s violating lines, %.1f%% total excess, %s not converged.",
        label, len(results), time.time() - started, workers, pairs, excess, failed,
    )
    return results


def screen_totals(results):
    pairs = 0
    excess = 0.0
    failed = 0
    for entry in results.values():
        if not entry["ok"]:
            failed += 1
            continue
        for pct in violations_of(entry["pct"]).values():
            pairs += 1
            excess += pct - float(THERMAL_LIMIT_PCT)
    return pairs, round(excess, 3), failed


def screen_score(results):
    pairs, excess, failed = screen_totals(results)
    return (failed, pairs, excess)


def rank_violating_contingencies(results):
    """Contingency names, most violations first, then the highest loading."""
    ranked = []
    for name, entry in results.items():
        if name == "BASE CASE" or not entry["ok"]:
            continue
        found = violations_of(entry["pct"])
        if found:
            ranked.append((len(found), max(found.values()), name))
    ranked.sort(reverse=True)
    return [name for _count, _worst, name in ranked]


_DISPATCH_REJECT = ""


def apply_dispatch_deltas(deltas, maintain, sink, targets):
    """Add MW changes to the machines of the loaded case, then hold the swing.

    Same rule as CurtailOverload_v2.py. The total P of the swing machines is
    held within SWING_TOL_MW. PSS/E may share that total across the machines
    on the swing bus.
    """
    global _DISPATCH_REJECT
    _DISPATCH_REJECT = ""
    live = {gen.key: gen for gen in read_generators([])}
    for key, delta in deltas.items():
        gen = live.get(key)
        if gen is None or int(gen.status) != 1:
            continue
        p_new = core.clip(gen.p_mw + delta, gen.pmin_mw, gen.pmax_mw)
        q_new = q_for_new_p(gen.p_mw, gen.q_mvar, p_new, gen.qmin_mvar, gen.qmax_mvar)
        set_machine_p(gen.bus, gen.machine_id, p_new, q_new)
    ok, _code = solve_case(FNSL_CURTAIL, "Common dispatch")
    if not ok:
        _DISPATCH_REJECT = "power flow did not solve with this step"
        return False
    protected = {key for key, delta in deltas.items() if delta < -0.05}
    correct_swing(maintain, sink, targets, protected)
    ok, _code = solve_case(FNSL_CURTAIL, "Common dispatch after swing correction")
    if not ok:
        _DISPATCH_REJECT = "power flow did not solve after holding the swing"
    return ok


def build_common_candidate(cons, base_path, work_path, maintain, sink, targets):
    """Mitigate each contingency in `cons` one after the other and carry the
    generator changes onto the base case. The result is saved to work_path.

    Returns (changed, notes).
    """
    maintain_set = set(int(b) for b in maintain)
    notes = []
    changed = False
    delta_list = []
    if psspy.case(str(base_path)) != 0 or psspy.save(str(work_path)) != 0:
        return False, ["Could not copy the common base case."], []
    for con in cons:
        psspy.case(str(work_path))
        outage_ok, issue = prepare_outage(con["actions"], con["name"])
        if not outage_ok:
            notes.append("%s: outage did not solve (%s)." % (con["name"], issue))
            continue
        before = {gen.key: gen for gen in read_generators([])}
        piece = analyze_and_maybe_apply(con["name"], True, "N-1-1", con["name"])
        after = {gen.key: gen for gen in read_generators([])}
        deltas = {}
        for key, old in before.items():
            new = after.get(key)
            if new is None or int(old.status) != 1 or int(new.status) != 1:
                continue
            if int(old.bus) in maintain_set:
                continue
            delta = float(new.p_mw) - float(old.p_mw)
            if abs(delta) >= 0.05:
                deltas[key] = delta
        notes.append(
            "%s: own mitigation %s, %s machines moved, cleared by itself = %s."
            % (con["name"], piece.get("mitigation", ""), len(deltas), piece.get("cleared"))
        )
        if not deltas:
            continue
        psspy.case(str(work_path))
        if not apply_dispatch_deltas(deltas, maintain, sink, targets):
            notes.append("%s: the base case did not solve with these changes. Skipped." % con["name"])
            continue
        if psspy.save(str(work_path)) == 0:
            changed = True
            delta_list.append(deltas)
    return changed, notes, delta_list


class _TargetOverride:
    """Temporarily change TARGET_LOADING_PCT (used by the per-contingency method)."""

    def __init__(self, value):
        self.value = float(value)

    def __enter__(self):
        self.old = globals()["TARGET_LOADING_PCT"]
        globals()["TARGET_LOADING_PCT"] = self.value

    def __exit__(self, *_args):
        globals()["TARGET_LOADING_PCT"] = self.old
        return False


def _entry_over(entry, tol):
    limit = float(THERMAL_LIMIT_PCT) + float(tol)
    return sum(max(0.0, pct - limit) for pct in entry["pct"].values())


def score_v2(results, tol):
    """(non-converged count, overload above limit + tol, overload above limit)."""
    failed = 0
    tolerant = 0.0
    full = 0.0
    for entry in results.values():
        if not entry["ok"]:
            failed += 1
            continue
        tolerant += _entry_over(entry, tol)
        full += _entry_over(entry, 0.0)
    return (failed, round(tolerant, 3), round(full, 3))


def score_better(new, old):
    if new[0] != old[0]:
        return new[0] < old[0]
    return new[1] <= old[1] - max(0.05, 0.01 * old[1])


def rank_bad(results, tol, skip):
    """Contingencies with overload above limit + tol, the largest first."""
    ranked = []
    for name, entry in results.items():
        if name == "BASE CASE" or name in skip or not entry["ok"]:
            continue
        over = _entry_over(entry, tol)
        if over > 0.0:
            ranked.append((over, name))
    ranked.sort(reverse=True)
    return [name for _over, name in ranked]


def count_violating(results):
    return sum(len(violations_of(e["pct"])) for e in results.values() if e["ok"])


def describe_change(new, old, tol, limit_items=3):
    """Why a candidate dispatch is worse: new non-converged cases and new violations."""
    parts = []
    newly_failed = [
        name for name, entry in new.items()
        if not entry["ok"] and old.get(name, {"ok": True})["ok"]
    ]
    if newly_failed:
        parts.append("new non-converged: " + ", ".join(newly_failed[:limit_items]))
    high = float(THERMAL_LIMIT_PCT) + float(tol)
    appeared = []
    for name, entry in new.items():
        if not entry["ok"]:
            continue
        previous = old.get(name, {"ok": True, "pct": {}})
        before = previous["pct"] if previous["ok"] else {}
        for key, pct in entry["pct"].items():
            if pct > high and before.get(key, 0.0) <= high:
                appeared.append((pct, name, key))
    appeared.sort(reverse=True)
    if appeared:
        text_items = "; ".join(
            "%s %s-%s-%s %.1f%%" % (name, key[0], key[1], key[2], pct)
            for pct, name, key in appeared[:limit_items]
        )
        more = len(appeared) - limit_items
        parts.append("new violations: " + text_items + (" (+%d more)" % more if more > 0 else ""))
    return "; ".join(parts)


def apply_delta_list(delta_list, base_path, out_path, maintain, sink, targets):
    """Apply MW changes (one dict after the other) to base_path and save the result."""
    if psspy.case(str(base_path)) != 0:
        return False
    for deltas in delta_list:
        if not deltas:
            continue
        if not apply_dispatch_deltas(deltas, maintain, sink, targets):
            return False
    return psspy.save(str(out_path)) == 0


def _scenario_names(screen, targeted):
    """BASE CASE, the targeted contingencies, and the heaviest others (protected)."""
    names = ["BASE CASE"] + list(targeted)
    extra = []
    for name, entry in screen.items():
        if name == "BASE CASE" or name in targeted or not entry["ok"]:
            continue
        worst = max(entry["pct"].values(), default=0.0)
        if worst >= float(COMMON_WATCH_PCT):
            extra.append((worst, name))
    extra.sort(reverse=True)
    names += [name for _worst, name in extra[: int(COMMON_COUPLED_PROTECT)]]
    return names


def collect_scenarios(names, by_name, case_path, base_gens, maintain, sink, also=None):
    """For each contingency: the heavy lines and their shift factors, on case_path."""
    scenarios = {}
    sources = source_buses_for(base_gens, maintain)
    for index, name in enumerate(names):
        con = None if name == "BASE CASE" else by_name[name]
        psspy.case(str(case_path))
        with _QuietLog():
            if con is None:
                ok, _code = solve_case(FNSL_STUDY, "Base case")
            else:
                ok, _issue = prepare_outage(con["actions"], name)
        if not ok:
            scenarios[name] = {"ok": False, "idx": index, "lines": [], "sf": {}}
            continue
        picked = []
        wanted = also.get(name, ()) if also else ()
        for branch in read_branches()[0]:
            pct = core.loading_pct(branch)
            key = _canonical_branch(branch.from_bus, branch.to_bus, branch.ckt)
            if pct >= float(COMMON_WATCH_PCT) or key in wanted:
                picked.append((branch, pct))
        sf = {}
        if picked:
            lines = [
                core.ConstrainedLine(branch, float(THERMAL_LIMIT_PCT), pct >= float(THERMAL_LIMIT_PCT))
                for branch, pct in picked
            ]
            with _QuietLog():
                sf, _note = compute_shift_factors(
                    lines, sources, sink, safe_name("cpl_%s_%s" % (index, name))
                )
        scenarios[name] = {"ok": True, "idx": index, "lines": picked, "sf": sf}
    return scenarios


def build_coupled_plan(scenarios, targeted, tol, base_gens, maintain, sink):
    """One linear plan over every scenario. Lines are told apart by a tag on the
    circuit id. Targeted violations are planned down; every other heavy line is
    held where it is allowed to be (never worse than now)."""
    high = float(THERMAL_LIMIT_PCT) + float(tol)
    violated = []
    constrained = []
    sf_table = {}
    for name, scenario in scenarios.items():
        if not scenario["ok"]:
            continue
        tag = "@%d" % scenario["idx"]
        for branch, pct in scenario["lines"]:
            tagged = dataclasses.replace(branch, ckt="%s%s" % (core.normalize_ckt(branch.ckt), tag))
            sf_table[tagged.key] = dict(scenario["sf"].get(branch.key, {}))
            if name in targeted and pct >= high:
                line = core.ConstrainedLine(
                    tagged, min(float(COMMON_TARGET_PCT), high), True
                )
                violated.append(line)
            else:
                line = core.ConstrainedLine(tagged, max(high, pct), False)
            constrained.append(line)
    if not violated:
        return None
    swing = swing_limits_from(base_gens, sink)
    return core.build_joint_plan(
        violated,
        constrained,
        sf_table,
        base_gens,
        swing,
        swing_buses=maintain,
        exclude_buses=EXCLUDE_GEN_BUSES,
        curtail_areas=curtail_areas(),
        balance_areas=BALANCE_AREAS,
        top_n=TOP_N_GENS,
        min_effectiveness=MIN_EFFECTIVENESS,
        min_relief_mw=MIN_RELIEF_MW,
        max_neutral=MAX_NEUTRAL_GENS,
    )


def propose_deltas(targeted, scenarios, tol, current_case, by_name, maintain, sink, targets, base_gens):
    """MW changes (a list of dicts) aimed at the targeted contingencies."""
    if scenarios is not None:
        plan = build_coupled_plan(scenarios, set(targeted), tol, base_gens, maintain, sink)
        if plan is None:
            return [], ["no targeted line is above the limit"]
        deltas = {move.key: float(move.delta_mw) for move in plan.moves}
        note = "plan: %s Curtail %.1f MW, pickup %.1f MW, %d machines." % (
            plan.message, plan.curtail_mw, plan.pickup_mw, len(deltas)
        )
        return ([deltas] if deltas else []), [note]
    work = WORK / "_n11_work.sav"
    with _TargetOverride(min(float(COMMON_TARGET_PCT), float(THERMAL_LIMIT_PCT))):
        _changed, notes, delta_list = build_common_candidate(
            [by_name[name] for name in targeted],
            current_case, work, maintain, sink, targets,
        )
    return delta_list, notes


def run_common_rounds(contingencies, by_name, initial_screen, current_case, candidate_case,
                      monitored, maintain, sink, targets):
    """Choose one dispatch for all contingencies. Returns the final screen, a
    history table, a stop reason, and the contingencies that could not be fixed."""
    history = []
    screen = initial_screen
    trial_case = WORK / "_n11_trial.sav"
    tolerances = [float(MARGINAL_PCT), 0.0] if float(MARGINAL_PCT) > 0.0 else [0.0]
    fractions = [float(f) for f in COMMON_STEP_FRACTIONS if float(f) > 0.0] or [1.0]
    reasons = []
    failed_targets = set()
    for phase, tol in enumerate(tolerances, start=1):
        failed_targets = set()
        log.info(
            "Phase %s: contingencies with a line above %.1f%% loading are targeted.",
            phase, float(THERMAL_LIMIT_PCT) + tol,
        )
        for round_no in range(1, int(COMMON_MAX_ROUNDS) + 1):
            bad = rank_bad(screen, tol, failed_targets)
            if not bad:
                reasons.append("phase %d finished (nothing left above %.1f%%)" % (
                    phase, float(THERMAL_LIMIT_PCT) + tol))
                break
            old_score = score_v2(screen, tol)
            psspy.case(str(current_case))
            base_gens = read_generators([])
            log.info(
                "Phase %s round %s: %s contingencies above %.1f%%. Worst: %s",
                phase, round_no, len(bad), float(THERMAL_LIMIT_PCT) + tol,
                ", ".join(bad[:5]),
            )
            scenarios = None
            attempts = []
            if COMMON_COUPLING:
                top = bad[: int(COMMON_COUPLED_TARGETS)]
                names = _scenario_names(screen, top)
                started = time.time()
                scenarios = collect_scenarios(names, by_name, current_case, base_gens, maintain, sink)
                log.info("  %s contingencies prepared for the plan in %.0f s.", len(names), time.time() - started)
                attempts.append(("coupled plan, %d targeted" % len(top), top))
                if len(top) > 1:
                    for name in bad[: max(1, int(COMMON_WORST_PER_ROUND))]:
                        attempts.append(("single: %s" % name, [name]))
            else:
                top = bad[: max(1, int(COMMON_WORST_PER_ROUND))]
                attempts.append(("together: %s" % ", ".join(top), top))
                if len(top) > 1:
                    for name in top:
                        attempts.append(("single: %s" % name, [name]))
            accepted = False
            tested_single = set()
            for label, targeted in attempts:
                delta_list, notes = propose_deltas(
                    targeted, scenarios, tol, current_case, by_name,
                    maintain, sink, targets, base_gens,
                )
                for note in notes:
                    log.info("  %s: %s", label, note)
                if label.startswith("single"):
                    tested_single.add(targeted[0])
                base_row = {
                    "Phase": phase, "Round": round_no, "Attempt": label,
                    "Targeted": "; ".join(targeted[:8]) + (" ..." if len(targeted) > 8 else ""),
                    "Step": None,
                    "Excess Before (%)": old_score[1], "Excess After (%)": None,
                    "Violating Lines Before": count_violating(screen),
                    "Violating Lines After": None,
                    "Not Converged Before": old_score[0], "Not Converged After": None,
                    "Result": "", "Reason": "",
                }
                if not delta_list:
                    row = dict(base_row)
                    row["Result"] = "No generator move"
                    row["Reason"] = "; ".join(str(n) for n in notes)[:300]
                    history.append(row)
                    continue
                best_row = None
                best = None
                rows = []
                for fraction in fractions:
                    row = dict(base_row)
                    row["Step"] = fraction
                    scaled = [{k: v * fraction for k, v in d.items()} for d in delta_list]
                    if not apply_delta_list(scaled, current_case, trial_case, maintain, sink, targets):
                        row["Result"] = "Rejected"
                        row["Reason"] = _DISPATCH_REJECT or "power flow did not solve with this step"
                        rows.append(row)
                        continue
                    candidate = screen_contingencies(
                        contingencies, trial_case, monitored,
                        "Phase %s round %s %s x%.2f" % (phase, round_no, label[:40], fraction),
                    )
                    score = score_v2(candidate, tol)
                    row["Excess After (%)"] = score[1]
                    row["Violating Lines After"] = count_violating(candidate)
                    row["Not Converged After"] = score[0]
                    if score_better(score, old_score):
                        row["Result"] = "Improves"
                        if best is None or score < best[0]:
                            shutil.copyfile(trial_case, candidate_case)
                            best = (score, candidate)
                            best_row = row
                    else:
                        row["Result"] = "Rejected"
                        row["Reason"] = describe_change(candidate, screen, tol)
                        log.info("    x%.2f rejected. %s", fraction, row["Reason"])
                    rows.append(row)
                    if score[0] <= old_score[0] and score[1] <= 0.0:
                        break
                for row in rows:
                    if row["Result"] == "Improves":
                        row["Result"] = "Accepted" if row is best_row else "Improves (a better step was used)"
                history.extend(rows)
                if best is not None:
                    shutil.copyfile(candidate_case, current_case)
                    screen = best[1]
                    accepted = True
                    log.info(
                        "  %s accepted at x%.2f. Overload above %.1f%%: %.1f -> %.1f.",
                        label, best_row["Step"], float(THERMAL_LIMIT_PCT) + tol,
                        old_score[1], best[0][1],
                    )
                    break
            if accepted:
                failed_targets.clear()
                continue
            failed_targets.update(tested_single if tested_single else top)
            log.info("  No step improved the total. Skipped for now: %s", ", ".join(sorted(failed_targets)))
        else:
            reasons.append("phase %d reached the round limit" % phase)
    unfixable = {}
    for name in failed_targets:
        entry = screen.get(name)
        if entry and entry["ok"]:
            found = violations_of(entry["pct"])
            if found:
                unfixable[name] = max(found.values())
    stop_reason = "; ".join(reasons)
    if unfixable:
        stop_reason += ". Not fixable with the allowed generators: " + ", ".join(sorted(unfixable))
    screen, base_note = clear_base_case(
        contingencies, by_name, initial_screen, screen,
        current_case, candidate_case, monitored, maintain, sink, targets, history,
    )
    if base_note:
        stop_reason += ". " + base_note
    return {
        "screen": screen, "history": history,
        "stop_reason": stop_reason, "unfixable": unfixable,
    }


def _mitigated_keys(initial_screen, screen):
    """Lines a contingency drove under the limit. The base-case cleanup must leave them there."""
    limit = float(THERMAL_LIMIT_PCT)
    found = {}
    for name, before in initial_screen.items():
        if name == "BASE CASE" or not before.get("ok"):
            continue
        after = screen.get(name)
        if after is None or not after.get("ok"):
            continue
        for key, pct in before["pct"].items():
            if pct >= limit and after["pct"].get(key, 0.0) < limit:
                found.setdefault(name, set()).add(key)
    return found


def _mitigation_regressed(candidate, protected):
    """Mitigated lines that are back at or above the limit."""
    limit = float(THERMAL_LIMIT_PCT)
    lost = []
    for name, keys in protected.items():
        entry = candidate.get(name)
        if entry is None or not entry.get("ok"):
            lost.append("%s did not solve" % name)
            continue
        for key in keys:
            pct = entry["pct"].get(key, 0.0)
            if pct >= limit:
                lost.append("%s %s-%s-%s %.1f%%" % (name, key[0], key[1], key[2], pct))
    return lost


def clear_base_case(contingencies, by_name, initial_screen, screen, current_case,
                     candidate_case, monitored, maintain, sink, targets, history):
    """Remove overloads from the case with no contingency applied.

    A step is kept only when the base case is less overloaded and every line
    already brought under the limit by the N-1 dispatch stays under the limit.
    """
    base = screen.get("BASE CASE")
    if base is None or not base.get("ok"):
        return screen, "Base case did not solve, so it was left unchanged."
    if not violations_of(base["pct"]):
        log.info("Base case has no line at or above %.1f%%.", THERMAL_LIMIT_PCT)
        return screen, "Base case has no overload."
    protected = _mitigated_keys(initial_screen, screen)
    names = ["BASE CASE"] + sorted(protected)
    psspy.case(str(current_case))
    base_gens = read_generators([])
    started = time.time()
    scenarios = collect_scenarios(
        names, by_name, current_case, base_gens, maintain, sink, also=protected,
    )
    log.info(
        "Base-case cleanup: %s protected contingencies prepared in %.0f s.",
        len(protected), time.time() - started,
    )
    delta_list, notes = propose_deltas(
        ["BASE CASE"], scenarios, 0.0, current_case, by_name,
        maintain, sink, targets, base_gens,
    )
    for note in notes:
        log.info("  base case: %s", note)
    before = _entry_over(base, 0.0)
    fractions = [float(f) for f in COMMON_STEP_FRACTIONS if float(f) > 0.0] or [1.0]
    trial_case = WORK / "_n11_base_trial.sav"
    best = None
    if not delta_list:
        note = "Base case still has an overload. No generator move could reduce it without a plan."
        log.info(note)
        return screen, note
    for fraction in fractions:
        scaled = [{key: value * fraction for key, value in deltas.items()} for deltas in delta_list]
        if not apply_delta_list(scaled, current_case, trial_case, maintain, sink, targets):
            reason = _DISPATCH_REJECT or "power flow did not solve"
            log.info("  base case x%.2f rejected. %s", fraction, reason)
            history.append({
                "Phase": "Base", "Round": 1, "Attempt": "base case cleanup",
                "Targeted": "BASE CASE", "Step": fraction,
                "Excess Before (%)": round(before, 3), "Excess After (%)": None,
                "Violating Lines Before": len(violations_of(base["pct"])),
                "Violating Lines After": None,
                "Not Converged Before": 0, "Not Converged After": None,
                "Result": "Rejected", "Reason": reason,
            })
            continue
        candidate = screen_contingencies(
            contingencies, trial_case, monitored, "Base case cleanup x%.2f" % fraction,
        )
        entry = candidate.get("BASE CASE", {"ok": False, "pct": {}})
        after = _entry_over(entry, 0.0) if entry.get("ok") else before
        lost = _mitigation_regressed(candidate, protected)
        row = {
            "Phase": "Base", "Round": 1, "Attempt": "base case cleanup",
            "Targeted": "BASE CASE", "Step": fraction,
            "Excess Before (%)": round(before, 3), "Excess After (%)": round(after, 3),
            "Violating Lines Before": len(violations_of(base["pct"])),
            "Violating Lines After": len(violations_of(entry["pct"])) if entry.get("ok") else None,
            "Not Converged Before": 0,
            "Not Converged After": 0 if entry.get("ok") else 1,
            "Result": "Rejected", "Reason": "",
        }
        if lost:
            row["Reason"] = "would re-overload a mitigated line: " + "; ".join(lost[:3])
        elif not entry.get("ok") or after > before - 0.05:
            row["Reason"] = "base-case overload did not fall"
        else:
            row["Result"] = "Improves"
            if best is None or after < best[0]:
                shutil.copyfile(trial_case, candidate_case)
                best = (after, candidate, row)
        if row["Result"] == "Rejected":
            log.info("  base case x%.2f rejected. %s", fraction, row["Reason"])
        history.append(row)
    if best is None:
        note = (
            "Base case still has an overload. No cleanup was applied, "
            "because each one either did not solve or re-overloaded a line the N-1 dispatch had cleared."
        )
        log.info(note)
        return screen, note
    shutil.copyfile(candidate_case, current_case)
    best[2]["Result"] = "Accepted"
    left = violations_of(best[1]["BASE CASE"]["pct"])
    log.info(
        "Base-case cleanup accepted at x%.2f. Base overload %.1f -> %.1f.",
        best[2]["Step"], before, best[0],
    )
    if left:
        return best[1], "Base case overload reduced from %.1f to %.1f. %s lines remain." % (
            before, best[0], len(left)
        )
    return best[1], "Base case has no overload after the cleanup."


def _n1_style_report_rows(contingencies, initial_case, final_case, initial_screen, screen, study_label):
    """N-0 style rows for every contingency that violates before or after the dispatch."""
    thermal_rows = []
    free_rows = []

    def _state(case_path, con):
        psspy.case(str(case_path))
        if con is None:
            ok, _code = solve_case(FNSL_STUDY, "Base case")
        else:
            ok, _issue = prepare_outage(con["actions"], con["name"])
        if not ok:
            return {}
        return {
            _canonical_branch(b.from_bus, b.to_bus, b.ckt): b for b in read_branches()[0]
        }

    with _QuietLog():
        for con in [None] + list(contingencies):
            name = "BASE CASE" if con is None else con["name"]
            before_entry = initial_screen.get(name)
            after_entry = screen.get(name)
            if before_entry is None or after_entry is None:
                continue
            before_v = violations_of(before_entry["pct"]) if before_entry["ok"] else {}
            after_v = violations_of(after_entry["pct"]) if after_entry["ok"] else {}
            keys = sorted(set(before_v) | set(after_v), key=lambda k: -max(
                before_entry["pct"].get(k, 0.0), after_entry["pct"].get(k, 0.0)
            ))
            if not keys:
                continue
            before_branches = _state(initial_case, con)
            after_branches = _state(final_case, con)
            for key in keys:
                before_branch = before_branches.get(key)
                after_branch = after_branches.get(key)
                if before_branch is None:
                    continue
                thermal = n0_thermal_row(before_branch)
                thermal["Study"] = study_label
                thermal["Contingency"] = name
                if key in before_v:
                    thermal_rows.append(thermal)
                free = n0_free_case_row(before_branch, after_branch)
                free["Study"] = study_label
                free["Contingency"] = name
                free_rows.append(free)
    return thermal_rows, free_rows


def _same_outage(first, second):
    if str(first["name"]) == str(second["name"]):
        return True
    outage = single_line_outage_of(first)
    return outage is not None and outage == single_line_outage_of(second)


def _seconds_for(first, seconds_all):
    return [con for con in seconds_all if not _same_outage(first, con)]


def second_level_raw(task, seconds_all, final_case, monitored, explicit, initial_case):
    """Second level for one first outage. Returns plain data (JSON-friendly).

    Runs in this process, or inside a worker PSS/E process. The result holds
    the loading of every line that appears in the report, with the starting
    dispatch ("first_before") and with the common dispatch ("first_loading").
    """
    state = {}
    out = _second_level_core(
        task, seconds_all, final_case, monitored, explicit, initial_case, state
    )
    first_loading = state.get("first_loading", {})
    first_before = state.get("first_before", {})
    needed = set(violations_of(first_loading))
    for pair in out["pairs"]:
        needed.update(_unpack_loading(pair["viol"]))
    out["first_loading"] = _pack_loading({k: v for k, v in first_loading.items() if k in needed})
    out["first_before"] = _pack_loading({k: v for k, v in first_before.items() if k in needed})
    return out


def _second_level_core(task, seconds_all, final_case, monitored, explicit, initial_case, state):
    first = task["first"]
    index = task["index"]
    name = first["name"]
    seconds = _seconds_for(first, seconds_all)
    out = {
        "index": index, "first": name, "first_ok": True, "issue": "",
        "first_loading": [], "pairs": [], "failed": [], "nonconv": [],
        "notes": [], "flagged": 0,
    }
    log.info("Second level %s on first outage %s (%s second outages)", index, name, len(seconds))
    with _QuietLog():
        psspy.case(str(final_case))
        outage_ok, issue = prepare_outage(first["actions"], name)
    if not outage_ok:
        out["first_ok"] = False
        out["issue"] = issue
        return out
    state["first_loading"] = read_loading_map(monitored, keep_all=True)
    first_case = WORK / ("N11_first_%s_%s.sav" % (index, safe_name(name)))
    if psspy.save(str(first_case)) != 0:
        out["notes"].append("Could not save the first-outage case for %s." % name)
        return out
    # The same first outage with the starting dispatch, for the First Level column.
    with _QuietLog():
        psspy.case(str(initial_case))
        before_ok, _before_issue = prepare_outage(first["actions"], name)
        if before_ok:
            state["first_before"] = read_loading_map(monitored, keep_all=True)
        psspy.case(str(first_case))
    if not seconds:
        return out
    limit = float(THERMAL_LIMIT_PCT)
    acc_failed = []
    if explicit:
        # Small studies: solve every pair. No screening is involved.
        flagged = [con["name"] for con in seconds]
    else:
        sub, mon = create_study_mon_sub("_%s" % index)
        second_con = WORK / ("second_%s_%s.con" % (index, safe_name(name)))
        write_con(seconds, second_con)
        dfx = WORK / ("n11_%s_%s.dfx" % (index, safe_name(name)))
        acc = WORK / ("n11_%s_%s.acc" % (index, safe_name(name)))
        if generate_dfax(second_con, dfx, sub, mon) != 0:
            out["notes"].append("Second-level DFAX failed for %s." % name)
            return out
        ierr = run_accc(acc, dfx)
        if ierr != 0:
            out["nonconv"].append({
                "Contingency": "",
                "Issue": "Second-level ACCC failed (ierr %s)." % ierr,
            })
            return out
        psspy.case(str(first_case))
        ratings, _name = rating_lookup()
        flagged_rows, acc_failed = parse_acc_thermal(
            acc, ratings, first_outage=name,
            limit_pct=limit - float(SECOND_LEVEL_SCREEN_MARGIN_PCT),
        )
        flagged = []
        for row in flagged_rows:
            second_name = str(row["Contingency"]).strip()
            if second_name.upper() != "BASE CASE" and second_name not in flagged:
                flagged.append(second_name)
    for failure in acc_failed:
        second_name = str(failure.get("Contingency", "")).strip()
        if second_name.upper() == "BASE CASE":
            continue
        out["failed"].append([second_name, str(failure.get("Issue", "Not converged"))])
    # The ACCC percent is only a screen. Solve every flagged pair and use the
    # PSS/E percent loading.
    found = 0
    with _QuietLog():
        for second_name in flagged:
            second = find_contingency(seconds, second_name)
            if second is None:
                continue
            psspy.case(str(first_case))
            second_ok, second_issue = prepare_outage(second["actions"], second_name)
            if not second_ok:
                out["failed"].append([second_name, str(second_issue)])
                continue
            violating = violations_of(read_loading_map(monitored))
            if violating:
                found += len(violating)
                out["pairs"].append({"second": second_name, "viol": _pack_loading(violating)})
    out["flagged"] = len(flagged)
    log.info(
        "Second level after %s: %s outages solved, %s violating lines confirmed.",
        name, len(flagged), found,
    )
    return out


def run_n11_common(study_label="N-1-1", second_level=True):
    contingencies = read_con(ACTIVE_CON_FILE)
    if MAX_N11_FIRST_OUTAGES:
        contingencies = contingencies[:MAX_N11_FIRST_OUTAGES]
    by_name = {con["name"]: con for con in contingencies}
    log.info(
        "%s (common dispatch). %s outages. Up to %s rounds, %s worst per round.",
        study_label, len(contingencies), COMMON_MAX_ROUNDS, COMMON_WORST_PER_ROUND,
    )
    combined = _empty_result()
    combined["second_level"] = []
    ok, code = load_base()
    if not ok:
        raise RuntimeError("The base case did not solve (%s)." % solved_meaning(code))
    gens_initial_list = read_generators([])
    gens_initial = {gen.key: gen for gen in gens_initial_list}
    maintain, sink, _swing_note = classify_swing(gens_initial_list)
    targets = swing_targets(gens_initial_list, maintain)
    monitored = monitored_branch_keys()
    branch_info = {}
    for branch in read_branches()[0]:
        branch_info[_canonical_branch(branch.from_bus, branch.to_bus, branch.ckt)] = branch
    emergency = branch_rating_lookup(EMERGENCY_RATING)
    log.info("%s monitored transmission branches.", len(monitored))

    initial_case = WORK / "_n11_initial.sav"
    current_case = WORK / "_n11_current.sav"
    candidate_case = WORK / "_n11_candidate.sav"
    if psspy.save(str(initial_case)) != 0:
        raise RuntimeError("Could not save the starting case.")
    shutil.copyfile(initial_case, current_case)

    # ---------------- Part 1: one dispatch that is free of first-level violations
    initial_screen = screen_contingencies(contingencies, initial_case, monitored, "Initial screen")
    start_pairs, start_excess, start_failed = screen_totals(initial_screen)
    engine = run_common_rounds(
        contingencies, by_name, initial_screen, current_case, candidate_case,
        monitored, maintain, sink, targets,
    )
    screen = engine["screen"]
    history = engine["history"]
    stop_reason = engine["stop_reason"]
    unfixable = engine["unfixable"]
    combined["round_history"] = history

    end_pairs, end_excess, end_failed = screen_totals(screen)
    log.info(
        "Common dispatch: violating lines %s -> %s, total excess %.1f -> %.1f. %s",
        start_pairs, end_pairs, start_excess, end_excess, stop_reason,
    )

    # Save the common case and list the generators that moved.
    destination = Path(COMMON_SAVE_CASE) if COMMON_SAVE_CASE else WORK / "N1_free_common.sav"
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(current_case, destination)
    final_case = destination
    psspy.case(str(final_case))
    final_gens = {gen.key: gen for gen in read_generators([])}
    for key, old in gens_initial.items():
        new = final_gens.get(key)
        if new is None or abs(new.p_mw - old.p_mw) < 0.05:
            continue
        combined["applied_rows"].append({
            "Label": "COMMON N-1 DISPATCH",
            "Study": study_label,
            "Contingency": "COMMON DISPATCH",
            "Bus": old.bus,
            "MachineID": old.machine_id,
            "Name": old.name,
            "Area": old.area,
            "MPID": old.mpid,
            "FacilityName": old.facility_name,
            "Technology": old.technology,
            "Region": old.region,
            "PBeforeMW": round(old.p_mw, 3),
            "PAfterMW": round(new.p_mw, 3),
            "DeltaPMW": round(new.p_mw - old.p_mw, 3),
            "PminMW": round(old.pmin_mw, 3),
            "PmaxMW": round(old.pmax_mw, 3),
            "QBeforeMVAR": round(old.q_mvar, 3),
            "QAfterMVAR": round(new.q_mvar, 3),
            "InsideRange": "Yes" if old.pmin_mw - 0.1 <= new.p_mw <= old.pmax_mw + 0.1 else "No",
        })
    combined["curtail_mw"] = sum(
        -row["DeltaPMW"] for row in combined["applied_rows"] if row["DeltaPMW"] < 0
    )

    # First-level before / after comparison, one row per violating pair.
    def _branch_fields(key):
        branch = branch_info.get(key)
        if branch is None:
            return {
                "BranchName": "",
                "LineViolation": "%s-%s-%s" % key,
                COL_RATING_LONG: None,
                COL_EMERGENCY_LONG: emergency.get(key),
            }
        return {
            "BranchName": branch.name,
            "LineViolation": n0_line_description(branch),
            COL_RATING_LONG: round(float(branch.rating_mva), 3),
            COL_EMERGENCY_LONG: (
                round(float(emergency[key]), 3)
                if key in emergency and float(emergency[key]) > core.NEAR_ZERO else None
            ),
        }

    screening_rows = []
    remaining_text = []
    for name in ["BASE CASE"] + [con["name"] for con in contingencies]:
        before_entry = initial_screen.get(name, {"ok": True, "pct": {}})
        after_entry = screen.get(name, {"ok": True, "pct": {}})
        before_v = violations_of(before_entry["pct"]) if before_entry["ok"] else {}
        after_v = violations_of(after_entry["pct"]) if after_entry["ok"] else {}
        for key in sorted(set(before_v) | set(after_v)):
            fields = {"First Level Contingency": name}
            fields.update(_branch_fields(key))
            fields["Loading Before Dispatch (%)"] = _pct_cell(before_entry["pct"], key)
            fields["Loading After Common Dispatch (%)"] = _pct_cell(after_entry["pct"], key)
            if key in after_v and key in before_v:
                fields["Status"] = "Still violating"
            elif key in after_v:
                fields["Status"] = "New violation"
            else:
                fields["Status"] = "Cleared"
            if key in after_v:
                remaining_text.append("%s on %s (%.1f%%)" % (name, fields["LineViolation"], after_v[key]))
            screening_rows.append(fields)
    for name, entry in screen.items():
        if not entry["ok"]:
            combined["non_converged"].append({
                "Contingency": name,
                "FirstOutage": "",
                "Issue": "After common dispatch: %s" % entry["issue"],
            })
    combined["n11"] = {"screening": screening_rows, "history": history}
    combined["notes"].append(
        "Common N-1 dispatch: violating lines %s -> %s, total excess %.1f%% -> %.1f%%, "
        "not converged %s -> %s. %s"
        % (start_pairs, end_pairs, start_excess, end_excess, start_failed, end_failed, stop_reason)
    )
    combined["notes"].append("Common N-1 case saved to %s." % final_case)
    for name, worst in sorted(unfixable.items()):
        combined["notes"].append(
            "NOT FIXABLE with the allowed generators: %s (worst line %.1f%%)." % (name, worst)
        )
    if remaining_text:
        combined["notes"].append(
            "Still violating after the common dispatch: " + "; ".join(remaining_text)
        )
    combined["remaining_rows"] = [
        {"Contingency": row["First Level Contingency"], "Line": row["LineViolation"],
         "LoadingPct": row["Loading After Common Dispatch (%)"]}
        for row in screening_rows if row["Status"] != "Cleared"
    ]

    # First-level report in the N-0 layout (thermal violations before the
    # dispatch and the free-case comparison). N-1 and N-1-1 both use it.
    thermal_rows, free_rows = _n1_style_report_rows(
        contingencies, initial_case, final_case, initial_screen, screen, "N-1",
    )
    combined["n0_thermal_rows"] = thermal_rows
    combined["n0_free_rows"] = free_rows
    combined["n1_report"] = True
    if not second_level:
        combined["saved_case"] = str(final_case)
        return combined

    # ---------------- Part 2: second level on top of each first outage, report only
    seconds_all = build_second_level_set(contingencies)
    tasks = [
        {"index": index, "first": first}
        for index, first in enumerate(contingencies, start=1)
    ]
    pair_count = sum(len(_seconds_for(task["first"], seconds_all)) for task in tasks)
    method = str(SECOND_LEVEL_METHOD).upper()
    explicit_second_level = (
        method == "EXPLICIT"
        or (method == "AUTO" and pair_count <= int(SECOND_LEVEL_EXPLICIT_MAX_PAIRS))
    )
    log.info(
        "Second level: %s first outages x %s second outages = %s pairs, method %s.",
        len(contingencies), len(seconds_all), pair_count,
        "EXPLICIT (every pair solved)" if explicit_second_level else "ACCC screen + explicit check",
    )
    combined["notes"].append(
        "Second-level outage list: %s outages (single-line range %.1f-%.1f kV)."
        % (len(seconds_all), SECOND_LEVEL_MIN_KV, SECOND_LEVEL_MAX_KV)
    )
    combined["notes"].append(
        "Second-level method: %s (%s pairs)."
        % ("explicit power flow on every pair" if explicit_second_level else "ACCC screen, flagged pairs re-solved", pair_count)
    )
    raw_list = None
    workers = _worker_count(len(tasks), 2) if pair_count >= int(PARALLEL_MIN_PAIRS) else 1
    if workers > 1:
        started = time.time()
        jobs = [
            {
                "kind": "second",
                "case": str(final_case),
                "tasks": tasks[k::workers],
                "seconds": seconds_all,
                "monitored": _pack_keys(monitored),
                "explicit": explicit_second_level,
                "initial": str(initial_case),
            }
            for k in range(workers)
        ]
        done = run_worker_jobs(jobs, "second")
        if done is None:
            log.warning("Parallel second level failed. Running in this process.")
        else:
            by_index = {}
            for part in done:
                for result in part["results"]:
                    by_index[result["index"]] = result
            raw_list = [by_index[task["index"]] for task in tasks if task["index"] in by_index]
            log.info("Second level finished in %.0f s with %s processes.", time.time() - started, workers)
    if raw_list is None:
        raw_list = [
            second_level_raw(
                task, seconds_all, final_case, monitored, explicit_second_level, initial_case
            )
            for task in tasks
        ]

    def _failed_row(first_name, second_name, issue):
        return {
            "First Level Contingency": first_name,
            "Second Level Contingency": second_name,
            "BranchName": "", "LineViolation": "",
            COL_RATING_LONG: None, COL_EMERGENCY_LONG: None,
            "First Level Loading (%)": None, "Mitigated Loading (%)": None,
            "Second Level Loading (%)": convergence_report_value(issue),
            "Mitigation": "Common dispatch",
        }

    for res in raw_list:
        first_name = res["first"]
        combined["notes"].extend(res["notes"])
        if not res["first_ok"]:
            combined["non_converged"].append({
                "Contingency": first_name, "FirstOutage": first_name,
                "Issue": "First outage did not solve with the common dispatch: %s" % res["issue"],
            })
            continue
        first_before = _unpack_loading(res["first_before"])
        first_loading = _unpack_loading(res["first_loading"])

        def _row(second_name, key, second_pct):
            fields = {
                "First Level Contingency": first_name,
                "Second Level Contingency": second_name,
            }
            fields.update(_branch_fields(key))
            fields["First Level Loading (%)"] = _pct_cell(first_before, key)
            fields["Mitigated Loading (%)"] = _pct_cell(first_loading, key)
            fields["Second Level Loading (%)"] = (
                round(second_pct, 2) if second_pct is not None else None
            )
            fields["Mitigation"] = "Common dispatch"
            return fields

        for key, pct in sorted(violations_of(first_loading).items()):
            combined["thermal_violation"].append(_row("", key, None))
        for item in res["nonconv"]:
            combined["non_converged"].append({
                "Contingency": item["Contingency"], "FirstOutage": first_name,
                "Issue": item["Issue"],
            })
        for second_name, issue in res["failed"]:
            combined["non_converged"].append({
                "Contingency": second_name, "FirstOutage": first_name, "Issue": issue,
            })
            combined["thermal_violation"].append(_failed_row(first_name, second_name, issue))
        for pair in res["pairs"]:
            for key, pct in sorted(_unpack_loading(pair["viol"]).items()):
                combined["thermal_violation"].append(_row(pair["second"], key, pct))
                combined["second_level"].append({
                    "FirstOutage": first_name,
                    "Contingency": pair["second"],
                    "Element": "%s-%s-%s" % key,
                    "LoadingPct": round(pct, 2),
                })
    combined["n11_report"] = True
    combined["saved_case"] = str(final_case)
    return combined


def run_n11():
    if str(N11_MODE).upper() == "PER_OUTAGE":
        return run_n11_per_outage()
    return run_n11_common()


def _empty_result():
    return {
        "n0_thermal_rows": [],
        "n0_free_rows": [],
        "violation_rows": [],
        "plan_rows": [],
        "applied_rows": [],
        "swing_rows": [],
        "remaining_rows": [],
        "notes": [],
        "non_converged": [],
        "second_level": [],
        "thermal_violation": [],
        "cleared": False,
        "curtail_mw": 0.0,
        "saved_case": "",
    }


def _merge(into, piece):
    for key in (
        "n0_thermal_rows", "n0_free_rows",
        "violation_rows", "plan_rows", "applied_rows", "swing_rows",
        "remaining_rows", "notes",
    ):
        into[key].extend(piece.get(key, []))
    into["curtail_mw"] += float(piece.get("curtail_mw", 0.0))


# ---------------------------------------------------------------------------
# Workbook
# ---------------------------------------------------------------------------

_CONTINGENCY_COLUMNS = (
    "Contingency", "FirstOutage", "Label",
    "First Level Contingency", "Second Level Contingency",
)

_N0_THERMAL_COLUMNS = [
    "Study", "Contingency", "BranchName", "LineViolation",
    COL_EMERGENCY, COL_NORMAL, "RatingPct", "LoadingPct",
]
_N0_FREE_COLUMNS = [
    "Study", "Contingency", "BranchName", "LineViolation",
    "FromBus", "ToBus", "Ckt", "Line", "FlowMW", "QMVAR", "MVA",
    COL_EMERGENCY, COL_NORMAL, "RatingPct", "LoadingPct",
    "Post RatingPct", "Post LoadingPct",
]


def _frame(rows, columns=None):
    if not rows:
        return pd.DataFrame(columns=columns or ["Note"])
    frame = pd.DataFrame(rows, columns=columns) if columns else pd.DataFrame(rows)
    # A generated single-line contingency is already named with its branch
    # name, for example TL_1097L_356S_611S. CON_DISPLAY only renames a
    # contingency when an alias was recorded.
    for column in _CONTINGENCY_COLUMNS:
        if column in frame.columns:
            frame[column] = frame[column].map(
                lambda value: CON_DISPLAY.get(value, value) if isinstance(value, str) else value
            )
    return frame


def _n0_generator_dispatch_frame(rows):
    columns = [
        "Study", "Contingency", "Bus", "MachineID", "MPID",
        "FacilityName", "Name", "Technology", "Area", "Region",
        "P Before (MW)", "Q Before (MVAR)",
        "P After (MW)", "Q After (MVAR)",
    ]
    values = []
    for row in rows:
        values.append({
            "Study": row.get("Study", "N-0"),
            "Contingency": row.get("Contingency", "BASE CASE"),
            "Bus": row.get("Bus", ""),
            "MachineID": row.get("MachineID", ""),
            "MPID": row.get("MPID", ""),
            "FacilityName": row.get("FacilityName", ""),
            "Name": row.get("Name", ""),
            "Technology": row.get("Technology", ""),
            "Area": row.get("Area", ""),
            "Region": row.get("Region", ""),
            "P Before (MW)": row.get("PBeforeMW", ""),
            "Q Before (MVAR)": row.get("QBeforeMVAR", ""),
            "P After (MW)": row.get("PAfterMW", ""),
            "Q After (MVAR)": row.get("QAfterMVAR", ""),
        })
    return _frame(values, columns=columns)


def write_workbook(pieces, study_ran):
    summary_rows = [
        ["Study", ", ".join(study_ran)],
        ["Case", str(CASE_FILE)],
        ["Special contingency file", str(CON_FILE) if USE_SPECIAL_CON else "not used"],
        ["Special contingencies", CON_SOURCE_COUNTS["special"] if ACTIVE_CON_FILE else "N/A"],
        ["Single-line contingencies", CON_SOURCE_COUNTS["single_line"] if ACTIVE_CON_FILE else "N/A"],
        ["Single-line kV range", "%s - %s" % (SINGLE_LINE_MIN_KV, SINGLE_LINE_MAX_KV) if (ACTIVE_CON_FILE and USE_SINGLE_LINE_CON) else ("not used" if ACTIVE_CON_FILE else "N/A")],
        ["Merged contingency file", str(ACTIVE_CON_FILE) or "N/A"],
        ["N-0 free case requested", "Yes" if MAKE_N0_FREE_CASE else "No"],
        ["Saved case", pieces.get("saved_case", "")],
        ["Study areas", ", ".join(str(a) for a in STUDY_AREAS) or "whole case"],
        ["Generator catalog", str(GENERATOR_CATALOG_FILE)],
        ["Dispatch-down technologies", ", ".join(DISPATCH_DOWN_TECHNOLOGIES) or "all mapped types"],
        ["Dispatch-up technologies", ", ".join(DISPATCH_UP_TECHNOLOGIES) or "all mapped types"],
        ["Dispatch-down areas", ", ".join(str(a) for a in curtail_areas()) or "whole case"],
        ["Dispatch-up areas", ", ".join(str(a) for a in BALANCE_AREAS) or "dispatch-down areas"],
        ["Allow unmapped dispatch down", "Yes" if ALLOW_UNMAPPED_DISPATCH_DOWN else "No"],
        ["Allow unmapped dispatch up", "Yes" if ALLOW_UNMAPPED_DISPATCH_UP else "No"],
        ["Swing buses", ", ".join(str(b) for b in SWING_BUSES) or "type-3 buses in the case"],
        ["Season", "%s: overload test on %s, emergency rating %s"
         % (str(SEASON).upper(), RATING, EMERGENCY_RATING)],
        ["Thermal limit (%)", THERMAL_LIMIT_PCT],
        ["Target loading (%)", min(TARGET_LOADING_PCT, THERMAL_LIMIT_PCT)],
        ["Watch loading (%)", WATCH_PCT],
        ["Top generators per line", TOP_N_GENS],
        ["Minimum effectiveness", MIN_EFFECTIVENESS],
        ["Thermal violations before dispatch (rows)", len(pieces.get("n0_thermal_rows", []))],
        ["Free-case comparisons (rows)", len(pieces.get("n0_free_rows", []))],
        ["Overload rows (line x generator)", len(pieces["violation_rows"])],
        ["Joint-plan moves", len(pieces["plan_rows"])],
        ["Applied generator changes", len(pieces["applied_rows"])],
        ["Remaining overloads", len(pieces["remaining_rows"])],
        ["Second-level overloads", len(pieces.get("second_level", []))],
        ["Thermal violation comparisons", len(pieces.get("thermal_violation", []))],
        ["Non-converged", len(pieces.get("non_converged", []))],
        ["Log", str(LOG_PATH)],
    ]
    for note in pieces["notes"]:
        for technical, readable in CON_DISPLAY.items():
            if note.startswith(technical + ":"):
                note = readable + note[len(technical):]
                break
        summary_rows.append(["Note", note])
    n0_only = [str(value).upper() for value in study_ran] == ["N-0"]
    n11_layout = (
        [str(value).upper() for value in study_ran] == ["N-1-1"]
        and bool(pieces.get("n1_report"))
        and bool(pieces.get("n11_report"))
    )
    n1_layout = (
        [str(value).upper() for value in study_ran] == ["N-1"]
        and bool(pieces.get("n1_report"))
    ) or n11_layout
    free_sheet = "N-1 Free Case" if n1_layout else "N-0 Free Case"
    if n0_only or n1_layout:
        sheets = {
            "Summary": pd.DataFrame(summary_rows, columns=["Item", "Value"]),
            "Thermal Violations": _frame(
                pieces.get("n0_thermal_rows", []),
                columns=_N0_THERMAL_COLUMNS,
            ),
            "Generator Dispatch": _n0_generator_dispatch_frame(
                pieces.get("applied_rows", [])
            ),
            free_sheet: _frame(
                pieces.get("n0_free_rows", []),
                columns=_N0_FREE_COLUMNS,
            ),
        }
        if n11_layout:
            # Same four sheets as N-1, plus the report-only second level.
            sheets["N-1-1 Violations"] = _frame(pieces.get("thermal_violation", []))
            if pieces.get("non_converged"):
                sheets["Non-Converged"] = _frame(pieces["non_converged"])
    else:
        sheets = {
            "Summary": pd.DataFrame(summary_rows, columns=["Item", "Value"]),
            "Violations and Gens": _frame(pieces["violation_rows"]),
            "Joint Plan": _frame(pieces["plan_rows"]),
            "Applied Changes": _frame(pieces["applied_rows"]),
            "Swing": _frame(pieces["swing_rows"]),
            "Remaining Overloads": _frame(pieces["remaining_rows"]),
            "Second Level": _frame(pieces.get("second_level", [])),
            "Thermal Violation": _frame(pieces.get("thermal_violation", [])),
            "Non-Converged": _frame(pieces.get("non_converged", [])),
        }
    if pieces.get("round_history"):
        sheets["Round History"] = _frame(pieces["round_history"])
    with pd.ExcelWriter(str(EXCEL_PATH), engine="openpyxl") as writer:
        for name, frame in sheets.items():
            frame.to_excel(writer, sheet_name=name[:31], index=False)
        _format_workbook(writer.book)
        if n0_only or n1_layout:
            _format_n0_free_case_sheet(writer.book, free_sheet)
            if n11_layout:
                _format_thermal_violation_sheet(
                    writer.book, "N-1-1 Violations",
                    headings=[
                        "First Level Contingency", "Second Level Contingency",
                        "BranchName", "LineViolation",
                        COL_RATING_LONG, COL_EMERGENCY_LONG,
                    ],
                )
        else:
            _format_thermal_violation_sheet(writer.book)
    log.info("Workbook written to %s.", EXCEL_PATH)
    return str(EXCEL_PATH)


def _format_workbook(workbook):
    header_fill = PatternFill("solid", fgColor="1F4E78")
    header_font = Font(color="FFFFFF", bold=True)
    for name in workbook.sheetnames:
        sheet = workbook[name]
        if sheet.max_row < 1:
            continue
        for cell in sheet[1]:
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal="center")
        for column in sheet.columns:
            letter = get_column_letter(column[0].column)
            width = 0
            for cell in column:
                try:
                    width = max(width, len(str(cell.value)))
                except Exception:
                    pass
            sheet.column_dimensions[letter].width = min(width + 3, 70)
        sheet.freeze_panes = "A2"
        if sheet.dimensions:
            sheet.auto_filter.ref = sheet.dimensions


def _format_thermal_violation_sheet(workbook, sheet_name="Thermal Violation", headings=None):
    """Add the two-level grouped header shown in the thermal comparison report."""
    if sheet_name not in workbook.sheetnames:
        return
    sheet = workbook[sheet_name]
    sheet.insert_rows(1)
    if headings is None:
        headings = [
            "First Level Contingency",
            "Second Level Contingency",
            "Violated Element",
            "Branch Key",
            "Rating MVA",
            COL_EMERGENCY_LONG,
        ]
    for column, heading in enumerate(headings, start=1):
        sheet.cell(1, column, heading)
        sheet.merge_cells(start_row=1, start_column=column, end_row=2, end_column=column)
    sheet.cell(1, 7, "Loading (%)")
    sheet.merge_cells("G1:I1")
    sheet.cell(2, 7, "First Level")
    sheet.cell(2, 8, "Mitigated")
    sheet.cell(2, 9, "Second Level")
    sheet.cell(1, 10, "Mitigation")
    sheet.merge_cells("J1:J2")
    fill = PatternFill("solid", fgColor="1F4E78")
    font = Font(color="FFFFFF", bold=True)
    for row in sheet.iter_rows(min_row=1, max_row=2, min_col=1, max_col=10):
        for cell in row:
            cell.fill = fill
            cell.font = font
            cell.alignment = Alignment(horizontal="center", vertical="center")
    sheet.freeze_panes = "A3"
    sheet.auto_filter.ref = "A2:J%d" % sheet.max_row


def _format_n0_free_case_sheet(workbook, sheet_name="N-0 Free Case"):
    """Group the two final columns under the requested Post Curtailment header."""
    if sheet_name not in workbook.sheetnames:
        return
    sheet = workbook[sheet_name]
    sheet.insert_rows(1)
    post_start = 16
    for column in range(1, post_start):
        heading = sheet.cell(2, column).value
        sheet.cell(1, column, heading)
        sheet.merge_cells(
            start_row=1, start_column=column, end_row=2, end_column=column
        )
    sheet.cell(1, post_start, "Post Curtailment")
    sheet.merge_cells(
        start_row=1, start_column=post_start,
        end_row=1, end_column=post_start + 1,
    )
    sheet.cell(2, post_start, "RatingPct")
    sheet.cell(2, post_start + 1, "LoadingPct")
    fill = PatternFill("solid", fgColor="1F4E78")
    font = Font(color="FFFFFF", bold=True)
    for row in sheet.iter_rows(
        min_row=1, max_row=2, min_col=1, max_col=post_start + 1
    ):
        for cell in row:
            cell.fill = fill
            cell.font = font
            cell.alignment = Alignment(
                horizontal="center", vertical="center"
            )
    sheet.freeze_panes = "A3"
    sheet.auto_filter.ref = "A2:Q%d" % sheet.max_row


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def validate_settings():
    if STUDY.upper() not in ("N-0", "N-1", "N-1-1", "ALL"):
        raise RuntimeError("STUDY must be N-0, N-1, N-1-1, or ALL.")
    if not Path(CASE_FILE).exists():
        raise RuntimeError("CASE_FILE was not found: %s" % CASE_FILE)
    needs_con = STUDY.upper() in ("N-1", "N-1-1", "ALL")
    if needs_con and not (USE_SPECIAL_CON or USE_SINGLE_LINE_CON):
        raise RuntimeError("Turn on USE_SPECIAL_CON, USE_SINGLE_LINE_CON, or both for N-1 / N-1-1.")
    if needs_con and USE_SPECIAL_CON and not Path(CON_FILE).exists():
        raise RuntimeError("CON_FILE was not found: %s" % CON_FILE)
    if float(SINGLE_LINE_MIN_KV) < 0 or float(SINGLE_LINE_MAX_KV) < float(SINGLE_LINE_MIN_KV):
        raise RuntimeError("SINGLE_LINE_MIN_KV/MAX_KV define an invalid kV range.")
    if str(SEASON).strip().upper() not in _SEASON_RATES:
        raise RuntimeError("SEASON must be SP or WP.")
    if Q_MODE.upper() not in ("PF", "HOLD"):
        raise RuntimeError("Q_MODE must be PF or HOLD.")
    if not Path(GENERATOR_CATALOG_FILE).exists():
        raise RuntimeError(
            "GENERATOR_CATALOG_FILE was not found: %s"
            % GENERATOR_CATALOG_FILE
        )
    try:
        import scipy  # noqa: F401
    except ImportError:
        raise RuntimeError(
            "scipy is not installed in this PSS/E Python. "
        )


def main():
    global ACTIVE_CON_FILE, GENERATOR_CATALOG
    validate_settings()
    GENERATOR_CATALOG = load_generator_catalog()
    psspy.psseinit(PSSE_BUSES)
    psspy.progress_output(2, str(PROGRESS_PATH), [1, 0])
    psspy.report_output(6, "", [])
    study = STUDY.upper()
    if study in ("N-1", "N-1-1", "ALL"):
        _contingencies, ACTIVE_CON_FILE = build_contingency_set()
    pieces = _empty_result()
    ran = []
    saved = []
    if study in ("N-0", "ALL"):
        ran.append("N-0")
        piece = run_n0()
        _merge(pieces, piece)
        if piece.get("saved_case"):
            saved.append(piece["saved_case"])
    if study in ("N-1", "ALL"):
        ran.append("N-1")
        piece = run_n1()
        _merge(pieces, piece)
        pieces["non_converged"].extend(piece.get("non_converged", []))
        if piece.get("n1_report"):
            pieces["n1_report"] = True
        if piece.get("round_history"):
            pieces["round_history"] = piece["round_history"]
        if piece.get("saved_case"):
            saved.append(piece["saved_case"])
    if study in ("N-1-1", "ALL"):
        ran.append("N-1-1")
        piece = run_n11()
        _merge(pieces, piece)
        pieces["non_converged"].extend(piece.get("non_converged", []))
        pieces["second_level"].extend(piece.get("second_level", []))
        pieces["thermal_violation"].extend(piece.get("thermal_violation", []))
        if piece.get("n11"):
            pieces["n11"] = piece["n11"]
        if piece.get("n1_report"):
            pieces["n1_report"] = True
        if piece.get("round_history"):
            pieces["round_history"] = piece["round_history"]
        if piece.get("n11_report"):
            pieces["n11_report"] = True
        if piece.get("saved_case"):
            saved.append(piece["saved_case"])
    pieces["saved_case"] = "; ".join(saved)
    excel = write_workbook(pieces, ran)
    cleared = "yes" if pieces["remaining_rows"] == [] and study == "N-0" and MAKE_N0_FREE_CASE else "see workbook"
    payload = {
        "success": True,
        "study": ran,
        "excel": excel,
        "log": str(LOG_PATH),
        "savedCase": pieces["saved_case"],
        "overloadGeneratorRows": len(pieces["violation_rows"]),
        "appliedChanges": len(pieces["applied_rows"]),
        "remainingOverloads": len(pieces["remaining_rows"]),
        "secondLevelOverloads": len(pieces["second_level"]),
        "n0Cleared": cleared,
    }
    print("PSSE_RESULT:" + json.dumps(payload), flush=True)
    log.info("Done. Workbook: %s", excel)


def worker_main():
    """Entry point of a worker PSS/E process started by run_worker_jobs()."""
    import traceback
    job_path = os.environ["CURTAIL_JOB"]
    with open(job_path, "r", encoding="utf-8") as handle:
        job = json.load(handle)
    out_path = job["out"]
    try:
        psspy.psseinit(PSSE_BUSES)
        psspy.progress_output(2, str(job.get("progress", "")), [1, 0])
        psspy.report_output(6, "", [])
        monitored = _unpack_keys(job["monitored"])
        if job["kind"] == "screen":
            result = {"results": {}}
            for name, entry in _screen_items(job["items"], job["case"], monitored).items():
                result["results"][name] = {
                    "ok": entry["ok"],
                    "issue": entry["issue"],
                    "pct": _pack_loading(entry["pct"]),
                }
        elif job["kind"] == "second":
            result = {
                "results": [
                    second_level_raw(
                        task, job["seconds"], job["case"], monitored, bool(job["explicit"]),
                        job["initial"],
                    )
                    for task in job["tasks"]
                ]
            }
        else:
            raise RuntimeError("Unknown job kind %s" % job["kind"])
    except BaseException:
        result = {"error": traceback.format_exc()}
    temporary = out_path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(result, handle)
    os.replace(temporary, out_path)
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)


if __name__ == "__main__" and IS_WORKER:
    worker_main()
elif __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log.exception("CurtailOverload stopped.")
        print("PSSE_RESULT:" + json.dumps({"success": False, "error": str(exc)}), flush=True)
        raise
