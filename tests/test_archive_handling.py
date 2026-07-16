"""Offline checks for CMS archive layouts and legacy AMA retry behavior."""

import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scraper import cms_scraper, mpfs_scraper


def _write_zip(path: Path, files: dict[str, str]):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)


def test_nested_mpfs_zip():
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        non_qp = root / "PFREV26C_nonQP.zip"
        qp = root / "PFREV26C_QP.zip"
        _write_zip(non_qp, {"PFREV26C/PPRRVU26C.txt": "NON-QP DATA"})
        _write_zip(qp, {"PFREV26C/PPRRVU26C.txt": "QP DATA"})
        outer = root / "PFREV26C.zip"
        with zipfile.ZipFile(outer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.write(non_qp, non_qp.name)
            archive.write(qp, qp.name)

        data_file, extracted = mpfs_scraper.unzip_and_locate_mpfs_data_file(outer)
        assert data_file.read_text() == "NON-QP DATA"
        assert any(path.name == "PFREV26C_nonQP.zip" for path in extracted)


def test_legacy_license_retry():
    class Response:
        def __init__(self, status_code, text="", content=b"PK", content_type=None):
            self.status_code = status_code
            self.text = text
            self.content = content
            self.headers = {"Content-Type": content_type or ("application/zip" if status_code == 200 else "text/html")}

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(f"HTTP {self.status_code}")

    class Session:
        def __init__(self):
            self.calls = []

        def get(self, url, **kwargs):
            self.calls.append((url, kwargs))
            if len(self.calls) == 1:
                return Response(200, '<form action="/files/zip/20clabq2.zip"><input name="agree" value="yes"><input type="submit" name="next" value="Accept"></form>', content=b"", content_type="text/html")
            if len(self.calls) == 2:
                return Response(404)
            return Response(200)

    session = Session()
    result = cms_scraper._accept_ama_license(session, "https://www.cms.gov/license.asp?file=20clabq2.zip")
    assert result.status_code == 200
    assert session.calls[-1][0] == "https://www.cms.gov/files/zip/20clabq2.zip"


def main():
    test_nested_mpfs_zip()
    test_legacy_license_retry()
    print("Nested MPFS ZIP extraction and legacy AMA retry PASSED.")


if __name__ == "__main__":
    main()