"""Recognize report headings without using filenames or changing PDF content."""

import re
import unicodedata

import pymupdf


PDF_MODES = {"auto", "background", "certificate"}
PDF_MODE_ERROR = "Elige un modo de PDF válido: Automático, Solo fondo o Certificado."

_REPORT = re.compile(
    r"(?im)^[ \t]*informe[\s:–—-]+(?:tecnico[\s:–—-]+)?"
    r"(?:de\s+)?(?:mantenimiento|calibracion|calificacion)\b"
)
_CERTIFICATE = re.compile(r"(?im)^\s*certificado\s+(?:de\s+)?calibracion\b")


def is_technical_report(data: bytes) -> bool:
    """Recognize varied report headings anywhere on the first page."""
    if not data.startswith(b"%PDF-"):
        return False
    try:
        with pymupdf.open(stream=data, filetype="pdf") as doc:
            if doc.needs_pass or not doc.page_count:
                return False
            page = doc[0]
            text = page.get_text("text", sort=True)
            text = "".join(char for char in unicodedata.normalize("NFKD", text)
                           if not unicodedata.combining(char))
            report = _REPORT.search(text)
            certificate = _CERTIFICATE.search(text)
            return bool(report and (not certificate or report.start() < certificate.start()))
    except (pymupdf.FileDataError, RuntimeError, ValueError):
        # The normal processor provides the existing damaged/encrypted PDF errors.
        return False
