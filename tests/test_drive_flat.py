"""Flat PDFs, folder-independent lookup and read-only legacy compatibility."""

from copy import deepcopy
import base64
import hashlib
import json
import random
from uuid import uuid4
import zlib

from fastapi.testclient import TestClient
import pytest

from app.drive_records import decode_record, encode_record
from app.main import create_app
from app.storage import Publication, StorageError
from test_drive import FakeDrive, ROOT
from test_portal import pdf_bytes, upload


def legacy_record(drive):
    """Seed the old layout without running or modifying production records."""
    identifier = str(uuid4())
    folder, file_id, record = (drive.new_id() for _ in range(3))
    data = pdf_bytes()
    info = {"id": identifier, "filename": "EMITIDO-ANTES.pdf", "sourceType": "pdf",
            "sha256": hashlib.sha256(data).hexdigest(), "storageBackend": "drive",
            "driveFileId": file_id, "driveUrl": f"https://drive.google.com/file/d/{file_id}/view",
            "verificationUrl": f"https://drive.google.com/file/d/{file_id}/view"}
    drive.files[folder] = {"id": folder, "name": "EMITIDO-ANTES", "parents": [ROOT],
                           "mimeType": "application/vnd.google-apps.folder",
                           "appProperties": {"qcp_certificate_id": identifier}}
    drive.files[file_id] = {"id": file_id, "name": info["filename"], "parents": [folder],
                            "mimeType": "application/pdf", "content": data}
    drive.files[record] = {"id": record, "name": "metadata.json", "parents": [folder],
                           "mimeType": "application/json", "content": json.dumps(info).encode()}
    return info, data


def test_moving_and_renaming_new_pdf_survives_restart_and_preserves_legacy():
    drive = FakeDrive()
    legacy, old_pdf = legacy_record(drive)
    before = deepcopy(drive.files)
    with TestClient(create_app(certificate_storage=drive.storage())) as client:
        response = upload(client)
        assert response.status_code == 201, response.text
        info = response.json()
        pdf = client.get(info["downloadUrl"]).content
        added = set(drive.files) - set(before)
        assert {key for key in added if ROOT in drive.files[key].get('parents',[])} == {info['driveFileId']}
        row = drive.files[info["driveFileId"]]
        assert row["parents"] == [ROOT] and row["name"] == info["filename"]
        assert decode_record(row["appProperties"]) == info
        # Simulate Drive's Move and Rename: only parents/name change, not ID.
        row["parents"] = ["another_folder_in_same_drive"]
        row["name"] = "CERTIFICADO-RENOMBRADO.pdf"
    writes = list(drive.writes)
    with TestClient(create_app(public_url="https://new.example.com", certificate_storage=drive.storage())) as restarted:
        assert restarted.get(f"/api/certificates/{info['id']}").json() == info
        assert restarted.get(info["downloadUrl"]).content == pdf
        assert restarted.get(f"/certificados/{info['id']}").status_code == 200
        assert restarted.get(f"/api/certificates/{legacy['id']}").json() == legacy
        assert restarted.get(f"/api/certificates/{legacy['id']}/download").content == old_pdf
    assert drive.writes == writes  # Reading old or moved documents performs no writes.
    assert all(drive.files[key] == value for key, value in before.items())


def test_moving_only_an_older_pdf_keeps_legacy_json_and_lookup_untouched():
    drive = FakeDrive()
    info, pdf = legacy_record(drive)
    drive.files[info["driveFileId"]]["parents"] = ["another_folder_in_same_drive"]
    before = deepcopy(drive.files)
    with TestClient(create_app(certificate_storage=drive.storage())) as client:
        assert client.get(f"/api/certificates/{info['id']}").json() == info
        assert client.get(f"/api/certificates/{info['id']}/download").content == pdf
    assert drive.files == before and drive.writes == []


def test_signed_drive_copy_serves_same_pdf_after_original_is_deleted():
    drive = FakeDrive()
    with TestClient(create_app(certificate_storage=drive.storage(portable=True))) as client:
        info = upload(client).json()
        original_id = info["driveFileId"]
        copied_id = drive.new_id()
        drive.files[copied_id] = {**deepcopy(drive.files[original_id]), "id": copied_id}
        assert client.get(f"/api/certificates/{info['id']}").json() == info
        # The new QR pins the publication key; an identical signed copy works.
        del drive.files[original_id]
        selected = client.get(f"/api/certificates/{info['id']}").json()
        assert selected['driveFileId'] == copied_id and selected['issuedDriveFileId'] == original_id
        qr = client.get(info['verificationUrl'], follow_redirects=False)
        assert qr.status_code == 303
        assert qr.headers['location'] == f'https://drive.google.com/file/d/{copied_id}/view'
        assert client.get(info['downloadUrl']).content == drive.files[copied_id]['content']


def test_original_record_is_found_after_more_than_one_page_of_copies():
    drive = FakeDrive()
    with TestClient(create_app(certificate_storage=drive.storage())) as client:
        info = upload(client).json()
        original = drive.files.pop(info["driveFileId"])
        for _ in range(101):
            copied_id = drive.new_id()
            drive.files[copied_id] = {**deepcopy(original), "id": copied_id}
        drive.files[original["id"]] = original
        assert client.get(f"/api/certificates/{info['id']}").json() == info


def test_failed_new_publication_never_cleans_up_issued_files():
    drive = FakeDrive()
    legacy_record(drive)
    with TestClient(create_app(certificate_storage=drive.storage())) as client:
        assert upload(client).status_code == 201
        before = deepcopy(drive.files)
        drive.fail_permission = True
        assert upload(client).status_code == 503
    assert drive.files == before
    assert all(path.rsplit("/", 1)[-1] not in before
               for method, path in drive.writes if method == "DELETE")


def test_legacy_uuid_is_not_reissued_or_overwritten():
    drive = FakeDrive()
    info, pdf = legacy_record(drive)
    store = drive.storage()
    before = deepcopy(drive.files)
    with pytest.raises(StorageError) as error:
        store.save(info["id"], pdf, info, Publication("https://example.com/new", drive.new_id()))
    assert error.value.status == 409 and drive.files == before and drive.writes == []
    store.close()


def test_mismatched_publication_id_is_not_saved():
    drive = FakeDrive()
    info, pdf = legacy_record(drive)
    before = deepcopy(drive.files)
    store = drive.storage()
    with pytest.raises(StorageError) as error:
        store.save(str(uuid4()), pdf, info, Publication("https://example.com/new", drive.new_id()))
    assert error.value.status == 422 and drive.files == before and drive.writes == []
    store.close()


def test_private_record_roundtrips_unicode_without_modifying_pdf():
    info = {"id": str(uuid4()), "driveFileId": "synthetic_identifier",
            "filename": "Certificado Perú · presión.pdf", "warnings": ["Se conservó la información."] * 50}
    properties = encode_record(info)
    assert len(properties) <= 30
    assert all(len(key.encode()) + len(value.encode()) <= 124 for key, value in properties.items())
    assert decode_record(properties) == info


@pytest.mark.parametrize("damage", ["part_missing", "invalid_base64", "truncated", "expanded", "unknown_version"])
def test_corrupt_private_records_are_rejected(damage):
    properties = encode_record({"id": str(uuid4()), "driveFileId": "synthetic_identifier", "filename": "test.pdf"})
    if damage == "part_missing":
        properties.pop("qcp_m00")
    elif damage == "invalid_base64":
        properties["qcp_m00"] = "!!!"
    elif damage == "truncated":
        properties["qcp_m00"] = properties["qcp_m00"][:-4]
    elif damage == "expanded":
        properties["qcp_m00"] = base64.b64encode(zlib.compress(b" " * (64 * 1024 + 1))).decode()
    else:
        properties["qcp_record"] = "unknown"
    with pytest.raises(ValueError, match="registro privado"):
        decode_record(properties)


def test_oversized_record_does_not_publish_or_truncate_or_touch_older_files():
    drive = FakeDrive()
    info, pdf = legacy_record(drive)
    before = deepcopy(drive.files)
    noise = random.Random(17).randbytes(8000).hex()
    metadata = {**info, "id": str(uuid4()), "warnings": [noise]}
    store = drive.storage()
    with pytest.raises(StorageError) as error:
        store.save(metadata["id"], pdf, metadata, Publication("https://example.com/new", drive.new_id()))
    assert error.value.status == 422
    assert drive.files == before and drive.writes == []
    store.close()
