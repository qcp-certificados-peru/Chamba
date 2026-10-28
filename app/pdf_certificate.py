"""Keep a PDF certificate's content while clearing only its guided QR slot."""

from io import BytesIO

from PIL import Image
import pymupdf
import zxingcpp

from app.pdf import CertificateError
from app.pdf_qr import qr_position


def prepare_qr_slot(doc, guide, qr):
    page=doc[0]
    target=qr_position(page,guide,qr)
    # Replace only a decoded QR beside this guide. Other images and signatures
    # remain occupied; an image reused elsewhere must not be deleted globally.
    for info in page.get_image_info(xrefs=True):
        area=pymupdf.Rect(info['bbox'])
        if (not info['xref'] or not area.intersects(target) or not 45<=area.width<=130
                or not .8<=area.width/area.height<=1.2 or abs(area.x0-target.x0)>8):
            continue
        pixels=page.get_pixmap(matrix=pymupdf.Matrix(3,3),clip=area)
        decoded=zxingcpp.read_barcodes(Image.open(BytesIO(pixels.tobytes('png'))),
                                      formats=zxingcpp.BarcodeFormat.QRCode)
        if len(decoded)!=1:
            continue
        uses=sum(image['xref']==info['xref'] for other in doc for image in other.get_image_info(xrefs=True))
        if uses!=1:
            raise CertificateError('El QR del PDF se comparte entre varias posiciones. No se puede reemplazar solo el de consulta sin modificar las demás.')
        from app.pdf_images import clear_image_paint
        clear_image_paint(doc,info['xref'])
        for link in page.get_links():
            if area.intersects(pymupdf.Rect(link['from'])):
                page.delete_link(link)
    # Drawing/annotation readers use the updated document. Reloading a one-page
    # PDF can fail while MuPDF still holds another reference to the same page.
    # Text and images are checked by the shared Excel/PDF QR inserter. Also
    # protect vector table borders, annotations and unrelated links in PDFs.
    for drawing in page.get_drawings():
        fill=drawing.get('fill')
        # Excel/Acrobat can paint empty cells as white rectangles. Keep that
        # paint in place; text, images and borders are checked independently.
        white_fill=fill is not None and all(component>=.999 for component in fill)
        if (fill is not None and not white_fill and drawing.get('fill_opacity',1)>0
                and drawing['rect'].intersects(target)):
            raise CertificateError('El espacio junto a «Escanee este QR» contiene un cuadro del PDF. No se cubrió su contenido.')
        if drawing.get('color') is None or drawing.get('stroke_opacity',1)==0:
            continue
        for item in drawing['items']:
            if item[0]=='re':
                r=item[1]
                edges=[pymupdf.Rect(r.x0,r.y0,r.x1,r.y0),pymupdf.Rect(r.x0,r.y1,r.x1,r.y1),
                       pymupdf.Rect(r.x0,r.y0,r.x0,r.y1),pymupdf.Rect(r.x1,r.y0,r.x1,r.y1)]
            elif item[0]=='l':
                edges=[pymupdf.Rect(item[1],item[2]).normalize()]
            else:
                edges=[drawing['rect']]
            pad=max(.5,drawing.get('width',1)/2)
            if any((edge+(-pad,-pad,pad,pad)).intersects(target)for edge in edges):
                raise CertificateError('El QR cubriría una línea o un cuadro del PDF. Deja libre el espacio junto a «Escanee este QR».')
    if any(target.intersects(annotation.rect)for annotation in (page.annots()or[])) or any(
            target.intersects(pymupdf.Rect(link['from']))for link in page.get_links()):
        raise CertificateError('El espacio del QR está ocupado por una anotación o un enlace del PDF.')
