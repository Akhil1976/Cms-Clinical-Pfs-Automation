"""
mpfs_parser.py
---------------
Turns a downloaded MPFS (Physician Fee Schedule) data file into a list of
normalized dicts:

    {
        "hcpcs_code": "99213",
        "carrier_id": "10112",
        "locality": "00",
        "modifier": "",
        "year": 2026,
        "non_facility_price": 92.34,
        "facility_price": 61.12,
        "status_code": "A",
        "description": "",
    }

Mirrors clfs_parser.py exactly — same "sniff delimiter / scan for header
row / fall back to a positional map" approach, using the shared helpers in
parser/common.py. The header aliases live in config/mpfs_column_map.json.
"""

import json
import logging
from pathlib import Path

from config.settings import MPFS_COLUMN_MAP_PATH
from parser import common

logger = logging.getLogger("clfs.parser.mpfs")

with open(MPFS_COLUMN_MAP_PATH, encoding="utf-8") as f:
    _COLUMN_MAP = json.load(f)

_LOGICAL_FIELDS = [k for k in _COLUMN_MAP.keys() if not k.startswith("_")]


class MPFSParseError(Exception):
    pass


def parse_mpfs_file(filepath: Path, calendar_year: int | None = None) -> list[dict]:
    """
    calendar_year is passed in from the caller (the MPFS file name/detail
    page gives us the year the same way CLFS's does) and used as a fallback
    if the file itself has no "Year" column.
    """
    filepath = Path(filepath)
    try:
        rows = common.read_rows(filepath)
    except ValueError as exc:
        raise MPFSParseError(str(exc)) from exc

    header_index, data_start = common.find_header_row(
        rows, _COLUMN_MAP, _LOGICAL_FIELDS, required_field="hcpcs_code"
    )
    data_rows = rows[data_start:]

    if not header_index:
        fallback = _COLUMN_MAP["_fallback_positional_map"]
        header_index = {k: v for k, v in fallback.items() if not k.startswith("_") and v is not None and v >= 0}
        logger.warning(
            "No header row detected/matched in %s — using the fallback positional "
            "mapping from config/mpfs_column_map.json. Verify this is correct for a new file layout.",
            filepath.name,
        )

    if "hcpcs_code" not in header_index:
        raise MPFSParseError(
            f"Could not identify the HCPCS code column in {filepath.name}. "
            "Add the real header text to config/mpfs_column_map.json under 'hcpcs_code'."
        )

    records = []
    for row in data_rows:
        if not row or not any(cell.strip() for cell in row):
            continue

        def get(field):
            idx = header_index.get(field)
            return row[idx].strip() if idx is not None and idx < len(row) else ""

        code = get("hcpcs_code").upper()
        if not code:
            continue

        year_raw = get("year")
        year = int(year_raw) if year_raw.isdigit() else calendar_year

        records.append(
            {
                "hcpcs_code": code,
                "carrier_id": get("carrier_id") or None,
                "locality": get("locality") or None,
                "modifier": get("modifier").upper(),
                "year": year,
                "non_facility_price": common.parse_price(get("non_facility_price")),
                "facility_price": common.parse_price(get("facility_price")) or None,
                "status_code": get("status_code"),
                "description": get("description"),
            }
        )

    if not records:
        raise MPFSParseError(f"Parsed {filepath.name} but found zero valid data rows.")

    logger.info("Parsed %d records from %s", len(records), filepath.name)
    return records
