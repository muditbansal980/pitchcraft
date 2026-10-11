"""Background plan jobs (WORKPLAN C8). Owner: Agent C.

In-memory, per user, kept ~1 h. ``POST /api/brand-plan/jobs`` starts
``generate_brand_plan(..., on_stage=...)`` in the background; ``GET
/api/brand-plan/jobs/{id}`` returns ``{status, stage, progress, result?, error?}``.
"""
