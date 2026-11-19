"""
Voltage target discovery for voltage profiling.

This module keeps the existing AIES target source and adds an optional
"AIES + remaining buses" source based on the additional area voltage checks.
It returns the same structure expected by VoltageProfileBatchMain:

    {bus_number: [target_kv_min, target_kv_max]}
"""

import psspy

from Voltage_Limit_Check_R6_V35 import AIES


ATCO_AREAS = [
    26, 27, 39, 24, 32, 33, 56, 37, 17, 35, 25, 28, 19,
    42, 13, 20, 18, 22, 23, 21, 36,
]

ATCO_EXCLUDED_BUSES = [
    7, 4, 23, 25, 21, 3, 1, 191, 80, 194, 58, 44, 42, 36, 10, 99, 59,
    421, 13, 231, 11, 301, 192, 251, 321, 8, 381, 15, 5, 41, 73, 362,
    371, 521, 29, 14, 12, 391, 441, 40, 514, 190, 67, 69, 70, 79, 2,
    30, 516, 9, 43, 416, 63, 312, 705, 82, 49, 33, 451, 39, 261, 110,
    501, 472, 473, 193, 211, 72, 200, 888, 999, 523, 9999, 515, 113,
    81, 57, 706, 520, 529, 116, 462, 83, 101, 998, 722, 481, 221, 361,
    24, 6, 210, 78, 37, 61, 120, 143, 16, 18, 20, 22, 26, 27, 28, 31,
    32, 34, 35, 38, 45, 46, 47, 48, 50, 51, 52, 53, 54, 55, 56, 60,
    62, 64, 65, 66, 68, 71, 74, 75, 76, 85, 88, 100, 102, 103, 105,
    121, 125, 130, 140, 150, 151, 152, 160, 170, 241, 250, 271, 281,
    291, 331, 332, 341, 351, 372, 373, 374, 393, 401, 402, 411, 412,
    431, 445, 446, 461, 471, 491, 502, 503, 504, 505, 506, 507, 508,
    509, 510, 511, 512, 513, 519,
]


def percentVoltDeviation(vpu, vmin, vmax):
    if vpu < vmin:
        return (vmin - vpu) * 100 / vmin
    if vpu > vmax:
        return (vpu - vmax) * 100 / vmax
    return 0.0


def normalize_target_source(target_source):
    source = str(target_source or "aies").strip().lower().replace("-", "_")
    aliases = {
        "aies": "aies",
        "aies_only": "aies",
        "id2010": "aies",
        "id2010_007rs": "aies",
        "aies_plus_all": "aies_plus_all",
        "aies_all": "aies_plus_all",
        "aies_plus_remaining": "aies_plus_all",
        "aies_plus_all_remaining": "aies_plus_all",
        "total": "aies_plus_all",
        "voltageprofile_total": "aies_plus_all",
    }
    if source not in aliases:
        raise ValueError(
            "Unsupported voltage target source '{}'. Use 'aies' or 'aies_plus_all'.".format(
                target_source
            )
        )
    return aliases[source]


def _aies_instance(area=None):
    try:
        return AIES(area)
    except TypeError:
        return AIES()


def get_aies_target_map(area, range_mode="operating"):
    aies = _aies_instance(area)
    targets = aies.get_voltage_targets(range_mode=range_mode)
    return {
        int(bus): [float(vrange[0]), float(vrange[1])]
        for bus, vrange in zip(
            targets["bus_numbers"],
            targets["voltage_buses_range"],
        )
    }


def get_aies_voltage_dict(area=None):
    try:
        return _aies_instance(area).Voltages_dict()
    except Exception:
        return {}


def _abus_for_kv_band(sid, kvmin, kvmax, arealist, buses=None):
    if buses is None:
        buses = []
    ierr = psspy.bsys(
        sid, 1, [float(kvmin), float(kvmax)],
        len(arealist), arealist,
        0, [], len(buses), buses, 0, []
    )
    if ierr != 0:
        raise RuntimeError("bsys failed for sid {} ierr={}".format(sid, ierr))

    ierr, bus_numbers = psspy.abusint(sid, 1, ["NUMBER"])
    if ierr != 0:
        raise RuntimeError("abusint failed for sid {} ierr={}".format(sid, ierr))
    ierr, bus_voltages = psspy.abusreal(sid, 1, ["PU"])
    if ierr != 0:
        raise RuntimeError("abusreal failed for sid {} ierr={}".format(sid, ierr))

    return list(zip(bus_numbers[0], bus_voltages[0]))


def _add_remaining_target(targets, bus, kvmin, kvmax, percent):
    bus = int(bus)
    row = [float(kvmin), float(kvmax), float(percent)]
    current = targets.get(bus)
    if current is None or row[2] > current[2]:
        targets[bus] = row


def _add_standard_band_violations(
    targets,
    entries,
    vmin,
    vmax,
    target_base_kv,
    excluded_buses,
):
    for bus, vpu in entries:
        bus = int(bus)
        if bus in excluded_buses:
            continue
        if vpu < vmin or vpu > vmax:
            percent = percentVoltDeviation(vpu, vmin, vmax)
            _add_remaining_target(
                targets,
                bus,
                float(vmin) * float(target_base_kv),
                float(vmax) * float(target_base_kv),
                percent,
            )


def _collect_atco_buses(arealist):
    area_list_atco = list(set(arealist).intersection(ATCO_AREAS))
    if not area_list_atco:
        return set(), [], [], []

    buses240 = _abus_for_kv_band(
        7, 239.0, 240.0, area_list_atco, buses=ATCO_EXCLUDED_BUSES
    )
    buses138 = _abus_for_kv_band(
        8, 137.0, 138.0, area_list_atco, buses=ATCO_EXCLUDED_BUSES
    )
    buses69 = _abus_for_kv_band(
        9, 68.0, 69.0, area_list_atco, buses=ATCO_EXCLUDED_BUSES
    )
    atco_buses = {int(bus) for bus, _ in buses240 + buses138 + buses69}
    return atco_buses, buses240, buses138, buses69


def get_remaining_voltage_target_map(case, arealist, excluded_aies_buses=None):
    """
    Return targets for non-AIES buses that violate the additional voltage bands.

    The returned values are kV limits, so they can be consumed directly by the
    existing voltage profiling controller.
    """
    if excluded_aies_buses is None:
        excluded_aies_buses = set()
    excluded_aies_buses = {int(bus) for bus in excluded_aies_buses}

    remaining = {}

    bus_zipped_500 = _abus_for_kv_band(0, 400.0, 500.0, arealist)
    bus_zipped_240 = _abus_for_kv_band(1, 230.0, 241.0, arealist)
    bus_zipped_144 = _abus_for_kv_band(2, 143.0, 144.0, arealist)
    bus_zipped_138 = _abus_for_kv_band(3, 137.0, 138.0, arealist)
    bus_zipped_72 = _abus_for_kv_band(4, 71.0, 72.0, arealist)
    bus_zipped_69 = _abus_for_kv_band(5, 30.0, 69.0, arealist)
    # Kept from the provided check: 72-kV modeled buses checked against 260-kV limits.
    bus_zipped_260 = _abus_for_kv_band(6, 71.0, 72.0, arealist)

    atco_buses, bus_zipped_240_atco, bus_zipped_138_atco, bus_zipped_69_atco = (
        _collect_atco_buses(arealist)
    )
    excluded = set(atco_buses).union(excluded_aies_buses)

    _add_standard_band_violations(remaining, bus_zipped_500, 1.0, 1.05, 500.0, excluded)
    _add_standard_band_violations(remaining, bus_zipped_240, 0.975, 1.05, 240.0, excluded)
    _add_standard_band_violations(remaining, bus_zipped_144, 0.95138, 1.048611, 144.0, excluded)
    _add_standard_band_violations(remaining, bus_zipped_138, 0.97826, 1.0507, 138.0, excluded)
    _add_standard_band_violations(remaining, bus_zipped_72, 0.95138, 1.048611, 72.0, excluded)
    _add_standard_band_violations(remaining, bus_zipped_69, 0.949, 1.1, 69.0, excluded)
    _add_standard_band_violations(remaining, bus_zipped_260, 0.95, 1.023, 260.0, excluded)

    # ATCO buses are modeled at one voltage base and checked against the target base.
    for bus, vpu in bus_zipped_240_atco:
        converted_pu = vpu * 240.0 / 260.0
        if bus in excluded_aies_buses:
            continue
        if converted_pu < 0.95 or converted_pu > 1.023:
            _add_remaining_target(
                remaining, bus, 0.95 * 260.0, 1.023 * 260.0,
                percentVoltDeviation(converted_pu, 0.95, 1.023)
            )

    for bus, vpu in bus_zipped_138_atco:
        converted_pu = vpu * 138.0 / 144.0
        if bus in excluded_aies_buses:
            continue
        if converted_pu < 0.95138 or converted_pu > 1.048611:
            _add_remaining_target(
                remaining, bus, 0.95138 * 144.0, 1.048611 * 144.0,
                percentVoltDeviation(converted_pu, 0.95138, 1.048611)
            )

    for bus, vpu in bus_zipped_69_atco:
        converted_pu = vpu * 69.0 / 72.0
        if bus in excluded_aies_buses:
            continue
        if converted_pu < 0.95138 or converted_pu > 1.048611:
            _add_remaining_target(
                remaining, bus, 0.95138 * 72.0, 1.048611 * 72.0,
                percentVoltDeviation(converted_pu, 0.95138, 1.048611)
            )

    return {
        bus: [row[0], row[1]]
        for bus, row in sorted(remaining.items())
    }


def get_voltage_target_map(case, arealist, range_mode="operating", target_source="aies"):
    """
    target_source:
      - "aies": current AIES behavior
      - "aies_plus_all": AIES plus non-AIES remaining bus voltage checks
    """
    source = normalize_target_source(target_source)
    aies_targets = get_aies_target_map(arealist, range_mode=range_mode)
    if source == "aies":
        return aies_targets

    aies_voltage_dict = get_aies_voltage_dict(arealist)
    excluded_aies_buses = set(aies_voltage_dict.keys()) or set(aies_targets.keys())
    remaining_targets = get_remaining_voltage_target_map(
        case, arealist, excluded_aies_buses=excluded_aies_buses
    )

    combined = dict(aies_targets)
    for bus, vrange in remaining_targets.items():
        combined.setdefault(int(bus), [float(vrange[0]), float(vrange[1])])
    return combined
