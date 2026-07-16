#!/usr/bin/env python3
"""
migrate_legacy_db.py
----------------------
One-time script to migrate an existing clfs.db (from before MPFS support
was added) into the new standardized `fee_schedule_pricing` table.

The OLD schema had a CLFS-only table called `hcpcs_pricing`. The current
schema has one shared table, `fee_schedule_pricing`, used by both CLFS and
MPFS, trimmed down to its 8 final standard columns (no id, no
description/date columns, no source_file/source_file_code/timestamps —
see db/database.py's module docstring for the full rationale). This
script copies every row from the old table into the new one, using the
sentinel 'NA' for MPFS-only key columns (mdcr_carrier_id, mdcr_fee_schd_id)
and leaving pos_fee_schd_price NULL, since CLFS never had that data to
begin with.

Usage:
    python migrate_legacy_db.py

It's safe to run more than once — since fee_schedule_pricing no longer
carries a source_file_code column, "already migrated" is instead checked
per-row via the table's own UNIQUE constraint (INSERT OR IGNORE), and it
skips entirely if the old `hcpcs_pricing` table doesn't exist (e.g. on a
brand-new install that never had the old schema).
"""

import sqlite3

from config.settings import DB_PATH
from db import database


def _table_exists(conn, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table_name,)
    ).fetchone()
    return row is not None


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    if not _table_exists(conn, "hcpcs_pricing"):
        print("No legacy 'hcpcs_pricing' table found — nothing to migrate.")
        conn.close()
        return

    # Make sure the current schema (fee_schedule_pricing, processed_files,
    # change_log) exists before we start copying into it.
    database.init_db()

    # init_db() uses CREATE TABLE IF NOT EXISTS, so an old processed_files /
    # change_log table (from before MPFS support) is left as-is and won't
    # have the new source_file column yet — add it if missing.
    for table in ("processed_files", "change_log"):
        cols = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if "source_file" not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN source_file TEXT NOT NULL DEFAULT 'CLFS'")
    change_log_cols = {row["name"] for row in conn.execute("PRAGMA table_info(change_log)")}
    if "procedure_code" not in change_log_cols:
        conn.execute("ALTER TABLE change_log ADD COLUMN procedure_code TEXT NOT NULL DEFAULT ''")
        conn.execute("UPDATE change_log SET procedure_code = hcpcs_code")
    if "pcd_modifier" not in change_log_cols:
        conn.execute("ALTER TABLE change_log ADD COLUMN pcd_modifier TEXT NOT NULL DEFAULT ''")
        conn.execute("UPDATE change_log SET pcd_modifier = modifier")
    conn.commit()

    old_rows = conn.execute("SELECT * FROM hcpcs_pricing").fetchall()
    migrated = 0
    for r in old_rows:
        cur = conn.execute(
            """INSERT OR IGNORE INTO fee_schedule_pricing
               (PROCEDURE_CODE, MDCR_CARRIER_ID, MDCR_FEE_SCHD_ID, PCD_MODIFIER,
                PROCEDURE_FEE_YEAR, FEE_SCHD_PRICE, POS_FEE_SCHD_PRICE, FEE_SCHD_TYPE_CODE)
               VALUES (?, 'NA', 'NA', ?, ?, ?, NULL, ?)""",
            (
                r["hcpcs_code"], r["modifier"], r["calendar_year"],
                r["price"], r["indicator"],
            ),
        )
        migrated += cur.rowcount

    # Carry over processed_files history too, tagging it as CLFS.
    old_files = conn.execute("SELECT * FROM processed_files").fetchall()
    for f in old_files:
        conn.execute(
            """INSERT OR IGNORE INTO processed_files
               (file_code, source_file, calendar_year, quarter, description,
                record_count, content_hash, processed_at, status)
               VALUES (?, 'CLFS', ?, ?, ?, ?, ?, ?, ?)""",
            (
                f["file_code"], f["calendar_year"], f["quarter"], f["description"],
                f["record_count"], f["content_hash"], f["processed_at"], f["status"],
            ),
        )

    conn.commit()
    print(f"Migrated {migrated} pricing row(s) and {len(old_files)} processed-file record(s) into "
          f"fee_schedule_pricing / processed_files (tagged source_file='CLFS' in processed_files).")
    print("The old 'hcpcs_pricing' table was left in place untouched — safe to drop manually once verified.")
    conn.close()


if __name__ == "__main__":
    main()
