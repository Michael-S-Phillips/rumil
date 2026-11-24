"""Background jobs (long verifications, Zotero syncs) with progress reporting."""

from __future__ import annotations

import threading
import time
import traceback
import uuid
from typing import Any, Callable


class Job:
    def __init__(self, label: str):
        self.id = uuid.uuid4().hex[:10]
        self.label = label
        self.state = "running"
        self.done = 0
        self.total = 0
        self.message = ""
        self.result: Any = None
        self.error: str | None = None
        self.started = time.time()
        self.finished: float | None = None
        self.cancel_requested = False

    def update(self, done: int, total: int, message: str = "") -> None:
        self.done, self.total = done, total
        if message:
            self.message = message

    def to_dict(self) -> dict:
        return {"id": self.id, "label": self.label, "state": self.state, "done": self.done, "total": self.total,
                "message": self.message, "error": self.error, "result": self.result,
                "elapsed_s": round((self.finished or time.time()) - self.started, 1)}


class JobManager:
    def __init__(self):
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def start(self, label: str, fn: Callable[[Job], Any]) -> Job:
        job = Job(label)
        with self._lock:
            self._jobs[job.id] = job
            # Keep the most recent 30 jobs.
            for old in sorted(self._jobs.values(), key=lambda j: j.started)[:-30]:
                self._jobs.pop(old.id, None)

        def run():
            try:
                job.result = fn(job)
                job.state = "cancelled" if job.cancel_requested else "finished"
            except Exception as e:  # report, don't crash the server
                job.state = "failed"
                job.error = f"{type(e).__name__}: {e}"
                job.message = traceback.format_exc(limit=3)
            finally:
                job.finished = time.time()

        threading.Thread(target=run, name=f"rumil-job-{job.id}", daemon=True).start()
        return job

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def running(self) -> list[Job]:
        return [j for j in self._jobs.values() if j.state == "running"]

    def all(self) -> list[dict]:
        return [j.to_dict() for j in sorted(self._jobs.values(), key=lambda j: -j.started)]
