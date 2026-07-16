"""
scheduler.py
-------------
Runs run_check_for_new_files() every CHECK_INTERVAL_DAYS (default 3).

Two ways to use this in practice:

  1. Keep this process running in the background:
         python main.py schedule
     It sleeps between checks and wakes up every 3 days.

  2. (Recommended for a real server) Don't keep a Python process running at
     all — instead let the OS scheduler call `python main.py check` every
     3 days:
         - Linux/macOS: a cron entry, e.g.  0 6 */3 * *  cd /path/to/project && python main.py check
         - Windows: Task Scheduler, trigger "Daily", recur every 3 days,
           action = run `python main.py check`
     This is more reliable than a long-running loop because it survives
     reboots without needing a process supervisor.

Both call the exact same pipeline code, so behavior is identical either way.
"""

import logging
import time

import schedule

from config.settings import CHECK_INTERVAL_DAYS
from pipeline.sync_pipeline import run_check_for_new_files

logger = logging.getLogger("clfs.scheduler")


def _job():
    try:
        run_check_for_new_files()
    except Exception:
        logger.exception("Scheduled check failed")


def run_forever():
    logger.info("Scheduler starting — checking CMS.gov every %d day(s).", CHECK_INTERVAL_DAYS)
    schedule.every(CHECK_INTERVAL_DAYS).days.do(_job)

    # Run once immediately on startup so you don't wait days to see it work.
    _job()

    while True:
        schedule.run_pending()
        time.sleep(60 * 60)  # check the schedule once an hour
