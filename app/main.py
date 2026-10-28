from contextlib import asynccontextmanager, suppress
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone
import asyncio
import hashlib
import ipaddress
import logging
import multiprocessing
import os
from pathlib import Path
from time import perf_counter
import re
from urllib.parse import quote, urlsplit
from uuid import UUID, uuid4

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.pdf import CertificateError, normalized_code, CODE, CODE_PATTERN, MAX_CODE_LENGTH, CODE_ERROR
from app.documents import prepare_document
from app.excel import inspect_excel
from app.storage import DriveStorage, LocalStorage, StorageError, storage_from_environment
from app.backgrounds import background_choices, require_background_type
from app.uploads import InspectionCache

STATIC = Path(__file__).parent / "static"
MAX_PDF_BYTES = 15 * 1024 * 1024
MAX_SIGNATURE_BYTES = 2 * 1024 * 1024
MAX_REQUEST_BYTES = 18 * 1024 * 1024
logger = logging.getLogger(__name__)


def versioned_html(path: Path) -> str:
    def asset(match):
        name = match[1]
        digest = hashlib.sha256((STATIC / name).read_bytes()).hexdigest()[:16]
        return f"/static/{name}?v={digest}"
    return re.sub(r"/static/([a-zA-Z0-9_.-]+\.(?:js|css))", asset, path.read_text(encoding="utf-8"))


def local_url(url: str) -> bool:
    hostname = urlsplit(url).hostname
    if hostname == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname or "").is_loopback
    except ValueError:
        return False


class RequestGuard:
    """Bound streamed upload request bodies before processing certificates."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        is_upload = scope["method"] == "POST" and scope["path"] in {"/api/certificates", "/api/excel/inspect"}
        if not is_upload:
            return await self.app(scope, receive, send)
        headers = dict(scope["headers"])
        try:
            length = int(headers.get(b"content-length", b"0"))
        except ValueError:
            return await JSONResponse({"detail": "Tamaño de solicitud inválido."}, status_code=400)(scope, receive, send)
        if length > MAX_REQUEST_BYTES:
            return await JSONResponse({"detail": "La carga supera el límite permitido."}, status_code=413)(scope, receive, send)
        received = 0

        async def bounded_receive():
            nonlocal received
            message = await receive()
            received += len(message.get("body", b""))
            if received > MAX_REQUEST_BYTES:
                raise StarletteHTTPException(413, "La carga supera el límite permitido.")
            return message

        await self.app(scope, bounded_receive, send)


async def read_upload(file: UploadFile, limit: int) -> bytes:
    chunks = []
    count = 0
    try:
        while chunk := await file.read(64 * 1024):
            count += len(chunk)
            if count > limit:
                raise HTTPException(413, f"El archivo supera el límite de {limit // (1024 * 1024)} MB.")
            chunks.append(chunk)
    finally:
        await file.close()
    return b"".join(chunks)


def filename_safe(name: str | None, source_type: str = "pdf") -> str:
    name = (name or "CERTIFICADO.pdf").replace("\\", "/").split("/")[-1]
    name = re.sub(r"[\x00-\x1f\x7f]", "", name).strip()
    extensions = ("xlsx", "xlsm") if source_type == "xlsx" else (source_type,)
    extension = next((suffix for suffix in extensions if name.lower().endswith("." + suffix)), None)
    if extension is None:
        raise HTTPException(422, "El archivo debe tener extensión .xlsx o .xlsm." if source_type == "xlsx" else f"El archivo debe tener extensión .{source_type}.")
    return name[:-(len(extension) + 1)][:176] + ".pdf"


def create_app(storage_dir: Path | None = None, public_url: str | None = None,
               certificate_storage: LocalStorage | DriveStorage | None = None) -> FastAPI:
    storage = storage_dir or Path(os.environ.get("CERTIFICATE_STORAGE_DIR", "data/certificates"))
    origin = (public_url or os.environ.get("PUBLIC_BASE_URL") or os.environ.get("RENDER_EXTERNAL_URL")
              or "http://127.0.0.1:8000").rstrip("/")
    parsed = urlsplit(origin)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.path or parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise ValueError("PUBLIC_BASE_URL debe ser una URL http(s) sin ruta, credenciales, consulta ni fragmento.")
    is_local = local_url(origin)
    revision = os.environ.get('RENDER_GIT_COMMIT', '')
    if not is_local and parsed.scheme != "https":
        raise ValueError("PUBLIC_BASE_URL debe usar HTTPS para publicar el portal.")
    quota = int(os.environ.get("MAX_STORAGE_MB", "1024")) * 1024 * 1024
    store = certificate_storage or (LocalStorage(storage, quota) if storage_dir else storage_from_environment(storage, quota))
    inspections = InspectionCache()

    async def expire_inspections():
        while True:
            await asyncio.sleep(60)
            await asyncio.to_thread(inspections.expire)

    @asynccontextmanager
    async def lifespan(app):
        await asyncio.to_thread(store.start)
        app.state.executor = ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context("spawn"))
        app.state.processing = asyncio.Semaphore(1)
        cleanup = asyncio.create_task(expire_inspections())
        try:
            yield
        finally:
            cleanup.cancel()
            with suppress(asyncio.CancelledError):
                await cleanup
            app.state.executor.shutdown(wait=True, cancel_futures=True)
            await asyncio.to_thread(inspections.close)
            await asyncio.to_thread(store.close)

    app = FastAPI(title="Portal de certificados QCP", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(RequestGuard)
    pages = {name: versioned_html(STATIC / name)
             for name in ("index.html", "privacy.html", "terms.html", "verify.html")}

    @app.exception_handler(StorageError)
    async def storage_error(request, error):
        return JSONResponse({"detail": str(error)}, status_code=error.status)

    @app.middleware("http")
    async def security_headers(request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        if re.fullmatch(r'[0-9a-f]{40}', revision):
            response.headers['X-App-Revision'] = revision
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data: blob:; script-src 'self'; "
            "style-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        )
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        elif request.url.path.startswith("/static/") or response.headers.get("content-type", "").startswith("text/html"):
            response.headers.setdefault("Cache-Control", "no-cache")
        return response

    @app.get("/healthz")
    async def health():
        return {"status": "ok"}

    @app.get("/api/pruebas/qr-portable.pdf")
    async def portable_test_source():
        from app.portable_smoke import fixture_pdf
        return Response(fixture_pdf(), media_type="application/pdf", headers={"Cache-Control": "no-store"})

    @app.get("/api/config")
    async def configuration():
        return {"requiresAccessKey": False, "uploadsEnabled": store.ready,
                "localMode": is_local and store.backend == "local", "storageBackend": store.backend,
                "maxPdfMB": 15, "maxExcelMB": 15, "maxSignatureMB": 2, "maxPages": 100,
                "certificateCodePattern": CODE_PATTERN, "maxCodeLength": MAX_CODE_LENGTH,
                "inputFormats": ["xlsx", "xlsm", "pdf"], "certificateBackgrounds": background_choices()}

    @app.get("/")
    async def home():
        return HTMLResponse(pages["index.html"])

    @app.get("/privacidad")
    async def privacy():
        return HTMLResponse(pages["privacy.html"])

    @app.get("/condiciones")
    async def terms():
        return HTMLResponse(pages["terms.html"])

    async def stored_info(certificate_id: UUID, key: str | None):
        if isinstance(store, DriveStorage):
            return await asyncio.to_thread(store.metadata, str(certificate_id), key)
        return await asyncio.to_thread(store.metadata, str(certificate_id))

    @app.get("/certificados/{certificate_id}")
    async def certificate_page(certificate_id: UUID, key: str | None = None):
        await stored_info(certificate_id, key)
        return HTMLResponse(pages["verify.html"], headers={"Cache-Control": "no-store"})

    @app.get("/api/certificates/{certificate_id}")
    async def details(certificate_id: UUID, key: str | None = None):
        return await stored_info(certificate_id, key)

    @app.get("/api/certificates/{certificate_id}/download")
    async def download(certificate_id: UUID, key: str | None = None):
        info = await stored_info(certificate_id, key)
        if isinstance(store, LocalStorage):
            return FileResponse(store.local_pdf(str(certificate_id)), media_type="application/pdf",
                                filename=info["filename"], headers={"Cache-Control": "no-store"})
        pdf = await asyncio.to_thread(store.download, info)
        return Response(pdf, media_type="application/pdf", headers={"Cache-Control": "no-store",
            "Content-Disposition": f"attachment; filename*=utf-8''{quote(info['filename'])}"})

    @app.get("/q/{certificate_id}/{public_key}")
    async def portable_certificate(certificate_id: UUID, public_key: str):
        if not isinstance(store, DriveStorage):
            raise HTTPException(404, "Este QR requiere el almacenamiento de Drive.")
        for attempt in range(2):
            info = await asyncio.to_thread(store.portable_metadata, str(certificate_id), public_key)
            try:
                pdf = await asyncio.to_thread(store.download, info)
            except StorageError as error:
                # The original can disappear between discovery and download.
                # Resolve once more, still requiring the same key and PDF hash.
                if attempt or error.status not in {404, 409}:
                    raise
                continue
            return Response(pdf, media_type="application/pdf", headers={"Cache-Control": "no-store",
                "Content-Disposition": f"inline; filename*=utf-8''{quote(info['filename'])}"})

    @app.post("/api/certificates", status_code=201)
    async def create_certificate(response: Response, excel: UploadFile | None = File(None), pdf: UploadFile | None = File(None), codigoCorrecto: str = Form(""),
                                 firma: UploadFile | None = File(None), tipoCertificado: str = Form(""),
                                 codigoFormato: str = Form(''), versionFormato: str = Form(''),
                                 excelToken: str = Form(""), pruebaCopia: bool = Form(False)):
        started = perf_counter()
        if not store.ready:
            raise StorageError("Falta completar la conexión con Google Drive. Contacta al administrador.")
        if sum((excel is not None, pdf is not None, bool(excelToken))) != 1:
            raise HTTPException(422, "Sube un Excel .xlsx, .xlsm o un PDF por solicitud.")
        source_type = "xlsx" if excel or excelToken else "pdf"
        pdf_only=source_type=='pdf' and bool(tipoCertificado)
        if pdf_only:
            codigoCorrecto=''
        if excelToken:
            cached = await asyncio.to_thread(inspections.get, excelToken)
            if cached is None:
                raise HTTPException(410, "La copia temporal del Excel caducó. Vuelve a enviar el archivo.")
            source_name, data = cached.filename, cached.data
        else:
            document = excel or pdf
            source_name = document.filename
            data = await read_upload(document, MAX_PDF_BYTES)
        filename = filename_safe(source_name, source_type)
        if pruebaCopia:
            from app.portable_smoke import fixture_pdf
            if not isinstance(store, DriveStorage) or source_type != "pdf" or data != fixture_pdf():
                raise HTTPException(422, "La prueba de copia solo admite el PDF ficticio SIN VALIDEZ proporcionado por el portal.")
        if len(codigoCorrecto) > MAX_CODE_LENGTH:
            raise HTTPException(422, CODE_ERROR)
        try:
            codigoCorrecto = normalized_code(codigoCorrecto)
        except CertificateError as exc:
            raise HTTPException(422, str(exc)) from exc
        if source_type == "xlsx":
            try:
                from app.excel_footers import entered_footer
                entered_footer(codigoFormato,versionFormato)
                layout = await asyncio.to_thread(inspect_excel, data)
                if layout.needs_background:
                    require_background_type(tipoCertificado)
            except CertificateError as exc:
                raise HTTPException(422, str(exc)) from exc
        elif pdf_only:
            try:
                require_background_type(tipoCertificado)
            except CertificateError as exc:
                raise HTTPException(422,str(exc))from exc
        signature = await read_upload(firma, MAX_SIGNATURE_BYTES) if firma and not pdf_only else None
        certificate_id = str(uuid4())
        inspected = perf_counter()
        try:
            await asyncio.wait_for(app.state.processing.acquire(), timeout=0.1)
        except TimeoutError:
            raise HTTPException(429, "El portal está procesando otros archivos. Inténtalo nuevamente en unos segundos.")
        try:
            publication = await asyncio.to_thread(store.allocate, certificate_id, origin)
            verification_url = publication.verification_url
            allocated = perf_counter()
            result = await asyncio.get_running_loop().run_in_executor(
                app.state.executor, prepare_document, data, source_type, codigoCorrecto, verification_url, signature,
                tipoCertificado, codigoFormato, versionFormato)
            processed = perf_counter()
            named_code = CODE.search(source_name or "")
            if named_code and result.final_code and named_code[0].upper() != result.final_code:
                result.warnings.append(f"El nombre del archivo dice {named_code[0].upper()}, pero el certificado contiene {result.final_code}. Se conservó el código del documento.")
            info = {"id": certificate_id, "filename": filename, "sourceType": source_type,
                    "createdAt": datetime.now(timezone.utc).isoformat(),
                    "detectedCodes": result.detected_codes, "finalCode": result.final_code,
                    "replacements": result.replacements, "warnings": result.warnings,
                    "stampPages": result.stamp_pages, "signatureIncluded": result.signature_included,
                    "sha256": hashlib.sha256(result.data).hexdigest(),
                    "verificationUrl": verification_url,
                    "downloadUrl": f"/api/certificates/{certificate_id}/download"}
            if publication.public_key:
                info["downloadUrl"] += f"?key={publication.public_key}"
            if source_type == "xlsx" or pdf_only:
                info.update(certificateType=result.certificate_type, backgroundVerified=bool(result.background_pages),
                            backgroundPages=result.background_pages, formatPreserved=result.format_preserved,
                            sheetName=result.sheet_name, formatFooterVerified=result.format_footer_verified)
            await asyncio.to_thread(store.save, certificate_id, result.data, info, publication)
            if pruebaCopia:
                info, proof = await asyncio.to_thread(store.exercise_fresh_test_copy, info)
                info["copyTest"] = proof
            if excelToken:
                await asyncio.to_thread(inspections.discard, excelToken)
            saved = perf_counter()
            stages = (("inspect", inspected - started), ("allocate", allocated - inspected),
                      ("process", processed - allocated), ("save", saved - processed))
            response.headers["Server-Timing"] = ", ".join(f"{name};dur={duration * 1000:.1f}" for name, duration in stages)
        except StorageError:
            raise
        except CertificateError as exc:
            raise HTTPException(422, str(exc)) from exc
        except Exception as exc:
            logger.exception("Error al procesar un certificado")
            raise HTTPException(500, "No se pudo procesar el certificado. Revisa el archivo o contacta al administrador.") from exc
        finally:
            app.state.processing.release()
        return info

    @app.post("/api/excel/inspect")
    async def inspect_workbook(excel: UploadFile = File(...)):
        filename_safe(excel.filename, "xlsx")
        data = await read_upload(excel, MAX_PDF_BYTES)
        try:
            layout = await asyncio.to_thread(inspect_excel, data)
        except CertificateError as exc:
            raise HTTPException(422, str(exc)) from exc
        result = {"sheetName": layout.sheet_name, "preserveFormat": layout.preserve_format,
                  "needsBackground": layout.needs_background}
        if layout.selection_reason:
            result["selectionReason"] = layout.selection_reason
        from app.excel_footers import inspect_format_footer
        footer = await asyncio.to_thread(inspect_format_footer,data)
        if footer:
            result['formatFooter'] = {'code':footer.code,'version':footer.version}
        token = await asyncio.to_thread(inspections.put, excel.filename, data)
        if token:
            result['uploadToken'] = token
        return result

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


app = create_app()
