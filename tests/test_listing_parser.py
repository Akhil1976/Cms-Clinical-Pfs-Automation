"""
test_listing_parser.py
------------------------
list_available_files() in cms_scraper.py can't be tested against the live
network from every environment (e.g. a sandboxed CI runner with no
internet), so this test feeds it a realistic HTML sample instead —
structured the same way CMS's actual CLFS Files table is (a plain <table>
with one <tr> per file, a file-code link, and a description cell) based on
the real page at cms.gov/medicare/payment/fee-schedules/
clinical-laboratory-fee-schedule-clfs/files.

If CMS changes their page's HTML structure, this test won't catch that on
its own — but it does confirm the row-walking logic works correctly against
the structure the site uses today. Run the real `python main.py list`
against the live site after any CMS redesign to re-verify.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bs4 import BeautifulSoup
from scraper.cms_scraper import list_available_files  # noqa: F401 (imported to prove module loads cleanly)
import scraper.cms_scraper as cms_scraper

SAMPLE_HTML = """
<html><body>
<table>
<thead><tr><th>File Name</th><th>Description</th><th>Calendar Year</th></tr></thead>
<tbody>
<tr>
  <td><a href="/medicare/payment/fee-schedules/clinical-laboratory-fee-schedule-clfs/files/26clabq2">26CLABQ2</a></td>
  <td>CY 2026 Q2 Release: Updated for April 2026. The update includes all changes identified in CR 14371. The file has 2,179 records.</td>
  <td>2026</td>
</tr>
<tr>
  <td><a href="/medicare/payment/fee-schedules/clinical-laboratory-fee-schedule-clfs/files/26clabq1">26CLABQ1</a></td>
  <td>CY 2026 Q1 Release: Updated for January 2026. The update includes all changes identified in CR 14312. The file has 2,162 records.</td>
  <td>2026</td>
</tr>
<tr>
  <td><a href="/medicare/payment/fee-schedules/clinical-laboratory-fee-schedule-clfs/files/25clabq4">25CLABQ4</a></td>
  <td>CY 2025 Q4 Release: Updated for October 2025. The update includes all changes identified in CR 14211. The file has 2,149 records.</td>
  <td>2025</td>
</tr>
</tbody>
</table>
</body></html>
"""


def main():
    # Monkey-patch the HTTP call so list_available_files() parses our sample
    # instead of hitting the network.
    class FakeResponse:
        text = SAMPLE_HTML
        def raise_for_status(self):
            pass

    class FakeSession:
        def get(self, *_args, **_kwargs):
            return FakeResponse()

    cms_scraper._session = lambda: FakeSession()  # noqa: SLF001

    files = cms_scraper.list_available_files()
    assert len(files) == 3, f"expected 3 rows, got {len(files)}"

    expected_codes = {"26CLABQ2", "26CLABQ1", "25CLABQ4"}
    found_codes = {f["file_code"] for f in files}
    assert found_codes == expected_codes, f"mismatch: {found_codes}"

    q2 = next(f for f in files if f["file_code"] == "26CLABQ2")
    assert q2["calendar_year"] == 2026
    assert q2["quarter"] == "Q2"
    assert q2["detail_url"].startswith("https://www.cms.gov/")

    for f in files:
        print(f"{f['file_code']:12} CY{f['calendar_year']} {f['quarter']:3} -> {f['detail_url']}")

    print("\nlist_available_files() parsing logic PASSED against the sample CMS table structure.")


if __name__ == "__main__":
    main()
