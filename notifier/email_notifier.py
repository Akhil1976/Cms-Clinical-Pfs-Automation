"""
email_notifier.py
------------------
Sends the three notification types the project needs:
  - new data processed (with counts of new/updated codes)
  - duplicate file detected, no update needed
  - a processing error occurred

Uses plain smtplib against Gmail's SMTP server by default. Gmail requires
an "App Password" (not your normal login password) if 2FA is on — see
.env.example for the exact setup steps.
"""

import logging
import smtplib
from email.message import EmailMessage

from config.settings import (
    EMAIL_ENABLED, NOTIFY_EMAIL_FROM, NOTIFY_EMAIL_TO,
    SMTP_HOST, SMTP_PASSWORD, SMTP_PORT, SMTP_USERNAME,
)

logger = logging.getLogger("clfs.notifier")


def _send(subject: str, body: str):
    if not EMAIL_ENABLED:
        logger.warning(
            "Email is not configured (missing SMTP_USERNAME/SMTP_PASSWORD/NOTIFY_EMAIL_TO in .env). "
            "Skipping send. Would have sent:\nSubject: %s\n%s", subject, body,
        )
        return

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = NOTIFY_EMAIL_FROM
    msg["To"] = NOTIFY_EMAIL_TO
    msg.set_content(body)

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls()
        server.login(SMTP_USERNAME, SMTP_PASSWORD)
        server.send_message(msg)
    logger.info("Sent email: %s", subject)


def notify_new_data(file_code: str, summary: dict):
    subject = f"[CLFS] New data processed: {file_code}"
    body = (
        f"File {file_code} was downloaded and loaded into the database.\n\n"
        f"  New HCPCS codes added:     {summary['new']}\n"
        f"  Existing codes updated:    {summary['updated']}\n"
        f"  Unchanged:                 {summary['unchanged']}\n"
        f"  Total records in file:     {summary['total']}\n\n"
        "This file has NOT been validated for production use yet — please "
        "review before relying on it downstream."
    )
    _send(subject, body)


def notify_no_update_needed(file_code: str):
    subject = f"[CLFS] No update needed: {file_code}"
    body = (
        f"CMS published/re-published {file_code}, but its contents are "
        "identical to a file already processed. No changes were made to the database."
    )
    _send(subject, body)


def notify_error(file_code: str, error: Exception):
    subject = f"[CLFS] ERROR processing {file_code}"
    body = f"Processing {file_code} failed with:\n\n{type(error).__name__}: {error}"
    _send(subject, body)


def notify_check_complete(new_files_found: list[str]):
    if not new_files_found:
        subject = "[CLFS] Scheduled check: no new files"
        body = "The scheduled 3-day check ran and found no new CLFS files on CMS.gov."
    else:
        subject = f"[CLFS] Scheduled check: {len(new_files_found)} new file(s) found"
        body = "The scheduled check found and processed:\n" + "\n".join(f"  - {f}" for f in new_files_found)
    _send(subject, body)
