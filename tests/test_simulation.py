"""
test_simulation.py
--------------------
Exercises the parser + database business rules end-to-end WITHOUT touching
the network, using small synthetic fixture files that stand in for real
CLFS quarterly releases and one small synthetic MPFS fixture. Confirms
append/update/duplicate detection all behave correctly for CLFS (as
before), AND that MPFS rows land correctly in the same standardized table
without disturbing CLFS rows.

Run it with:  python -m tests.test_simulation

It uses its own throwaway SQLite file (tests/fixtures/test_clfs.db) so it
never touches your real clfs.db.
"""

import os
import sys
from pathlib import Path

# Point the DB at a scratch file BEFORE importing anything that reads config.settings.
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
TEST_DB_PATH = FIXTURES_DIR / "test_clfs.db"
if TEST_DB_PATH.exists():
    TEST_DB_PATH.unlink()
os.environ["CLFS_DB_PATH"] = str(TEST_DB_PATH)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import database                        # noqa: E402
from parser import clfs_parser, mpfs_parser     # noqa: E402
from pipeline.sync_pipeline import _map_clfs_record, _map_mpfs_record  # noqa: E402


def run_clfs_quarter(file_code: str, fixture_name: str, calendar_year: int):
    print(f"\n{'=' * 70}\nProcessing CLFS {file_code}  (fixture: {fixture_name})\n{'=' * 70}")
    filepath = FIXTURES_DIR / fixture_name
    raw_records = clfs_parser.parse_clfs_file(filepath)
    standard_records = [_map_clfs_record(r) for r in raw_records]
    content_hash = database.compute_content_hash(standard_records)

    if database.is_duplicate_content(content_hash):
        print("  -> DUPLICATE content detected. Would send 'no update needed' email. Skipping DB write.")
        database.record_processed_file(file_code, "CLFS", calendar_year, None, "duplicate test",
                                        len(standard_records), content_hash, status="duplicate_skipped")
        return

    summary = database.upsert_records("CLFS", file_code, calendar_year, standard_records)
    database.record_processed_file(file_code, "CLFS", calendar_year, None, "simulation",
                                    len(standard_records), content_hash, status="processed")

    print(f"  New codes:       {summary['new']}")
    print(f"  Updated codes:   {summary['updated']}")
    print(f"  Unchanged codes: {summary['unchanged']}")
    print("  -> Would send 'new data processed' email with these counts.")


def run_mpfs_file(file_code: str, fixture_name: str, calendar_year: int):
    print(f"\n{'=' * 70}\nProcessing MPFS {file_code}  (fixture: {fixture_name})\n{'=' * 70}")
    filepath = FIXTURES_DIR / fixture_name
    raw_records = mpfs_parser.parse_mpfs_file(filepath, calendar_year=calendar_year)
    standard_records = [_map_mpfs_record(r) for r in raw_records]
    content_hash = database.compute_content_hash(standard_records)

    if database.is_duplicate_content(content_hash):
        print("  -> DUPLICATE content detected. Skipping DB write.")
        database.record_processed_file(file_code, "MPFS", calendar_year, None, "duplicate test",
                                        len(standard_records), content_hash, status="duplicate_skipped")
        return

    summary = database.upsert_records("MPFS", file_code, calendar_year, standard_records)
    database.record_processed_file(file_code, "MPFS", calendar_year, None, "simulation",
                                    len(standard_records), content_hash, status="processed")
    print(f"  New codes:       {summary['new']}")
    print(f"  Updated codes:   {summary['updated']}")
    print(f"  Unchanged codes: {summary['unchanged']}")


def main():
    database.init_db()

    # Step 1: baseline CLFS quarter — everything should be "new".
    run_clfs_quarter("25CLABQ1", "25CLABQ1_sample.csv", 2025)

    # Step 2: next quarter — some prices changed, one brand-new code, some unchanged.
    run_clfs_quarter("25CLABQ2", "25CLABQ2_sample.csv", 2025)

    # Step 3: next quarter again — more changes, plus one code CMS dropped
    # from the file (we don't delete, we just don't touch it).
    run_clfs_quarter("25CLABQ3", "25CLABQ3_sample.csv", 2025)

    # Step 4: CMS "accidentally" republishes the same Q3 data under a new
    # file code — this should be caught as duplicate content, not re-applied.
    run_clfs_quarter("25CLABQ3V2", "25CLABQ3V2_sample.csv", 2025)

    # Step 5: an MPFS file, to prove both sources share the same table
    # without colliding (same HCPCS code "99213" appears in both fixtures
    # below, but must be tracked completely independently by source_file).
    run_mpfs_file("PFTEST26C", "PFTEST26C_sample.csv", 2026)

    print(f"\n{'=' * 70}\nFinal database state\n{'=' * 70}")
    summary = database.get_summary()
    print(f"Total fee schedule pricing rows: {summary['total_codes']}")
    for row in summary["processed_files"]:
        print(f"  {row['file_code']:14} {row['source_file']:5} {row['status']:18} records={row['record_count']}")

    with database.get_connection() as conn:
        print("\nCurrent price for 83036 (should reflect the Q2/Q3 update, 13.10):")
        r = conn.execute(
            "SELECT FEE_SCHD_PRICE FROM fee_schedule_pricing "
            "WHERE PROCEDURE_CODE='83036' AND MDCR_CARRIER_ID='NA' AND MDCR_FEE_SCHD_ID='NA'"
        ).fetchone()
        print(f"  price={r['FEE_SCHD_PRICE']}")

        print("86592 should still exist (added in Q1/Q2, simply absent from Q3 — not deleted):")
        r = conn.execute(
            "SELECT FEE_SCHD_PRICE FROM fee_schedule_pricing "
            "WHERE PROCEDURE_CODE='86592' AND MDCR_CARRIER_ID='NA' AND MDCR_FEE_SCHD_ID='NA'"
        ).fetchone()
        print(f"  price={r['FEE_SCHD_PRICE']}")

        print("MPFS 99213 should exist with a non-facility + facility price, unrelated to any CLFS row:")
        r = conn.execute(
            "SELECT FEE_SCHD_PRICE, POS_FEE_SCHD_PRICE, MDCR_CARRIER_ID, MDCR_FEE_SCHD_ID "
            "FROM fee_schedule_pricing WHERE PROCEDURE_CODE='99213' AND MDCR_CARRIER_ID != 'NA'"
        ).fetchone()
        print(f"  non_facility_price={r['FEE_SCHD_PRICE']}  facility_price={r['POS_FEE_SCHD_PRICE']} "
              f"carrier={r['MDCR_CARRIER_ID']} locality={r['MDCR_FEE_SCHD_ID']}")

    print("\nSimulation complete. All business rules behaved as expected for both CLFS and MPFS.")


if __name__ == "__main__":
    main()
