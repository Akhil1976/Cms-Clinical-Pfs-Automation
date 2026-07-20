"""
mpfs_scraper.py
----------------
Everything that talks to the MPFS "PFS National Payment Amount File" pages
on www.cms.gov. Structured exactly like scraper/cms_scraper.py (same three
jobs), but the MPFS pages are simpler than the CLFS ones:

1. list_available_files()        -> what MPFS files exist right now
2. download_file(file_code, url) -> get the zip onto disk. Confirmed from
                                     the live site (July 2026): the file's
                                     detail page has a "Downloads" section
                                     with a direct link straight to the
                                     zip â there is no AMA license
                                     click-through step like CLFS has.
3. unzip_and_locate_mpfs_data_file() -> extract the zip and find the real
                                     data file. MPFS zips sometimes contain
                                     two folders, "QP" and "non-QP" (QP =
                                     Qualifying APM Participant pricing).
                                     Per the project requirements we always
                                     use the non-QP folder, since that's the
                                     standard (non-differential) fee. If
                                     there's no QP/non-QP split, the zip's
                                     top level is used directly.
"""

import logging
import os
import re
import shutil
import subprocess
import zipfile
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from config.settings import (
    CMS_BASE_URL,
    DOWNLOAD_DIR,
    HTTP_HEADERS,
    MPFS_FILES_LIST_URL,
    REQUEST_TIMEOUT,
)

logger = logging.getLogger("clfs.scraper.mpfs")


class MPFSScraperError(Exception):
    """Raised whenever the MPFS pages don't respond the way we expect."""


class MPFSFileUnavailable(MPFSScraperError):
    """Raised when a historical MPFS file is no longer served by CMS."""


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HTTP_HEADERS)
    return s


# ---------------------------------------------------------------------------
# 1. Discover which files exist
# ---------------------------------------------------------------------------
def list_available_files() -> list[dict]:
    """Return every MPFS entry published by CMS, including historical archives."""
    resp = _session().get(MPFS_FILES_LIST_URL, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    files = []
    seen_codes = set()
    for row in soup.find_all("tr"):
        cells = row.find_all("td")
        link = row.find("a", href=True)
        if not cells or not link:
            continue

        # CMS has changed the detail-page URL several times since 2003.
        # File-name text and the table year are stable, so do not filter URLs.
        file_code = re.sub(r"\.zip$", "", link.get_text(" ", strip=True), flags=re.I).strip().upper()
        if not file_code or file_code in seen_codes:
            continue
        row_text = " ".join(cell.get_text(" ", strip=True) for cell in cells)
        year_match = re.search(r"\b(19|20)\d{2}\b", row_text)
        files.append(
            {
                "file_code": file_code,
                "description": f"PFS National Payment Amount File ({file_code})",
                "calendar_year": int(year_match.group(0)) if year_match else None,
                "quarter": None,
                "detail_url": urljoin(CMS_BASE_URL, link["href"]),
            }
        )
        seen_codes.add(file_code)

    if not files:
        raise MPFSScraperError("No linked MPFS file rows were found; CMS may have changed the page layout.")
    return files


# ---------------------------------------------------------------------------
# 2. Download one file  no license gate, just a direct "Downloads" link
# ---------------------------------------------------------------------------
def _find_zip_link(detail_page_html: str) -> str:
    soup = BeautifulSoup(detail_page_html, "html.parser")
    # Confirmed from the live site (July 2026): the detail page has a
    # "Downloads" section containing a direct link to the zip file.
    link = soup.find("a", href=re.compile(r"\.zip($|\?)", re.I))
    if not link:
        raise MPFSScraperError("Could not find a .zip download link on the MPFS file's detail page.")
    return urljoin(CMS_BASE_URL, link["href"])


def download_file(file_code: str, detail_url: str) -> Path:
    """
    Downloads the zip for one MPFS file (e.g. "PFREV26C") into
    DOWNLOAD_DIR/<file_code>/<file_code>.zip and returns that path.
    """
    session = _session()
    try:
        detail_resp = session.get(detail_url, timeout=REQUEST_TIMEOUT)
        detail_resp.raise_for_status()
    except requests.exceptions.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 404:
            raise MPFSFileUnavailable(
                f"CMS no longer serves the historical archive requested by {detail_url}."
            ) from exc
        raise

    try:
        zip_link = _find_zip_link(detail_resp.text)
    except MPFSScraperError as exc:
        raise MPFSFileUnavailable(
            f"Historical MPFS file {file_code} does not contain a data download."
        ) from exc
    logger.info("Resolved MPFS download link for %s: %s", file_code, zip_link)

    if "apps/ama/license.asp" in zip_link:
        from scraper import cms_scraper
        final_resp = cms_scraper._accept_ama_license(session, zip_link)
    else:
        final_resp = session.get(zip_link, timeout=REQUEST_TIMEOUT)
        final_resp.raise_for_status()

    if "zip" not in final_resp.headers.get("Content-Type", "") and final_resp.content[:2] != b"PK":
        raise MPFSScraperError(
            f"Response for {file_code} doesn't look like a zip file "
            f"(Content-Type={final_resp.headers.get('Content-Type')!r})."
        )

    out_dir = DOWNLOAD_DIR / file_code
    out_dir.mkdir(parents=True, exist_ok=True)
    zip_path = out_dir / f"{file_code}.zip"
    zip_path.write_bytes(final_resp.content)
    logger.info("Saved %s (%d bytes)", zip_path, len(final_resp.content))
    return zip_path


# ---------------------------------------------------------------------------
# 3. Unzip and find the actual data file â honoring the QP / non-QP split
# ---------------------------------------------------------------------------
EXCLUDE_PATTERNS = re.compile(r"(layout|readme|instructions|record[_ ]?spec|ddl)", re.I)
DATA_EXTENSIONS = (".csv", ".txt")



def _normalize_paths_on_disk(destination: Path):
    """
    On Windows, external extractors (like tar) can create files or folders
    with trailing spaces or backslashes in their names. Standard Win32 APIs
    used by Python cannot access them, resulting in FileNotFoundError.
    This helper walks the extracted tree bottom-up and renames files/folders
    to clean up any trailing spaces or internal backslashes.
    """
    import os
    dest_str = str(destination.resolve())
    prefix = "\\\\?\\" if os.name == "nt" and not dest_str.startswith("\\\\?\\") else ""
    raw_dest = prefix + dest_str

    for root, dirs, files in os.walk(raw_dest, topdown=False):
        for f in files:
            clean_f = f.strip()
            if clean_f != f or "\\" in f or "/" in f:
                clean_f = clean_f.replace("\\", "/").split("/")[-1]
                old_path = os.path.join(root, f)
                new_path = os.path.join(root, clean_f)
                try:
                    os.rename(old_path, new_path)
                except Exception as e:
                    logger.warning("Failed to rename file %s to %s: %s", old_path, new_path, e)
        for d in dirs:
            clean_d = d.strip()
            if clean_d != d:
                old_path = os.path.join(root, d)
                new_path = os.path.join(root, clean_d)
                try:
                    os.rename(old_path, new_path)
                except Exception as e:
                    logger.warning("Failed to rename directory %s to %s: %s", old_path, new_path, e)


def _safe_extract_zip(zip_path: Path, destination: Path) -> list[Path]:
    """Extract only usable archive members, normalizing malformed CMS paths."""
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    wanted_extensions = {".csv", ".txt", ".zip"}
    try:
        with zipfile.ZipFile(zip_path) as archive:
            members = []
            for member in archive.infolist():
                # CMS has published names such as "PFREV26A \\PFREV26AR_nonQP.zip".
                # Normalize separators and harmless surrounding whitespace first.
                parts = [part.strip() for part in member.filename.replace("\\", "/").split("/") if part not in ("", ".")]
                if not parts or member.is_dir() or Path(parts[-1]).suffix.lower() not in wanted_extensions:
                    continue
                target = (destination.joinpath(*parts)).resolve()
                try:
                    target.relative_to(root)
                except ValueError as exc:
                    raise MPFSScraperError(f"Unsafe path {member.filename!r} found in {zip_path.name}.") from exc
                members.append((member, target))

            for member, target in members:
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
    except (NotImplementedError, zipfile.BadZipFile, OSError) as exc:
        logger.info("Python zipfile extraction failed for %s (%s). Falling back to tar.", zip_path.name, type(exc).__name__)
        # Ensure destination exists
        destination.mkdir(parents=True, exist_ok=True)
        # Fall back to command line tar
        result = subprocess.run(
            ["tar", "-xf", str(zip_path), "-C", str(destination)],
            capture_output=True, text=True, check=False,
        )
        # Normalize any trailing spaces/backslashes created by tar on disk
        _normalize_paths_on_disk(destination)

        # Check if we successfully extracted at least one CSV/TXT/ZIP file
        extracted_files = [p for p in destination.rglob("*") if p.is_file() and p.suffix.lower() in wanted_extensions]
        if not extracted_files:
            detail = result.stderr.strip() or result.stdout.strip() or "unknown extractor error"
            raise MPFSScraperError(
                f"Could not extract {zip_path.name}; its data member uses unsupported compression: {detail}"
            ) from exc

    # Final normalization just in case
    _normalize_paths_on_disk(destination)
    return [path for path in destination.rglob("*") if path.is_file()]


def _pick_data_file(paths: list[Path]) -> Path | None:
    candidates = [
        path for path in paths
        if path.suffix.lower() in DATA_EXTENSIONS and not EXCLUDE_PATTERNS.search(path.name)
    ]
    if not candidates:
        candidates = [path for path in paths if path.suffix.lower() in DATA_EXTENSIONS]
    return max(candidates, key=lambda path: path.stat().st_size) if candidates else None


def unzip_and_locate_mpfs_data_file(zip_path: Path) -> tuple[Path, list[Path]]:
    """Extract MPFS data, including CMS's nested non-QP ZIP packaging."""
    zip_path = Path(zip_path)
    extract_dir = zip_path.parent / "extracted"
    if extract_dir.exists():
        shutil.rmtree(extract_dir)
    extracted = _safe_extract_zip(zip_path, extract_dir)

    # Current CMS packages can contain PFREVxx_nonQP.zip and PFREVxx_QP.zip.
    # The standard fee schedule is always the non-QP package.
    nested_non_qp = [
        path for path in extracted
        if path.suffix.lower() == ".zip" and re.search(r"non[-_ ]?qp", path.name, re.I)
    ]
    nested_qp = [
        path for path in extracted
        if path.suffix.lower() == ".zip" and re.search(r"(?:^|[-_ ])qp(?:[-_ ]|$)", path.stem, re.I)
    ]
    if nested_non_qp:
        selected = max(nested_non_qp, key=lambda path: path.stat().st_size)
        nested_dir = extract_dir / "non_qp"
        extracted.extend(_safe_extract_zip(selected, nested_dir))
        search_pool = [path for path in extracted if nested_dir in path.parents]
        logger.info("Nested QP/non-QP package detected in %s; using %s.", zip_path.name, selected.name)
    else:
        non_qp_paths = [path for path in extracted if re.search(r"(^|[\\/])non[-_ ]?qp([\\/]|$)", str(path), re.I)]
        qp_paths = [path for path in extracted if re.search(r"(^|[\\/])qp([\\/]|$)", str(path), re.I)]
        if non_qp_paths:
            search_pool = non_qp_paths
            logger.info("QP/non-QP folder split detected in %s; using non-QP.", zip_path.name)
        elif nested_qp or qp_paths:
            raise MPFSScraperError(f"{zip_path.name} contains QP pricing but no non-QP companion package.")
        else:
            search_pool = extracted

    data_file = _pick_data_file(search_pool)
    if data_file is None:
        names = [path.relative_to(extract_dir).as_posix() for path in extracted]
        raise MPFSScraperError(f"No CSV/TXT data file found inside {zip_path.name}. Contents: {names}")
    return data_file, extracted