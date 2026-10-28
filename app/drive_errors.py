"""Classify Google failures without exposing upstream messages or credentials."""

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx

RATE_LIMITS = {"rateLimitExceeded", "userRateLimitExceeded", "sharingRateLimitExceeded"}
MESSAGES = {
    "rateLimitExceeded": ("DRIVE_RATE_LIMIT", "Google Drive limitó temporalmente las solicitudes. Espera unos minutos y vuelve a intentar."),
    "userRateLimitExceeded": ("DRIVE_RATE_LIMIT", "La cuenta empresarial alcanzó un límite temporal de solicitudes de Google Drive. Espera unos minutos y vuelve a intentar."),
    "sharingRateLimitExceeded": ("DRIVE_SHARING_LIMIT", "Google Drive limitó temporalmente la publicación de enlaces. Espera y vuelve a intentar; si continúa, el administrador debe revisar la cuota de uso compartido."),
    "storageQuotaExceeded": ("DRIVE_STORAGE_FULL", "La cuenta empresarial de Google Drive no tiene espacio disponible. El administrador debe revisar su almacenamiento."),
    "insufficientFilePermissions": ("DRIVE_PERMISSION_DENIED", "La cuenta conectada al portal no tiene permiso para esta operación en Google Drive. El administrador debe revisar su acceso."),
    "insufficientPermissions": ("DRIVE_PERMISSION_DENIED", "La autorización del portal no incluye los permisos necesarios de Google Drive. El administrador debe revisar la conexión."),
    "appNotAuthorizedToFile": ("DRIVE_PERMISSION_DENIED", "La aplicación no está autorizada para acceder a este archivo de Google Drive. El administrador debe revisar la conexión."),
    "domainPolicy": ("DRIVE_POLICY", "Una política de la organización impide esta operación en Google Drive. El administrador debe revisar las restricciones de uso compartido."),
    "cannotShareAcrossDomains": ("DRIVE_POLICY", "La organización no permite compartir este PDF mediante un enlace público. El administrador debe revisar esa política de Drive."),
    "dailyLimitExceeded": ("DRIVE_DAILY_LIMIT", "Se alcanzó una cuota diaria de la API de Google Drive. El administrador debe revisar la cuota del proyecto."),
    "activeItemCreationLimitExceeded": ("DRIVE_ITEM_LIMIT", "La cuenta conectada alcanzó el límite de archivos de Google Drive. El administrador debe revisar ese límite."),
    "teamDriveFileLimitExceeded": ("DRIVE_ITEM_LIMIT", "La unidad compartida alcanzó su límite de archivos. El administrador debe revisar ese límite."),
}
RPC_REASONS = {"RATE_LIMIT_EXCEEDED": "rateLimitExceeded", "USER_RATE_LIMIT_EXCEEDED": "userRateLimitExceeded"}


def rejection_reason(response: httpx.Response) -> str | None:
    try:
        body = response.json() if len(response.content) <= 64 * 1024 else None
    except ValueError:
        body = None
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, str) and error in MESSAGES:
        return error
    if isinstance(error, dict):
        for key in ("errors", "details"):
            rows = error.get(key)
            if not isinstance(rows, list):
                continue
            for row in rows:
                reason = row.get("reason") if isinstance(row, dict) else None
                if not isinstance(reason, str):
                    continue
                reason = RPC_REASONS.get(reason, reason)
                if reason in MESSAGES:
                    return reason
    return "rateLimitExceeded" if response.status_code == 429 else None


def retry_after(response: httpx.Response) -> int | None:
    value = response.headers.get("Retry-After", "").strip()
    try:
        if value.isascii() and value.isdecimal() and len(value) <= 10:
            return int(value)
        deadline = parsedate_to_datetime(value)
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        return max(0, int((deadline - datetime.now(timezone.utc)).total_seconds()) + 1)
    except (ValueError, TypeError, OverflowError):
        return None


def operation_name(method: str, path: str, upload: bool) -> str:
    if path == "/about":
        return "check_storage"
    if path.endswith("/permissions"):
        return "publish_link" if method == "POST" else "check_permissions"
    if path == "/files/generateIds":
        return "reserve_pdf_link"
    if upload:
        return "upload_pdf" if method == "POST" else "update_record"
    if method == "DELETE":
        return "cleanup_incomplete_publication"
    if path == "/files":
        return "find_record" if method == "GET" else "create_record"
    return "read_file" if method == "GET" else "update_file"
