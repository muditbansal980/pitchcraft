"""Background plan jobs (WORKPLAN C8). Owner: Agent C.

In-memory, per user, kept ~1 h. ``POST /api/brand-plan/jobs`` starts
``generate_brand_plan(..., on_stage=...)`` in the background; ``GET
/api/brand-plan/jobs/{id}`` returns ``{status, stage, progress, result?, error?}``.

The store lives in this process: with several uvicorn workers a job is only
visible to the worker that started it, and a restart forgets every job (the
client then gets a 404 and says so).
"""

import asyncio
import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal

from app.schemas.plan import BrandPlanResponse

logger = logging.getLogger(__name__)

JobStatus = Literal["queued", "running", "done", "error"]
StageCallback = Callable[[str, float], None]
# Runs the generation; gets the stage callback, returns the finished plan.
JobWork = Callable[[StageCallback], Awaitable[BrandPlanResponse]]
# Turns a failure into (HTTP status code, message), like the sync route's errors.
ErrorMapper = Callable[[Exception], tuple[int, str]]

RETENTION_SECONDS = 60 * 60
MAX_ACTIVE_PER_USER = 3
MAX_JOBS_PER_USER = 30
# Far above a flagship deck (~5 min); stops a hung provider call from holding a slot forever.
JOB_TIMEOUT_SECONDS = 20 * 60


class JobLimitError(Exception):
    pass


@dataclass
class PlanJob:
    id: str
    user_id: int
    brand: str
    status: JobStatus = "queued"
    stage: str = "queued"
    progress: float = 0.0
    result: BrandPlanResponse | None = None
    error: str | None = None
    status_code: int | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    finished_at: float | None = None

    @property
    def active(self) -> bool:
        return self.status in ("queued", "running")

    def set_stage(self, stage: str, progress: float) -> None:
        """Record progress. Progress never goes backwards; finished jobs ignore late calls."""
        if not self.active:
            return
        self.status = "running"
        self.stage = str(stage)[:40] or self.stage
        try:
            value = float(progress)
        except (TypeError, ValueError):
            value = self.progress
        # Keep a sliver for "done", which only the finished job reports.
        self.progress = max(self.progress, min(max(value, 0.0), 0.99))
        self.updated_at = time.time()


class JobStore:
    def __init__(
        self,
        *,
        retention_seconds: float = RETENTION_SECONDS,
        max_active_per_user: int = MAX_ACTIVE_PER_USER,
        max_jobs_per_user: int = MAX_JOBS_PER_USER,
        timeout_seconds: float = JOB_TIMEOUT_SECONDS,
    ):
        self.retention_seconds = retention_seconds
        self.max_active_per_user = max_active_per_user
        self.max_jobs_per_user = max_jobs_per_user
        self.timeout_seconds = timeout_seconds
        self._jobs: dict[str, PlanJob] = {}
        # Strong references, so running tasks aren't garbage-collected mid-flight.
        self._tasks: dict[str, asyncio.Task] = {}

    def cleanup(self, now: float | None = None) -> None:
        """Forget finished jobs older than the retention window."""
        now = time.time() if now is None else now
        for job_id, job in list(self._jobs.items()):
            if not job.active and (job.finished_at or job.created_at) + self.retention_seconds < now:
                del self._jobs[job_id]

    def get(self, user_id: int, job_id: str) -> PlanJob | None:
        self.cleanup()
        job = self._jobs.get(job_id)
        # Someone else's job looks exactly like a missing one.
        return job if job is not None and job.user_id == user_id else None

    def start(self, user_id: int, brand: str, work: JobWork, map_error: ErrorMapper) -> PlanJob:
        self.cleanup()
        mine = [job for job in self._jobs.values() if job.user_id == user_id]
        if sum(job.active for job in mine) >= self.max_active_per_user:
            raise JobLimitError(
                f"You already have {self.max_active_per_user} plans generating. Wait for one to finish."
            )
        # Drop this user's oldest finished jobs beyond the cap.
        finished = sorted((job for job in mine if not job.active), key=lambda job: job.created_at)
        for job in finished[: max(0, len(mine) + 1 - self.max_jobs_per_user)]:
            self._jobs.pop(job.id, None)

        job = PlanJob(id=secrets.token_urlsafe(16), user_id=user_id, brand=brand)
        self._jobs[job.id] = job
        task = asyncio.create_task(self._run(job, work, map_error), name=f"plan-job-{job.id}")
        self._tasks[job.id] = task
        task.add_done_callback(lambda _t, job_id=job.id: self._tasks.pop(job_id, None))
        return job

    async def _run(self, job: PlanJob, work: JobWork, map_error: ErrorMapper) -> None:
        job.status = "running"
        job.updated_at = time.time()
        started = time.monotonic()
        try:
            result = await asyncio.wait_for(work(job.set_stage), timeout=self.timeout_seconds)
        except TimeoutError:
            logger.warning("Plan job %s for %r timed out after %.0f s", job.id, job.brand, self.timeout_seconds)
            self._fail(job, 504, "Plan generation took too long. Please try again.")
        except asyncio.CancelledError:
            self._fail(job, 503, "Plan generation was stopped (the server is restarting). Please try again.")
            raise
        except Exception as exc:  # noqa: BLE001 - every failure becomes a job error
            try:
                code, message = map_error(exc)
            except Exception:
                logger.exception("Mapping a plan job error failed")
                code, message = 502, "Brand plan failed"
            self._fail(job, code, message)
        else:
            job.result = result
            job.status = "done"
            job.stage = "done"
            job.progress = 1.0
            job.finished_at = job.updated_at = time.time()
            logger.info("Plan job %s for %r done in %.1f s", job.id, job.brand, time.monotonic() - started)

    @staticmethod
    def _fail(job: PlanJob, code: int, message: str) -> None:
        job.status = "error"
        job.status_code = code
        job.error = message
        job.finished_at = job.updated_at = time.time()


store = JobStore()
