"""Local development storage and durable Google Drive publication."""

from dataclasses import dataclass, field
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import threading
import time
from uuid import UUID, uuid4

import httpx

from app.drive_records import decode_record, encode_record, original_file
from app.portable_qr import KEY, MODE, new_key, signed_record, verified_record

logger = logging.getLogger(__name__)
DRIVE_API = "https://www.googleapis.com/drive/v3"
DRIVE_UPLOAD = "https://www.googleapis.com/upload/drive/v3"
TOKEN_URL = "https://oauth2.googleapis.com/token"
FILE_ID = re.compile(r"[A-Za-z0-9_-]{10,200}")
MAX_DOWNLOAD = 64 * 1024 * 1024


class StorageError(Exception):
    def __init__(self, message: str, status: int = 503):
        self.status = status
        super().__init__(message)


@dataclass(frozen=True)
class Publication:
    verification_url: str
    drive_file_id: str | None = None
    public_key: str | None = None
    signing_key: bytes | None = field(default=None, repr=False)


class LocalStorage:
    backend = "local"
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
        headers = {"Authorization": f"Bearer {self._token()}", **kwargs.pop("headers", {})}
        try:
            response = self.client.request(method, (DRIVE_UPLOAD if upload else DRIVE_API) + path,
                                           headers=headers, **kwargs)
        except httpx.HTTPError:
            raise StorageError("No se pudo conectar con Google Drive. Inténtalo de nuevo.") from None
        if response.status_code == 404:
            raise StorageError("El archivo o la carpeta no está disponible en Google Drive.", 404)
        if response.status_code == 403:
            raise StorageError("Google Drive rechazó la operación. Revisa los permisos y el espacio de la cuenta.")
        if response.status_code == 401:
            self.token_deadline = 0
            raise StorageError("Google Drive requiere renovar su autorización. Inténtalo de nuevo.")
        if response.status_code < 200 or response.status_code >= 300:
            raise StorageError("Google Drive no pudo completar la operación. Inténtalo de nuevo.")
        return response

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

    def _list(self, parent: str | None, extra: str) -> list[dict]:
        location = f"'{parent}' in parents and " if parent else ""
        params = {
            "q": f"{location}trashed = false and ({extra})",
            "fields": "files(id,name,mimeType,appProperties,properties,sha256Checksum,size),nextPageToken,incompleteSearch", "pageSize": 100,
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
        public, private = new_key()
        return Publication(f"{origin}/q/{certificate_id}/{public}", file_id, public, private)

    def _upload(self, metadata: dict, data: bytes, mime: str) -> str:
        boundary = "qcp_" + uuid4().hex
        body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n".encode()
                + json.dumps(metadata, ensure_ascii=False).encode() + b"\r\n"
                + f"--{boundary}\r\nContent-Type: {mime}\r\n\r\n".encode()
                + data + f"\r\n--{boundary}--\r\n".encode())
        result = self._json(self._request("POST", "/files", upload=True,
            params={"uploadType": "multipart", "supportsAllDrives": "true", "fields": "id"},
            headers={"Content-Type": f"multipart/related; boundary={boundary}"}, content=body))
        return self._id(result.get("id"))

    def save(self, certificate_id: str, pdf: bytes, metadata: dict, publication: Publication):
        identifier = str(UUID(certificate_id))
        file_id = self._id(publication.drive_file_id)
        with self.write_lock:
            existing = self._list(self.folder_id, "mimeType = 'application/vnd.google-apps.folder' and "
                                 f"appProperties has {{ key='qcp_certificate_id' and value='{identifier}' }}")
            if existing or self._record_pdf(identifier) or self._portable_records(identifier):
                raise StorageError("El certificado ya existe; se conservó el registro anterior.", 409)
            if metadata.get("id") != identifier:
                raise StorageError("El registro no corresponde a la emisión solicitada.", 422)
            info = {**metadata, "storageBackend": "drive", "driveFileId": file_id,
                    "driveUrl": f"https://drive.google.com/file/d/{file_id}/view"}
            if publication.public_key:
                info.update(qrMode=MODE, qrPublicKey=publication.public_key, issuedDriveFileId=file_id)
            try:
                properties = encode_record(info)
                public_properties = signed_record(info, publication.signing_key) if publication.public_key else None
            except ValueError as error:
                raise StorageError(str(error), 422) from None
            created = False
            try:
                file_metadata = {"id": file_id, "name": metadata["filename"], "mimeType": "application/pdf",
                                 "parents": [self.folder_id], "appProperties": properties}
                if public_properties:
                    file_metadata["properties"] = public_properties
                actual_id = self._upload(file_metadata, pdf, "application/pdf")
                if actual_id != file_id:
                    raise StorageError("Google Drive no conservó el enlace reservado del certificado.")
                created = True
                # Private properties remain private; the signed public record
                # travels with the published PDF and its native Drive copies.
                self._request("POST", f"/files/{file_id}/permissions", params={"supportsAllDrives": "true"},
                              json={"type": "anyone", "role": "reader", "allowFileDiscovery": False})
                metadata.update(info)
            except Exception:
                if created:
                    try:
                        self._request("DELETE", f"/files/{file_id}", params={"supportsAllDrives": "true"})
                    except StorageError:
                        logger.warning("No se pudo limpiar el PDF de una publicación incompleta en Drive.")
                raise

    def _portable_records(self, identifier: str, public_key: str | None = None) -> list[tuple[dict, dict]]:
        # Public properties survive native Drive copies made by other clients.
        # Restrict discovery to this account's files, never adopt a shared forgery.
        rows = self._list(None, "'me' in owners and mimeType = 'application/pdf' and "
                          f"properties has {{ key='qcp_certificate_id' and value='{identifier}' }}")
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
            return selected
        if candidates:
            raise StorageError("El PDF almacenado no coincide con el registro y no se encontró una copia íntegra para este QR.", 409)
        raise StorageError("No se encontró el certificado ni una copia válida en esta cuenta de Drive.", 404)

    def metadata(self, certificate_id: str, public_key: str | None = None) -> dict:
        if public_key is not None:
            return self.portable_metadata(certificate_id, public_key)
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
            copied = verified_record(row.get("properties", {}), info["id"], info["qrPublicKey"])
            copied.update(driveFileId=copied_id, driveUrl=f"https://drive.google.com/file/d/{copied_id}/view")
            self.download(copied)  # Exact SHA-256, not filename matching.
            self._request("DELETE", f"/files/{source}", params={"supportsAllDrives": "true"})
            deleted = True
            resolved = self.portable_metadata(info["id"], info["qrPublicKey"])
            if resolved["driveFileId"] != copied_id:
                raise StorageError("No se localizó la copia de prueba después de eliminar su origen.")
            self.download(resolved)
            return resolved, {"ok": True, "originalDeleted": True, "sameQr": True,
                              "copiedFileId": copied_id, "privatePropertiesRemoved": not bool(row.get("appProperties"))}
        except (StorageError, ValueError) as error:
            return info, {"ok": False, "originalDeleted": deleted, "detail": str(error)}

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
