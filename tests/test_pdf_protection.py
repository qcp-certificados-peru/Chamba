"""Owner permissions use a fictitious password, never a production secret."""

import hashlib
from io import BytesIO

from fastapi.testclient import TestClient
from PIL import Image
import pymupdf
import pytest
import zxingcpp

from app.backgrounds import load_letterhead, verify_letterhead
from app.documents import prepare_document
from app.main import create_app
from app.pdf import CertificateError, pdf_save_options, prepare_pdf
from test_backgrounds import synthetic_backgrounds
from test_drive import FakeDrive
from test_excel import workbook_bytes
from test_pdf_certificate import certificate_pdf
from test_portal import pdf_bytes, upload

PASSWORD = "owner-test-only"
URL = "https://drive.google.com/file/d/SIN_VALIDEZ/view"


def check_permissions(doc):
    assert not doc.needs_pass
    assert "256-bit AES" in doc.metadata["encryption"]
    for permission in (pymupdf.PDF_PERM_MODIFY, pymupdf.PDF_PERM_ANNOTATE,
                       pymupdf.PDF_PERM_FORM, pymupdf.PDF_PERM_ASSEMBLE,
                       pymupdf.PDF_PERM_COPY):
        assert not doc.permissions & permission
    for permission in (pymupdf.PDF_PERM_PRINT, pymupdf.PDF_PERM_PRINT_HQ,
                       pymupdf.PDF_PERM_ACCESSIBILITY):
        assert doc.permissions & permission
    assert doc.authenticate("") == 2  # Viewing requires no password.
    assert doc.authenticate("wrong-password") == 0
    assert doc.authenticate(PASSWORD) == 4  # Owner can change the editing/copying restrictions.


def test_protection_preserves_all_pages_pixels_text_links_and_qr(monkeypatch):
    source = pdf_bytes(("QCP-2026-0001", "QCP-2026-0001"))
    monkeypatch.delenv("CERTIFICATE_PDF_OWNER_PASSWORD", raising=False)
    plain = prepare_pdf(source, "", URL)
    monkeypatch.setenv("CERTIFICATE_PDF_OWNER_PASSWORD", PASSWORD)
    protected = prepare_pdf(source, "", URL)
    with pymupdf.open(stream=plain.data, filetype="pdf") as before, \
            pymupdf.open(stream=protected.data, filetype="pdf") as after:
        assert len(before) == len(after) == 2
        for old, new in zip(before, after, strict=True):
            assert old.get_text("words") == new.get_text("words")
            assert old.get_pixmap().samples == new.get_pixmap().samples
            assert [(link["from"], link["uri"]) for link in old.get_links()] == [
                (link["from"], link["uri"]) for link in new.get_links()]
        image = Image.open(BytesIO(after[0].get_pixmap(dpi=150).tobytes("png")))
        assert URL in [code.text for code in zxingcpp.read_barcodes(image)]
        check_permissions(after)


@pytest.mark.parametrize("source_type", ["pdf", "xlsx"])
def test_pdf_and_excel_backgrounds_and_final_validation_with_protection(
        monkeypatch, synthetic_backgrounds, source_type):
    monkeypatch.setenv("CERTIFICATE_PDF_OWNER_PASSWORD", PASSWORD)
    source = certificate_pdf() if source_type == "pdf" else workbook_bytes()
    result = prepare_document(source, source_type, "", URL, certificate_type="no_acreditado")
    with pymupdf.open(stream=result.data, filetype="pdf") as doc:
        assert len(doc) == 2 and result.background_pages == [1, 2]
        assert verify_letterhead(doc, load_letterhead("no_acreditado"), preserve_page_size=True) == [1, 2]
        assert all("QCP-2026-0001" in page.get_text() for page in doc)
        check_permissions(doc)


@pytest.mark.parametrize("drive_backend", [False, True])
def test_api_saves_protected_bytes_and_hash_without_exposing_password(tmp_path, monkeypatch, drive_backend):
    monkeypatch.setenv("CERTIFICATE_PDF_OWNER_PASSWORD", PASSWORD)
    drive = FakeDrive() if drive_backend else None
    app = (create_app(certificate_storage=drive.storage(), public_url="https://example.com")
           if drive else create_app(tmp_path / "store"))
    with TestClient(app) as client:
        response = upload(client)
        assert response.status_code == 201, response.text
        info = response.json()
        pdf = client.get(info["downloadUrl"]).content
        assert hashlib.sha256(pdf).hexdigest() == info["sha256"]
        assert PASSWORD not in response.text
        assert PASSWORD not in client.get("/api/config").text
        assert PASSWORD not in client.get(f"/api/certificates/{info['id']}").text
        if drive:
            assert pdf == drive.files[info["driveFileId"]]["content"]
            assert info["verificationUrl"] == info["driveUrl"]
        with pymupdf.open(stream=pdf, filetype="pdf") as doc:
            assert "Resultado: conforme" in doc[0].get_text()
            check_permissions(doc)


def test_empty_password_disables_protection_and_invalid_length_fails_explicitly(monkeypatch):
    monkeypatch.setenv("CERTIFICATE_PDF_OWNER_PASSWORD", "")
    assert pdf_save_options() == {"encryption": pymupdf.PDF_ENCRYPT_NONE}
    monkeypatch.setenv("CERTIFICATE_PDF_OWNER_PASSWORD", "x" * 41)
    with pytest.raises(CertificateError, match="40 bytes UTF-8"):
        pdf_save_options()
    monkeypatch.setenv("CERTIFICATE_PDF_OWNER_PASSWORD", "á" * 21)
    with pytest.raises(CertificateError, match="40 bytes UTF-8"):
        pdf_save_options()
