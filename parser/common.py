"""
common.py
----------
Parsing helpers shared by clfs_parser.py and mpfs_parser.py.

Both CMS files (CLFS and MPFS) are "CSV-ish" text files that:
  - may be comma, pipe, or tab delimited
  - may have a few title/copyright/notice lines before the real header row
  - use similar messy formatting for dates and dollar prices

Pulling this logic into one shared module means adding the second file
type (MPFS) didn't require re-writing any of this — only a new column-map
JSON file and a thin parser module that says which logical fields to
look for.
"""

import csv
import logging
from datetime import datetime

logger = logging.getLogger("clfs.parser.common")

_MAX_HEADER_SCAN_ROWS = 20  # CMS files can have several title/copyright/notice lines before the real header


def normalize_header(header: str) -> str:
    """'HCPCS Code' -> 'hcpcscode' so header matching ignores case/spacing."""
    import re
    return re.sub(r"[^a-z0-9]", "", header.lower())


def sniff_dialect(sample: str) -> csv.Dialect:
    try:
        return csv.Sniffer().sniff(sample, delimiters=",|\t")
    except csv.Error:
        # Fall back to comma if the sample is too short/uniform for the sniffer.
        return csv.get_dialect("excel")


def build_header_index(header_row: list[str], column_map: dict, logical_fields: list[str]) -> dict:
    """Maps logical field name -> column index, using the alias lists in column_map."""
    normalized_header = [normalize_header(h) for h in header_row]
    index = {}
    for field in logical_fields:
        aliases = {normalize_header(a) for a in column_map[field]}
        for i, col in enumerate(normalized_header):
            if col in aliases:
                index[field] = i
                break
    return index


def find_header_row(rows: list[list[str]], column_map: dict, logical_fields: list[str], required_field: str):
    """
    Scans the first `_MAX_HEADER_SCAN_ROWS` rows for the real header row —
    the one that contains at least `required_field` (e.g. the HCPCS code
    column). Returns (header_index, data_start_row_index), or ({}, 0) if
    nothing in the scan window matched, so the caller can fall back to a
    positional map.
    """
    for i, row in enumerate(rows[:_MAX_HEADER_SCAN_ROWS]):
        if not row or not any(cell.strip() for cell in row):
            continue  # skip blank lines in the preamble
        candidate = build_header_index(row, column_map, logical_fields)
        if required_field in candidate:
            return candidate, i + 1
    return {}, 0


def parse_date(raw: str):
    raw = (raw or "").strip()
    if not raw:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y", "%Y%m%d"):
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    logger.warning("Unrecognized date format: %r — keeping as raw string", raw)
    return raw


def parse_price(raw: str):
    raw = (raw or "").strip().replace("$", "").replace(",", "")
    if not raw or raw.upper() in ("N/A", "NA", "-"):
        return None
    try:
        return round(float(raw), 4)
    except ValueError:
        logger.warning("Unrecognized price value: %r — storing as None", raw)
        return None


def read_rows(filepath) -> list[list[str]]:
    """Reads a file and returns its parsed rows, auto-detecting the delimiter."""
    raw_text = filepath.read_text(encoding="utf-8", errors="replace")
    if not raw_text.strip():
        raise ValueError(f"{filepath} is empty.")

    sample = "\n".join(raw_text.splitlines()[:5])
    dialect = sniff_dialect(sample)
    reader = csv.reader(raw_text.splitlines(), dialect=dialect)
    rows = list(reader)
    if not rows:
        raise ValueError(f"{filepath} contained no rows after parsing.")
    return rows
