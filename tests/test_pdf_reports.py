"""Synthetic reports: direct background downloads must never publish to storage."""

from urllib.parse import unquote

from fastapi.testclient import TestClient
import pymupdf
import pytest

from app.backgrounds import load_letterhead, verify_letterhead
from app.documents import prepare_document
from app.main import create_app
from app.pdf_reports import is_technical_report
from test_backgrounds import synthetic_backgrounds
from test_drive import FakeDrive, ROOT
from test_pdf_certificate import certificate_pdf


def report_pdf(title="INFORME TÉCNICO DE MANTENIMIENTO Y CALIFICACIÓN", **options):
    with pymupdf.open(stream=certificate_pdf(guide=False, **options), filetype="pdf") as doc:
        doc[0].insert_text((80, 105), title, fontsize=9)
        return doc.tobytes()


@pytest.mark.parametrize("title", [
    "INFORME TÉCNICO DE MANTENIMIENTO Y CALIFICACIÓN",
    "Informe tecnico de mantenimiento preventivo",
    "INFORME TÉCNICO DE CALIFICACIÓN",
    "INFORME DE CALIFICACION",
    "INFORME\nTÉCNICO DE\nCALIFICACIÓN",
])
def test_recognizes_report_headings(title):
    assert is_technical_report(report_pdf(title))


@pytest.mark.parametrize("placement", ["paragraph", "bottom", "second_page", "certificate_heading"])
def test_mentions_do_not_turn_certificates_into_reports(placement):
    with pymupdf.open(stream=certificate_pdf(), filetype="pdf") as doc:
        page = doc[1] if placement == "second_page" else doc[0]
        point = (80, 600) if placement == "bottom" else (80, 280)
        text = "INFORME TÉCNICO DE MANTENIMIENTO"
        if placement == "paragraph":
            text = "Referencia al informe técnico de mantenimiento anterior."
        if placement == "certificate_heading":
            page.insert_text((80, 100), "CERTIFICADO DE CALIBRACION")
        page.insert_text(point, text, fontsize=9)
        assert not is_technical_report(doc.tobytes())


@pytest.mark.parametrize("kind", ["acreditado", "no_acreditado"])
@pytest.mark.parametrize("existing_qr", [False, True])
def test_background_only_preserves_pages_words_tables_images_and_existing_links(
        synthetic_backgrounds, kind, existing_qr):
    source = report_pdf(existing_qr=existing_qr)
    url = "https://example.com/must-not-be-inserted"
    result = prepare_document(source, "pdf", "QCP-2026-9999", url,
                              b"not-a-signature", certificate_type=kind,
                              format_code="F-GCA-10.99", format_version="99")
    assert result.stamp_pages == [] and not result.signature_included
    assert result.replacements == 0 and result.warnings == []
    assert result.background_pages == [1, 2]
    selected = load_letterhead(kind)
    with pymupdf.open(stream=source, filetype="pdf") as before, \
            pymupdf.open(stream=result.data, filetype="pdf") as after:
        assert len(before) == len(after) == 2
        assert verify_letterhead(after, selected, preserve_page_size=True) == [1, 2]
        for old, new in zip(before, after, strict=True):
            assert old.rect == new.rect and old.rotation == new.rotation
            assert old.get_text("words") == new.get_text("words")
            shapes = lambda page: [{k: v for k, v in drawing.items() if k != "seqno"}
                                   for drawing in page.get_drawings()]
            assert shapes(old) == shapes(new)
            links = lambda page: [{k: v for k, v in link.items() if k not in {"xref", "id"}}
                                  for link in page.get_links()]
            assert links(old) == links(new)
            original_images = [(i["width"], i["height"], i["bbox"], i["digest"])
                               for i in old.get_image_info(hashes=True)]
            preserved_images = [(i["width"], i["height"], i["bbox"], i["digest"])
                                for i in new.get_image_info(hashes=True)
                                if i["digest"] != selected.digest]
            assert original_images == preserved_images
            assert not any(link.get("uri") == url for link in new.get_links())


@pytest.mark.parametrize("kind", ["acreditado", "no_acreditado"])
def test_report_response_never_contacts_or_writes_drive(synthetic_backgrounds, kind):
    drive = FakeDrive()
    source = report_pdf()
    with TestClient(create_app(certificate_storage=drive.storage())) as client:
        response = client.post("/api/certificates", files={
            "pdf": ("Informe técnico.pdf", source, "application/pdf"),
            "firma": ("invalid.png", b"invalid", "image/png"),
        }, data={"tipoCertificado": kind, "codigoCorrecto": "not-a-code"})
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/pdf")
    assert response.headers["x-processing-mode"] == "background-only"
    assert "Informe técnico.pdf" in unquote(response.headers["content-disposition"])
    assert drive.token_requests == 0
    assert drive.writes == [] and set(drive.files) == {ROOT}
    with pymupdf.open(stream=response.content, filetype="pdf") as doc:
        assert len(doc) == 2
        assert verify_letterhead(doc, load_letterhead(kind), preserve_page_size=True) == [1, 2]


def test_report_download_does_not_store_local_files_or_depend_on_quota(
        tmp_path, monkeypatch, synthetic_backgrounds):
    monkeypatch.setenv("MAX_STORAGE_MB", "0")
    with TestClient(create_app(tmp_path / "store")) as client:
        response = client.post("/api/certificates", files={"pdf": ("report.pdf", report_pdf())},
                               data={"tipoCertificado": "no_acreditado"})
    assert response.status_code == 200
    assert not list((tmp_path / "store").rglob("*.pdf"))
    assert not list((tmp_path / "store").rglob("*.json"))


@pytest.mark.parametrize("kind", ["", "unknown"])
def test_report_requires_a_valid_background_without_using_drive(kind):
    drive = FakeDrive()
    with TestClient(create_app(certificate_storage=drive.storage())) as client:
        response = client.post("/api/certificates", files={"pdf": ("report.pdf", report_pdf())},
                               data={"tipoCertificado": kind})
    assert response.status_code == 422
    assert drive.token_requests == 0 and drive.writes == []


def test_report_preserves_existing_permissions_instead_of_applying_certificate_password(
        monkeypatch, synthetic_backgrounds):
    monkeypatch.setenv("CERTIFICATE_PDF_OWNER_PASSWORD", "certificate-owner-test")
    plain = prepare_document(report_pdf(), "pdf", "", "", certificate_type="no_acreditado")
    with pymupdf.open(stream=plain.data, filetype="pdf") as doc:
        assert not doc.metadata["encryption"]
    with pymupdf.open(stream=report_pdf(), filetype="pdf") as doc:
        protected = doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256,
                               owner_pw="original-owner-test", user_pw="",
                               permissions=pymupdf.PDF_PERM_PRINT)
    result = prepare_document(protected, "pdf", "", "", certificate_type="acreditado")
    with pymupdf.open(stream=result.data, filetype="pdf") as doc:
        assert not doc.needs_pass and not doc.permissions & pymupdf.PDF_PERM_COPY
        assert doc.authenticate("certificate-owner-test") == 0
        assert doc.authenticate("original-owner-test") == 4


def test_signed_report_is_rejected_without_creating_a_drive_file(synthetic_backgrounds):
    with pymupdf.open(stream=report_pdf(), filetype="pdf") as doc:
        widget = pymupdf.Widget()
        widget.field_type = pymupdf.PDF_WIDGET_TYPE_SIGNATURE
        widget.field_name = "signed"
        widget.rect = pymupdf.Rect(100, 400, 200, 450)
        doc[0].add_widget(widget)
        source = doc.tobytes()
    drive = FakeDrive()
    with TestClient(create_app(certificate_storage=drive.storage())) as client:
        response = client.post("/api/certificates", files={"pdf": ("report.pdf", source)},
                               data={"tipoCertificado": "acreditado"})
    assert response.status_code == 422 and "firma digital" in response.json()["detail"]
    assert drive.token_requests == 0 and drive.writes == []


def test_invalid_and_password_locked_pdf_do_not_crash_detection():
    assert not is_technical_report(b"not a PDF")
    assert not is_technical_report(b"%PDF-corrupt")
    with pymupdf.open(stream=report_pdf(), filetype="pdf") as doc:
        encrypted = doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256,
                               owner_pw="owner-test", user_pw="locked-test")
    assert not is_technical_report(encrypted)
