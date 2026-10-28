"""New QR survives native Drive copies; old QR never changes its destination."""

from copy import deepcopy
import hashlib
import json
from urllib.parse import urlsplit

from fastapi.testclient import TestClient
import httpx
import pytest

from app.drive_records import encode_record
from app.main import create_app
from app.portable_qr import new_key, signed_record, verified_record
from app.portable_smoke import fixture_pdf
from app.storage import Publication, StorageError
from test_drive import FakeDrive, ROOT
from test_drive_flat import legacy_record
from test_portal import pdf_bytes, upload


@pytest.mark.parametrize("permanent", [False, True])
def test_same_printed_qr_works_after_copy_and_trash_or_delete_original(permanent):
    drive = FakeDrive()
    with TestClient(create_app(certificate_storage=drive.storage())) as client:
        info = upload(client).json()
        original = info["driveFileId"]
        pdf = client.get(info["downloadUrl"]).content
        copied_id = drive.new_id()
        copied = {**deepcopy(drive.files[original]), "id": copied_id,
                  "parents": ["outside_configured_folder"], "name": "RENAMED-COPY.pdf", "appProperties": {},'properties':{}}
        drive.files[copied_id] = copied
        if permanent:
            del drive.files[original]
        else:
            drive.files[original]["trashed"] = True
        before = deepcopy(drive.files); writes = list(drive.writes)
    # No disk/cache or original private metadata is needed after a restart.
    with TestClient(create_app(certificate_storage=drive.storage())) as restarted:
        qr = restarted.get(info["verificationUrl"])
        assert qr.status_code == 200, qr.text
        assert qr.content == pdf and "inline;" in qr.headers["content-disposition"]
        assert hashlib.sha256(qr.content).hexdigest() == info["sha256"]
        assert restarted.get(info["downloadUrl"]).content == pdf
        selected = restarted.get(f"/api/certificates/{info['id']}?key={info['qrPublicKey']}").json()
        assert selected["driveFileId"] == copied_id and selected["issuedDriveFileId"] == original
        assert selected["verificationUrl"] == info["verificationUrl"]
    assert all(drive.files[k]==v for k,v in before.items()if v.get('mimeType')!='application/json')
    # Only the new private registry's location hint may be updated.
    assert all(method=='PATCH'and drive.files[path.rsplit('/',1)[-1]]['mimeType']=='application/json'
               for method,path in drive.writes[len(writes):])


@pytest.mark.parametrize("damage", ["pdf", "record", "key", "foreign_owner"])
def test_a_changed_or_forged_copy_never_replaces_the_issued_pdf(damage):
    drive = FakeDrive()
    with TestClient(create_app(certificate_storage=drive.storage())) as client:
        info = upload(client).json(); original = info["driveFileId"]
        copied_id = drive.new_id(); row = deepcopy(drive.files.pop(original)); row["id"] = copied_id
        if damage == "pdf":
            row["content"] += b"changed"
        elif damage == "record":
            forged = {**info, "filename": "OTHER-CERTIFICATE.pdf"}
            props = encode_record(forged); props.pop("qcp_origin")
            registry=next(r for r in drive.files.values()if r.get('name')==info['id']+'.json')
            envelope=__import__('json').loads(registry['content'])
            props["qcp_signature"] = envelope['signedRecord']["qcp_signature"]
            envelope['signedRecord']=props;registry['content']=__import__('json').dumps(envelope).encode()
        elif damage == "key":
            other_public, other_private = new_key()
            forged = {**info, "qrPublicKey": other_public,
                      "verificationUrl": info["verificationUrl"].rsplit("/", 1)[0] + "/" + other_public}
            registry=next(r for r in drive.files.values()if r.get('name')==info['id']+'.json')
            envelope=__import__('json').loads(registry['content'])
            envelope['signedRecord']=signed_record(forged,other_private)
            registry['content']=__import__('json').dumps(envelope).encode()
        else:
            row["ownedByMe"] = False
        drive.files[copied_id] = row
        response = client.get(info["verificationUrl"])
        assert response.status_code in {404, 409}
        assert response.headers["content-type"].startswith("application/json")


def test_valid_copy_is_selected_among_bad_copies_and_identical_names():
    drive = FakeDrive()
    with TestClient(create_app(certificate_storage=drive.storage())) as client:
        first = upload(client).json(); second = upload(client).json()
        original = drive.files.pop(first["driveFileId"])
        bad_id, good_id = drive.new_id(), drive.new_id()
        drive.files[bad_id] = {**deepcopy(original), "id": bad_id, "content": original["content"] + b"changed"}
        drive.files[good_id] = {**deepcopy(original), "id": good_id, "appProperties": {}}
        assert client.get(first["verificationUrl"]).content == original["content"]
        assert client.get(second["verificationUrl"]).content == drive.files[second["driveFileId"]]["content"]
        assert first["verificationUrl"] != second["verificationUrl"]


def test_diagnostic_copies_only_its_fresh_fictitious_pdf_then_deletes_its_source():
    drive = FakeDrive(); legacy_record(drive); before = deepcopy(drive.files)
    assert fixture_pdf() == fixture_pdf()
    with TestClient(create_app(certificate_storage=drive.storage())) as client:
        source = client.get("/api/pruebas/qr-portable.pdf").content
        response = client.post("/api/certificates", files={"pdf": ("PRUEBA-SIN-VALIDEZ.pdf", source)},
                               data={"tipoCertificado": "no_acreditado", "pruebaCopia": "true"})
        assert response.status_code == 201, response.text
        info = response.json(); proof = info["copyTest"]
        assert proof == {"ok": True, "originalDeleted": True, "sameQr": True,
                         "copiedFileId": info["driveFileId"], "privatePropertiesRemoved": True,'customPropertiesRemoved':True}
        assert info["issuedDriveFileId"] not in drive.files
        assert drive.files[info["driveFileId"]]["parents"] == ["root"]
        assert client.get(info["verificationUrl"]).status_code == 200
    assert all(drive.files[key] == value for key, value in before.items())
    assert len([r for k,r in drive.files.items()if k not in before and r.get('mimeType')=='application/pdf'])==1
    assert all(path.rsplit("/", 1)[-1] not in before for method, path in drive.writes if method == "DELETE")


def test_diagnostic_flag_cannot_copy_or_delete_a_real_upload():
    drive = FakeDrive()
    with TestClient(create_app(certificate_storage=drive.storage())) as client:
        response = client.post("/api/certificates", files={"pdf": ("PRUEBA-SIN-VALIDEZ.pdf", pdf_bytes())},
                               data={"tipoCertificado": "no_acreditado", "pruebaCopia": "true"})
        assert response.status_code == 422
    assert list(drive.files) == [ROOT] and drive.token_requests == 0 and drive.writes == []


def test_diagnostic_keeps_source_if_new_copy_is_not_identical():
    class DamagedCopyDrive(FakeDrive):
        def handle(self, request):
            response = super().handle(request)
            if request.url.path.endswith('/copy') and response.status_code == 200:
                self.files[response.json()['id']]['content'] += b'changed'
            return response
    drive = DamagedCopyDrive()
    with TestClient(create_app(certificate_storage=drive.storage())) as client:
        response = client.post('/api/certificates',files={'pdf':('TEST.pdf',fixture_pdf())},
                               data={'tipoCertificado':'no_acreditado','pruebaCopia':'true'})
        assert response.status_code == 201,response.text
        info = response.json()
        assert not info['copyTest']['ok'] and not info['copyTest']['originalDeleted']
        assert info['driveFileId'] in drive.files
        assert client.get(info['verificationUrl']).status_code == 200
        assert not any(method == 'DELETE' for method,_ in drive.writes)


def test_single_page_pdf_can_be_issued_and_reissued_with_a_new_qr():
    drive = FakeDrive()
    with TestClient(create_app(certificate_storage=drive.storage())) as client:
        first = client.post("/api/certificates", files={"pdf": ("TEST.pdf", fixture_pdf())},
                            data={"tipoCertificado": "no_acreditado"})
        assert first.status_code == 201, first.text
        data = client.get(first.json()["downloadUrl"]).content
        second = client.post("/api/certificates", files={"pdf": ("TEST.pdf", data)},
                             data={"tipoCertificado": "no_acreditado"})
        assert second.status_code == 201, second.text
        assert first.json()["verificationUrl"] != second.json()["verificationUrl"]


def test_public_record_contains_no_private_key_and_fits_drive_limits():
    public, private = new_key()
    info = {"id": "00000000-0000-4000-8000-000000000001", "driveFileId": "synthetic_file_id",
            "qrPublicKey": public, "qrMode": "portable-v1",
            "verificationUrl": f"https://example.com/q/00000000-0000-4000-8000-000000000001/{public}"}
    properties = signed_record(info, private)
    assert verified_record(properties, info["id"], public) == info
    assert len(properties) <= 30 and all(len(k.encode()) + len(v.encode()) <= 124 for k, v in properties.items())
    assert private.hex() not in str(properties) and "signing_key" not in str(properties)


def test_publication_id_cannot_be_reissued_after_only_a_copy_remains():
    drive = FakeDrive()
    with TestClient(create_app(certificate_storage=drive.storage())) as client:
        info = upload(client).json()
        row = drive.files.pop(info['driveFileId']);copy_id=drive.new_id()
        drive.files[copy_id]={**row,'id':copy_id,'appProperties':{}}
    before=deepcopy(drive.files);store=drive.storage()
    with pytest.raises(StorageError)as error:
        store.save(info['id'],row['content'],info,Publication(info['verificationUrl'],drive.new_id()))
    assert error.value.status==409 and drive.files==before
    store.close()


def test_copy_is_used_if_original_disappears_during_qr_download():
    class ConcurrentDeleteDrive(FakeDrive):
        delete_on_download = None
        def handle(self, request):
            if (self.delete_on_download and request.url.path.endswith('/'+self.delete_on_download)
                    and request.url.params.get('alt')=='media'):
                del self.files[self.delete_on_download]
                self.delete_on_download=None
                return httpx.Response(404)
            return super().handle(request)
    drive=ConcurrentDeleteDrive()
    with TestClient(create_app(certificate_storage=drive.storage()))as client:
        info=upload(client).json();original=info['driveFileId'];copy_id=drive.new_id()
        drive.files[copy_id]={**deepcopy(drive.files[original]),'id':copy_id,'appProperties':{}}
        expected=drive.files[original]['content'];drive.delete_on_download=original
        response=client.get(info['verificationUrl'])
        assert response.status_code==200 and response.content==expected
        assert original not in drive.files


def test_first_portable_format_can_recover_its_record_from_trash():
    drive=FakeDrive()
    with TestClient(create_app(certificate_storage=drive.storage()))as client:
        info=upload(client).json();original=info['driveFileId'];copy_id=drive.new_id()
        registry=next(row for row in drive.files.values()if row.get('name')==info['id']+'.json')
        signed=json.loads(registry['content'])['signedRecord']
        drive.files[original]['properties']=signed
        drive.files[copy_id]={**deepcopy(drive.files[original]),'id':copy_id,'appProperties':{},'properties':{}}
        for fid in list(drive.files):
            if drive.files[fid].get('mimeType')=='application/json' or drive.files[fid].get('properties',{}).get('qcp_portable_registry'):
                del drive.files[fid]
        drive.files[original]['trashed']=True
    with TestClient(create_app(certificate_storage=drive.storage()))as restarted:
        response=restarted.get(info['verificationUrl'])
        assert response.status_code==200
        assert hashlib.sha256(response.content).hexdigest()==info['sha256']


def test_md5_is_only_a_locator_when_drive_omits_sha256():
    class NoShaDrive(FakeDrive):
        def handle(self,request):
            response=super().handle(request)
            if response.status_code!=200 or request.url.params.get('alt')=='media':return response
            payload=response.json()
            for row in payload.get('files',[payload]):
                row.pop('sha256Checksum',None)
                data=self.files.get(row.get('id'),{}).get('content')
                if data is not None:
                    row['md5Checksum']=hashlib.md5(data,usedforsecurity=False).hexdigest()
            return httpx.Response(200,json=payload)
    drive=NoShaDrive()
    with TestClient(create_app(certificate_storage=drive.storage()))as client:
        info=upload(client).json();original=drive.files.pop(info['driveFileId']);copy_id=drive.new_id()
        drive.files[copy_id]={**original,'id':copy_id,'appProperties':{},'properties':{}}
        response=client.get(info['verificationUrl'])
        assert response.status_code==200
        assert hashlib.sha256(response.content).hexdigest()==info['sha256']
