"""Admission, resource limits and cancellation independent of file conversion."""

import asyncio
from pathlib import Path

import pytest

from app.processing import Emissions, ProcessingBusy, conversion_workers, memory_limit, MiB


@pytest.mark.parametrize("limit,requested,expected", [
    (None, "2", 1), (512, "2", 1), (1024, "3", 1),
    (2048, "2", 2), (2048, "3", 2), (4096, "3", 3), (8192, "1", 1),
])
def test_worker_count_respects_memory_and_operator_limit(monkeypatch, limit, requested, expected):
    monkeypatch.setenv("CERTIFICATE_CONVERSION_WORKERS", requested)
    monkeypatch.setattr("app.processing.memory_limit", lambda: limit * MiB if limit else None)
    assert conversion_workers() == expected


@pytest.mark.parametrize("value", ["0", "4", "two"])
def test_worker_count_rejects_invalid_configuration(monkeypatch, value):
    monkeypatch.setenv("CERTIFICATE_CONVERSION_WORKERS", value)
    with pytest.raises(ValueError, match="1, 2 o 3"):
        conversion_workers()


@pytest.mark.parametrize("files,expected", [
    ({"/sys/fs/cgroup/memory.max": "536870912", "/proc/meminfo": "MemTotal: 8388608 kB"}, 512),
    ({"/sys/fs/cgroup/memory.max": "max", "/sys/fs/cgroup/memory/memory.limit_in_bytes": "1073741824"}, 1024),
    ({"/sys/fs/cgroup/memory.max": "max", "/proc/meminfo": "MemTotal: 2097152 kB"}, 2048),
    ({"/sys/fs/cgroup/memory/memory.limit_in_bytes": str(2**63 - 4096)}, None),
])
def test_container_memory_takes_precedence_over_host_memory(monkeypatch, files, expected):
    def read(path, *args, **kwargs):
        if str(path) not in files:
            raise FileNotFoundError
        return files[str(path)]
    monkeypatch.setattr(Path, "read_text", read)
    assert memory_limit() == (expected * MiB if expected else None)


def test_three_jobs_are_admitted_and_fourth_never_starts():
    async def scenario():
        emissions = Emissions()
        release = asyncio.Event()
        started = []
        async def job(number):
            started.append(number)
            await release.wait()
            return number
        requests = [asyncio.create_task(emissions.run(lambda i=i: job(i))) for i in range(3)]
        while len(started) < 3:
            await asyncio.sleep(0)
        with pytest.raises(ProcessingBusy):
            await emissions.run(lambda: job(4))
        assert started == [0, 1, 2]
        release.set()
        assert await asyncio.gather(*requests) == [0, 1, 2]
        assert await emissions.run(lambda: job(5)) == 5
        await emissions.close()
    asyncio.run(asyncio.wait_for(scenario(), timeout=5))


def test_disconnected_browser_keeps_its_slot_and_shutdown_waits_for_publication():
    async def scenario():
        emissions = Emissions()
        release = asyncio.Event()
        started = asyncio.Event()
        published = []
        async def job():
            started.set()
            await release.wait()
            published.append("saved")
        request = asyncio.create_task(emissions.run(job))
        await started.wait()
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        assert len(emissions.tasks) == 1
        closing = asyncio.create_task(emissions.close())
        await asyncio.sleep(0)
        assert not closing.done() and not published
        with pytest.raises(ProcessingBusy):
            await emissions.run(job)
        release.set()
        await closing
        assert published == ["saved"] and not emissions.tasks
    asyncio.run(asyncio.wait_for(scenario(), timeout=5))


def test_failed_job_releases_capacity_without_cancelling_its_neighbours():
    async def scenario():
        emissions = Emissions()
        release = asyncio.Event()
        started = asyncio.Event()
        async def waiting():
            started.set()
            await release.wait()
            return "other certificate"
        async def failed():
            raise ValueError("invalid certificate")
        other = asyncio.create_task(emissions.run(waiting))
        await started.wait()
        with pytest.raises(ValueError, match="invalid certificate"):
            await emissions.run(failed)
        assert len(emissions.tasks) == 1 and not other.done()
        release.set()
        assert await other == "other certificate"
        await emissions.close()
    asyncio.run(asyncio.wait_for(scenario(), timeout=5))
