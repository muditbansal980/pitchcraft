"""Service playbooks (WORKPLAN C9). Owner: Agent D.

How WLDD has actually sold and run each catalog service in past pitches
(mechanics, formats, volumes, creator tiers, handle categories, phase fit,
deliverables, KPIs, best-fit brand types, anonymised example applications).
Built offline by ``app.ingestion.service_playbooks`` into
``settings.service_playbooks_path`` (confidential, never in git). Missing file
=> empty mapping; callers fall back to the catalog descriptions.
"""

from pydantic import BaseModel, Field


class ServicePlaybook(BaseModel):
    service: str = Field(description="Exact catalog name")
    mechanics: list[str] = Field(default_factory=list)
    formats: list[str] = Field(default_factory=list)
    typical_volumes: list[str] = Field(default_factory=list)
    creator_tiers: list[str] = Field(default_factory=list)
    handle_categories: list[str] = Field(default_factory=list)
    phase_fit: list[str] = Field(default_factory=list)
    deliverables: list[str] = Field(default_factory=list)
    kpis: list[str] = Field(default_factory=list)
    best_for: list[str] = Field(default_factory=list)
    pairs_with: list[str] = Field(default_factory=list, description="Other services it combines with, and how")
    examples: list[str] = Field(default_factory=list, description="Anonymised applications (no client/creator names)")


def load_playbooks() -> dict[str, ServicePlaybook]:
    """Service name -> playbook. Empty dict if the playbooks haven't been built."""
    return {}
