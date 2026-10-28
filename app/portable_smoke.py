"""A fixed fictitious input bounds the optional live copy/delete smoke test."""

import pymupdf


def fixture_pdf() -> bytes:
    with pymupdf.open() as doc:
        page = doc.new_page()
        page.insert_text((80, 150), "PRUEBA QR PORTABLE - SIN VALIDEZ", fontsize=16)
        page.insert_text((80, 180), "QCP-2026-0001")
        page.insert_text((80, 230), "Documento ficticio generado para comprobar copias en Drive.")
        for index, line in enumerate(["Escanee este QR", "Para consultar el certificado", "con firma electronica"]):
            page.insert_text((220, 650 + index * 11), line, fontsize=9)
        return doc.tobytes(no_new_id=True)
