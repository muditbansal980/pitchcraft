"""Deck -> Markdown (web plan + PDF).

``# <deck title>`` from the cover, then one ``##`` per slide; fields become
short paragraphs, bullets or tables; visuals with a stored image become
``![caption](url)``, unfilled ones an italic "Visual idea" line.
"""

from __future__ import annotations

from app.schemas.deck import (
    CardGridSlide,
    ChipsSlide,
    CoverSlide,
    Deck,
    DividerSlide,
    FinSlide,
    FormulaSlide,
    FromToSlide,
    IdeaSlide,
    RefSlide,
    SamplesSlide,
    StatementSlide,
    TableSlide,
    TimelineSlide,
    TwoColumnSlide,
    Visual,
)


def _cell(text: str) -> str:
    return " ".join(str(text).split()).replace("|", "\\|")


def _table(headers: list[str], rows: list[list[str]]) -> list[str]:
    if not headers:
        return []
    width = len(headers)
    lines = ["| " + " | ".join(_cell(h) for h in headers) + " |", "|" + " --- |" * width]
    for row in rows:
        cells = [_cell(c) for c in row[:width]] + [""] * max(0, width - len(row))
        lines.append("| " + " | ".join(cells) + " |")
    return lines


def _visual(v: Visual | None) -> list[str]:
    if v is None:
        return []
    caption = v.brief or v.credit or {"ref": "Ref", "meme": "Sample meme", "mock": "Sample visual"}.get(v.want, "Visual")
    if v.url:
        lines = [f"![{_cell(caption)}]({v.url})"]
        if v.source_url:
            lines.append(f"*[{_cell(v.credit or 'Source')}]({v.source_url})*")
        elif v.credit:
            lines.append(f"*{_cell(v.credit)}*")
        return lines
    if v.meme_boxes:
        return [f"*Sample meme: {' / '.join(_cell(b) for b in v.meme_boxes if b)}*"]
    return [f"*Visual idea: {_cell(v.brief)}*"] if v.brief else []


def _slide_lines(slide) -> list[str]:
    out: list[str] = []
    if isinstance(slide, StatementSlide):
        if slide.strip:
            out.append(f"**{slide.strip}**")
        if slide.body:
            out.append(slide.body)
        out += [f"- {b}" for b in slide.bullets]
    elif isinstance(slide, FromToSlide):
        if slide.strip:
            out.append(f"**{slide.strip}**")
        out += [f"- **From:** {slide.from_text}", f"- **To:** {slide.to_text}"]
    elif isinstance(slide, FormulaSlide):
        out.append(" × ".join(f"**{p.label}**" for p in slide.parts) + f" = **{slide.result.label}**")
        out += [f"- **{p.label}:** {p.text}" for p in slide.parts]
        out.append(f"- **{slide.result.label}:** {slide.result.text}")
    elif isinstance(slide, CardGridSlide):
        if slide.strip:
            out.append(f"**{slide.strip}**")
        for card in slide.cards:
            tags = f" _({', '.join(card.tags)})_" if card.tags else ""
            out.append(f"- **{card.title}**: {card.body}{tags}".rstrip(": "))
            out += ["  " + line for line in _visual(card.visual)]
    elif isinstance(slide, DividerSlide):
        if slide.subtitle:
            out.append(f"*{slide.subtitle}*")
    elif isinstance(slide, IdeaSlide):
        out.append(f"**Concept note:** {slide.concept_note}")
        for label, value in (
            ("Hook", slide.hook), ("Format", slide.format), ("Creator", slide.creator), ("Scalability", slide.scalability),
        ):
            if value:
                out.append(f"- **{label}:** {value}")
        if slide.execution:
            out.append("**Execution**")
            out += [f"- {k}. {step}" for k, step in enumerate(slide.execution, start=1)]
        if slide.sample_posts:
            out.append("**Sample posts**")
            out += [f"- “{p}”" for p in slide.sample_posts]
        if slide.services:
            out.append(f"**Powered by:** {', '.join(slide.services)}"
                       + (f" ({slide.service_mechanics})" if slide.service_mechanics else ""))
        out += _visual(slide.visual)
    elif isinstance(slide, SamplesSlide):
        if slide.strip:
            out.append(f"**{slide.strip}**")
        for v in slide.visuals:
            out += _visual(v)
    elif isinstance(slide, RefSlide):
        if slide.why:
            out.append(slide.why)
        for v in slide.visuals:
            out += _visual(v)
    elif isinstance(slide, TwoColumnSlide):
        for col in (slide.left, slide.right):
            out.append(f"**{col.heading}**")
            out += [f"- {b}" for b in col.bullets]
    elif isinstance(slide, TableSlide):  # also KpiTableSlide
        if slide.strip:
            out.append(f"**{slide.strip}**")
        out += _table(slide.headers, slide.rows)
    elif isinstance(slide, ChipsSlide):
        if slide.strip:
            out.append(f"**{slide.strip}**")
        out += [f"- **{g.label}:** {', '.join(g.chips)}" for g in slide.groups]
    elif isinstance(slide, TimelineSlide):
        if slide.strip:
            out.append(f"**{slide.strip}**")
        for phase in slide.phases:
            when = f" ({phase.when})" if phase.when else ""
            out.append(f"- **{phase.name}{when}:** " + "; ".join(phase.points))
    elif isinstance(slide, FinSlide):
        if slide.subtitle:
            out.append(slide.subtitle)
    return out


def deck_to_markdown(deck: Deck) -> str:
    blocks: list[str] = []
    slides = list(deck.slides)
    cover = slides[0] if slides and isinstance(slides[0], CoverSlide) else None
    blocks.append(f"# {cover.title if cover else deck.title}")
    if cover:
        intro = [line for line in (cover.subtitle, f"**{cover.tagline}**" if cover.tagline else "") if line]
        if intro:
            blocks.append("\n\n".join(intro))
        if deck.theme.hero_image and deck.theme.hero_image.url:
            blocks.append(f"![{_cell(deck.title)}]({deck.theme.hero_image.url})")
        slides = slides[1:]
    for slide in slides:
        lines = [f"## {slide.title}"]
        body = _slide_lines(slide)
        if body:
            lines.append("")
            lines += _join(body)
        if slide.notes and not isinstance(slide, FinSlide):
            lines += ["", slide.notes]
        if slide.sources:
            lines += ["", "*Sources: " + "; ".join(_cell(x) for x in slide.sources) + "*"]
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks).strip() + "\n"


def _join(lines: list[str]) -> list[str]:
    """Blank line between paragraphs; list items and table rows stay together."""
    out: list[str] = []
    for line in lines:
        is_block = line.startswith(("- ", "  ", "|"))
        if out and not (is_block and out[-1].startswith(("- ", "  ", "|"))):
            out.append("")
        out.append(line)
    return out
