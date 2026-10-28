"""Provider errors, bounded safe retries and serial corporate publication."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import json
import threading
import time

from fastapi.testclient import TestClient
import httpx
import pytest

from app.drive_errors import rejection_reason, retry_after
from app.main import create_app
from app.storage import DriveStorage, StorageError
from test_drive import FakeDrive, ROOT
from test_drive_flat import legacy_record
from test_portal import pdf_bytes, upload


def rejected(reason, status=403, headers=None):
    return httpx.Response(status, headers=headers, json={"error": {
        "message": "synthetic-secret synthetic-refresh PRIVATE_FILE_ID",
        "errors": [{"reason": reason}]}})


def scripted_store(handler):
    store = DriveStorage(ROOT, "synthetic-client", "synthetic-secret", "synthetic-refresh",
                         client=httpx.Client(transport=httpx.MockTransport(handler)))
    store.token = "synthetic-access"
    store.token_deadline = time.monotonic() + 300
    return store


@pytest.mark.parametrize("reason,code,status", [
    ("storageQuotaExceeded", "DRIVE_STORAGE_FULL", 507),
    ("insufficientFilePermissions", "DRIVE_PERMISSION_DENIED", 503),
    ("insufficientPermissions", "DRIVE_PERMISSION_DENIED", 503),
    ("domainPolicy", "DRIVE_POLICY", 503),
    ("dailyLimitExceeded", "DRIVE_DAILY_LIMIT", 503),
    ("synthetic-secret", "DRIVE_REJECTED", 503),
])
def test_permanent_failures_are_specific_private_and_never_retried(caplog, reason, code, status):
    calls = []
    def handle(request):
        calls.append(request)
        return rejected(reason)
    store = scripted_store(handle)
    try:
        with pytest.raises(StorageError) as caught:
            store._request("POST", "/files/PRIVATE_FILE_ID/permissions", json={"type": "anyone", "role": "reader"})
        error = caught.value
        assert error.code == code and error.status == status
        assert error.operation == "publish_link" and len(calls) == 1
        assert all(secret not in str(error) + caplog.text for secret in ("synthetic-secret", "synthetic-refresh", "PRIVATE_FILE_ID"))
    finally:
        store.close()


@pytest.mark.parametrize("reason", ["rateLimitExceeded", "userRateLimitExceeded", "sharingRateLimitExceeded"])
def test_explicit_rate_rejections_retry_the_same_request_without_duplicate_writes(monkeypatch, reason):
    calls, delays = [], []
    monkeypatch.setattr("app.storage.time.sleep", delays.append)
    monkeypatch.setattr("app.storage.random.uniform", lambda *args: 0)
    def handle(request):
        calls.append(request)
        return rejected(reason) if len(calls) < 3 else httpx.Response(200, json={"id": "synthetic_permission"})
    store = scripted_store(handle)
    try:
        response = store._request("POST", "/files/reserved_identifier/permissions", json={"type": "anyone", "role": "reader"})
        assert response.status_code == 200 and delays == [1, 2]
        assert len(calls) == 3 and len({request.content for request in calls}) == 1
        assert len({str(request.url) for request in calls}) == 1
    finally:
        store.close()


def test_rate_limit_retries_are_bounded_and_server_retry_after_is_respected(monkeypatch):
    calls, delays = [], []
    monkeypatch.setattr("app.storage.time.sleep", delays.append)
    def handle(request):
        calls.append(request)
        return rejected("userRateLimitExceeded", headers={"Retry-After": "2"})
    store = scripted_store(handle)
    try:
        with pytest.raises(StorageError) as caught:
            store._request("GET", "/files/generateIds")
        assert len(calls) == 3 and len(delays) == 2 and all(delay >= 2 for delay in delays)
        assert caught.value.code == "DRIVE_RATE_LIMIT" and caught.value.retry_seconds == 2
    finally:
        store.close()
    calls.clear(); delays.clear()
    store = scripted_store(lambda request: calls.append(request) or rejected("sharingRateLimitExceeded", headers={"Retry-After": "60"}))
    try:
        with pytest.raises(StorageError) as caught:
            store._request("POST", "/files/reserved_identifier/permissions", json={})
        assert len(calls) == 1 and delays == [] and caught.value.retry_seconds == 60
    finally:
        store.close()


@pytest.mark.parametrize("failure", ["timeout", "server"])
def test_ambiguous_write_failures_are_never_automatically_repeated(failure):
    calls = []
    def handle(request):
        calls.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("synthetic-secret", request=request)
        return httpx.Response(503)
    store = scripted_store(handle)
    try:
        with pytest.raises(StorageError):
            store._request("POST", "/files", upload=True, content=b"synthetic PDF")
        assert len(calls) == 1
    finally:
        store.close()


def test_expired_access_token_is_renewed_once_without_reissuing_the_certificate():
    calls, refreshes = [], []
    def handle(request):
        if request.url.host == "oauth2.googleapis.com":
            refreshes.append(request)
            return httpx.Response(200, json={"access_token": "renewed-access", "expires_in": 3600})
        calls.append(request)
        return httpx.Response(401) if len(calls) == 1 else httpx.Response(200, json={"ids": ["reserved_identifier"]})
    store = scripted_store(handle)
    try:
        assert store._request("GET", "/files/generateIds").status_code == 200
        assert len(calls) == 2 and len(refreshes) == 1
        assert calls[1].headers["authorization"] == "Bearer renewed-access"
    finally:
        store.close()


def test_malformed_errors_and_rpc_reasons_are_handled_without_echoing_messages():
    assert rejection_reason(httpx.Response(403, json={"error": {"errors": [None, {"reason": ["bad"]}]}})) is None
    assert rejection_reason(httpx.Response(429, content=b"not JSON")) == "rateLimitExceeded"
    assert rejection_reason(httpx.Response(403, json={"error": {"details": [{"reason": "RATE_LIMIT_EXCEEDED"}]}})) == "rateLimitExceeded"
    assert retry_after(httpx.Response(403, headers={"Retry-After": "invalid"})) is None
    future = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=60), usegmt=True)
    assert 58 <= retry_after(httpx.Response(403, headers={"Retry-After": future})) <= 61


def test_temporary_sharing_failure_recovers_without_modifying_any_issued_file(monkeypatch):
    monkeypatch.setattr("app.storage.time.sleep", lambda _: None)
    class OnceLimitedDrive(FakeDrive):
        shares = 0
        def handle(self, request):
            if request.method == "POST" and request.url.path.endswith("/permissions"):
                self.shares += 1
                if self.shares == 1:
                    return rejected("sharingRateLimitExceeded")
            return super().handle(request)
    drive = OnceLimitedDrive()
    legacy_record(drive)
    before = deepcopy(drive.files)
    with TestClient(create_app(certificate_storage=drive.storage())) as client:
        response = upload(client)
        assert response.status_code == 201, response.text
        info = response.json()
        assert info["verificationUrl"] == info["driveUrl"]
    assert drive.shares == 2 and len(drive.permissions) == 1
    assert all(drive.files[key] == value for key, value in before.items())
    assert set(drive.files) - set(before) == {info["driveFileId"]}
    assert not any(method == "DELETE" for method, _ in drive.writes)


def test_three_browsers_queue_complete_drive_flows_without_overlapping_account_requests():
    drive = FakeDrive()
    store = drive.storage()
    allocate, save = store.allocate, store.save
    lock, start = threading.Lock(), threading.Barrier(3)
    active, peak = 0, 0
    def counted_allocate(*args):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        # Allow a broken implementation to overlap all three allocations.
        threading.Event().wait(.05)
        return allocate(*args)
    def counted_save(*args):
        nonlocal active
        try:
            return save(*args)
        finally:
            with lock:
                active -= 1
    store.allocate, store.save = counted_allocate, counted_save
    source = pdf_bytes()
    with TestClient(create_app(certificate_storage=store)) as client:
        def send(index):
            start.wait(timeout=5)
            return client.post("/api/certificates", files={"pdf": (f"worker-{index}.pdf", source)})
        with ThreadPoolExecutor(max_workers=3) as browsers:
            responses = list(browsers.map(send, range(3)))
        assert all(response.status_code == 201 for response in responses)
        assert len({response.json()["driveUrl"] for response in responses}) == 3
    assert peak == 1 and active == 0


@pytest.mark.parametrize("quota,expected_status,expected_storage", [
    ({"limit": "1000", "usage": "500"}, 200, "available"),
    ({"limit": "1000", "usage": "1000"}, 507, None),
    ({"usage": "500"}, 200, "not_reported"),
])
def test_read_only_account_diagnostic_is_cached_and_never_exposes_private_details(quota, expected_status, expected_storage):
    class CheckedDrive(FakeDrive):
        checks = 0
        def handle(self, request):
            if request.url.path == "/drive/v3/about":
                assert request.method == "GET" and request.url.params["fields"] == "storageQuota(limit,usage)"
                self.checks += 1
                return httpx.Response(200, json={"storageQuota": quota, "user": {"emailAddress": "private@example.com"}})
            return super().handle(request)
    drive = CheckedDrive()
    with TestClient(create_app(certificate_storage=drive.storage())) as client:
        first = client.get("/api/storage/check")
        second = client.get("/api/storage/check")
        assert first.status_code == second.status_code == expected_status and first.json() == second.json()
        assert first.headers["cache-control"] == "no-store"
        if expected_storage:
            assert first.json()["storage"] == expected_storage
        else:
            assert first.json()["storageError"]["code"] == "DRIVE_STORAGE_FULL"
        assert all(value not in first.text for value in (ROOT, "private@example.com", "synthetic-secret", "1000", "500"))
    assert drive.checks == 1 and drive.writes == [] and list(drive.files) == [ROOT]


def test_provider_rate_limit_reaches_the_frontend_as_a_specific_safe_error(monkeypatch):
    monkeypatch.setattr("app.storage.time.sleep", lambda _: None)
    class LimitedDrive(FakeDrive):
        attempts = 0
        def handle(self, request):
            if request.url.path.endswith("/files/generateIds"):
                self.attempts += 1
                return rejected("userRateLimitExceeded", headers={"Retry-After": "2"})
            return super().handle(request)
    drive = LimitedDrive()
    with TestClient(create_app(certificate_storage=drive.storage())) as client:
        response = upload(client)
        assert response.status_code == 503 and response.headers["retry-after"] == "2"
        assert response.json()["storageError"] == {"code": "DRIVE_RATE_LIMIT", "reason": "userRateLimitExceeded",
                                                   "operation": "reserve_pdf_link", "upstreamStatus": 403}
        assert "DRIVE_RATE_LIMIT" in response.json()["detail"]
        assert "synthetic-secret" not in response.text and "PRIVATE_FILE_ID" not in response.text
    assert drive.attempts == 3 and drive.writes == [] and list(drive.files) == [ROOT]
