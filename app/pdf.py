"""Procesamiento local: corrección de texto, QR, imagen de firma y permisos PDF."""

from dataclasses import dataclass, field
from io import BytesIO
import re

from PIL import Image, ImageOps, UnidentifiedImageError
import pymupdf
import qrcode
import zxingcpp

_CODE_PARTS = ((r"QCP", r"[0-9]{4}", r"[0-9]{4}"),
               (r"LP", r"[A-Z]{2,12}[0-9]{4}", r"[A-Z]{1,8}[0-9]{3,6}"))


def _code_pattern(separator):
    return r"(?:" + "|".join(separator.join(parts) for parts in _CODE_PARTS) + ")"


CODE_PATTERN = _code_pattern('-')
MAX_CODE_LENGTH = 40
CODE_ERROR = "El código debe tener el formato QCP-0000-0000 o LP-ALT0125-PC001."
# Printed text may include spaces after a hyphen. Require complete identifiers,
# so a longer instrument/reference number is never treated as a certificate.
CODE = re.compile(r"(?<![A-Z0-9_-])" + _code_pattern(r'-\s*') + r"(?![A-Z0-9_-])",
                  re.IGNORECASE | re.ASCII)
MAX_PAGES = 100


class CertificateError(ValueError):
    pass


def normalized_code(value: str) -> str:
    value = value.strip()
    if value and not re.fullmatch(CODE_PATTERN, value, re.IGNORECASE | re.ASCII):
        raise CertificateError(CODE_ERROR)
    return value.upper()


@dataclass
class PreparedPDF:
    data: bytes
    detected_codes: list[str]
    final_code: str | None
    replacements: int
    warnings: list[str]
    stamp_pages: list[int]
    signature_included: bool
    certificate_type: str | None = None
    background_pages: list[int] = field(default_factory=list)
    format_preserved: bool = False
    sheet_name: str | None = None
    format_footer_verified: bool = False


def signature_png(data: bytes | None) -> bytes | None:
    if not data:
        return None
    try:
        with Image.open(BytesIO(data)) as image:
            if image.format not in {"PNG", "JPEG"}:
                raise CertificateError("La firma debe ser una imagen PNG o JPG.")
            if image.width * image.height > 12_000_000:
                raise CertificateError("La imagen de firma es demasiado grande.")
            normalized = ImageOps.exif_transpose(image).convert("RGBA")
            normalized.thumbnail((1600, 800))
            output = BytesIO()
            normalized.save(output, format="PNG")
            return output.getvalue()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise CertificateError("No se pudo leer la imagen de firma.") from exc


def _style(page: pymupdf.Page, area: pymupdf.Rect) -> tuple[float, tuple, str]:
    for block in page.get_text("dict", flags=pymupdf.TEXTFLAGS_DICT & ~pymupdf.TEXT_PRESERVE_IMAGES)["blocks"]:
        for line in block.get("lines", []):
            for span in line["spans"]:
                if pymupdf.Rect(span["bbox"]).intersects(area):
                    color = span["color"]
                    rgb = tuple(((color >> shift) & 255) / 255 for shift in (16, 8, 0))
                    font = span["font"].lower()
                    family = "tiro" if "times" in font else "cour" if "courier" in font else "helv"
                    return float(span["size"]), rgb, family
    return 10.0, (0, 0, 0), "helv"


def _replace_code(doc: pymupdf.Document, old_code: str, new_code: str) -> int:
    replacements = 0
    for page in doc:
        areas = page.search_for(old_code, quads=False)
        # Do not redact text spanning rotated lines: an explicit error preserves the original.
        for block in page.get_text("dict", flags=pymupdf.TEXTFLAGS_DICT & ~pymupdf.TEXT_PRESERVE_IMAGES)["blocks"]:
            for line in block.get("lines", []):
                if any(pymupdf.Rect(line["bbox"]).intersects(area) for area in areas):
                    if abs(line["dir"][0] - 1) > 0.01 or abs(line["dir"][1]) > 0.01:
                        raise CertificateError("El código está girado. Usa un PDF con texto horizontal para corregirlo.")
        styled = [(area, *_style(page, area)) for area in areas]
        for area, _, _, _ in styled:
            page.add_redact_annot(area, fill=False, cross_out=False)
        if areas:
            page.apply_redactions(images=0, graphics=0, text=0)
        for area, font_size, color, font in styled:
            width = pymupdf.get_text_length(new_code, fontname=font, fontsize=font_size)
            if width > area.width:
                font_size *= area.width / width
            if font_size < 5:
                raise CertificateError("No hay espacio suficiente para reemplazar el código sin alterar el documento.")
            ascender = pymupdf.Font(font).ascender
            page.insert_text((area.x0, area.y0 + ascender * font_size), new_code,
                             fontsize=font_size, fontname=font, color=color)
            replacements += 1
    if not replacements:
        raise CertificateError("El código se detectó, pero no se pudo ubicar para corregirlo.")
    return replacements


def _occupied(page: pymupdf.Page) -> list[pymupdf.Rect]:
    areas = [pymupdf.Rect(block[:4]) for block in page.get_text("blocks")]
    areas.extend(pymupdf.Rect(info["bbox"]) for info in page.get_image_info())
    areas.extend(pymupdf.Rect(drawing["rect"]) for drawing in page.get_drawings())
    # Clickable annotations can also contain content or navigation.
    areas.extend(pymupdf.Rect(link["from"]) for link in page.get_links())
    if page.annots():
        areas.extend(annotation.rect for annotation in page.annots())
    return areas


def _stamp_position(page: pymupdf.Page, with_signature: bool) -> pymupdf.Rect | None:
    width, height = (250, 110) if with_signature else (180, 110)
    bounds = page.rect
    if bounds.width < width + 40 or bounds.height < height + 40 or page.rotation:
        return None
    occupied = _occupied(page)
    # Prefer whitespace below a guide text; otherwise search empty footer space.
    guides = ["verificar certificado", "código qr", "codigo qr", "validar certificado"]
    candidates = []
    for guide in guides:
        for anchor in page.search_for(guide):
            x = min(max(anchor.x0, 20), bounds.width - width - 20)
            candidates.append(pymupdf.Rect(x, anchor.y1 + 8, x + width, anchor.y1 + height + 8))
    for y in range(int(bounds.height - height - 20), max(20, int(bounds.height / 2)), -20):
        for x in (bounds.width - width - 20, 20, (bounds.width - width) / 2):
            candidates.append(pymupdf.Rect(x, y, x + width, y + height))
    for candidate in candidates:
        if not bounds.contains(candidate):
            continue
        padded = candidate + (-4, -4, 4, 4)
        if not any(padded.intersects(area) for area in occupied):
            return candidate
    return None


def prepare_pdf(data: bytes, requested_code: str, verification_url: str,
                signature: bytes | None = None, template: bool = False,
                background_digests: tuple[bytes, ...] = (), automatic_qr: bool = False,
                letterhead=None) -> PreparedPDF:
    if letterhead is not None:
        requested_code,signature,template,automatic_qr='',None,True,False
    requested_code = normalized_code(requested_code)
    if not data.startswith(b"%PDF-"):
        raise CertificateError("El archivo no es un PDF válido.")
    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except (pymupdf.FileDataError, RuntimeError) as exc:
        raise CertificateError("El PDF está dañado o no se puede leer.") from exc
    with doc:
        if doc.needs_pass:
            raise CertificateError("El PDF tiene contraseña. Sube una copia sin contraseña.")
        if not 1 <= doc.page_count <= MAX_PAGES:
            raise CertificateError(f"El PDF debe tener entre 1 y {MAX_PAGES} páginas.")
        # Rewriting a digitally signed file invalidates its signature.
        for page in doc:
            if any(widget.field_type == pymupdf.PDF_WIDGET_TYPE_SIGNATURE for widget in (page.widgets() or [])):
                raise CertificateError("Este PDF contiene un campo de firma digital. No se modificará para preservar su firma.")
        if letterhead is not None:
            from app.backgrounds import apply_letterhead
            apply_letterhead(doc,letterhead,preserve_page_size=True,verify=False)
            background_digests=(letterhead.digest,)
        original_text = "\n".join(page.get_text() for page in doc)
        raw_codes = list(dict.fromkeys(code.upper() for code in CODE.findall(original_text)))
        codes = list(dict.fromkeys(re.sub(r"\s+", "", code) for code in raw_codes))
        warnings = []
        replacements = 0
        final_code = codes[0] if len(codes) == 1 else None
        if not codes:
            warnings.append("No se detectó un código en el texto. No se modificó el código; los PDF escaneados necesitan OCR previo.")
        elif len(codes) > 1:
            warnings.append("Se encontraron varios códigos distintos. Se conservaron todos sin cambios.")
        elif requested_code and requested_code != codes[0]:
            replacements = sum(_replace_code(doc, code, requested_code) for code in raw_codes)
            final_code = requested_code
            resulting = "\n".join(page.get_text() for page in doc).upper()
            remaining = [re.sub(r"\s+", "", code) for code in CODE.findall(resulting)]
            if codes[0] in remaining or requested_code not in remaining:
                raise CertificateError("No se pudo verificar el reemplazo completo del código.")
        signature = signature_png(signature)
        qr_buffer = BytesIO()
        qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, border=4, box_size=8)
        qr.add_data(verification_url)
        qr.make(fit=True)
        qr_image = qr.make_image(fill_color="black", back_color="white").convert("RGBA")
        # Keep the four-module clear margin, letting the page show through it.
        # White cells inside the QR stay opaque to preserve contrast for scanning.
        padding = qr.border * qr.box_size
        alpha = Image.new("L", qr_image.size, 0)
        alpha.paste(255, (padding, padding, qr_image.width - padding, qr_image.height - padding))
        qr_image.putalpha(alpha)
        qr_image.save(qr_buffer, format="PNG")
        original_pages = doc.page_count
        stamped_pages = []
        appended = False
        if template:
            from app.pdf_qr import verify_template_qr
            guides = _template_guides(doc[0])
            if letterhead is not None:
                if len(guides)!=1:
                    raise CertificateError('El PDF necesita una única indicación «Escanee este QR» legible en la primera página para colocar el QR sin mover su contenido.')
                from app.pdf_certificate import prepare_qr_slot
                prepare_qr_slot(doc,guides[0],qr_buffer.getvalue())
            if not guides:
                qr_area, inserted = _replace_existing_template_qr(doc, qr_buffer.getvalue(), verification_url, signature,
                                                                 background_digests, automatic_qr)
                warnings.append("La plantilla no incluía QR; se añadió en un espacio libre de la primera página, conservando su contenido y paginación." if inserted else
                                "Se actualizó el QR existente para enlazar al nuevo PDF; se conservó su ubicación.")
            elif len(guides) != 1:
                raise CertificateError("Se detectaron varios espacios para el QR en la primera página. Deja una sola indicación para ubicarlo.")
            else:
                qr_area = _insert_template_stamp(doc[0], guides[0], qr_buffer.getvalue(), signature, verification_url, background_digests)
            stamped_pages.append(1)
        else:
            for index in range(original_pages):
                page = doc[index]
                area = _stamp_position(page, bool(signature))
                if area is None:
                    continue
                _insert_stamp(page, area, qr_buffer.getvalue(), signature, verification_url)
                stamped_pages.append(index + 1)
        if not stamped_pages:
            page = doc.new_page(width=595, height=842)
            page.insert_text((40, 70), "Verificacion del certificado", fontsize=20)
            page.insert_text((40, 100), "Escanea el QR para consultar el documento registrado en el portal.", fontsize=10)
            _insert_stamp(page, pymupdf.Rect(40, 130, 290, 240), qr_buffer.getvalue(), signature, verification_url)
            stamped_pages.append(doc.page_count)
            appended = True
        if appended:
            warnings.append("No había espacio libre suficiente. El QR y la firma opcional se colocaron en una página adicional.")
        elif not template and len(stamped_pages) < original_pages:
            warnings.append("El QR y la firma opcional se insertaron solo en las páginas con espacio libre.")
        if letterhead is not None:
            from app.pdf_images import compact_backgrounds
            compact_backgrounds(doc,background_digests)
        result = doc.tobytes(garbage=4, deflate=True, use_objstms=1,
                             encryption=pymupdf.PDF_ENCRYPT_NONE)
        background_pages=[]
        if template:
            with pymupdf.open(stream=result,filetype='pdf') as final:
                verify_template_qr(final, qr_area, verification_url, qr_buffer.getvalue(), guides[0] if guides else None)
                if letterhead is not None:
                    from app.backgrounds import verify_letterhead
                    background_pages=verify_letterhead(final,letterhead,preserve_page_size=True)
        prepared=PreparedPDF(result, codes, final_code, replacements, warnings, stamped_pages, bool(signature))
        if letterhead is not None:
            prepared.certificate_type=letterhead.kind
            prepared.background_pages=background_pages
            prepared.format_preserved=True
        return prepared


def _replace_existing_template_qr(doc: pymupdf.Document, qr: bytes, url: str,
                                 signature: bytes | None, background_digests=(), automatic=False) -> tuple[pymupdf.Rect, bool]:
    """Recognize a saved Drive QR instead of guessing a blank slot in a template."""
    slots, other_qr = [], False
    page = doc[0]
    for info in page.get_image_info(xrefs=True):
        area = pymupdf.Rect(info["bbox"])
        if not info["xref"] or not (45 <= area.width <= 130 and .8 <= area.width / area.height <= 1.2):
            continue
        pixmap = page.get_pixmap(matrix=pymupdf.Matrix(3, 3), clip=area)
        image = Image.open(BytesIO(pixmap.tobytes("png")))
        decoded = zxingcpp.read_barcodes(image, formats=zxingcpp.BarcodeFormat.QRCode)
        if len(decoded) == 1 and re.match(r"https://drive\.google\.com/file/d/[A-Za-z0-9_-]+/view(?:[?#]|$)", decoded[0].text):
            slots.append((area, info["xref"]))
        elif decoded:
            other_qr = True
    if not slots and not other_qr and automatic and background_digests and not signature:
        from app.pdf_qr import insert_automatic_qr
        return insert_automatic_qr(page, qr, url, background_digests), True
    if len(slots) != 1:
        raise CertificateError("No se pudo identificar el espacio para el QR de forma única en la primera página. Usa una indicación Escanee este QR o conserva un QR de Drive legible en su posición.")
    area, xref = slots[0]
    # Removing this image also removes repeated uses on later pages, without
    # redacting the full-page background or any certificate text below it.
    page.delete_image(xref)
    for link in page.get_links():
        if area.intersects(pymupdf.Rect(link["from"])):
            page.delete_link(link)
    side = min(area.width, area.height)
    target = pymupdf.Rect(area.x0, area.y0, area.x0 + side, area.y0 + side)
    page.insert_image(target, stream=qr)
    page.insert_link({"kind": pymupdf.LINK_URI, "from": target, "uri": url})
    if signature:
        raise CertificateError("Esta plantilla tiene un QR existente y un bloque de firma diferente. Conserva la firma del Excel o usa la plantilla con Autorizado y firmado por para reemplazarla.")
    return target, False


def _template_guides(page: pymupdf.Page) -> list[pymupdf.Rect]:
    """Read printed cells/textboxes, including a guide wrapped onto several lines."""
    guides = []
    pattern = re.compile(r"escane\w*\s+(?:este\s+)?qr|c[oó]digo\s+qr", re.I)
    for block in page.get_text("dict", flags=pymupdf.TEXTFLAGS_DICT & ~pymupdf.TEXT_PRESERVE_IMAGES)["blocks"]:
        lines = block.get("lines", [])
        text, spans = "", []
        for line in lines:
            for span in line["spans"]:
                start = len(text)
                text += span["text"] + " "
                spans.append((start, len(text), span["bbox"]))
        for match in pattern.finditer(text):
            areas = [pymupdf.Rect(box) for start, end, box in spans
                     if start < match.end() and end > match.start()]
            if areas:
                area = pymupdf.Rect(areas[0])
                for other in areas[1:]:
                    area |= other
                guides.append(area)
    return guides


def _insert_template_stamp(page: pymupdf.Page, guide: pymupdf.Rect, qr: bytes,
                           signature: bytes | None, url: str, background_digests: tuple[bytes, ...] = ()) -> pymupdf.Rect:
    """Place the QR beside the printed guide and the signature beside its author."""
    if page.rotation:
        raise CertificateError("La hoja Certificado debe imprimirse sin giro de página.")
    from app.pdf_qr import qr_position
    qr_area = qr_position(page, guide, qr)
    areas = [qr_area]
    signature_area = None
    if signature:
        authors = page.search_for("Autorizado y firmador por") or page.search_for("Autorizado y firmado por")
        if len(authors) != 1:
            raise CertificateError("La plantilla necesita un único texto Autorizado y firmado por: para colocar la firma.")
        author = authors[0]
        signature_area = pymupdf.Rect(author.x0 - 82, author.y0 + 7.4, author.x0 - 4, author.y0 + 52.4)
        areas.append(signature_area)
    occupied = [pymupdf.Rect(span["bbox"]) for block in page.get_text("dict", flags=pymupdf.TEXTFLAGS_DICT & ~pymupdf.TEXT_PRESERVE_IMAGES)["blocks"]
                for line in block.get("lines", []) for span in line["spans"]]
    # The full-page letterhead is expected beneath the certificate content.
    # Read current paints after a previous QR/background was cleared; cached
    # get_image_info may still describe the old image on a single-page PDF.
    current_images = page.get_textpage(flags=pymupdf.TEXT_PRESERVE_IMAGES).extractIMGINFO(hashes=True)
    occupied.extend(pymupdf.Rect(info["bbox"]) for info in current_images
                    if not (info["digest"] in background_digests
                            and pymupdf.Rect(info["bbox"]).get_area() >= page.rect.get_area() * 0.9))
    for area in areas:
        if not page.rect.contains(area) or any(area.intersects(other) for other in occupied):
            raise CertificateError("No hay espacio libre al lado del texto guía. Revisa la posición del QR y la firma en la plantilla.")
    page.insert_image(qr_area, stream=qr)
    page.insert_link({"kind": pymupdf.LINK_URI, "from": qr_area, "uri": url})
    if signature_area is not None:
        page.insert_image(signature_area, stream=signature, keep_proportion=True)
    return qr_area


def _insert_stamp(page: pymupdf.Page, area: pymupdf.Rect, qr: bytes,
                  signature: bytes | None, url: str) -> None:
    page.draw_rect(area, color=(0.85, 0.87, 0.89), fill=(1, 1, 1), width=0.6)
    qr_area = pymupdf.Rect(area.x0 + 7, area.y0 + 7, area.x0 + 83, area.y0 + 83)
    page.insert_image(qr_area, stream=qr)
    page.insert_link({"kind": pymupdf.LINK_URI, "from": qr_area, "uri": url})
    if signature:
        page.insert_image(pymupdf.Rect(area.x0 + 94, area.y0 + 10, area.x1 - 8, area.y0 + 65),
                          stream=signature, keep_proportion=True)
        page.insert_text((area.x0 + 94, area.y0 + 79), "Firma autorizada", fontsize=8)
    else:
        page.insert_text((area.x0 + 88, area.y0 + 35), "Consulta el", fontsize=9)
        page.insert_text((area.x0 + 88, area.y0 + 49), "certificado", fontsize=9)
    page.insert_text((area.x0 + 9, area.y0 + 99), "Verificar en el portal QCP", fontsize=8)
