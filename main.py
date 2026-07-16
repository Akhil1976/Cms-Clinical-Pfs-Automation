#!/usr/bin/env python3
"""
main.py
--------
Command-line entry point.

    python main.py list                    # show every CLFS file currently on CMS.gov
    python main.py list --source mpfs       # show every MPFS file currently on CMS.gov
    python main.py sync 25CLABQ1            # download+parse+store ONE CLFS file
    python main.py sync PFREV26C --source mpfs   # download+parse+store ONE MPFS file
    python main.py check                    # look for new files in BOTH CLFS and MPFS
    python main.py schedule                 # start the long-running 3-day scheduler
    python main.py report                   # print what's currently in the database
"""

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config.settings import LOG_DIR
from db import database


def _setup_logging():
    LOG_DIR.mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(LOG_DIR / "clfs.log"),
        ],
    )


def cmd_list(args):
    if args.source == "mpfs":
        from scraper import mpfs_scraper
        for f in mpfs_scraper.list_available_files():
            print(f"{f['file_code']:14} CY{f['calendar_year']}      {f['description'][:90]}")
    else:
        from scraper import cms_scraper
        for f in cms_scraper.list_available_files():
            print(f"{f['file_code']:14} CY{f['calendar_year']} {f['quarter'] or '':4} {f['description'][:90]}")


def cmd_sync(args):
    from pipeline import sync_pipeline
    if args.source == "mpfs":
        result = sync_pipeline.process_mpfs_file(args.file_code)
    else:
        result = sync_pipeline.process_clfs_file(args.file_code)
    print(result)


def cmd_check(_args):
    from pipeline.sync_pipeline import run_check_for_new_files
    found = run_check_for_new_files()
    print(f"Processed {len(found)} new file(s): {found}" if found else "No new files.")


def cmd_schedule(_args):
    from pipeline.scheduler import run_forever
    run_forever()


def cmd_report(_args):
    summary = database.get_summary()
    print(f"Total fee schedule pricing rows: {summary['total_codes']}")
    print("By calendar year (fee_schedule_pricing no longer tags source_file per row):")
    for row in summary["by_year"]:
        print(f"  {row['calendar_year']}: {row['n']} rows")
    print("By source (from processed_files):")
    for row in summary["by_source"]:
        print(f"  {row['source_file']:5} {row['file_count']} file(s), {row['total_records']} record(s)")
    print("Processed files:")
    for row in summary["processed_files"]:
        print(f"  {row['file_code']:14} {row['source_file']:5} {row['status']:18} {row['processed_at']}")


def main():
    parser = argparse.ArgumentParser(description="CMS Fee Schedule (CLFS + MPFS) automation")
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="List every file currently published on CMS.gov for a source")
    p_list.add_argument("--source", choices=["clfs", "mpfs"], default="clfs")
    p_list.set_defaults(func=cmd_list)

    p_sync = sub.add_parser("sync", help="Download, parse, and store one specific file (e.g. 25CLABQ1 or PFREV26C)")
    p_sync.add_argument("file_code")
    p_sync.add_argument("--source", choices=["clfs", "mpfs"], default="clfs")
    p_sync.set_defaults(func=cmd_sync)

    sub.add_parser("check", help="Check CMS.gov for any new CLFS or MPFS file, and process it").set_defaults(func=cmd_check)
    sub.add_parser("schedule", help="Run the check every N days, forever").set_defaults(func=cmd_schedule)
    sub.add_parser("report", help="Print a summary of what's in the database").set_defaults(func=cmd_report)

    args = parser.parse_args()
    _setup_logging()
    database.init_db()
    args.func(args)


if __name__ == "__main__":
    main()
