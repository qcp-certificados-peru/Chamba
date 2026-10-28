"""Direct Google viewer links; moving the same file never needs the portal."""

from copy import deepcopy
import hashlib
from io import BytesIO
from urllib.parse import urlsplit

from fastapi.testclient import TestClient
import httpx
from PIL import Image
import pymupdf
import pytest
import zxingcpp

from app.main import create_app
from app.portable_smoke import fixture_pdf
from test_drive import FakeDrive, ROOT
from test_drive_flat import legacy_record
from test_portal import pdf_bytes, upload


def test_new_qr_is_google_link_and_creates_only_one_pdf_without_registry():
    drive = FakeDrive()
    with TestClient(create_app(public_url="https://portal.example.com", certificate_storage=drive.storage())) as client:
        assert client.get("/api/config").json()["qrMode"] == "drive-direct"
        info = upload(client).json()
        pdf = client.get(info["downloadUrl"]).content
    assert info["verificationUrl"] == info["driveUrl"] == f"https://drive.google.com/file/d/{info['driveFileId']}/view"
    assert info["qrMode"] == "drive-direct"
    assert "qrPublicKey" not in info and "copyViewerAccess" not in info
    assert set(drive.files) == {ROOT, info["driveFileId"]}
    with pymupdf.open(stream=pdf, filetype="pdf") as doc:
        link = next(link for page in doc for link in page.get_links() if link.get("uri") == info["driveUrl"])
        image = doc[0].get_pixmap(matrix=pymupdf.Matrix(3, 3), clip=link["from"])
        barcode = zxingcpp.read_barcode(Image.open(BytesIO(image.tobytes("png"))))
        assert barcode and barcode.text == info["driveUrl"]
        assert urlsplit(barcode.text).hostname == "drive.google.com"


def test_fresh_move_diagnostic_preserves_file_id_bytes_qr_and_old_certificates():
    drive = FakeDrive(); legacy_record(drive); before = deepcopy(drive.files)
    with TestClient(create_app(certificate_storage=drive.storage())) as client:
        response = client.post("/api/certificates", files={"pdf": ("SIN-VALIDEZ.pdf", fixture_pdf())},
                               data={"tipoCertificado": "no_acreditado", "pruebaMovimiento": "true"})
        assert response.status_code == 201, response.text
        info = response.json()
        assert info["moveTest"] == {"ok": True, "sameFileId": True, "sameQr": True,
                                    "movedOutsideOutputFolder": True, "renamed": True}
        assert drive.files[info["driveFileId"]]["parents"] == ["root"]
        assert info["verificationUrl"] == info["driveUrl"]
        pdf = client.get(info["downloadUrl"]).content
        assert hashlib.sha256(pdf).hexdigest() == info["sha256"]
    assert all(drive.files[key] == value for key, value in before.items())
    assert set(drive.files) - set(before) == {info["driveFileId"]}
    assert not any(method == "DELETE" or path.endswith("/copy") for method, path in drive.writes)


def test_direct_qr_never_adopts_a_same_named_copy_after_original_is_deleted():
    drive = FakeDrive()
    with TestClient(create_app(certificate_storage=drive.storage())) as client:
        info = upload(client).json()
        original = drive.files.pop(info["driveFileId"]); copied = drive.new_id()
        drive.files[copied] = {**original, "id": copied}
        assert client.get(f"/api/certificates/{info['id']}").status_code == 404
        assert info["verificationUrl"].endswith(f"/{info['driveFileId']}/view")
        assert all(fid != copied for fid, _ in drive.permissions)


@pytest.mark.parametrize("flag,data", [("pruebaMovimiento", pdf_bytes()), ("pruebaCopia", fixture_pdf())])
def test_diagnostic_flags_cannot_move_customer_files_or_copy_delete_with_direct_qr(flag, data):
    drive = FakeDrive()
    with TestClient(create_app(certificate_storage=drive.storage())) as client:
        response = client.post("/api/certificates", files={"pdf": ("SIN-VALIDEZ.pdf", data)},
                               data={"tipoCertificado": "no_acreditado", flag: "true"})
        assert response.status_code == 422
    assert set(drive.files) == {ROOT} and drive.writes == [] and drive.token_requests == 0


def test_failed_move_keeps_the_fresh_original_pdf_and_never_deletes_files():
    class FailedMoveDrive(FakeDrive):
        def handle(self, request):
            if request.method == "PATCH" and request.url.params.get("addParents"):
                return httpx.Response(403)
            return super().handle(request)
    drive = FailedMoveDrive()
    with TestClient(create_app(certificate_storage=drive.storage())) as client:
        response = client.post("/api/certificates", files={"pdf": ("SIN-VALIDEZ.pdf", fixture_pdf())},
                               data={"tipoCertificado": "no_acreditado", "pruebaMovimiento": "true"})
        assert response.status_code == 201
        info = response.json()
        assert not info["moveTest"]["ok"]
        assert drive.files[info["driveFileId"]]["parents"] == [ROOT]
        assert client.get(info["downloadUrl"]).status_code == 200
    assert not any(method == "DELETE" for method, _ in drive.writes)
