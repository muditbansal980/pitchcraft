"""Service playbooks (WORKPLAN C9). Owner: Agent D.

How WLDD has actually sold and run each catalog service in past pitches
(mechanics, formats, volumes, creator tiers, handle categories, phase fit,
deliverables, KPIs, best-fit brand types, anonymised example applications).
Built offline by ``app.ingestion.service_playbooks`` into
``settings.service_playbooks_path`` (confidential, never in git). Missing file
=> empty mapping; callers fall back to the catalog descriptions.

``basis`` says where a playbook comes from: ``"past_pitches"`` (distilled from
decks that sold the service) or ``"general"`` (no past pitch sold it; built from
the services PDF + general agency practice, so treat volumes as indicative).
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from app.core.config import settings

logger = logging.getLogger(__name__)


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
    basis: Literal["past_pitches", "general"] = "general"
    pitches_used: int = Field(0, description="Number of past pitches the playbook was distilled from")


_lock = threading.Lock()
_cache: tuple[str, float, dict[str, ServicePlaybook]] | None = None


def load_playbooks() -> dict[str, ServicePlaybook]:
    """Service name -> playbook. Empty dict if the playbooks haven't been built.

    Cached in memory and re-read when the file's mtime changes.
    """
    global _cache
    path = Path(settings.service_playbooks_path)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}
    with _lock:
        if _cache and _cache[0] == str(path) and _cache[1] == mtime:
            return dict(_cache[2])
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("service playbooks unreadable (%s): %s", path, exc)
            return {}
        items = raw.get("playbooks", raw) if isinstance(raw, dict) else {}
        out: dict[str, ServicePlaybook] = {}
        for name, data in items.items() if isinstance(items, dict) else []:
            try:
                pb = ServicePlaybook.model_validate({"service": name, **data})
            except ValidationError as exc:
                logger.warning("service playbook %r invalid: %s", name, exc)
                continue
            out[pb.service] = pb
        _cache = (str(path), mtime, out)
        return dict(out)


def playbook_text(pb: ServicePlaybook, max_items: int = 4) -> str:
    """Compact prompt rendering of one playbook."""
    head = pb.service + (" (general practice; no past WLDD pitch on file)" if pb.basis == "general" else "")
    lines = [head]
    for label, vals in (
        ("Mechanics", pb.mechanics), ("Formats", pb.formats), ("Typical volumes", pb.typical_volumes),
        ("Creator tiers", pb.creator_tiers), ("Handle categories", pb.handle_categories), ("Phase fit", pb.phase_fit),
        ("Deliverables", pb.deliverables), ("KPIs", pb.kpis), ("Best for", pb.best_for), ("Pairs with", pb.pairs_with),
        ("Examples", pb.examples),
    ):
        if vals:
            lines.append(f"- {label}: " + "; ".join(vals[:max_items]))
    return "\n".join(lines)
