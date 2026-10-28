"""Exercise only a server-generated SIN VALIDEZ PDF, never customer uploads."""

import argparse
import hashlib
from io import BytesIO
import json
from time import perf_counter

import httpx
from PIL import Image
import pymupdf
import zxingcpp


def main():
    parser = argparse.ArgumentParser(description="Comprobar copia nativa y eliminación de un PDF ficticio recién generado.")
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--expected-revision")
    args = parser.parse_args()
    base = args.url.rstrip("/")
    with httpx.Client(timeout=httpx.Timeout(180, connect=30)) as client:
        health = client.get(base + "/healthz")
        assert health.status_code == 200
        if args.expected_revision:
            assert health.headers.get("x-app-revision") == args.expected_revision
        assert client.get(base + "/api/config").json()["storageBackend"] == "drive"
        source = client.get(base + "/api/pruebas/qr-portable.pdf")
        assert source.status_code == 200 and source.content.startswith(b"%PDF-")
        started = perf_counter()
        response = client.post(base + "/api/certificates", files={
            "pdf": ("PRUEBA-QR-PORTABLE-SIN-VALIDEZ.pdf", source.content, "application/pdf")},
            data={"tipoCertificado": "no_acreditado", "pruebaCopia": "true"})
        assert response.status_code == 201, (response.status_code, response.text[:1000])
        elapsed = perf_counter() - started
        info = response.json()
        assert info["copyTest"]["ok"], info["copyTest"]
        assert info["copyTest"]["originalDeleted"] and info["copyTest"]["privatePropertiesRemoved"]
        assert info['copyTest']['customPropertiesRemoved']
        assert info["driveFileId"] != info["issuedDriveFileId"]
        assert info["copyViewerAccess"] == "anyone-reader"
        assert info["verificationUrl"].startswith(base + "/q/")
        qr_response = client.get(info["verificationUrl"], follow_redirects=False)
        assert qr_response.status_code == 303
        assert qr_response.headers["location"] == info["driveUrl"]
        assert qr_response.headers["cache-control"] == "no-store"
        assert not qr_response.content and "content-disposition" not in qr_response.headers
        viewer = client.get(qr_response.headers["location"], follow_redirects=False)
        assert viewer.status_code == 200, (viewer.status_code, "La copia no abre en el visor anónimo de Drive")
        assert "PRUEBA-COPIA-QR-PORTABLE-SIN-VALIDEZ.pdf" in viewer.text
        pdf = client.get(base + info["downloadUrl"])
        assert pdf.status_code == 200
        assert hashlib.sha256(pdf.content).hexdigest() == info["sha256"]
        with pymupdf.open(stream=pdf.content, filetype="pdf") as doc:
            assert len(doc) == 1 and "SIN VALIDEZ" in doc[0].get_text()
            links = [link for link in doc[0].get_links() if link.get("uri") == info["verificationUrl"]]
            assert len(links) == 1
            pixels = doc[0].get_pixmap(matrix=pymupdf.Matrix(3, 3), clip=links[0]["from"])
            qr = zxingcpp.read_barcode(Image.open(BytesIO(pixels.tobytes("png"))))
            assert qr and qr.text == info["verificationUrl"]
        print(json.dumps({"source": "server-generated SIN VALIDEZ PDF", "pages": 1,
            "seconds": round(elapsed, 2), "serverTiming": response.headers.get("server-timing"),
            "originalPermanentlyDeleted": True, "copyWithoutPrivateProperties": True,
            "qrDecoded": True, "sha256Verified": True, "googleViewerRedirect": True,
            "anonymousDriveViewer": True, "qrUrl": info["verificationUrl"],
            "copyDriveUrl": info["driveUrl"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
