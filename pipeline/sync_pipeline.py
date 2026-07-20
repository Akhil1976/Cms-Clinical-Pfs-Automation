"""
sync_pipeline.py
------------------
The orchestration layer. Handles BOTH fee schedules (CLFS and MPFS) through
the same shape of pipeline: download -> parse -> map to standard columns ->
upsert into the one shared `fee_schedule_pricing` table -> notify.

Entry points:

  process_clfs_file(file_code)   -> download+parse+store ONE named CLFS file.
  process_mpfs_file(file_code)   -> download+parse+store ONE named MPFS file.
  process_file(...)              -> back-compat alias for process_clfs_file,
                                     kept so nothing that imported the old
                                     name breaks.

  run_check_for_new_files()      -> checks BOTH CLFS and MPFS listing pages,
                                     compares each against processed_files,
                                     and processes anything new. This is
                                     what the scheduler calls every N days.

Only two small "map raw parser output -> standard column dict" functions
were added (_map_clfs_record / _map_mpfs_record) — everything else
(scraper, parser, db, notifier) is reused as-is per file type.
"""

import logging
import re

from db import database
from notifier import email_notifier
from scraper import cms_scraper, mpfs_scraper
from parser import clfs_parser, mpfs_parser

logger = logging.getLogger("clfs.pipeline")


# ---------------------------------------------------------------------------
# Mapping: raw parser output -> standardized fee_schedule_pricing columns
# ---------------------------------------------------------------------------
def _map_clfs_record(rec: dict) -> dict:
    """
    CLFS doesn't have carrier/locality/facility-price data.

    mdcr_carrier_id and mdcr_fee_schd_id are set to the sentinel string
    'NA' (not NULL) specifically so these two columns can participate in a
    real database UNIQUE constraint — SQL never matches NULL to NULL, so a
    UNIQUE(..., mdcr_carrier_id, mdcr_fee_schd_id) constraint would silently
    let duplicate CLFS rows through if these were left NULL. 'NA' is a
    documented placeholder meaning "this concept doesn't apply to CLFS",
    not a real carrier/locality value.

    pos_fee_schd_price is a genuine PRICE column (not a key column), so it
    stays NULL — inventing a fake price would misrepresent real data.

    Note: the parser also extracts description/effective_date/termination_date
    from the raw CLFS file, but fee_schedule_pricing was trimmed down to the
    8 standard columns only, so those three are intentionally not included
    in this mapping — they simply aren't persisted anywhere anymore.
    """
    return {
        "procedure_code": rec["hcpcs_code"],
        "pcd_modifier": rec["modifier"],
        "mdcr_carrier_id": "NA",
        "mdcr_fee_schd_id": "NA",
        "fee_schd_price": rec["price"],
        "pos_fee_schd_price": None,
        "fee_schd_type_code": rec["indicator"],
    }


def _map_mpfs_record(rec: dict) -> dict:
    """MPFS -> the 8 standard fee_schedule_pricing columns."""
    return {
        "procedure_code": rec["hcpcs_code"],
        "pcd_modifier": rec["modifier"],
        "mdcr_carrier_id": rec["carrier_id"],
        "mdcr_fee_schd_id": rec["locality"],
        "fee_schd_price": rec["non_facility_price"],
        "pos_fee_schd_price": rec["facility_price"],
        "fee_schd_type_code": rec["status_code"],
    }


# ---------------------------------------------------------------------------
# CLFS
# ---------------------------------------------------------------------------
def process_clfs_file(file_code: str, detail_url: str | None = None, description: str = "",
                       calendar_year: int | None = None, quarter: str | None = None) -> dict:
    """Runs the full CLFS pipeline for one specific file code, e.g. '25CLABQ1'."""
    file_code = file_code.upper()

    if detail_url is None:
        all_files = cms_scraper.list_available_files()
        match = next((f for f in all_files if f["file_code"] == file_code), None)
        if match is None:
            raise ValueError(f"{file_code} was not found on the CMS CLFS Files page right now.")
        detail_url, description = match["detail_url"], match["description"]
        calendar_year, quarter = match["calendar_year"], match["quarter"]

    if database.is_file_already_processed(file_code):
        logger.info("%s was already processed previously — re-downloading to check for silent corrections.", file_code)

    try:
        zip_path = cms_scraper.download_file(file_code, detail_url)
        data_file, _all_extracted = cms_scraper.unzip_and_locate_data_file(zip_path)
        raw_records = clfs_parser.parse_clfs_file(data_file)

        year_for_db = calendar_year or _infer_year_from_code(file_code)
        return _finish_processing("CLFS", file_code, year_for_db, quarter, description,
                                   [_map_clfs_record(r) for r in raw_records])
    except cms_scraper.CMSFileUnavailable as exc:
        year_for_db = calendar_year or _infer_year_from_code(file_code)
        database.record_processed_file(
            file_code, "CLFS", year_for_db, quarter, description, 0, "", status="unavailable"
        )
        logger.warning("Skipping unavailable historical CLFS file %s: %s", file_code, exc)
        return {"file_code": file_code, "source_file": "CLFS", "status": "unavailable", "summary": None}
    except Exception as exc:  # noqa: BLE001 - we want to email unexpected failures, then re-raise
        logger.exception("Failed processing CLFS file %s", file_code)
        email_notifier.notify_error(file_code, exc)
        raise


# Back-compat alias — older code (and main.py before this change) calls process_file().
process_file = process_clfs_file


def _infer_year_from_code(file_code: str) -> int:
    """'25CLABQ1' -> 2025. Fallback for when the listing page didn't give us a year."""
    prefix = file_code[:2]
    if prefix.isdigit():
        return 2000 + int(prefix)
    raise ValueError(f"Could not infer calendar year from file code {file_code!r}")


def _infer_mpfs_year_from_code(file_code: str) -> int:
    """
    'PFREV24A' -> 2024, 'PFREV21B' -> 2021, 'PFALL24' -> 2024.

    Unlike CLFS codes, MPFS codes don't start with the year — it's embedded
    after a letter prefix (PFREV/PFALL/etc). Fallback for when the listing
    page's year column couldn't be parsed either.
    """
    match = re.search(r"(\d{2})", file_code)
    if match:
        return 2000 + int(match.group(1))
    raise ValueError(f"Could not infer calendar year from MPFS file code {file_code!r}")


# ---------------------------------------------------------------------------
# MPFS
# ---------------------------------------------------------------------------
def process_mpfs_file(file_code: str, detail_url: str | None = None, description: str = "",
                       calendar_year: int | None = None) -> dict:
    """Runs the full MPFS pipeline for one specific file code, e.g. 'PFREV26C'."""
    file_code = file_code.upper()

    if detail_url is None:
        all_files = mpfs_scraper.list_available_files()
        match = next((f for f in all_files if f["file_code"] == file_code), None)
        if match is None:
            raise ValueError(f"{file_code} was not found on the MPFS listing page right now.")
        detail_url, description = match["detail_url"], match["description"]
        calendar_year = match["calendar_year"]

    if database.is_file_already_processed(file_code):
        logger.info("%s was already processed previously — re-downloading to check for silent corrections.", file_code)

    try:
        zip_path = mpfs_scraper.download_file(file_code, detail_url)
        data_file, _all_extracted = mpfs_scraper.unzip_and_locate_mpfs_data_file(zip_path)
        raw_records = mpfs_parser.parse_mpfs_file(data_file, calendar_year=calendar_year)

        # Every parsed row carries its own year if the file had a Year column;
        # fall back to the file-level year (from the listing page), and if
        # that's also missing (listing page year column didn't parse, e.g.
        # for codes like PFREV24A/PFREV21B), infer it from the file code itself.
        year_for_db = calendar_year or (raw_records[0]["year"] if raw_records else None)
        if year_for_db is None:
            year_for_db = _infer_mpfs_year_from_code(file_code)

        return _finish_processing("MPFS", file_code, year_for_db, None, description,
                                   [_map_mpfs_record(r) for r in raw_records])
    except mpfs_scraper.MPFSFileUnavailable as exc:
        year_for_db = calendar_year or _infer_mpfs_year_from_code(file_code)
        database.record_processed_file(
            file_code, "MPFS", year_for_db, None, description, 0, "", status="unavailable"
        )
        logger.warning("Skipping unavailable historical MPFS file %s: %s", file_code, exc)
        return {"file_code": file_code, "source_file": "MPFS", "status": "unavailable", "summary": None}
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed processing MPFS file %s", file_code)
        email_notifier.notify_error(file_code, exc)
        raise


# ---------------------------------------------------------------------------
# Shared tail end: dedup check -> upsert -> record -> notify
# ---------------------------------------------------------------------------
def _finish_processing(source_file: str, file_code: str, calendar_year: int, quarter, description: str,
                        standard_records: list[dict]) -> dict:
    content_hash = database.compute_content_hash(standard_records)
    if database.is_duplicate_content(content_hash):
        database.record_processed_file(
            file_code, source_file, calendar_year, quarter, description,
            len(standard_records), content_hash, status="duplicate_skipped",
        )
        email_notifier.notify_no_update_needed(file_code)
        return {"file_code": file_code, "source_file": source_file, "status": "duplicate", "summary": None}

    summary = database.upsert_records(source_file, file_code, calendar_year, standard_records)
    database.record_processed_file(
        file_code, source_file, calendar_year, quarter, description,
        len(standard_records), content_hash, status="processed",
    )
    email_notifier.notify_new_data(file_code, summary)
    logger.info("%s (%s) processed: %s", file_code, source_file, summary)
    return {"file_code": file_code, "source_file": source_file, "status": "processed", "summary": summary}


# ---------------------------------------------------------------------------
# Check for new files across BOTH sources
# ---------------------------------------------------------------------------
def run_check_for_new_files() -> list[str]:
    """
    Compares the live CLFS and MPFS listing pages against processed_files
    and processes anything not yet seen in either one. Returns the list of
    file_codes newly processed (successfully or not) in this run.
    """
    logger.info("Checking CMS.gov for new CLFS and MPFS files...")
    processed_now = []

    # --- CLFS ---
    clfs_live = cms_scraper.list_available_files()
    for f in [f for f in clfs_live if not database.is_file_already_processed(f["file_code"])]:
        try:
            process_clfs_file(
                f["file_code"], detail_url=f["detail_url"], description=f["description"],
                calendar_year=f["calendar_year"], quarter=f["quarter"],
            )
            processed_now.append(f["file_code"])
        except Exception:
            continue  # process_*_file() already emailed the error; keep checking the rest.

    # --- MPFS ---
    mpfs_live = mpfs_scraper.list_available_files()
    for f in [f for f in mpfs_live if not database.is_file_already_processed(f["file_code"])]:
        try:
            process_mpfs_file(
                f["file_code"], detail_url=f["detail_url"], description=f["description"],
                calendar_year=f["calendar_year"],
            )
            processed_now.append(f["file_code"])
        except Exception:
            continue

    email_notifier.notify_check_complete(processed_now)
    return processed_now
