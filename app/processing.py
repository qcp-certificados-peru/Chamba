"""Bound concurrent emissions independently of the number of open browsers."""

import asyncio
import os
from pathlib import Path
from typing import Awaitable, Callable, TypeVar

MAX_ACTIVE_CERTIFICATES = 3
MiB = 1024 * 1024
T = TypeVar("T")


def memory_limit() -> int | None:
    """Use the container limit, including older cgroup v1 deployments."""
    limits = []
    for name in ("/sys/fs/cgroup/memory.max",
                 "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        try:
            value = int(Path(name).read_text().strip())
        except (OSError, ValueError):
            continue
        if 0 < value < 2**60:
            limits.append(value)
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                limits.append(int(line.split()[1]) * 1024)
                break
    except (OSError, ValueError, IndexError):
        pass
    return min(limits) if limits else None


def conversion_workers() -> int:
    requested = os.environ.get("CERTIFICATE_CONVERSION_WORKERS", "2")
    if requested not in {"1", "2", "3"}:
        raise ValueError("CERTIFICATE_CONVERSION_WORKERS debe ser 1, 2 o 3.")
    limit = memory_limit()
    # Office and full-resolution PDF images are expensive. Small/unknown
    # containers still accept three users, but convert one file at a time.
    # This budget is an admission safeguard, not a maximum PDF memory usage.
    capacity = max(1, (limit - 256 * MiB) // (768 * MiB)) if limit else 1
    return min(int(requested), capacity)


class ProcessingBusy(Exception):
    pass


class Emissions:
    """Keep at most three jobs, including conversion and publication.

    A disconnected request must not free capacity while its worker or Drive
    write is still running. Jobs own their slots until completion; shutdown
    drains them before closing the executor and storage client.
    """

    def __init__(self):
        self.tasks: set[asyncio.Task] = set()
        self.closing = False

    def _completed(self, task: asyncio.Task):
        self.tasks.discard(task)
        if not task.cancelled():
            # Retrieve exceptions even if the originating browser disconnected.
            task.exception()

    async def run(self, operation: Callable[[], Awaitable[T]]) -> T:
        # No await between checking and reserving: admission is atomic on the
        # application event loop, with no unbounded executor backlog.
        if self.closing or len(self.tasks) >= MAX_ACTIVE_CERTIFICATES:
            raise ProcessingBusy
        task = asyncio.create_task(operation())
        self.tasks.add(task)
        task.add_done_callback(self._completed)
        return await asyncio.shield(task)

    async def close(self):
        self.closing = True
        await asyncio.gather(*self.tasks, return_exceptions=True)
