"""Convert Excel certificates and share the existing PDF publication pipeline."""

from app.excel import convert_excel, inspect_excel
from app.pdf import CertificateError, PreparedPDF, prepare_pdf, signature_png, normalized_code
from app.backgrounds import load_letterhead, verify_letterhead
from app.excel_footers import verify_format_footers
from app.pdf_reports import is_technical_report
import pymupdf


def prepare_document(data: bytes, source_type: str, requested_code: str,
                     verification_url: str, signature: bytes | None = None,
                     certificate_type: str = "", format_code: str = '', format_version: str = '') -> PreparedPDF:
    requested_code = normalized_code(requested_code) if source_type=='xlsx' or not certificate_type else ''
    if source_type == "xlsx":
        # Validate the optional replacement before starting the Office conversion.
        signature = signature_png(signature)
        layout = inspect_excel(data)
        letterhead = load_letterhead(certificate_type) if layout.needs_background else None
        converted = convert_excel(data, replace_signature=bool(signature), letterhead=letterhead,
                                  format_code=format_code, format_version=format_version)
        result = prepare_pdf(converted.pdf, requested_code, verification_url, signature, template=True,
                             background_digests=converted.background_digests,
                             automatic_qr=bool(layout.preserve_format and layout.page_numbers))
        result.signature_included |= converted.embedded_signature
        result.warnings = converted.warnings + result.warnings
        # Validate the final PDF after QR, signature and code processing.
        if letterhead:
            with pymupdf.open(stream=result.data, filetype="pdf") as doc:
                result.background_pages = verify_letterhead(doc, letterhead, converted.letterhead_pages)
            result.certificate_type = certificate_type
        result.format_preserved = layout.preserve_format
        result.sheet_name = layout.sheet_name
        with pymupdf.open(stream=result.data,filetype='pdf') as final:
            verify_format_footers(final,converted.format_footers)
        result.format_footer_verified = bool(converted.format_footers) and all(converted.format_footers)
        pymupdf.TOOLS.store_shrink(100)
        if not result.detected_codes:
            raise CertificateError("No se pudo verificar el código del certificado en la hoja impresa. Revisa ese dato esencial antes de emitirlo.")
        return result
    result = prepare_pdf(data, requested_code, verification_url, signature,
                         letterhead=load_letterhead(certificate_type) if certificate_type else None,
                         background_only=is_technical_report(data))
    pymupdf.TOOLS.store_shrink(100)
    return result
