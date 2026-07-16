"""
cms_scraper.py
---------------
Everything that talks to www.cms.gov directly. Nothing in this file uses a
third-party proxy or VPN service — CMS.gov is a public federal website and
is fetched directly over HTTPS.

Three jobs:
1. list_available_files()   -> what quarterly files exist right now
2. download_file(file_code) -> get the zip onto disk, handling the AMA
                                 "click to accept the CPT license" page that
                                 sits in front of every CLFS zip download
3. unzip_and_locate_data_file() -> extract the zip and figure out which of
                                     the extracted files is the actual data
                                     file (as opposed to the layout/readme
                                     text file or the Excel copy)
"""

import logging
import re
import shutil
import subprocess
import zipfile
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

from config.settings import (
    CLFS_FILES_LIST_URL,
    CMS_BASE_URL,
    DOWNLOAD_DIR,
    HTTP_HEADERS,
    REQUEST_TIMEOUT,
)

logger = logging.getLogger("clfs.scraper")


class CMSScraperError(Exception):
    """Raised whenever CMS.gov doesn't respond the way we expect."""

class CMSFileUnavailable(CMSScraperError):
    """Raised when CMS has removed a historical archive from public access."""

def _is_pricing_file_code(file_code: str) -> bool:
    """Exclude CMS change-request/news rows which are not data downloads."""
    return "CLAB" in file_code.upper()


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HTTP_HEADERS)
    return s


# ---------------------------------------------------------------------------
# 1. Discover which files exist
# ---------------------------------------------------------------------------
def list_available_files() -> list[dict]:
    """
    Fetches the CLFS Files page and returns one dict per row, e.g.:

        {
            "file_code": "26CLABQ2",
            "description": "CY 2026 Q2 Release: Updated for April 2026 ...",
            "calendar_year": 2026,
            "quarter": "Q2",
            "detail_url": "https://www.cms.gov/.../files/26clabq2",
        }

    IMPORTANT: file_code naming is NOT consistent across CMS's history and
    is deliberately treated as an opaque label, never parsed for meaning.
    Confirmed examples seen on the live CLFS Files page alone: "26CLABQ3"
    (year-first), "11CLABMAR" (year + letters + month), "09CLAB" (no
    quarter at all), and "CR5362" (letters-first, no "CLAB" substring at
    all). No single regex covers all of these, and a new one could show up
    at any time — so this function does not try.

    Instead, a row is considered valid using only the table's actual
    structure, which CMS already gives us as clean, explicit data:
      1. It has a link to a file detail page.
      2. It has a Calendar Year cell (found by matching the "Calendar Year"
         column header — not by assuming a fixed column position, in case
         CMS reorders the columns).
    That's it. The filename text itself is only ever used as an opaque
    identifier (file_code) for the download step and DB records — never
    inspected for a pattern.

    Quarter (Q1-Q4) has no dedicated column on this page, so it's still
    extracted from the description text on a best-effort basis. It is NOT
    part of what makes a row valid, and is simply None if not found.
    """
    resp = _session().get(CLFS_FILES_LIST_URL, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    all_tables = soup.find_all("table")
    if not all_tables:
        raise CMSScraperError(
            "No table was found on the CLFS Files page. CMS may have changed "
            "the page layout — open the page in a browser and check whether "
            "the file listing is still a table."
        )

    # The page can contain more than one <table> (e.g. layout/filter widgets
    # like the "Show Entries" control), so the first table on the page is
    # not necessarily the file listing. Find the "Calendar Year" column by
    # its header text within EACH table, rather than assuming the first
    # table found is the right one or hardcoding a column index — this
    # survives both CMS reordering columns and adding extra tables.
    table = None
    year_col_index = None
    for candidate_table in all_tables:
        header_row = candidate_table.find("tr")
        if header_row is None:
            continue
        header_cells = header_row.find_all(["th", "td"])
        idx = next(
            (i for i, c in enumerate(header_cells) if "calendar year" in c.get_text(strip=True).lower()),
            None,
        )
        if idx is not None:
            table = candidate_table
            year_col_index = idx
            break

    if table is None:
        raise CMSScraperError(
            "Could not find a 'Calendar Year' column on the CLFS Files page. "
            "CMS may have renamed or removed it — open the page in a browser "
            "and check the table headers."
        )

    files = []
    for row in table.find_all("tr"):
        cells = row.find_all("td")
        if len(cells) <= year_col_index:
            continue  # header row, or a malformed row with fewer cells than expected

        link = row.find("a", href=True)

        # CMS's responsive table markup duplicates the column header text
        # inside every cell for mobile display (e.g. a Calendar Year cell's
        # actual .get_text() is "Calendar Year2026", not just "2026"). So we
        # can't require the cell text to be a clean digit string — instead
        # pull the 4-digit year out with a regex.
        year_cell_text = cells[year_col_index].get_text(strip=True)
        year_match = re.search(r"(19|20)\d{2}", year_cell_text)
        if not (link and year_match):
            continue  # doesn't satisfy either of the two required signals — skip it

        file_code = link.get_text(strip=True).upper()
        if not _is_pricing_file_code(file_code):
            logger.info("Skipping non-pricing CLFS listing row %s.", file_code)
            continue
        # Same duplicated-label issue applies to the description cell (its
        # raw text starts with "Description" before the actual content), so
        # strip any leading label word(s) rather than assuming the text is
        # clean.
        description_raw = cells[1].get_text(" ", strip=True) if len(cells) > 1 else ""
        description = re.sub(r"^(description)\s*", "", description_raw, flags=re.I).strip()
        quarter_match = re.search(r"\bQ([1-4])\b", description, re.I)

        files.append(
            {
                "file_code": file_code,
                "description": description,
                "calendar_year": int(year_match.group(0)),
                "quarter": f"Q{quarter_match.group(1)}" if quarter_match else None,
                "detail_url": urljoin(CMS_BASE_URL, link["href"]),
            }
        )

    if not files:
        raise CMSScraperError(
            "No valid file rows (link + Calendar Year) were found on the CLFS "
            "Files page. CMS may have changed the page layout — open the page "
            "in a browser and check whether the table structure still matches "
            "what this scraper expects."
        )
    return files


# ---------------------------------------------------------------------------
# 2. Download one file, handling the AMA license click-through
# ---------------------------------------------------------------------------
def _find_zip_link(detail_page_html: str) -> str:
    soup = BeautifulSoup(detail_page_html, "html.parser")
    # Modern CMS.gov route (confirmed from live site, July 2026):
    #   cms.gov/license/ama?file=/files/zip/26clabq3.zip
    # The old "license.asp?file=..." path is gone — CMS migrated this flow
    # to Drupal at /license/ama, and the query param's *value* is what ends
    # in .zip, not the href path itself.
    link = soup.find("a", href=re.compile(r"/license/ama\?file=.*\.zip", re.I))
    if not link:
        # Some older/ungated file pages link straight to the zip with no
        # license gate at all.
        link = soup.find("a", href=re.compile(r"\.zip($|\?)", re.I))
    if not link:
        raise CMSScraperError("Could not find a .zip download link on the file's detail page.")
    return urljoin(CMS_BASE_URL, link["href"])


def _is_license_gate_url(url: str) -> bool:
    """
    True if this URL is CMS's AMA license click-through page rather than a
    direct link to the zip itself. Matches the current /license/ama route
    (and keeps the legacy license.asp check around in case CMS ever reverts
    or runs both in parallel during a migration).
    """
    return "/license/ama" in url or "license.asp" in url


def _find_accept_form(soup: BeautifulSoup):
    """
    CMS's AMA license page (confirmed markup, July 2026) renders the two
    buttons as two SEPARATE <form> elements, not one form with two
    same-named buttons:

        <form action="/files/zip/26clabq3.zip" style="display: inline;">
            <input type="hidden" name="agree" value="yes">
            <input type="submit" data-license="ama" ... name="next" value="Accept">
        </form>
        <form action="" style="display: inline;">
            <input type="hidden" name="ReferURL" value="...">
            <input type="submit" ... name="Cancel" value="Don't Accept">
        </form>

    Note neither form has a `method` attribute, so both default to GET.

    Picking "the first form on the page" (soup.find("form")) happens to work
    today because Accept is listed first, but it's one markup reshuffle away
    from silently submitting Cancel instead. Instead we explicitly find the
    form whose submit button is labeled "Accept" (and isn't "Don't Accept"),
    or — even more robustly — the one carrying the `data-license="ama"`
    marker CMS puts on the real accept button.
    """
    for form in soup.find_all("form"):
        submit = form.find("input", attrs={"data-license": "ama"})
        if submit:
            return form, submit
        for candidate in form.find_all("input", {"type": "submit"}):
            label = (candidate.get("value") or "").strip().lower()
            if "accept" in label and "don't" not in label and "dont" not in label:
                return form, candidate
    return None, None


def _accept_ama_license(session: requests.Session, license_url: str) -> requests.Response:
    """
    CLFS zips are gated behind CMS's standard "End User Point and Click
    License Agreement" for AMA CPT content. In a browser this is a page with
    two buttons — "Accept" and "Don't Accept" — that are actually two
    independent forms (see _find_accept_form for the exact markup). We
    build the request from the Accept form specifically, ignoring the
    Cancel form entirely, so there's no chance of the wrong one being
    submitted.
    """
    get_resp = session.get(license_url, timeout=REQUEST_TIMEOUT)
    get_resp.raise_for_status()

    content_type = get_resp.headers.get("Content-Type", "")
    if "zip" in content_type or "octet-stream" in content_type:
        # No license page was actually served — we already have the file.
        return get_resp

    soup = BeautifulSoup(get_resp.text, "html.parser")
    form, accept_button = _find_accept_form(soup)
    if not form:
        raise CMSScraperError(
            "Expected an AMA license 'Accept' form at "
            f"{license_url} but found none. CMS may have changed this flow."
        )

    # form action is a path like "/files/zip/26clabq3.zip" — resolve against
    # the license page URL, not CMS_BASE_URL, in case it's ever relative to
    # something more specific.
    action = urljoin(license_url, form.get("action") or license_url)
    method = (form.get("method") or "GET").upper()  # no method attr -> GET, per HTML spec

    # Only pull fields from THIS form (hidden "agree=yes" plus the Accept
    # submit's own name/value) — never fields from the sibling Cancel form.
    payload = {}
    for field in form.find_all("input"):
        name = field.get("name")
        if name:
            payload[name] = field.get("value", "")

    if method == "POST":
        resp = session.post(action, data=payload, timeout=REQUEST_TIMEOUT)
    else:
        resp = session.get(action, params=payload, timeout=REQUEST_TIMEOUT)

    # Legacy CMS forms add agree/next to an already-direct ZIP URL. Those
    # routes can return 404 with query parameters but work with the accepted
    # session cookies when retried as the bare ZIP URL.
    if resp.status_code == 404 and method == "GET":
        bare_action = urlunsplit((*urlsplit(action)[:3], "", ""))
        logger.info("Legacy AMA acceptance returned 404; retrying %s.", bare_action)
        resp = session.get(bare_action, timeout=REQUEST_TIMEOUT)
    if resp.status_code == 404:
        raise CMSFileUnavailable(
            f"CMS no longer serves the historical archive requested by {license_url}."
        )
    resp.raise_for_status()
    return resp


def download_file(file_code: str, detail_url: str) -> Path:
    """
    Downloads the zip for one quarter's file (e.g. "25CLABQ1") into
    DOWNLOAD_DIR/<file_code>/<file_code>.zip and returns that path.
    """
    session = _session()
    detail_resp = session.get(detail_url, timeout=REQUEST_TIMEOUT)
    detail_resp.raise_for_status()

    zip_link = _find_zip_link(detail_resp.text)
    logger.info("Resolved download link for %s: %s", file_code, zip_link)

    final_resp = (
        _accept_ama_license(session, zip_link)
        if _is_license_gate_url(zip_link)
        else session.get(zip_link, timeout=REQUEST_TIMEOUT)
    )

    if "zip" not in final_resp.headers.get("Content-Type", "") and not final_resp.content[:2] == b"PK":
        raise CMSScraperError(
            f"Response for {file_code} doesn't look like a zip file "
            f"(Content-Type={final_resp.headers.get('Content-Type')!r}). "
            "The AMA license step likely changed — inspect the page manually."
        )

    out_dir = DOWNLOAD_DIR / file_code
    out_dir.mkdir(parents=True, exist_ok=True)
    zip_path = out_dir / f"{file_code}.zip"
    zip_path.write_bytes(final_resp.content)
    logger.info("Saved %s (%d bytes)", zip_path, len(final_resp.content))
    return zip_path


# ---------------------------------------------------------------------------
# 3. Unzip and find the actual data file among layout/readme/excel files
# ---------------------------------------------------------------------------
EXCLUDE_PATTERNS = re.compile(r"(layout|readme|instructions|record[_ ]?spec|ddl)", re.I)
DATA_EXTENSIONS = (".csv", ".txt")

def _safe_extract_zip(zip_path: Path, destination: Path) -> list[Path]:
    """Extract only usable archive members, avoiding legacy Excel compression."""
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    wanted_extensions = {".csv", ".txt", ".zip"}
    try:
        with zipfile.ZipFile(zip_path) as archive:
            members = []
            for member in archive.infolist():
                parts = [part.strip() for part in member.filename.replace("\\", "/").split("/") if part not in ("", ".")]
                if not parts or member.is_dir() or Path(parts[-1]).suffix.lower() not in wanted_extensions:
                    continue
                target = (destination.joinpath(*parts)).resolve()
                try:
                    target.relative_to(root)
                except ValueError as exc:
                    raise CMSScraperError(f"Unsafe path {member.filename!r} found in {zip_path.name}.") from exc
                members.append((member, target))

            for member, target in members:
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
    except NotImplementedError:
        seven_zip = shutil.which("7z")
        if not seven_zip:
            installed = Path(r"C:\Program Files\7-Zip\7z.exe")
            seven_zip = str(installed) if installed.exists() else None
        if seven_zip:
            # Ask 7-Zip only for relevant CMS data/archive members; this also
            # avoids extracting the legacy Excel member that Python cannot read.
            result = subprocess.run(
                [seven_zip, "x", "-y", f"-o{destination}", str(zip_path), "*.csv", "*.txt", "*.zip"],
                capture_output=True, text=True, check=False,
            )
            if result.returncode == 0:
                return [path for path in destination.rglob("*") if path.is_file()]
            detail = result.stderr.strip() or result.stdout.strip() or "unknown 7-Zip error"
        else:
            result = subprocess.run(
                ["tar", "-xf", str(zip_path), "-C", str(destination)],
                capture_output=True, text=True, check=False,
            )
            detail = result.stderr.strip() or result.stdout.strip() or "unknown extractor error"
        raise CMSScraperError(
            f"Could not extract {zip_path.name}; its data member uses unsupported compression: {detail}"
        )
    return [path for path in destination.rglob("*") if path.is_file()]

def unzip_and_locate_data_file(zip_path: Path) -> tuple[Path, list[Path]]:
    """
    Extracts the zip next to itself and returns (data_file_path, all_extracted_paths).

    Heuristic for "which file is the actual data": prefer .csv/.txt files
    whose name doesn't look like a layout/readme/instructions file, and
    among candidates pick the largest (the real data file is always far
    bigger than a one-page layout description).
    """
    extract_dir = zip_path.parent / "extracted"
    if extract_dir.exists():
        shutil.rmtree(extract_dir)
    extracted = _safe_extract_zip(Path(zip_path), extract_dir)

    candidates = [
        p for p in extracted
        if p.suffix.lower() in DATA_EXTENSIONS and not EXCLUDE_PATTERNS.search(p.name)
    ]
    if not candidates:
        # Fall back to any .csv/.txt at all if our naming heuristic excluded everything.
        candidates = [p for p in extracted if p.suffix.lower() in DATA_EXTENSIONS]
    if not candidates:
        raise CMSScraperError(f"No CSV/TXT data file found inside {zip_path.name}. Contents: {[p.name for p in extracted]}")

    data_file = max(candidates, key=lambda p: p.stat().st_size)
    return data_file, extracted