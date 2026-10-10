import re
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

# "1. Amplification (VOXPOP, Memes, DP)"
HEADING_RE = re.compile(r"^(\d+)\.\s+(.+)$")
BULLET = "●"


@dataclass
class ServiceSection:
    """One numbered service from the PDF, split into paragraphs (description + bullets)."""

    number: int
    name: str
    page: int
    paragraphs: list[str] = field(default_factory=list)


def _clean(line: str) -> str:
    # Google Docs exports sprinkle zero-width spaces after headings and bullets.
    return line.replace("\u200b", "").strip()


def load_service_sections(pdf_path: str | Path) -> list[ServiceSection]:
    """Extract the numbered services from the PDF, keeping wrapped lines together."""
    sections: list[ServiceSection] = []
    current: list[str] = []

    def flush() -> None:
        if sections and current:
            sections[-1].paragraphs.append(" ".join(current))
        current.clear()

    with pymupdf.open(pdf_path) as doc:
        for page in doc:
            for raw in page.get_text().splitlines():
                line = _clean(raw)
                if not line:
                    continue
                if match := HEADING_RE.match(line):
                    flush()
                    sections.append(
                        ServiceSection(
                            number=int(match.group(1)),
                            name=match.group(2).strip(),
                            page=page.number + 1,
                        )
                    )
                elif line.startswith(BULLET):
                    flush()
                    current.append(line.removeprefix(BULLET).strip())
                elif sections:
                    current.append(line)
    flush()
    return sections
