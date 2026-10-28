"""Issue and move only the server's fixed SIN VALIDEZ fixture; never client files."""

import argparse
import hashlib
from io import BytesIO
import json
from time import perf_counter
from urllib.parse import urlsplit

import httpx
from PIL import Image
import pymupdf
import zxingcpp


def main():
    parser = argparse.ArgumentParser(description="Comprobar QR directo y movimiento de un PDF ficticio recién generado.")
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--expected-revision")
    args = parser.parse_args()
    base = args.url.rstrip("/")
    with httpx.Client(timeout=httpx.Timeout(180, connect=30), follow_redirects=False) as client:
        health = client.get(base + "/healthz")
        assert health.status_code == 200
        if args.expected_revision:
            assert health.headers.get("x-app-revision") == args.expected_revision
        config = client.get(base + "/api/config").json()
        assert config["storageBackend"] == "drive" and config["qrMode"] == "drive-direct"
        source = client.get(base + "/api/pruebas/qr-directo.pdf")
        assert source.status_code == 200 and source.content.startswith(b"%PDF-")
        started = perf_counter()
        response = client.post(base + "/api/certificates", files={
            "pdf": ("PRUEBA-QR-DIRECTO-SIN-VALIDEZ.pdf", source.content, "application/pdf")},
            data={"tipoCertificado": "no_acreditado", "pruebaMovimiento": "true"})
        assert response.status_code == 201, (response.status_code, response.text[:1000])
        elapsed = perf_counter() - started
        info = response.json()
        assert all(info["moveTest"][key] for key in ("ok", "sameFileId", "sameQr", "movedOutsideOutputFolder", "renamed")), info["moveTest"]
        assert info["qrMode"] == "drive-direct" and "qrPublicKey" not in info
        expected_url = f"https://drive.google.com/file/d/{info['driveFileId']}/view"
        assert info["verificationUrl"] == info["driveUrl"] == expected_url
        pdf = client.get(base + info["downloadUrl"])
        assert pdf.status_code == 200 and hashlib.sha256(pdf.content).hexdigest() == info["sha256"]
        with pymupdf.open(stream=pdf.content, filetype="pdf") as doc:
            assert len(doc) == 1 and "SIN VALIDEZ" in doc[0].get_text()
            links = [link for link in doc[0].get_links() if link.get("uri") == expected_url]
            assert len(links) == 1
            pixels = doc[0].get_pixmap(matrix=pymupdf.Matrix(3, 3), clip=links[0]["from"])
            qr = zxingcpp.read_barcode(Image.open(BytesIO(pixels.tobytes("png"))))
            assert qr and qr.text == expected_url

    # Scan in a fresh anonymous client that refuses every non-Google request.
    # No portal session, redirects from Render or issuing laptop are involved.
    def only_google(request):
        assert request.url.host == "drive.google.com", "El escaneo intentó consultar un intermediario"

    with httpx.Client(timeout=45, follow_redirects=False, event_hooks={"request": [only_google]}) as scanner:
        started = perf_counter()
        viewer = scanner.get(qr.text)
        viewer_seconds = perf_counter() - started
        assert viewer.status_code == 200
        assert "PRUEBA-MOVIDO-QR-DIRECTO-SIN-VALIDEZ.pdf" in viewer.text
        assert urlsplit(str(viewer.request.url)).hostname == "drive.google.com"
    print(json.dumps({"source": "server-generated SIN VALIDEZ PDF", "pages": 1,
        "issueAndMoveSeconds": round(elapsed, 2), "viewerSeconds": round(viewer_seconds, 3),
        "sameFileIdAfterMoveAndRename": True, "noCopyOrDeletion": True, "qrDecoded": True,
        "sha256Verified": True, "anonymousDriveViewer": True, "scanWithoutPortal": True,
        "qrUrl": expected_url}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
