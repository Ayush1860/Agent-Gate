"""An asyncio job queue with bounded workers and per-job timeouts."""

from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

log = logging.getLogger(__name__)

DEFAULT_WORKERS = 4
DEFAULT_TIMEOUT = 30.0


@dataclass
class Job:
    job_id: str
    run: Callable[[], Awaitable[Any]]
    timeout: float = DEFAULT_TIMEOUT
    result: Any = None
    error: str | None = None


@dataclass
class JobQueue:
    """Fan work out across a fixed pool. One failing job never stops the pool."""

    workers: int = DEFAULT_WORKERS
    _queue: asyncio.Queue = field(default_factory=asyncio.Queue, init=False)
    _done: list[Job] = field(default_factory=list, init=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)

    async def submit(self, job: Job) -> None:
        await self._queue.put(job)

    async def _record(self, job: Job) -> None:
        async with self._lock:
            self._done.append(job)

    async def _worker(self, name: str) -> None:
        while True:
            job = await self._queue.get()
            try:
                job.result = await asyncio.wait_for(job.run(), timeout=job.timeout)
            except asyncio.TimeoutError:
                job.error = f"timed out after {job.timeout}s"
                log.warning("job %s timed out on %s", job.job_id, name)
            except Exception as exc:
                job.error = f"{type(exc).__name__}: {exc}"
                log.exception("job %s failed on %s", job.job_id, name)
            finally:
                await self._record(job)
                self._queue.task_done()

    async def drain(self) -> list[Job]:
        """Run everything queued, then stop the workers cleanly."""
        tasks = [
            asyncio.create_task(self._worker(f"worker-{i}")) for i in range(self.workers)
        ]
        await self._queue.join()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        return list(self._done)


async def run_archive_hook(archive: str, destination: str) -> int:
    """Post-processing hook. Arguments are passed as a list, never through a shell."""
    binary = shutil.which("tar")
    if binary is None:
        raise RuntimeError("tar is not available on this host")
    proc = await asyncio.create_subprocess_exec(
        binary,
        "-xf",
        archive,
        "-C",
        destination,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    _out, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"archive hook failed: {err.decode(errors='replace')[:200]}")
    return proc.returncode
