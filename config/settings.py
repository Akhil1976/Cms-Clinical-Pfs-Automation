"""
settings.py
-----------
Single place for every constant/setting the project uses.

Nothing here talks to the network or a database — it just reads
environment variables (via a local .env file, see .env.example)
and exposes plain Python values that the other modules import.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# Load variables from a .env file if one exists (created by the user from .env.example)
load_dotenv()

# ---------------------------------------------------------------------------
# Project paths
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DOWNLOAD_DIR = DATA_DIR / "downloads"       # raw zip + unzipped files land here (the "local repo")
LOG_DIR = BASE_DIR / "logs"
DB_PATH = os.getenv("CLFS_DB_PATH", str(BASE_DIR / "clfs.db"))
COLUMN_MAP_PATH = BASE_DIR / "config" / "column_map.json"
MPFS_COLUMN_MAP_PATH = BASE_DIR / "config" / "mpfs_column_map.json"

for _dir in (DATA_DIR, DOWNLOAD_DIR, LOG_DIR):
    _dir.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# CMS.gov — real, public site. No proxies / mirrors are used anywhere.
# ---------------------------------------------------------------------------
CMS_BASE_URL = "https://www.cms.gov"
CLFS_FILES_LIST_URL = f"{CMS_BASE_URL}/medicare/payment/fee-schedules/clinical-laboratory-fee-schedule-clfs/files"

# Physician Fee Schedule (MPFS) — "PFS National Payment Amount File" listing page.
MPFS_FILES_LIST_URL = f"{CMS_BASE_URL}/medicare/payment/fee-schedules/physician/national-payment-amount-file"

# A realistic desktop User-Agent. CMS.gov does not require this, but some
# government sites reject requests that look like default python-requests.
HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}
REQUEST_TIMEOUT = 30  # seconds

# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------
# "Read the website at least 3 days a week" -> check every 3 days by default.
CHECK_INTERVAL_DAYS = int(os.getenv("CLFS_CHECK_INTERVAL_DAYS", "3"))

# ---------------------------------------------------------------------------
# Email notifications
# ---------------------------------------------------------------------------
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USERNAME = os.getenv("SMTP_USERNAME", "")      # e.g. your Gmail address
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")      # Gmail "App Password", not your login password
NOTIFY_EMAIL_TO = os.getenv("NOTIFY_EMAIL_TO", "")  # where alerts get sent (Rasul's / your own inbox)
NOTIFY_EMAIL_FROM = os.getenv("NOTIFY_EMAIL_FROM", SMTP_USERNAME)

EMAIL_ENABLED = bool(SMTP_USERNAME and SMTP_PASSWORD and NOTIFY_EMAIL_TO)

# ---------------------------------------------------------------------------
# Data retention
# ---------------------------------------------------------------------------
# "Storing data for 24, 25 is really mandatory... so we can validate against
# the old data." Keep this many years of quarterly data in the DB/repo.
RETENTION_YEARS = int(os.getenv("CLFS_RETENTION_YEARS", "3"))
