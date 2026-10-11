"""Background plan jobs (WORKPLAN C8). Owner: Agent C.

POST /api/brand-plan/jobs       body: BrandPlanRequest -> {"job_id": str}
GET  /api/brand-plan/jobs/{id}  -> {status, stage, progress, result?, error?, status_code?}
"""

from fastapi import APIRouter

router = APIRouter(prefix="/api/brand-plan/jobs", tags=["plan"])
