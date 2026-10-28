"""Local development storage and durable Google Drive publication."""

from dataclasses import dataclass, field
import hashlib
import json
import logging
import os
import random
from pathlib import Path
import re
import threading
import time
from uuid import UUID, uuid4

import httpx

from app.drive_records import decode_record, encode_record, original_file
from app.portable_qr import KEY, MODE, signed_record, verified_record
from app.drive_errors import MESSAGES, RATE_LIMITS, operation_name, rejection_reason, retry_after

logger = logging.getLogger(__name__)
DRIVE_API = "https://www.googleapis.com/drive/v3"
DRIVE_UPLOAD = "https://www.googleapis.com/upload/drive/v3"
TOKEN_URL = "https://oauth2.googleapis.com/token"
FILE_ID = re.compile(r"[A-Za-z0-9_-]{10,200}")
MAX_DOWNLOAD = 64 * 1024 * 1024


class StorageError(Exception):
    def __init__(self, message: str, status: int = 503, *, code: str | None = None,
                 reason: str | None = None, operation: str | None = None,
                 upstream_status: int | None = None, retry_seconds: int | None = None):
        self.status = status
        self.code = code
        self.reason = reason
        self.operation = operation
        self.upstream_status = upstream_status
        self.retry_seconds = retry_seconds
        super().__init__(message)


@dataclass(frozen=True)
class Publication:
    verification_url: str
    drive_file_id: str | None = None
    public_key: str | None = None
    signing_key: bytes | None = field(default=None, repr=False)


class LocalStorage:
    backend = "local"
    qr_mode = "portal-local"
    ready = True

    def __init__(self, directory: Path, quota: int):
        self.directory = directory
        self.quota = quota
        self.lock = threading.Lock()

    def start(self):
        self.directory.mkdir(parents=True, exist_ok=True)

    def close(self):
        pass

    def allocate(self, certificate_id: str, origin: str) -> Publication:
        return Publication(f"{origin}/certificados/{certificate_id}")

    def save(self, certificate_id: str, pdf: bytes, metadata: dict, publication: Publication):
        with self.lock:
            used = sum(path.stat().st_size for path in self.directory.glob("*/*") if path.is_file())
            if used + len(pdf) + 8192 > self.quota:
                raise StorageError("El almacenamiento está lleno. Contacta al administrador.", 507)
            directory = self.directory / str(UUID(certificate_id))
            if directory.exists():
                raise StorageError("El certificado ya existe; se conservó el registro anterior.", 409)
            try:
                directory.mkdir(mode=0o700)
                (directory / "certificate.pdf").write_bytes(pdf)
                temporary = directory / "metadata.tmp"
                temporary.write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
                temporary.replace(directory / "metadata.json")
            except OSError:
                for name in ("certificate.pdf", "metadata.tmp", "metadata.json"):
                    (directory / name).unlink(missing_ok=True)
                if directory.exists():
                    directory.rmdir()
                raise StorageError("No se pudo guardar el certificado. Contacta al administrador.", 507) from None

    def metadata(self, certificate_id: str) -> dict:
        directory = self.directory / str(UUID(certificate_id))
        path = directory / "metadata.json"
        if not path.is_file() or not (directory / "certificate.pdf").is_file():
            raise StorageError("Certificado no encontrado.", 404)
        return json.loads(path.read_text(encoding="utf-8"))

    def local_pdf(self, certificate_id: str) -> Path:
        return self.directory / str(UUID(certificate_id)) / "certificate.pdf"


class DriveStorage:
    backend = "drive"
    qr_mode = "drive-direct"

    def __init__(self, folder_id: str, client_id: str, client_secret: str, refresh_token: str,
                 client: httpx.Client | None = None):
        self.folder_id = folder_id.strip()
        self.client_id = client_id.strip()
        self.client_secret = client_secret.strip()
        self.refresh_token = refresh_token.strip()
        self.ready = bool(FILE_ID.fullmatch(self.folder_id) and self.client_id and self.client_secret and self.refresh_token)
        self.client = client or httpx.Client(timeout=httpx.Timeout(45, connect=10), follow_redirects=False)
        self.token = ""
        self.token_deadline = 0.0
        self.token_lock = threading.Lock()
        self.write_lock = threading.Lock()
        self.registry_folders = None
        self.registry_record_ids = {}

    def start(self):
        # A missing configuration leaves the home page usable, with uploads disabled.
        pass

    def close(self):
        self.client.close()

    def _token(self) -> str:
        if not self.ready:
            raise StorageError("Falta completar la conexión con Google Drive. Contacta al administrador.")
        with self.token_lock:
            if time.monotonic() < self.token_deadline:
                return self.token
            try:
                response = self.client.post(TOKEN_URL, data={
                    "grant_type": "refresh_token", "client_id": self.client_id,
                    "client_secret": self.client_secret, "refresh_token": self.refresh_token,
                })
                if response.status_code in {400, 401, 403}:
                    raise StorageError("La autorización de Google Drive no es válida o fue revocada. Contacta al administrador.")
                response.raise_for_status()
                result = response.json()
                token = result["access_token"]
                expires = int(result.get("expires_in", 3600))
                if not isinstance(token, str) or not token or expires <= 60:
                    raise ValueError
            except (httpx.HTTPError, ValueError, KeyError, TypeError):
                raise StorageError("No se pudo renovar la conexión con Google Drive. Inténtalo de nuevo.") from None
            self.token = token
            self.token_deadline = time.monotonic() + expires - 60
            return token

    def _request(self, method: str, path: str, *, upload: bool = False, **kwargs) -> httpx.Response:
        extra_headers = kwargs.pop("headers", {})
        operation = operation_name(method, path, upload)
        rate_retries = 0
        renewed = False
        while True:
            token = self._token()
            headers = {"Authorization": f"Bearer {token}", **extra_headers}
            try:
                response = self.client.request(method, (DRIVE_UPLOAD if upload else DRIVE_API) + path,
                                               headers=headers, **kwargs)
            except httpx.HTTPError:
                # A network failure can happen after Google accepted a write.
                # Do not automatically repeat an upload with an unknown outcome.
                raise StorageError("No se pudo conectar con Google Drive. Revisa Drive antes de volver a emitir el certificado.",
                                   code="DRIVE_NETWORK", operation=operation) from None
            if response.status_code == 401 and not renewed:
                with self.token_lock:
                    # Another request may already have refreshed this token.
                    if self.token == token:
                        self.token_deadline = 0
                renewed = True
                continue
            reason = rejection_reason(response) if response.status_code in {403, 429} else None
            if reason in RATE_LIMITS and rate_retries < 2:
                instructed = retry_after(response)
                if instructed is None or instructed <= 5:
                    delay = max(2**rate_retries + random.uniform(0, .25), instructed or 0)
                    logger.warning("Drive temporary rejection: operation=%s http=%s reason=%s retry=%s",
                                   operation, response.status_code, reason, rate_retries + 1)
                    time.sleep(delay)
                    rate_retries += 1
                    continue
            break
        if response.status_code == 404:
            raise StorageError("El archivo o la carpeta no está disponible en Google Drive.", 404)
        if response.status_code in {403, 429}:
            code, message = MESSAGES.get(reason, ("DRIVE_REJECTED",
                "Google Drive rechazó esta operación sin indicar una causa reconocible. Comunica el código de este aviso al administrador."))
            logger.warning("Drive rejection: operation=%s http=%s reason=%s code=%s",
                           operation, response.status_code, reason or "unknown", code)
            raise StorageError(message, 507 if reason == "storageQuotaExceeded" else 503,
                               code=code, reason=reason, operation=operation, upstream_status=response.status_code,
                               retry_seconds=(retry_after(response) or 5) if reason in RATE_LIMITS else None)
        if response.status_code == 401:
            with self.token_lock:
                if self.token == token:
                    self.token_deadline = 0
            raise StorageError("Google Drive requiere renovar su autorización. El administrador debe revisar la conexión.",
                               code="DRIVE_AUTH", operation=operation, upstream_status=401)
        if response.status_code < 200 or response.status_code >= 300:
            raise StorageError("Google Drive no pudo completar la operación. Inténtalo de nuevo.")
        return response

    def check(self) -> dict:
        """Read-only folder/account check; never expose identities or quota sizes."""
        self._root()
        about = self._json(self._request("GET", "/about", params={"fields": "storageQuota(limit,usage)"}))
        quota = about.get("storageQuota", {})
        storage = "not_reported"
        try:
            limit, used = int(quota["limit"]), int(quota["usage"])
        except (KeyError, ValueError, TypeError):
            pass
        else:
            if 0 <= limit <= used:
                raise StorageError(MESSAGES["storageQuotaExceeded"][1], 507, code="DRIVE_STORAGE_FULL",
                                   reason="storageQuotaExceeded", operation="check_storage")
            if limit >= 0 and used >= 0:
                storage = "available"
        return {"backend": "drive", "status": "ok", "folderWritable": True, "storage": storage}

    def _json(self, response: httpx.Response) -> dict:
        try:
            result = response.json()
            if not isinstance(result, dict):
                raise ValueError
            return result
        except ValueError:
            raise StorageError("Google Drive devolvió una respuesta no válida.") from None

    def _root(self):
        folder = self._json(self._request("GET", f"/files/{self.folder_id}", params={
            "fields": "id,mimeType,trashed,capabilities(canAddChildren)", "supportsAllDrives": "true"}))
        if folder.get("mimeType") != "application/vnd.google-apps.folder" or folder.get("trashed"):
            raise StorageError("La carpeta configurada en Google Drive no está disponible.")
        if folder.get("capabilities", {}).get("canAddChildren") is not True:
            raise StorageError("La cuenta conectada no puede guardar archivos en la carpeta de Google Drive.")
        permissions = self._json(self._request("GET", f"/files/{self.folder_id}/permissions", params={
            "fields": "permissions(type),nextPageToken", "pageSize": 100, "supportsAllDrives": "true"}))
        if permissions.get("nextPageToken") or any(row.get("type") in {"anyone", "domain"}
                                                   for row in permissions.get("permissions", [])):
            raise StorageError("Usa una carpeta de Drive restringida a personas concretas; solo se publicará cada PDF final.")

    def _list(self, parent: str | None, extra: str, *, include_trashed=False) -> list[dict]:
        location = f"'{parent}' in parents and " if parent else ""
        active = '' if include_trashed else 'trashed = false and '
        params = {
            "q": f"{location}{active}({extra})",
            "fields": "files(id,name,mimeType,appProperties,properties,trashed,sha256Checksum,size),nextPageToken,incompleteSearch", "pageSize": 100,
            "supportsAllDrives": "true", "includeItemsFromAllDrives": "true"}
        rows, tokens = [], set()
        while True:
            result = self._json(self._request("GET", "/files", params=params))
            if result.get("incompleteSearch"):
                raise StorageError("Google Drive no pudo completar la búsqueda del certificado. Inténtalo de nuevo.")
            page = result.get("files", [])
            if not isinstance(page, list) or any(not isinstance(row, dict) or not FILE_ID.fullmatch(str(row.get("id", ""))) for row in page):
                raise StorageError("Google Drive devolvió un listado no válido.")
            rows.extend(page)
            token = result.get("nextPageToken")
            if len(rows) > 1000 or (token and (not isinstance(token, str) or token in tokens)):
                raise StorageError("Hay demasiados registros o copias para este certificado en Drive.")
            if not token:
                return rows
            tokens.add(token)
            params["pageToken"] = token

    def _record_pdf(self, certificate_id: str) -> dict | None:
        identifier = str(UUID(certificate_id))
        # Search by immutable publication ID, irrespective of its current folder.
        rows = self._list(None, "mimeType = 'application/pdf' and "
                          f"appProperties has {{ key='qcp_certificate_id' and value='{identifier}' }}")
        issued = [row for row in rows if original_file(row.get("appProperties", {}), row["id"])]
        if len(issued) > 1:
            raise StorageError("Hay registros duplicados para este certificado. Contacta al administrador.")
        return issued[0] if issued else None

    def _record_folder(self, certificate_id: str) -> str:
        identifier = str(UUID(certificate_id))
        rows = self._list(self.folder_id, "mimeType = 'application/vnd.google-apps.folder' and "
                          f"appProperties has {{ key='qcp_certificate_id' and value='{identifier}' }}")
        if not rows:
            raise StorageError("Certificado no encontrado.", 404)
        if len(rows) != 1:
            raise StorageError("Hay registros duplicados para este certificado. Contacta al administrador.")
        return self._id(rows[0]["id"])

    def _id(self, value: str) -> str:
        if not isinstance(value, str) or not FILE_ID.fullmatch(value):
            raise StorageError("Google Drive devolvió un identificador no válido.")
        return value

    def allocate(self, certificate_id: str, origin: str) -> Publication:
        self._root()
        result = self._json(self._request("GET", "/files/generateIds", params={"count": 1, "space": "drive"}))
        try:
            file_id = self._id(result["ids"][0])
        except (KeyError, IndexError, TypeError):
            raise StorageError("No se pudo reservar el enlace del certificado en Google Drive.") from None
        # Pin the Google file ID before placing the QR. Moving/renaming this
        # same file preserves its link, independent of the portal's availability.
        return Publication(f"https://drive.google.com/file/d/{file_id}/view", file_id)

    def _upload(self, metadata: dict, data: bytes, mime: str) -> str:
        boundary = "qcp_" + uuid4().hex
        body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n".encode()
                + json.dumps(metadata, ensure_ascii=False).encode() + b"\r\n"
                + f"--{boundary}\r\nContent-Type: {mime}\r\n\r\n".encode()
                + data + f"\r\n--{boundary}--\r\n".encode())
        result = self._json(self._request("POST", "/files", upload=True,
            params={"uploadType": "multipart", "supportsAllDrives": "true", "ignoreDefaultVisibility":"true", "fields": "id"},
            headers={"Content-Type": f"multipart/related; boundary={boundary}"}, content=body))
        return self._id(result.get("id"))

    def save(self, certificate_id: str, pdf: bytes, metadata: dict, publication: Publication):
        identifier = str(UUID(certificate_id))
        file_id = self._id(publication.drive_file_id)
        with self.write_lock:
            existing = self._list(self.folder_id, "mimeType = 'application/vnd.google-apps.folder' and "
                                 f"appProperties has {{ key='qcp_certificate_id' and value='{identifier}' }}")
            if existing or self._registry_record(identifier) or self._record_pdf(identifier) or self._portable_records(identifier):
                raise StorageError("El certificado ya existe; se conservó el registro anterior.", 409)
            if metadata.get("id") != identifier:
                raise StorageError("El registro no corresponde a la emisión solicitada.", 422)
            info = {**metadata, "storageBackend": "drive", "driveFileId": file_id,
                    "qrMode": MODE if publication.public_key else "drive-direct",
                    "driveUrl": f"https://drive.google.com/file/d/{file_id}/view"}
            if publication.public_key:
                info.update(qrMode=MODE, qrPublicKey=publication.public_key, issuedDriveFileId=file_id,
                            pdfMd5=hashlib.md5(pdf, usedforsecurity=False).hexdigest(),
                            copyViewerAccess="anyone-reader")
            try:
                properties = encode_record(info)
                public_properties = signed_record(info, publication.signing_key) if publication.public_key else None
            except ValueError as error:
                raise StorageError(str(error), 422) from None
            created = False
            record_id = None
            try:
                file_metadata = {"id": file_id, "name": metadata["filename"], "mimeType": "application/pdf",
                                 "parents": [self.folder_id], "appProperties": properties}
                actual_id = self._upload(file_metadata, pdf, "application/pdf")
                if actual_id != file_id:
                    raise StorageError("Google Drive no conservó el enlace reservado del certificado.")
                created = True
                # Publish the PDF only. The signed registry is kept separately
                # in an account-private folder, never in the output folder.
                self._request("POST", f"/files/{file_id}/permissions", params={"supportsAllDrives": "true"},
                              json={"type": "anyone", "role": "reader", "allowFileDiscovery": False})
                if public_properties:
                    record_id = self._create_registry_record(identifier, public_properties, file_id)
                metadata.update(info)
            except Exception:
                if created:
                    try:
                        self._request("DELETE", f"/files/{file_id}", params={"supportsAllDrives": "true"})
                    except StorageError:
                        logger.warning("No se pudo limpiar el PDF de una publicación incompleta en Drive.")
                if record_id:
                    try:
                        self._request("DELETE", f"/files/{record_id}", params={"supportsAllDrives": "true"})
                    except StorageError:
                        logger.warning("No se pudo limpiar un registro nuevo de una publicación incompleta.")
                raise

    def _registry_folders(self, create=False) -> list[str]:
        if self.registry_folders:
            return self.registry_folders
        rows = self._list(None, "'me' in owners and mimeType = 'application/vnd.google-apps.folder' and "
                          "properties has { key='qcp_portable_registry' and value='2' }")
        folders = sorted(row['id'] for row in rows)
        if not folders and create:
            result = self._json(self._request('POST','/files',params={
                'fields':'id','supportsAllDrives':'true','ignoreDefaultVisibility':'true'},json={
                'name':'QCP - Registros del portal (no eliminar)',
                'mimeType':'application/vnd.google-apps.folder','parents':['root'],
                'properties':{'qcp_portable_registry':'2'}}))
            folders = [self._id(result.get('id'))]
        for folder in folders:
            permissions = self._json(self._request('GET',f'/files/{folder}/permissions',params={
                'fields':'permissions(type),nextPageToken','supportsAllDrives':'true'}))
            if permissions.get('nextPageToken') or any(p.get('type') in {'anyone','domain'}
                                                       for p in permissions.get('permissions',[])):
                raise StorageError('El registro técnico de QCP debe permanecer privado en Drive.')
        if folders:
            self.registry_folders = folders
        return folders

    def _registry_record(self, identifier: str):
        cached=self.registry_record_ids.get(identifier)
        if cached:
            try:
                response=self._request('GET',f'/files/{cached}',params={'alt':'media'})
                if len(response.content)>64*1024:
                    raise StorageError('El registro técnico supera el tamaño permitido.')
                return cached,self._json(response)
            except StorageError as error:
                if error.status!=404:raise
                self.registry_record_ids.pop(identifier,None)
        found = []
        for folder in self._registry_folders():
            found.extend(self._list(folder,f"name = '{identifier}.json' and mimeType = 'application/json'"))
        if len(found)>1:
            raise StorageError('Hay registros técnicos duplicados para este certificado.')
        if not found:
            return None
        record_id = self._id(found[0]['id'])
        response = self._request('GET',f'/files/{record_id}',params={'alt':'media'})
        if len(response.content)>64*1024:
            raise StorageError('El registro técnico supera el tamaño permitido.')
        envelope = self._json(response)
        self._remember_record(identifier,record_id)
        return record_id,envelope

    def _remember_record(self, identifier, record_id):
        if len(self.registry_record_ids)>=256:
            self.registry_record_ids.pop(next(iter(self.registry_record_ids)))
        self.registry_record_ids[identifier]=record_id

    def _create_registry_record(self, identifier: str, signed: dict, file_id: str) -> str:
        folder = self._registry_folders(create=True)[0]
        envelope = {'signedRecord':signed,'currentFileId':file_id}
        record_id=self._upload({'name':f'{identifier}.json','mimeType':'application/json','parents':[folder]},
                               json.dumps(envelope,ensure_ascii=False).encode(),'application/json')
        self._remember_record(identifier,record_id)
        return record_id

    def _checked_file(self, file_id: str, info: dict):
        try:
            row = self._json(self._request('GET',f'/files/{self._id(file_id)}',params={
                'fields':'id,mimeType,ownedByMe,trashed,sha256Checksum,md5Checksum','supportsAllDrives':'true'}))
        except StorageError as error:
            if error.status==404:
                return None
            raise
        if row.get('trashed') or not row.get('ownedByMe') or row.get('mimeType')!='application/pdf':
            return None
        if row.get('sha256Checksum') and row['sha256Checksum']!=info['sha256']:
            return None
        selected = {**info,'driveFileId':file_id,'driveUrl':f'https://drive.google.com/file/d/{file_id}/view'}
        if not row.get('sha256Checksum'):
            try:
                self.download(selected)
            except StorageError as error:
                if error.status in {404,409}:
                    return None
                raise
        return selected

    def _identical_copy(self, info: dict):
        # Drive native copies can drop ALL custom properties. Search only binary
        # checksums, reading file metadata rather than downloading unrelated PDFs.
        params = {'q':"trashed = false and 'me' in owners and mimeType = 'application/pdf'",
                  'fields':'files(id,sha256Checksum,md5Checksum),nextPageToken,incompleteSearch',
                  'pageSize':1000,'supportsAllDrives':'true','includeItemsFromAllDrives':'true'}
        tokens = set()
        while True:
            result = self._json(self._request('GET','/files',params=params))
            if result.get('incompleteSearch'):
                raise StorageError('Drive no pudo completar la búsqueda de una copia. Inténtalo nuevamente.')
            rows = result.get('files',[])
            if not isinstance(rows,list):
                raise StorageError('Drive devolvió una búsqueda de copias no válida.')
            for row in rows:
                if not isinstance(row,dict):
                    raise StorageError('Drive devolvió una búsqueda de copias no válida.')
                sha = row.get('sha256Checksum')
                md5_match = info.get('pdfMd5') and row.get('md5Checksum')==info['pdfMd5']
                if sha==info['sha256'] or (not sha and md5_match):
                    selected = self._checked_file(self._id(row.get('id')),info)
                    if selected:
                        return selected
            token = result.get('nextPageToken')
            if not token:
                return None
            if not isinstance(token,str) or token in tokens or len(tokens)>1000:
                raise StorageError('Drive no pudo completar la búsqueda de copias de este certificado.')
            tokens.add(token);params['pageToken']=token

    def _registered_portable(self, identifier: str, public_key: str | None):
        stored = self._registry_record(identifier)
        if not stored:
            return None
        record_id,envelope = stored
        try:
            info = verified_record(envelope.get('signedRecord'),identifier,public_key)
            self._validate_metadata(info,identifier)
        except ValueError as error:
            raise StorageError(str(error),404)from None
        hinted = envelope.get('currentFileId')
        if isinstance(hinted,str) and FILE_ID.fullmatch(hinted):
            selected = self._checked_file(hinted,info)
            if selected:
                return selected
        selected = self._identical_copy(info)
        if not selected:
            if isinstance(hinted,str) and FILE_ID.fullmatch(hinted):
                try:
                    row=self._json(self._request('GET',f'/files/{hinted}',params={'fields':'id,trashed'}))
                    if row.get('trashed'):
                        raise StorageError('No se encontró el PDF ni una copia íntegra en Drive.',404)
                except StorageError as error:
                    if error.status==404:
                        raise StorageError('No se encontró el PDF ni una copia íntegra en Drive.',404)from None
                    raise
            raise StorageError('El PDF almacenado no coincide con el registro y no se encontró una copia íntegra.',409)
        # This advisory pointer only updates our private technical record. The
        # signed record, PDF bytes, QR, permissions and other certificates stay intact.
        try:
            self._request('PATCH',f'/files/{record_id}',upload=True,params={
                'uploadType':'media','supportsAllDrives':'true'},
                headers={'Content-Type':'application/json'},
                content=json.dumps({**envelope,'currentFileId':selected['driveFileId']},ensure_ascii=False).encode())
        except StorageError:
            logger.warning('No se pudo actualizar el localizador privado; se conservó el PDF verificado.')
        return selected

    def _portable_records(self, identifier: str, public_key: str | None = None) -> list[tuple[dict, dict]]:
        # Compatibility reader for the first portable format. Its signed record
        # may still be recoverable on the original in trash, even if copies lost it.
        rows = self._list(None, "'me' in owners and mimeType = 'application/pdf' and "
                          f"properties has {{ key='qcp_certificate_id' and value='{identifier}' }}",include_trashed=True)
        candidates = []
        for row in rows:
            try:
                info = verified_record(row.get("properties", {}), identifier, public_key)
                self._validate_metadata(info, identifier)
            except (ValueError, StorageError):
                continue
            candidates.append((row, info))
        return candidates

    def portable_metadata(self, certificate_id: str, public_key: str | None = None) -> dict:
        identifier = str(UUID(certificate_id))
        if public_key is not None and not KEY.fullmatch(public_key):
            raise StorageError("El enlace de consulta del certificado no es válido.", 404)
        registered = self._registered_portable(identifier,public_key)
        if registered:
            return registered
        candidates = self._portable_records(identifier, public_key)
        # Prefer the issued file while it is intact. Identical copies are valid
        # alternatives; names, folders and copied private properties are irrelevant.
        candidates.sort(key=lambda item: (item[0]["id"] != item[1].get("issuedDriveFileId"), item[0]["id"]))
        for row, info in candidates:
            checksum = row.get("sha256Checksum")
            if checksum and checksum != info["sha256"]:
                continue
            selected = {**info, "driveFileId": row["id"],
                        "driveUrl": f"https://drive.google.com/file/d/{row['id']}/view"}
            if not checksum:
                try:
                    self.download(selected)
                except StorageError as error:
                    if error.status in {404, 409}:
                        continue
                    raise
            # Compatibility for the briefly deployed property-based format:
            # preserve its already signed record before any later copy loses it.
            try:
                with self.write_lock:
                    if not self._registry_record(identifier):
                        self._create_registry_record(identifier,row['properties'],row['id'])
            except StorageError:
                logger.warning('No se pudo registrar el formato portable anterior; se conserva el PDF verificado.')
                if row.get('trashed'):
                    raise
            else:
                registered=self._registered_portable(identifier,public_key)
                if registered:return registered
                if row.get('trashed'):
                    raise StorageError('El registro se está actualizando. Intenta nuevamente.',503)
            return selected
        if candidates:
            raise StorageError("El PDF almacenado no coincide con el registro y no se encontró una copia íntegra para este QR.", 409)
        raise StorageError("No se encontró el certificado ni una copia válida en esta cuenta de Drive.", 404)

    def metadata(self, certificate_id: str, public_key: str | None = None) -> dict:
        if public_key is not None:
            return self.portable_metadata(certificate_id, public_key)
        registered = self._registered_portable(str(UUID(certificate_id)),None)
        if registered:
            return registered
        record = self._record_pdf(certificate_id)
        if record:
            try:
                info = decode_record(record["appProperties"])
            except ValueError as error:
                raise StorageError(str(error)) from None
            if info.get("driveFileId") != record["id"]:
                raise StorageError("El registro no corresponde al PDF emitido.")
            return self._validate_metadata(info, certificate_id)
        try:
            return self.portable_metadata(certificate_id)
        except StorageError as error:
            if error.status != 404:
                raise
        # Read legacy folders/JSON exactly as issued. Never migrate or edit them.
        folder = self._record_folder(certificate_id)
        records = self._list(folder, "name = 'metadata.json' and mimeType = 'application/json'")
        if len(records) != 1:
            raise StorageError("El registro de este certificado está incompleto.", 404)
        response = self._request("GET", f"/files/{self._id(records[0]['id'])}", params={"alt": "media"})
        if len(response.content) > 64 * 1024:
            raise StorageError("El registro del certificado supera el tamaño permitido.")
        return self._validate_metadata(self._json(response), certificate_id)

    def _validate_metadata(self, info: dict, certificate_id: str) -> dict:
        if (info.get("id") != str(UUID(certificate_id))
                or not isinstance(info.get("driveFileId"), str) or not FILE_ID.fullmatch(info["driveFileId"])
                or not isinstance(info.get("filename"), str) or not re.fullmatch(r"[a-f0-9]{64}", str(info.get("sha256", "")))):
            raise StorageError("El registro del certificado no es válido.")
        return info

    def exercise_fresh_test_move(self, info: dict) -> dict:
        """Move/rename only the fresh, fixed SIN VALIDEZ fixture from this request.

        The caller cannot supply a Drive ID. No copy or deletion is performed.
        """
        file_id = self._id(info["driveFileId"])
        try:
            self.download(info)
            moved = self._json(self._request("PATCH", f"/files/{file_id}", params={
                "addParents": "root", "removeParents": self.folder_id, "supportsAllDrives": "true",
                "fields": "id,name,parents"}, json={"name": "PRUEBA-MOVIDO-QR-DIRECTO-SIN-VALIDEZ.pdf"}))
            if (moved.get("id") != file_id or self.folder_id in moved.get("parents", [])
                    or not moved.get("parents")):
                raise StorageError("Drive no confirmó el movimiento del PDF ficticio.")
            self.download(info)
            if not self._public_reader(file_id):
                raise StorageError("El PDF ficticio movido no conservó su permiso de lectura por enlace.")
            return {"ok": True, "sameFileId": True, "sameQr": True,
                    "movedOutsideOutputFolder": True,
                    "renamed": moved.get("name") == "PRUEBA-MOVIDO-QR-DIRECTO-SIN-VALIDEZ.pdf"}
        except StorageError as error:
            return {"ok": False, "detail": str(error)}

    def exercise_fresh_test_copy(self, info: dict) -> tuple[dict, dict]:
        """Only called for a freshly issued, byte-checked SIN VALIDEZ fixture.

        No caller-supplied Drive IDs or existing certificates enter this path.
        Clear private properties on the copy, leave public properties to native
        Drive inheritance, and verify its bytes BEFORE removing our new source.
        """
        source = self._id(info["driveFileId"])
        deleted = False
        try:
            row = self._json(self._request("POST", f"/files/{source}/copy", params={
                "supportsAllDrives": "true", "fields": "id,mimeType,properties,appProperties,sha256Checksum,parents"},
                json={"name": "PRUEBA-COPIA-QR-PORTABLE-SIN-VALIDEZ.pdf", "parents": ["root"],
                      "appProperties": {key: None for key in encode_record(info)}}))
            copied_id = self._id(row.get("id"))
            if copied_id == source:
                raise StorageError("Drive no creó un identificador nuevo para la prueba.")
            copied = {**info,'driveFileId':copied_id,'driveUrl':f'https://drive.google.com/file/d/{copied_id}/view'}
            self.download(copied)  # Exact SHA-256, not filename matching.
            self._request("DELETE", f"/files/{source}", params={"supportsAllDrives": "true"})
            deleted = True
            resolved = self.portable_metadata(info["id"], info["qrPublicKey"])
            if resolved["driveFileId"] != copied_id:
                raise StorageError("No se localizó la copia de prueba después de eliminar su origen.")
            self.download(resolved)
            return resolved, {"ok": True, "originalDeleted": True, "sameQr": True,
                              "copiedFileId": copied_id, "privatePropertiesRemoved": not bool(row.get("appProperties")),
                              "customPropertiesRemoved":not bool(row.get('appProperties')or row.get('properties'))}
        except (StorageError, ValueError) as error:
            return info, {"ok": False, "originalDeleted": deleted, "detail": str(error)}

    def viewer_url(self, certificate_id: str, public_key: str) -> str:
        info = self.portable_metadata(certificate_id, public_key)
        file_id = self._id(info.get("driveFileId"))
        # New publications explicitly retain public reader access on an intact
        # copy, just as on the issued original. Older records without that signed
        # policy keep their existing permissions. Never share a folder or a file
        # merely because its name matches.
        if (info.get("copyViewerAccess") == "anyone-reader"
                and file_id != info.get("issuedDriveFileId")):
            with self.write_lock:
                if not self._public_reader(file_id):
                    self._request("POST", f"/files/{file_id}/permissions", params={"supportsAllDrives": "true"},
                                  json={"type": "anyone", "role": "reader", "allowFileDiscovery": False})
        # Build the destination from the checked ID, never from caller input or
        # a stored URL. The browser opens Drive without OAuth credentials.
        return f"https://drive.google.com/file/d/{file_id}/view"

    def _public_reader(self, file_id: str) -> bool:
        params = {"fields": "permissions(type,role),nextPageToken", "pageSize": 100,
                  "supportsAllDrives": "true"}
        tokens = set()
        while True:
            result = self._json(self._request("GET", f"/files/{file_id}/permissions", params=params))
            if any(p.get("type") == "anyone" and p.get("role") in {"reader", "commenter", "writer"}
                   for p in result.get("permissions", [])):
                return True
            token = result.get("nextPageToken")
            if not token:
                return False
            if not isinstance(token, str) or token in tokens or len(tokens) >= 100:
                raise StorageError("No se pudieron comprobar los permisos de lectura de la copia.")
            tokens.add(token)
            params["pageToken"] = token

    def download(self, metadata: dict) -> bytes:
        file_id = self._id(metadata.get("driveFileId"))
        headers = {"Authorization": f"Bearer {self._token()}"}
        try:
            with self.client.stream("GET", f"{DRIVE_API}/files/{file_id}", params={"alt": "media"}, headers=headers) as response:
                if response.status_code == 404:
                    raise StorageError("Certificado no encontrado.", 404)
                if response.status_code != 200:
                    raise StorageError("Google Drive no pudo entregar el certificado. Inténtalo de nuevo.")
                chunks = []
                size = 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > MAX_DOWNLOAD:
                        raise StorageError("El PDF almacenado supera el tamaño permitido.")
                    chunks.append(chunk)
                pdf = b"".join(chunks)
        except httpx.HTTPError:
            raise StorageError("No se pudo descargar el certificado desde Google Drive.") from None
        if hashlib.sha256(pdf).hexdigest() != metadata.get("sha256"):
            raise StorageError("El PDF almacenado cambió y no coincide con el registro del certificado.", 409)
        return pdf


def storage_from_environment(directory: Path, quota: int) -> LocalStorage | DriveStorage:
    backend = os.environ.get("CERTIFICATE_STORAGE_BACKEND", "local").strip().lower()
    if backend == "local":
        return LocalStorage(directory, quota)
    if backend != "drive":
        raise ValueError("CERTIFICATE_STORAGE_BACKEND debe ser local o drive.")
    return DriveStorage(*(os.environ.get(name, "") for name in (
        "GOOGLE_DRIVE_FOLDER_ID", "GOOGLE_DRIVE_CLIENT_ID", "GOOGLE_DRIVE_CLIENT_SECRET", "GOOGLE_DRIVE_REFRESH_TOKEN")))
