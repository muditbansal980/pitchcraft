"""Background plan jobs (WORKPLAN C8). Owner: Agent C.

POST /api/brand-plan/jobs       body: BrandPlanRequest -> 202 {"job_id": str}
GET  /api/brand-plan/jobs/{id}  -> {status, stage, progress, result?, error?, status_code?}

A flagship deck takes minutes, longer than browsers and proxies like to hold a
request open, so the client starts a job and polls it. Validation and error
messages match the synchronous ``POST /api/brand-plan`` (routes/plan.py): request
problems are answered straight away, generation failures land in the job as
``error`` + ``status_code``.
"""

import inspect
import logging
import time

import groq
from fastapi import APIRouter, HTTPException, status
from langchain_core.exceptions import ModelAuthenticationError, ModelError, ModelRateLimitError
from pydantic import BaseModel
from tavily import InvalidAPIKeyError, UsageLimitExceededError

from app.api.deps import CurrentUser
from app.core.config import settings
from app.schemas.plan import BrandPlanRequest, BrandPlanResponse
from app.services import brand_plan, jobs
from app.services.brand_plan import PlanGenerationError, ServicesCatalogEmptyError
from app.services.llm_router import LLMOutputError, LLMUnavailableError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/brand-plan/jobs", tags=["plan"])

# Same text as routes/plan.py, so both endpoints read the same to users.
_RATE_LIMIT_MESSAGE = (
    "Every AI model is at its free-tier limit right now (Groq allows ~8K tokens/minute per model). "
    "Try again in a minute."
)


class JobCreated(BaseModel):
    job_id: str


class JobView(BaseModel):
    job_id: str
    status: jobs.JobStatus
    stage: str
    progress: float
    result: BrandPlanResponse | None = None
    error: str | None = None
    status_code: int | None = None
    elapsed: float


def plan_error(exc: Exception) -> tuple[int, str]:
    """(status code, detail) for a generation failure; mirrors routes/plan.py's except chain."""
    if isinstance(exc, HTTPException):
        return exc.status_code, str(exc.detail)
    if isinstance(exc, ServicesCatalogEmptyError):
        return 503, "WLDD services are not ingested yet (run `uv run python -m app.ingestion.services_pipeline`)"
    if isinstance(exc, InvalidAPIKeyError):
        return 502, "Tavily rejected the API key"
    if isinstance(exc, UsageLimitExceededError):
        return 429, "Tavily usage limit reached"
    if isinstance(exc, LLMUnavailableError):
        logger.warning("Plan job: %s", exc)
        if "No LLM is configured" in str(exc):
            return 503, str(exc)
        if "out of free-tier budget" in str(exc):
            return 429, _RATE_LIMIT_MESSAGE
        return 429, f"{_RATE_LIMIT_MESSAGE} ({exc})"
    if isinstance(exc, (groq.RateLimitError, ModelRateLimitError)):
        return 429, _RATE_LIMIT_MESSAGE
    if isinstance(exc, groq.AuthenticationError):
        return 502, "Groq rejected the API key"
    if isinstance(exc, ModelAuthenticationError):
        return 502, "The LLM provider (Groq/Gemini) rejected the API key"
    if isinstance(exc, groq.APIStatusError):
        logger.error("Groq request failed", exc_info=exc)
        return 502, f"Plan generation failed (Groq {exc.status_code}): {exc.message}"
    if isinstance(exc, groq.APIError):
        logger.error("Groq request failed", exc_info=exc)
        return 502, f"Plan generation failed (Groq unreachable): {exc}"
    if isinstance(exc, ModelError):
        logger.error("LLM request failed", exc_info=exc)
        return 502, f"Plan generation failed (LLM error): {exc}"
    if isinstance(exc, (PlanGenerationError, LLMOutputError)):
        return 502, f"Plan generation failed: {exc}"
    logger.error("Brand plan job failed", exc_info=exc)
    return 502, f"Brand plan failed: {exc}"


def _accepts_on_stage() -> bool:
    try:
        return "on_stage" in inspect.signature(brand_plan.generate_brand_plan).parameters
    except (TypeError, ValueError):
        return False


def _view(job: jobs.PlanJob) -> JobView:
    end = job.finished_at or time.time()
    return JobView(
        job_id=job.id,
        status=job.status,
        stage=job.stage,
        progress=round(job.progress, 3),
        result=job.result,
        error=job.error,
        status_code=job.status_code,
        elapsed=round(end - job.created_at, 1),
    )


@router.post("", response_model=JobCreated, status_code=status.HTTP_202_ACCEPTED)
async def start_job(payload: BrandPlanRequest, user: CurrentUser) -> JobCreated:
    brand = payload.brand.strip()
    if not brand:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Brand name is required")
    brief = (payload.brief or "").strip() or None
    if not settings.tavily_api_key:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "TAVILY_API_KEY is not configured")
    if not settings.groq_api_key and not settings.google_api_key:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "No LLM key is configured (set GROQ_API_KEY or GOOGLE_API_KEY)")

    # Looked up per call so a test (or a reload) can swap the generator.
    with_stages = _accepts_on_stage()

    async def work(on_stage: jobs.StageCallback) -> BrandPlanResponse:
        on_stage("research", 0.01)
        if with_stages:
            return await brand_plan.generate_brand_plan(brand, brief=brief, scale=payload.scale, on_stage=on_stage)
        return await brand_plan.generate_brand_plan(brand, brief=brief, scale=payload.scale)

    try:
        job = jobs.store.start(user.id, brand, work, plan_error)
    except jobs.JobLimitError as exc:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, str(exc)) from None
    logger.info("Plan job %s started for %r (scale=%s, stages=%s)", job.id, brand, payload.scale, with_stages)
    return JobCreated(job_id=job.id)


# async: the store is only touched from the event loop thread, never from the threadpool.
@router.get("/{job_id}", response_model=JobView)
async def get_job(job_id: str, user: CurrentUser) -> JobView:
    job = jobs.store.get(user.id, job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Plan job not found (it may have expired)")
    return _view(job)
