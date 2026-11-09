"""Load and resolve generator metadata from the facility-list workbook.

The workbook does not contain PSS/E machine IDs, so its strongest key is the
bus number.  When more than one facility shares a bus, a generator name that
contains an MPID is used to narrow the match.  Otherwise all records at that
bus are retained and dispatch eligibility is conservative: every possible
technology must be selected.
"""

from __future__ import annotations

import math
import re


REQUIRED_COLUMNS = (
    "MPID",
    "Facility Name",
    "Bus",
    "Area",
    "Region",
    "Technology",
)


def normalize_choice(value):
    """Case-insensitive value used by user selections and workbook fields."""
    if value is None:
        return ""
    try:
        if math.isnan(value):
            return ""
    except (TypeError, ValueError):
        pass
    return str(value).strip().upper()


def _name_token(value):
    return re.sub(r"[^A-Z0-9]+", "", normalize_choice(value))


def _unique(values):
    result = []
    seen = set()
    for value in values:
        text = str(value).strip()
        marker = text.casefold()
        if text and marker not in seen:
            seen.add(marker)
            result.append(text)
    return result


def load_generator_catalog(path, sheet_name="Merged MPID Data"):
    """Return ``{bus: [records]}`` from the generator workbook."""
    import pandas as pd

    frame = pd.read_excel(path, sheet_name=sheet_name)
    missing = [column for column in REQUIRED_COLUMNS if column not in frame.columns]
    if missing:
        raise ValueError(
            "Generator catalog is missing required columns: %s"
            % ", ".join(missing)
        )

    catalog = {}
    seen = set()
    for _index, row in frame.iterrows():
        try:
            bus = int(float(row["Bus"]))
        except (TypeError, ValueError):
            continue
        record = {
            "mpid": str(row["MPID"]).strip(),
            "facility_name": str(row["Facility Name"]).strip(),
            "bus": bus,
            "area": int(float(row["Area"])) if pd.notna(row["Area"]) else 0,
            "region": str(row["Region"]).strip(),
            "technology": normalize_choice(row["Technology"]),
        }
        marker = tuple(record[column] for column in (
            "mpid", "facility_name", "bus", "area", "region", "technology"
        ))
        if marker in seen:
            continue
        seen.add(marker)
        catalog.setdefault(bus, []).append(record)
    return catalog


def resolve_generator(catalog, bus, generator_name=""):
    """Resolve metadata for one PSS/E machine.

    If the PSS/E name identifies one MPID on a shared bus, only that facility
    is returned. Otherwise the metadata is combined and ``ambiguous`` is true
    when multiple MPIDs or technologies remain.
    """
    records = list(catalog.get(int(bus), []))
    if not records:
        return {
            "matched": False,
            "ambiguous": False,
            "mpid": "",
            "facility_name": "",
            "catalog_area": 0,
            "region": "",
            "technology": "",
            "technologies": set(),
        }

    name_token = _name_token(generator_name)
    if name_token:
        named = [
            record for record in records
            if _name_token(record["mpid"])
            and _name_token(record["mpid"]) in name_token
        ]
        if named:
            records = named

    mpids = _unique(record["mpid"] for record in records)
    facilities = _unique(record["facility_name"] for record in records)
    areas = sorted(set(int(record["area"]) for record in records if int(record["area"])))
    regions = _unique(record["region"] for record in records)
    technologies = set(
        normalize_choice(record["technology"]) for record in records
        if normalize_choice(record["technology"])
    )
    return {
        "matched": True,
        "ambiguous": len(mpids) > 1 or len(technologies) > 1,
        "mpid": "; ".join(mpids),
        "facility_name": "; ".join(facilities),
        "catalog_area": areas[0] if len(areas) == 1 else 0,
        "region": "; ".join(regions),
        "technology": "; ".join(sorted(technologies)),
        "technologies": technologies,
    }


def technology_is_allowed(metadata, selected_technologies, allow_unmapped=False):
    """Whether metadata satisfies a technology selection.

    An empty selection means every mapped technology. For an ambiguous bus,
    every possible technology must be selected; this prevents a renewable
    machine from being dispatched up merely because a thermal facility shares
    the same bus.
    """
    if not metadata.get("matched"):
        return bool(allow_unmapped)
    selected = {
        normalize_choice(value) for value in selected_technologies
        if normalize_choice(value)
    }
    if not selected:
        return True
    technologies = set(metadata.get("technologies") or [])
    return bool(technologies) and technologies.issubset(selected)
