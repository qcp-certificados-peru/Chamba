"""Recognize report headings without using filenames or changing PDF content."""

import re
import unicodedata

import pymupdf


_REPORT = re.compile(r"(?im)^\s*informe\s+(?:tecnico\s+)?de\s+(?:mantenimiento|calificacion)\b")
_CERTIFICATE = re.compile(r"(?im)^\s*certificado\s+(?:de\s+)?calibracion\b")


def is_technical_report(data: bytes) -> bool:
    """Only a heading in the upper half of the first page selects report mode."""
    if not data.startswith(b"%PDF-"):
        return False
    try:
        with pymupdf.open(stream=data, filetype="pdf") as doc:
            if doc.needs_pass or not doc.page_count:
                return False
            page = doc[0]
            area = pymupdf.Rect(0, 0, page.rect.width, page.rect.height / 2)
            text = page.get_text("text", clip=area, sort=True)
            text = "".join(char for char in unicodedata.normalize("NFKD", text)
                           if not unicodedata.combining(char))
            report = _REPORT.search(text)
            certificate = _CERTIFICATE.search(text)
            return bool(report and (not certificate or report.start() < certificate.start()))
    except (pymupdf.FileDataError, RuntimeError, ValueError):
        # The normal processor provides the existing damaged/encrypted PDF errors.
        return False
