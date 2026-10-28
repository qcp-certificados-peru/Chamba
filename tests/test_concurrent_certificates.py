"""Concurrent browsers use real isolated workers, PDF conversion and QR checks."""

from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
import hashlib
import os
from pathlib import Path
import time
import threading

from fastapi.testclient import TestClient
from PIL import Image
import pymupdf
import pytest
import zxingcpp

from app.documents import prepare_document
from app.main import create_app
from app.storage import LocalStorage
from test_drive import FakeDrive, ROOT
from test_excel import workbook_bytes
from test_portal import pdf_bytes


def rendezvous_document(*args):
    """A real process cannot finish until the other worker has also started."""
    directory = Path(os.environ["QCP_TEST_RENDEZVOUS"])
    expected = int(os.environ["QCP_TEST_EXPECTED_WORKERS"])
    (directory / f"{os.getpid()}.started").touch()
    deadline = time.monotonic() + 20
    while len(list(directory.glob("*.started"))) < expected:
        if time.monotonic() > deadline:
            raise RuntimeError("The second conversion did not start concurrently")
        time.sleep(.02)
    return prepare_document(*args)


def verify_result(client, response, index, source_type="pdf"):
    assert response.status_code == 201, response.text
    info = response.json()
    assert info["filename"] == f"worker-{index}.pdf"
    assert info["finalCode"] == f"QCP-2026-{8000 + index}"
    assert info["sourceType"] == source_type
    data = client.get(info["downloadUrl"]).content
    assert hashlib.sha256(data).hexdigest() == info["sha256"]
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        text = "".join(page.get_text() for page in doc)
        assert info["finalCode"] in text
        assert all(f"QCP-2026-{8000 + other}" not in text for other in range(3) if other != index)
        decoded = []
        for page in doc:
            for link in page.get_links():
                if link.get("uri") == info["verificationUrl"]:
                    pixmap = page.get_pixmap(matrix=pymupdf.Matrix(3, 3), clip=link["from"])
                    barcode = zxingcpp.read_barcode(Image.open(BytesIO(pixmap.tobytes("png"))))
                    if barcode:
                        decoded.append(barcode.text)
        assert decoded == [info["verificationUrl"]]
    return info


@pytest.mark.parametrize("people,workers", [(2, 2), (3, 2), (3, 3)])
def test_simultaneous_users_get_distinct_pdfs_codes_and_direct_drive_qrs(tmp_path, monkeypatch, people, workers):
    monkeypatch.setattr("app.main.conversion_workers", lambda: workers)
    monkeypatch.setenv("QCP_TEST_RENDEZVOUS", str(tmp_path))
    monkeypatch.setenv("QCP_TEST_EXPECTED_WORKERS", "1")
    monkeypatch.setattr("app.main.prepare_document", rendezvous_document)
    drive = FakeDrive()
    with TestClient(create_app(certificate_storage=drive.storage())) as client:
        assert client.get("/api/config").json()["maxActiveCertificates"] == 3
        payloads = [pdf_bytes((f"QCP-2026-{8000+i}",)) for i in range(people)]
        def upload(index):
            return client.post("/api/certificates", files={"pdf": (f"worker-{index}.pdf", payloads[index])})
        with ThreadPoolExecutor(max_workers=people) as browsers:
            futures = [browsers.submit(upload, i) for i in range(people)]
            responses = [job.result(timeout=40) for job in futures]
        infos = [verify_result(client, response, index) for index, response in enumerate(responses)]
    assert 1 <= len(list(tmp_path.glob("*.started"))) <= workers
    assert len({info["id"] for info in infos}) == people
    assert len({info["driveUrl"] for info in infos}) == people
    assert all(info["verificationUrl"] == info["driveUrl"] for info in infos)
    assert set(drive.files) == {ROOT, *(info["driveFileId"] for info in infos)}
    assert drive.token_requests == 1


def test_three_cached_excels_keep_their_pages_backgrounds_and_control_footers(tmp_path, monkeypatch):
    monkeypatch.setattr("app.main.conversion_workers", lambda: 2)
    monkeypatch.setenv("QCP_TEST_RENDEZVOUS", str(tmp_path))
    monkeypatch.setenv("QCP_TEST_EXPECTED_WORKERS", "2")
    monkeypatch.setattr("app.main.prepare_document", rendezvous_document)
    with TestClient(create_app(tmp_path / "certificates")) as client:
        def inspect(index):
            response = client.post("/api/excel/inspect", files={
                "excel": (f"worker-{index}.xlsx", workbook_bytes(f"QCP-2026-{8000+index}"))})
            assert response.status_code == 200, response.text
            return response.json()["uploadToken"]
        with ThreadPoolExecutor(max_workers=3) as browsers:
            tokens = list(browsers.map(inspect, range(3)))
            assert len(set(tokens)) == 3
            def upload(index):
                return client.post("/api/certificates", data={
                    "excelToken": tokens[index], "tipoCertificado": "no_acreditado",
                    "codigoFormato": f"F-GCA-10.{20+index}", "versionFormato": f"0{index}"})
            responses = list(browsers.map(upload, range(3)))
        infos = [verify_result(client, response, index, "xlsx") for index, response in enumerate(responses)]
        for index, info in enumerate(infos):
            assert info["formatFooterVerified"] and info["backgroundVerified"] and info["signatureIncluded"]
            with pymupdf.open(stream=client.get(info["downloadUrl"]).content, filetype="pdf") as doc:
                assert len(doc) == 2
                assert "17,5" in doc[0].get_text()  # Saved formula value, never recalculated as 2.
                for page in doc:
                    assert f"F-GCA-10.{20+index}" in page.get_text()
                    assert all(f"F-GCA-10.{20+other}" not in page.get_text() for other in range(3) if other != index)
            assert client.post("/api/certificates", data={"excelToken": tokens[index]}).status_code == 410
    assert len(list(tmp_path.glob("*.started"))) == 2


def test_fourth_request_is_rejected_before_allocating_and_the_portal_stays_responsive(tmp_path):
    entered, release = threading.Event(), threading.Event()
    class HeldStorage(LocalStorage):
        allocated = 0
        def allocate(self, *args):
            with self.lock:
                self.allocated += 1
                if self.allocated == 3:
                    entered.set()
            if not release.wait(15):
                raise RuntimeError("Test did not release publication")
            return super().allocate(*args)
    storage = HeldStorage(tmp_path, 20 * 1024 * 1024)
    payload = pdf_bytes()
    with TestClient(create_app(certificate_storage=storage)) as client:
        def upload():
            return client.post("/api/certificates", files={"pdf": ("test.pdf", payload)})
        with ThreadPoolExecutor(max_workers=3) as browsers:
            jobs = [browsers.submit(upload) for _ in range(3)]
            try:
                assert entered.wait(10)
                assert client.get("/healthz").status_code == 200
                assert client.get("/").status_code == 200
                response = upload()
                assert response.status_code == 429 and response.headers["retry-after"] == "5"
                assert storage.allocated == 3
            finally:
                release.set()
            assert all(job.result(timeout=30).status_code == 201 for job in jobs)
        assert upload().status_code == 201


def test_three_users_wait_automatically_on_a_small_server_and_errors_do_not_block(tmp_path, monkeypatch):
    monkeypatch.setattr("app.main.conversion_workers", lambda: 1)
    payloads = [pdf_bytes((f"QCP-2026-{8000+i}",)) for i in range(3)]
    with TestClient(create_app(tmp_path)) as client:
        assert client.get("/api/config").json()["conversionWorkers"] == 1
        def upload(index):
            data = b"not a PDF" if index == 1 else payloads[index]
            return client.post("/api/certificates", files={"pdf": (f"worker-{index}.pdf", data)})
        with ThreadPoolExecutor(max_workers=3) as browsers:
            responses = list(browsers.map(upload, range(3)))
        assert responses[1].status_code == 422
        infos = [verify_result(client, responses[index], index) for index in (0, 2)]
        recovered = client.post("/api/certificates", files={"pdf": ("worker-1.pdf", payloads[1])})
        infos.append(verify_result(client, recovered, 1))
    assert len({info["id"] for info in infos}) == 3
