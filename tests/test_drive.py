"""Drive API simulations; no customer files, credentials or Google writes."""

from email.parser import BytesParser
from email.policy import default
from copy import deepcopy
import hashlib
from io import BytesIO
import json
import re
from urllib.parse import parse_qs
from uuid import uuid4

from fastapi.testclient import TestClient
import httpx
from PIL import Image
import pymupdf
import pytest
import zxingcpp

from app.main import create_app
from app.storage import DriveStorage, LocalStorage, Publication, StorageError, storage_from_environment
from app.portable_qr import new_key
from test_excel import workbook_bytes
from test_portal import pdf_bytes, upload

ROOT = "folder_for_synthetic_tests_only"


class EarlierPortableStorage(DriveStorage):
    """Emulate a previous release's issuance to test already printed QR URLs."""
    qr_mode = "portable-v1"

    def allocate(self, certificate_id, origin):
        direct = super().allocate(certificate_id, origin)
        public, private = new_key()
        return Publication(f"{origin}/q/{certificate_id}/{public}", direct.drive_file_id, public, private)


class FakeDrive:
    def __init__(self):
        self.files = {ROOT: {"id": ROOT, "mimeType": "application/vnd.google-apps.folder", "parents": [],
                             "capabilities": {"canAddChildren": True}}}
        self.permissions = []
        self.root_permissions = [{"type": "user"}]
        self.token_requests = 0
        self.counter = 0
        self.fail_permission = False
        self.fail_metadata = False
        self.fail_token = False
        self.fail_network = False
        self.queries = []
        self.writes = []

    def new_id(self):
        self.counter += 1
        # Match real Drive IDs in length to exercise QR density.
        return "synthetic_" + str(self.counter).zfill(34)

    def storage(self, *, portable=False):
        storage_class = EarlierPortableStorage if portable else DriveStorage
        return storage_class(ROOT, "synthetic-client", "synthetic-secret", "synthetic-refresh",
                            client=httpx.Client(transport=httpx.MockTransport(self.handle)))

    def handle(self, request):
        if request.url.host == "oauth2.googleapis.com":
            self.token_requests += 1
            form = parse_qs(request.content.decode())
            assert form["grant_type"] == ["refresh_token"]
            assert form["client_secret"] == ["synthetic-secret"]
            assert form["refresh_token"] == ["synthetic-refresh"]
            if self.fail_token:
                return httpx.Response(400, json={"error": "invalid_grant", "description": "synthetic-secret"})
            return httpx.Response(200, json={"access_token": "synthetic-access", "expires_in": 3600})
        assert request.url.host == "www.googleapis.com"
        assert request.headers["authorization"] == "Bearer synthetic-access"
        if self.fail_network:
            raise httpx.ConnectError("synthetic-secret", request=request)
        path = request.url.path
        if request.method != "GET":
            self.writes.append((request.method, path))
        if path.endswith("/files/generateIds"):
            return httpx.Response(200, json={"ids": [self.new_id()]})
        if path == "/drive/v3/files" and request.method == "GET":
            query = request.url.params["q"]
            self.queries.append(query)
            parent = re.search(r"'([^']+)' in parents", query)
            properties = re.findall(r"(appProperties|properties) has \{ key='([^']+)' and value='([^']+)' \}", query)
            rows = [row for row in self.files.values()
                    if ("trashed = false" not in query or not row.get("trashed"))
                    and (not parent or parent[1] in row.get("parents", []))]
            for mime in ("application/pdf", "application/vnd.google-apps.folder", "application/json"):
                if f"mimeType = '{mime}'" in query:
                    rows = [row for row in rows if row.get("mimeType") == mime]
            if "'me' in owners" in query:
                rows = [row for row in rows if row.get("ownedByMe", True)]
            for scope,key,value in properties:
                rows=[row for row in rows if row.get(scope,{}).get(key)==value]
            name=re.search(r"name = '([^']+)'",query)
            if name:
                rows=[row for row in rows if row.get('name')==name[1]]
            offset = int(request.url.params.get("pageToken", "0"))
            rows = [{**row, "sha256Checksum": hashlib.sha256(row["content"]).hexdigest()}
                    if row.get("mimeType") == "application/pdf" else row for row in rows]
            result = {"files": [
                {key: value for key, value in row.items() if key in {"id", "name", "mimeType", "appProperties", "properties", "trashed", "sha256Checksum"}}
                for row in rows[offset:offset + 100]]}
            if len(rows) > offset + 100:
                result["nextPageToken"] = str(offset + 100)
            return httpx.Response(200, json=result)
        if path == "/drive/v3/files" and request.method == "POST":
            row = json.loads(request.content)
            row["id"] = self.new_id()
            self.files[row["id"]] = row
            return httpx.Response(200, json={"id": row["id"]})
        if path == "/upload/drive/v3/files":
            assert request.url.params["uploadType"] == "multipart"
            message = BytesParser(policy=default).parsebytes(
                f"Content-Type: {request.headers['content-type']}\r\nMIME-Version: 1.0\r\n\r\n".encode() + request.content)
            meta_part, content_part = list(message.iter_parts())
            row = json.loads(meta_part.get_payload(decode=True))
            if self.fail_metadata and (row["name"] == "metadata.json" or row.get("appProperties")):
                return httpx.Response(403, json={"error": "storageQuotaExceeded"})
            row.setdefault("id", self.new_id())
            if row["id"] in self.files:
                return httpx.Response(409, json={"error": "fileAlreadyExists"})
            for scope in ("appProperties", "properties"):
                properties = row.get(scope, {})
                assert len(properties) <= 30
                assert all(len(key.encode()) + len(value.encode()) <= 124 for key, value in properties.items())
            row["content"] = content_part.get_payload(decode=True)
            self.files[row["id"]] = row
            return httpx.Response(200, json={"id": row["id"]})
        update=re.fullmatch(r'/upload/drive/v3/files/([^/]+)',path)
        if update and request.method=='PATCH':
            if update[1] not in self.files:return httpx.Response(404)
            self.files[update[1]]['content']=request.content
            return httpx.Response(200,json={'id':update[1]})
        copy = re.fullmatch(r"/drive/v3/files/([^/]+)/copy", path)
        if copy and request.method == "POST":
            if copy[1] not in self.files:
                return httpx.Response(404)
            row = deepcopy(self.files[copy[1]])
            updates = json.loads(request.content)
            for scope in ("appProperties", "properties"):
                for key, value in updates.pop(scope, {}).items():
                    if value is None:
                        row.get(scope, {}).pop(key, None)
                    else:
                        row.setdefault(scope, {})[key] = value
            row.update(updates)
            row["id"] = self.new_id()
            row["sha256Checksum"] = hashlib.sha256(row["content"]).hexdigest()
            self.files[row["id"]] = row
            return httpx.Response(200, json={key: value for key, value in row.items() if key != "content"})
        match = re.fullmatch(r"/drive/v3/files/([^/]+)(/permissions)?", path)
        assert match, f"Unexpected Google request: {request.method} {path}"
        file_id, permissions = match.groups()
        if file_id not in self.files:
            return httpx.Response(404)
        if permissions:
            if request.method == "GET":
                if self.files[file_id]['mimeType']=='application/vnd.google-apps.folder':
                    return httpx.Response(200, json={"permissions": self.root_permissions if file_id==ROOT else [{'type':'user'}]})
                return httpx.Response(200, json={"permissions": [grant for fid,grant in self.permissions if fid==file_id]})
            if self.fail_permission:
                return httpx.Response(403)
            self.permissions.append((file_id, json.loads(request.content)))
            return httpx.Response(200, json={"id": "public_permission"})
        if request.method == "DELETE":
            descendants = [key for key, row in self.files.items() if file_id in row.get("parents", [])]
            for key in descendants + [file_id]:
                del self.files[key]
            return httpx.Response(204)
        if request.method == "PATCH":
            row = self.files[file_id]
            removed = request.url.params.get("removeParents", "").split(",")
            row["parents"] = [parent for parent in row.get("parents", []) if parent not in removed]
            added = request.url.params.get("addParents")
            if added:
                row["parents"].extend(added.split(","))
            row.update(json.loads(request.content))
            return httpx.Response(200, json={key: value for key, value in row.items() if key != "content"})
        assert request.method == "GET"
        row = self.files[file_id]
        if request.url.params.get("alt") == "media":
            return httpx.Response(200, content=row["content"])
        result={key: value for key, value in row.items() if key != "content"}
        result['ownedByMe']=row.get('ownedByMe',True)
        if row.get('mimeType')=='application/pdf':
            result['sha256Checksum']=hashlib.sha256(row['content']).hexdigest()
            result['md5Checksum']=hashlib.md5(row['content'],usedforsecurity=False).hexdigest()
        return httpx.Response(200, json=result)


def test_drive_excel_survives_loss_of_local_storage_and_domain_change(tmp_path, monkeypatch):
    drive = FakeDrive()
    ephemeral = tmp_path / "ephemeral"
    monkeypatch.setenv("CERTIFICATE_STORAGE_DIR", str(ephemeral))
    with TestClient(create_app(public_url="https://old.example.com", certificate_storage=drive.storage())) as client:
        config = client.get("/api/config").json()
        assert config["storageBackend"] == "drive"
        assert config["requiresAccessKey"] is False
        inspection = client.post("/api/excel/inspect", files={
            "excel": ("Certificado ficticio PERÚ.xlsx", workbook_bytes(), "application/octet-stream")})
        assert inspection.status_code == 200
        response = client.post("/api/certificates", data={
            "excelToken": inspection.json()["uploadToken"], "tipoCertificado": "acreditado"})
        assert response.status_code == 201, response.text
        info = response.json()
        assert info["sourceType"] == "xlsx"
        assert info["signatureIncluded"] is True
        assert info["stampPages"] == [1]
        assert info["certificateType"] == "acreditado"
        assert info["backgroundVerified"] is True
        assert info["backgroundPages"] == [1, 2]
        assert info["verificationUrl"] == f"https://drive.google.com/file/d/{info['driveFileId']}/view"
        assert info["qrMode"] == "drive-direct" and "qrPublicKey" not in info
        assert info["driveUrl"] == f"https://drive.google.com/file/d/{info['driveFileId']}/view"
        download = client.get(info["downloadUrl"])
        assert download.status_code == 200
        pdf = download.content
        assert pdf == drive.files[info["driveFileId"]]["content"]
        assert hashlib.sha256(pdf).hexdigest() == info["sha256"]
        with pymupdf.open(stream=pdf, filetype="pdf") as document:
            assert len(document) == 2
            assert "17,5" in document[0].get_text()  # Saved formula, not recalculated.
            assert "OTHER SHEET" not in "".join(page.get_text() for page in document)
            decoded = []
            for image in document[0].get_image_info():
                pixmap = document[0].get_pixmap(matrix=pymupdf.Matrix(150 / 72, 150 / 72), clip=pymupdf.Rect(image["bbox"]))
                barcode = zxingcpp.read_barcode(Image.open(BytesIO(pixmap.tobytes("png"))))
                if barcode:
                    decoded.append(barcode.text)
            assert info["verificationUrl"] in decoded
        assert not ephemeral.exists()
        # One named PDF in the configured folder; no new folder or JSON sidecar.
        assert {row['id']for row in drive.files.values()if ROOT in row.get('parents',[])} == {info['driveFileId']}
        assert drive.files[info["driveFileId"]]["parents"] == [ROOT]
        assert drive.permissions == [(info["driveFileId"], {"type": "anyone", "role": "reader", "allowFileDiscovery": False})]
        assert drive.token_requests == 1
    # New process, empty local directory, different portal origin: Drive is sufficient.
    with TestClient(create_app(public_url="https://new.example.com", certificate_storage=drive.storage())) as restarted:
        assert restarted.get(f"/certificados/{info['id']}").status_code == 200
        assert restarted.get(f"/api/certificates/{info['id']}").json() == info
        assert restarted.get(info["downloadUrl"]).content == pdf
        assert "filename*=utf-8''Certificado%20ficticio%20PER%C3%9A.pdf" in restarted.get(info["downloadUrl"]).headers["content-disposition"]
    assert drive.token_requests == 2
    assert any(query.startswith("trashed = false") for query in drive.queries)


def test_same_filename_creates_distinct_drive_links():
    drive = FakeDrive()
    with TestClient(create_app(certificate_storage=drive.storage())) as client:
        first, second = upload(client), upload(client)
        assert first.status_code == second.status_code == 201
        assert first.json()["id"] != second.json()["id"]
        assert first.json()["verificationUrl"] != second.json()["verificationUrl"]
        assert len(drive.permissions) == 2


@pytest.mark.parametrize("failure", ["fail_permission", "fail_metadata", "fail_network", "fail_token"])
def test_drive_failure_never_returns_success_or_saves_locally(tmp_path, monkeypatch, failure):
    drive = FakeDrive()
    setattr(drive, failure, True)
    monkeypatch.setenv("CERTIFICATE_STORAGE_DIR", str(tmp_path / "should_not_exist"))
    with TestClient(create_app(certificate_storage=drive.storage())) as client:
        response = upload(client)
        assert response.status_code == (507 if failure == "fail_metadata" else 503)
        assert "synthetic-secret" not in response.text
        assert "synthetic-refresh" not in response.text
    assert list(drive.files) == [ROOT]
    assert not (tmp_path / "should_not_exist").exists()


@pytest.mark.parametrize("permission", ["anyone", "domain"])
def test_public_parent_is_rejected_before_publishing(permission):
    drive = FakeDrive()
    drive.root_permissions = [{"type": permission}]
    with TestClient(create_app(certificate_storage=drive.storage())) as client:
        response = upload(client)
        assert response.status_code == 503
        assert "carpeta" in response.json()["detail"]
    assert list(drive.files) == [ROOT]


def test_missing_authorization_disables_uploads_without_hiding_home(tmp_path, monkeypatch):
    monkeypatch.setenv("CERTIFICATE_STORAGE_BACKEND", "drive")
    monkeypatch.setenv("CERTIFICATE_STORAGE_DIR", str(tmp_path / "not_used"))
    for key in ("GOOGLE_DRIVE_FOLDER_ID", "GOOGLE_DRIVE_CLIENT_ID", "GOOGLE_DRIVE_CLIENT_SECRET", "GOOGLE_DRIVE_REFRESH_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    with TestClient(create_app()) as client:
        assert client.get("/").status_code == 200
        assert client.get("/healthz").status_code == 200
        assert client.get("/api/config").json()["uploadsEnabled"] is False
        assert upload(client).status_code == 503
    assert not (tmp_path / "not_used").exists()


def test_invalid_document_is_not_published():
    drive = FakeDrive()
    with TestClient(create_app(certificate_storage=drive.storage())) as client:
        response = upload(client, b"not a pdf")
        assert response.status_code == 422
    assert list(drive.files) == [ROOT]
    assert drive.permissions == []


def test_tampered_pdf_is_not_served_as_registered_copy():
    drive = FakeDrive()
    with TestClient(create_app(certificate_storage=drive.storage())) as client:
        response = upload(client)
        assert response.status_code == 201
        info = response.json()
        drive.files[info["driveFileId"]]["content"] += b"changed"
        response = client.get(info["downloadUrl"])
        assert response.status_code == 409
        assert "no coincide" in response.json()["detail"]


def test_missing_drive_certificate_and_deleted_pdf():
    drive = FakeDrive()
    with TestClient(create_app(certificate_storage=drive.storage())) as client:
        assert client.get(f"/api/certificates/{uuid4()}").status_code == 404
        info = upload(client).json()
        del drive.files[info["driveFileId"]]
        assert client.get(info["downloadUrl"]).status_code == 404


def test_existing_record_is_never_overwritten():
    drive = FakeDrive()
    store = drive.storage()
    identifier = str(uuid4())
    publication = store.allocate(identifier, "https://example.com")
    pdf = pdf_bytes()
    metadata = {"id": identifier, "filename": "synthetic.pdf", "sha256": hashlib.sha256(pdf).hexdigest()}
    store.save(identifier, pdf, metadata, publication)
    before = dict(drive.files)
    with pytest.raises(StorageError) as error:
        store.save(identifier, b"replacement", metadata, publication)
    assert error.value.status == 409
    assert drive.files == before
    store.close()


def test_folder_without_write_access_is_rejected():
    drive = FakeDrive()
    drive.files[ROOT]["capabilities"]["canAddChildren"] = False
    store = drive.storage()
    with pytest.raises(StorageError, match="no puede guardar"):
        store.allocate(str(uuid4()), "https://example.com")
    assert list(drive.files) == [ROOT]
    store.close()


def test_unknown_backend_is_not_silently_local(tmp_path, monkeypatch):
    monkeypatch.setenv("CERTIFICATE_STORAGE_BACKEND", "drvie")
    with pytest.raises(ValueError, match="local o drive"):
        storage_from_environment(tmp_path, 1024)
    monkeypatch.setenv("CERTIFICATE_STORAGE_BACKEND", "local")
    assert isinstance(storage_from_environment(tmp_path, 1024), LocalStorage)
