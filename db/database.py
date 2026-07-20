"""
database.py
------------
SQLite is used instead of a vector DB. This data is fully structured/
relational (a procedure code + modifier + year + carrier/locality uniquely
identifies a row we either insert or update), which is exactly what a
relational table with an explicit lookup key is built for.

*** ONE database, ONE table for BOTH fee schedules ***
Per project requirements, CLFS and MPFS rows both live in the same
`fee_schedule_pricing` table, using a standardized set of column names
that both sources map onto.

*** fee_schedule_pricing now holds ONLY the 8 standardized data columns ***
Per the finalized standard schema, `fee_schedule_pricing` was trimmed down
to exactly these 8 columns (matching the standard column list) — no `id`,
no `description`/date columns, and no bookkeeping columns:

    PROCEDURE_CODE, MDCR_CARRIER_ID, MDCR_FEE_SCHD_ID, PCD_MODIFIER,
    PROCEDURE_FEE_YEAR, FEE_SCHD_PRICE, POS_FEE_SCHD_PRICE, FEE_SCHD_TYPE_CODE

Column meaning per source (see README.md for the full mapping table):

    Standard column       | CLFS meaning        | MPFS meaning
    -----------------------------------------------------------------
    PROCEDURE_CODE         | HCPCS code          | HCPCS code
    PCD_MODIFIER           | Modifier            | Modifier
    PROCEDURE_FEE_YEAR     | Calendar year       | Year
    MDCR_CARRIER_ID        | sentinel 'NA'       | Carrier Number
    MDCR_FEE_SCHD_ID       | sentinel 'NA'       | Locality
    FEE_SCHD_PRICE         | Price               | Non Facility Fee Sched Amt
    POS_FEE_SCHD_PRICE     | NULL (n/a to CLFS)  | Facility Fee Schedule Amt
    FEE_SCHD_TYPE_CODE     | Indicator           | Status Code

Two different conventions are used for CLFS's "not applicable" columns,
on purpose:
  - MDCR_CARRIER_ID / MDCR_FEE_SCHD_ID (KEY columns) use the sentinel
    string 'NA' for CLFS, not NULL. This is a deliberate, documented
    placeholder — not real data — that exists solely so these two columns
    can participate in a real database UNIQUE constraint. SQL never
    matches NULL to NULL, so leaving them NULL would let a real UNIQUE
    constraint silently let duplicate CLFS rows through.
  - POS_FEE_SCHD_PRICE (a genuine PRICE column, not a key column) stays
    NULL for CLFS — there's no uniqueness reason to fake a value here, and
    inventing a fake price would misrepresent real data.

Where did source_file / source_file_code / description / dates /
first_seen_at / last_updated_at go? They're no longer stored per-pricing-
row at all — they were dropped along with the schema trim. Traceability
of *which file* touched the data still lives in `processed_files` (one row
per CMS file, keyed by file_code, with its own `source_file` column
distinguishing 'CLFS'/'MPFS') and in `change_log` (one row per individual
NEW/UPDATED pricing change, also carrying its own `source_file`). Nothing
about "which source" is lost — it just isn't duplicated onto every pricing
row anymore.

Composite key / uniqueness note: since `fee_schedule_pricing` no longer
has a `source_file` column, the key is now
(PROCEDURE_CODE, PCD_MODIFIER, PROCEDURE_FEE_YEAR, MDCR_CARRIER_ID, MDCR_FEE_SCHD_ID)
with no source_file involved. This still safely keeps CLFS and MPFS
rows apart in practice: CLFS always uses carrier/locality = 'NA'/'NA',
while MPFS always uses a real carrier number and locality — so the two
sources' key-spaces never actually overlap.

Business rules implemented here (same as before, just generalized):
  - Same (procedure_code, modifier, year, carrier, locality) already in
    the DB -> UPDATE it.
  - Not present yet -> INSERT (append) it.
  - If an entire incoming file is byte-for-byte identical (by content hash)
    to a file already processed -> skip it and let the caller know so it
    can send a "no update needed" email instead of a "new data" email.
"""

import hashlib
import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from config.settings import DB_PATH

logger = logging.getLogger("clfs.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS fee_schedule_pricing (
    PROCEDURE_CODE      TEXT NOT NULL,
    MDCR_CARRIER_ID     TEXT NOT NULL,   -- MPFS: real carrier number. CLFS: sentinel 'NA' (n/a to CLFS, not a real value)
    MDCR_FEE_SCHD_ID    TEXT NOT NULL,   -- MPFS: real "Locality". CLFS: sentinel 'NA' (n/a to CLFS, not a real value)
    PCD_MODIFIER        TEXT NOT NULL DEFAULT '',
    PROCEDURE_FEE_YEAR  INTEGER NOT NULL,
    FEE_SCHD_PRICE      REAL,      -- CLFS price / MPFS non-facility price
    POS_FEE_SCHD_PRICE  REAL,      -- MPFS facility price; NULL for CLFS (a genuine price column — never invented)
    FEE_SCHD_TYPE_CODE  TEXT,      -- CLFS indicator / MPFS status code
    UNIQUE (PROCEDURE_CODE, PCD_MODIFIER, PROCEDURE_FEE_YEAR, MDCR_CARRIER_ID, MDCR_FEE_SCHD_ID)
);

CREATE TABLE IF NOT EXISTS processed_files (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    file_code       TEXT NOT NULL UNIQUE,
    source_file     TEXT NOT NULL,   -- 'CLFS' or 'MPFS'
    calendar_year   INTEGER,
    quarter         TEXT,
    description     TEXT,
    record_count    INTEGER,
    content_hash    TEXT NOT NULL,
    processed_at    TEXT NOT NULL,
    status          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS change_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    file_code       TEXT NOT NULL,
    source_file     TEXT NOT NULL,   -- 'CLFS' or 'MPFS'
    procedure_code  TEXT NOT NULL,
    pcd_modifier    TEXT NOT NULL DEFAULT '',
    change_type     TEXT NOT NULL,   -- 'NEW' or 'UPDATED'
    old_price       REAL,
    new_price       REAL,
    changed_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pricing_lookup
    ON fee_schedule_pricing(PROCEDURE_CODE, PROCEDURE_FEE_YEAR);
"""


@contextmanager
def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_connection() as conn:
        conn.executescript(SCHEMA)
    logger.info("Database ready at %s", DB_PATH)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def compute_content_hash(records: list[dict]) -> str:
    """
    A stable hash of the record set, used to detect "CMS accidentally
    republished the same file under a new link" per the requirement:
    'if the data is the same you send an email saying no updates required'.

    Works for both standardized CLFS and MPFS record dicts, since both are
    sorted the same way (by procedure_code + modifier).
    """
    canonical = json.dumps(
        sorted(records, key=lambda r: (r["procedure_code"], r["pcd_modifier"])), sort_keys=True
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def is_duplicate_content(content_hash: str) -> bool:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM processed_files WHERE content_hash = ? AND status = 'processed' LIMIT 1",
            (content_hash,),
        ).fetchone()
        return row is not None


def is_file_already_processed(file_code: str) -> bool:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM processed_files WHERE file_code = ? AND status IN ('processed', 'unavailable') LIMIT 1",
            (file_code,),
        ).fetchone()
        return row is not None


def upsert_records(source_file: str, file_code: str, calendar_year: int, records: list[dict]) -> dict:
    """
    Optimized version of upsert_records. Uses in-memory lookup, deduplicates records
    within the same file, and performs bulk operations to prevent IntegrityError
    and maximize performance.
    """
    new_count = 0
    updated_count = 0
    unchanged_count = 0
    now = _now()

    with get_connection() as conn:
        logger.info("Fetching existing records for year %d from database...", calendar_year)
        existing_rows = conn.execute(
            """SELECT PROCEDURE_CODE, PCD_MODIFIER, MDCR_CARRIER_ID, MDCR_FEE_SCHD_ID,
                      FEE_SCHD_PRICE, POS_FEE_SCHD_PRICE, FEE_SCHD_TYPE_CODE
               FROM fee_schedule_pricing
               WHERE PROCEDURE_FEE_YEAR = ?""",
            (calendar_year,)
        ).fetchall()

        # Build a fast lookup dictionary
        lookup = {}
        for row in existing_rows:
            key = (row["PROCEDURE_CODE"], row["PCD_MODIFIER"], row["MDCR_CARRIER_ID"], row["MDCR_FEE_SCHD_ID"])
            lookup[key] = (row["FEE_SCHD_PRICE"], row["POS_FEE_SCHD_PRICE"], row["FEE_SCHD_TYPE_CODE"])

        logger.info("Loaded %d existing records for lookup.", len(lookup))

        # Dictionaries to buffer bulk DB operations and handle duplicates in the same file
        pending_inserts = {}
        pending_updates = {}
        pending_changes = {}  # key -> change log tuple

        for rec in records:
            carrier_id = rec.get("mdcr_carrier_id") or "NA"
            locality_id = rec.get("mdcr_fee_schd_id") or "NA"
            key = (rec["procedure_code"], rec["pcd_modifier"], carrier_id, locality_id)

            existing = lookup.get(key)

            if existing is None:
                # Deduplicates inserts within the same file (keeps the last one)
                pending_inserts[key] = (
                    rec["procedure_code"], carrier_id, locality_id, rec["pcd_modifier"],
                    calendar_year, rec.get("fee_schd_price"), rec.get("pos_fee_schd_price"),
                    rec.get("fee_schd_type_code")
                )
                pending_changes[key] = (
                    file_code, source_file, rec["procedure_code"], rec["pcd_modifier"],
                    'NEW', None, rec.get("fee_schd_price"), now
                )
            else:
                existing_price, existing_pos_price, existing_type = existing
                changed = (
                    existing_price != rec.get("fee_schd_price")
                    or existing_pos_price != rec.get("pos_fee_schd_price")
                    or existing_type != rec.get("fee_schd_type_code")
                )
                if changed:
                    # Deduplicates updates within the same file (keeps the last one)
                    pending_updates[key] = (
                        rec.get("fee_schd_type_code"), rec.get("fee_schd_price"), rec.get("pos_fee_schd_price"),
                        rec["procedure_code"], rec["pcd_modifier"], calendar_year,
                        carrier_id, locality_id
                    )
                    pending_changes[key] = (
                        file_code, source_file, rec["procedure_code"], rec["pcd_modifier"],
                        'UPDATED', existing_price, rec.get("fee_schd_price"), now
                    )
                else:
                    # If it wasn't modified from the database, but was previously updated/inserted
                    # by another record in the same file, keep unchanged count correct.
                    if key not in pending_inserts and key not in pending_updates:
                        unchanged_count += 1

        # Calculate final counts
        new_count = len(pending_inserts)
        updated_count = len(pending_updates)

        # Convert dict values to lists for SQL execution
        insert_pricing = list(pending_inserts.values())
        update_pricing = list(pending_updates.values())
        insert_changes = list(pending_changes.values())

        # Execute bulk operations
        if insert_pricing:
            logger.info("Bulk inserting %d new pricing records...", len(insert_pricing))
            conn.executemany(
                """INSERT INTO fee_schedule_pricing
                   (PROCEDURE_CODE, MDCR_CARRIER_ID, MDCR_FEE_SCHD_ID, PCD_MODIFIER,
                    PROCEDURE_FEE_YEAR, FEE_SCHD_PRICE, POS_FEE_SCHD_PRICE, FEE_SCHD_TYPE_CODE)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                insert_pricing
            )
        if update_pricing:
            logger.info("Bulk updating %d existing pricing records...", len(update_pricing))
            conn.executemany(
                """UPDATE fee_schedule_pricing
                   SET FEE_SCHD_TYPE_CODE = ?, FEE_SCHD_PRICE = ?, POS_FEE_SCHD_PRICE = ?
                   WHERE PROCEDURE_CODE = ? AND PCD_MODIFIER = ? AND PROCEDURE_FEE_YEAR = ?
                     AND MDCR_CARRIER_ID = ? AND MDCR_FEE_SCHD_ID = ?""",
                update_pricing
            )
        if insert_changes:
            logger.info("Bulk inserting %d change log entries...", len(insert_changes))
            conn.executemany(
                """INSERT INTO change_log (file_code, source_file, procedure_code, pcd_modifier,
                                            change_type, old_price, new_price, changed_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                insert_changes
            )

    return {"new": new_count, "updated": updated_count, "unchanged": unchanged_count, "total": len(records)}


def record_processed_file(file_code, source_file, calendar_year, quarter, description, record_count, content_hash, status="processed"):
    with get_connection() as conn:
        conn.execute(
            """INSERT INTO processed_files (file_code, source_file, calendar_year, quarter, description,
                                             record_count, content_hash, processed_at, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(file_code) DO UPDATE SET
                   record_count = excluded.record_count,
                   content_hash = excluded.content_hash,
                   processed_at = excluded.processed_at,
                   status = excluded.status""",
            (file_code, source_file, calendar_year, quarter, description, record_count, content_hash, _now(), status),
        )


def get_summary() -> dict:
    """
    fee_schedule_pricing no longer has its own source_file column, so a
    per-source breakdown of pricing rows isn't directly queryable from that
    table alone anymore. Total row count and by-year counts still come
    straight from fee_schedule_pricing; the per-source view is provided via
    `processed_files` (one row per CMS file, each still tagged CLFS/MPFS),
    which is where "how many files/records came from which source" now
    lives.
    """
    with get_connection() as conn:
        total_codes = conn.execute("SELECT COUNT(*) FROM fee_schedule_pricing").fetchone()[0]
        by_year = conn.execute(
            """SELECT PROCEDURE_FEE_YEAR AS calendar_year, COUNT(*) AS n
               FROM fee_schedule_pricing
               GROUP BY PROCEDURE_FEE_YEAR
               ORDER BY PROCEDURE_FEE_YEAR"""
        ).fetchall()
        by_source = conn.execute(
            """SELECT source_file, COUNT(*) AS file_count, COALESCE(SUM(record_count), 0) AS total_records
               FROM processed_files
               WHERE status = 'processed'
               GROUP BY source_file"""
        ).fetchall()
        files = conn.execute(
            "SELECT file_code, source_file, record_count, processed_at, status FROM processed_files ORDER BY processed_at"
        ).fetchall()
        return {
            "total_codes": total_codes,
            "by_year": [dict(r) for r in by_year],
            "by_source": [dict(r) for r in by_source],
            "processed_files": [dict(r) for r in files],
        }
