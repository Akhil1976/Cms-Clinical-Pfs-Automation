"""
clfs_parser.py
---------------
Turns a downloaded CLFS data file (CSV/pipe/tab delimited, with or without
a header row) into a list of normalized dicts:

    {
        "hcpcs_code": "80053",
        "modifier": "",
        "description": "Comprehensive metabolic panel",
        "effective_date": "2026-01-01",
        "termination_date": None,
        "indicator": "N",
        "price": 12.34,
    }

The column-name aliases live in config/column_map.json (not in this file),
so CMS renaming a header doesn't require touching code.

The low-level "sniff the delimiter / find the header row / parse a date or
price" logic lives in parser/common.py and is shared with mpfs_parser.py.
"""

import json
import logging
from pathlib import Path

from config.settings import COLUMN_MAP_PATH
from parser import common

logger = logging.getLogger("clfs.parser")

with open(COLUMN_MAP_PATH, encoding="utf-8") as f:
    _COLUMN_MAP = json.load(f)

_LOGICAL_FIELDS = [k for k in _COLUMN_MAP.keys() if not k.startswith("_")]


class CLFSParseError(Exception):
    pass


def parse_clfs_file(filepath: Path) -> list[dict]:
    filepath = Path(filepath)
    try:
        rows = common.read_rows(filepath)
    except ValueError as exc:
        raise CLFSParseError(str(exc)) from exc

    header_index, data_start = common.find_header_row(
        rows, _COLUMN_MAP, _LOGICAL_FIELDS, required_field="hcpcs_code"
    )
    data_rows = rows[data_start:]

    if not header_index:
        fallback = _COLUMN_MAP["_fallback_positional_map"]
        header_index = {k: v for k, v in fallback.items() if not k.startswith("_") and v is not None and v >= 0}
        logger.warning(
            "No header row detected/matched in %s — using the fallback positional "
            "mapping from column_map.json. Verify this is correct for a new file layout.",
            filepath.name,
        )

    if "hcpcs_code" not in header_index:
        raise CLFSParseError(
            f"Could not identify the HCPCS code column in {filepath.name}. "
            "Add the real header text to config/column_map.json under 'hcpcs_code'."
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

        records.append(
            {
                "hcpcs_code": code,
                "modifier": get("modifier").upper(),
                "description": get("description"),
                "effective_date": common.parse_date(get("effective_date")),
                "termination_date": common.parse_date(get("termination_date")),
                "indicator": get("indicator"),
                "price": common.parse_price(get("price")),
            }
        )

    if not records:
        raise CLFSParseError(f"Parsed {filepath.name} but found zero valid data rows.")

    logger.info("Parsed %d records from %s", len(records), filepath.name)
    return records
