"""Campaign season / cultural moment detection (Agent B).

``detect_season(today, text)`` finds the moment a campaign is planned around:
keywords in the brief/research win, otherwise the calendar picks whatever big
Indian moment is near (festival dates move every year, so windows are kept per
year where they matter). Each season carries palette accents, a decor pattern
for the renderer and words for hero/mock image prompts.
"""

import re
from dataclasses import dataclass, field
from datetime import date, timedelta


@dataclass(frozen=True)
class Season:
    key: str
    label: str
    accents: tuple[str, ...]  # palette accents the theme may borrow
    decor: str  # Theme.decor value
    prompt: str  # words for hero / mock prompts
    keywords: tuple[str, ...] = field(default_factory=tuple)
    photo: str = ""  # search query for a real, freely licensed season photo (Wikimedia Commons)


SEASONS: dict[str, Season] = {s.key: s for s in [
    Season("diwali", "Diwali", ("#FFB300", "#FF6F00", "#C2185B"), "diyas",
           "Diwali festive night, glowing diyas, marigold garlands, rangoli, warm fairy lights",
           ("diwali", "deepavali", "festival of lights", "dhanteras", "diya", "diyas", "rangoli", "bhai dooj"), photo="diwali diya lights"),
    Season("navratri", "Navratri / Durga Puja", ("#E53935", "#FFB300", "#8E24AA"), "confetti",
           "Navratri garba night, colourful chaniya choli, dandiya sticks, festive lights, Durga Puja pandal",
           ("navratri", "garba", "dandiya", "durga puja", "pujo", "dussehra", "dasara", "vijayadashami"), photo="navratri garba dance"),
    Season("holi", "Holi", ("#E91E63", "#00BCD4", "#FFEB3B"), "confetti",
           "Holi colour festival, clouds of gulal powder, water balloons, joyful crowd",
           ("holi", "gulal", "rang barse", "dhulandi"), photo="holi colours festival"),
    Season("rakhi", "Raksha Bandhan", ("#F57C00", "#C2185B", "#FFC107"), "dots",
           "Raksha Bandhan, sibling tying a rakhi, sweets box, warm family home",
           ("rakhi", "raksha bandhan", "rakshabandhan"), photo="raksha bandhan rakhi"),
    Season("eid", "Eid", ("#00897B", "#FFD54F", "#3949AB"), "dots",
           "Eid celebration, crescent moon, lanterns, sheer khurma, festive family feast",
           ("eid", "ramadan", "ramzan", "iftar", "eid ul fitr", "bakrid"), photo="eid celebration india"),
    Season("christmas", "Christmas & New Year", ("#C62828", "#2E7D32", "#FFD54F"), "confetti",
           "Christmas and New Year party, fairy lights, gifts, confetti, countdown",
           ("christmas", "xmas", "new year", "nye", "secret santa", "year end"), photo="christmas lights decoration"),
    Season("valentines", "Valentine's", ("#E91E63", "#F48FB1", "#B71C1C"), "dots",
           "Valentine's Day, hearts, roses, couple date night, pink and red tones",
           ("valentine", "valentines", "galentine", "14 feb"), photo="red roses bouquet"),
    Season("ipl", "IPL season", ("#1565C0", "#FFC107", "#E53935"), "pitch_lines",
           "IPL cricket night match, stadium floodlights, fans cheering, cricket bat and ball",
           ("ipl", "indian premier league", "t20", "csk", "rcb", "mumbai indians"), photo="ipl cricket stadium"),
    Season("world_cup", "Cricket World Cup", ("#1E88E5", "#FF9800", "#43A047"), "pitch_lines",
           "cricket World Cup, Indian fans in blue jerseys, tricolour face paint, stadium",
           ("world cup", "icc", "champions trophy", "asia cup", "team india", "men in blue"), photo="cricket stadium india fans"),
    Season("monsoon", "Monsoon", ("#26A69A", "#5C6BC0", "#90A4AE"), "waves",
           "Indian monsoon rain, chai and pakoras by the window, wet streets, umbrellas",
           ("monsoon", "barish", "baarish", "rainy season", "rains"), photo="monsoon rain india"),
    Season("summer", "Summer", ("#FFB300", "#FF7043", "#29B6F6"), "dots",
           "Indian summer heat, cold drinks, mango, sunlight, beach and pool vibes",
           ("summer", "heatwave", "garmi", "mango season", "summer vacation"), photo="mango fruit summer india"),
    Season("back_to_school", "Back to school", ("#1E88E5", "#FDD835", "#43A047"), "dots",
           "back to school and college, notebooks, backpacks, campus corridors",
           ("back to school", "back-to-school", "exam season", "board exams", "college fest", "admissions"), photo="indian school students classroom"),
    Season("independence_day", "Independence Day", ("#FF9933", "#138808", "#000080"), "stripes",
           "Indian Independence Day, tricolour flags, kites in the sky, patriotic celebration",
           ("independence day", "15 august", "republic day", "26 january", "har ghar tiranga"), photo="indian flag tricolour"),
    Season("big_sale", "Festive sale", ("#FF6F00", "#FFC107", "#D500F9"), "confetti",
           "festive online shopping sale, parcels and shopping bags, price tags, phone checkout",
           ("great indian festival", "big billion days", "prime day", "big deal days", "end of season sale",
            "eoss", "mega sale", "sale event", "flash sale", "black friday", "cyber monday"), photo="diwali market shopping india"),
]}

# (month, day) windows, plus year-specific ones for moving festivals.
_FIXED: list[tuple[tuple[int, int], tuple[int, int], str]] = [
    ((12, 15), (12, 31), "christmas"),
    ((1, 1), (1, 3), "christmas"),
    ((2, 1), (2, 15), "valentines"),
    ((3, 20), (5, 31), "ipl"),
    ((4, 1), (6, 15), "summer"),
    ((6, 15), (9, 15), "monsoon"),
    ((8, 1), (8, 15), "independence_day"),
    ((1, 20), (1, 26), "independence_day"),
    ((6, 1), (7, 10), "back_to_school"),
]
_BY_YEAR: dict[int, list[tuple[date, date, str]]] = {
    2026: [
        (date(2026, 2, 25), date(2026, 3, 5), "holi"),  # Holi 4 Mar
        (date(2026, 3, 12), date(2026, 3, 22), "eid"),  # Eid al-Fitr ~20 Mar
        (date(2026, 8, 20), date(2026, 8, 29), "rakhi"),  # Raksha Bandhan 28 Aug
        (date(2026, 9, 25), date(2026, 10, 10), "big_sale"),  # festive sales open
        (date(2026, 10, 11), date(2026, 10, 21), "navratri"),  # Sharad Navratri 11-20 Oct, Dussehra 20 Oct
        (date(2026, 10, 22), date(2026, 11, 12), "diwali"),  # Dhanteras 6 Nov, Diwali 8 Nov
    ],
    2027: [
        (date(2027, 3, 3), date(2027, 3, 12), "eid"),  # Eid al-Fitr ~10 Mar
        (date(2027, 3, 14), date(2027, 3, 23), "holi"),  # Holi 22 Mar
        (date(2027, 8, 9), date(2027, 8, 18), "rakhi"),  # Raksha Bandhan 17 Aug
        (date(2027, 9, 30), date(2027, 10, 10), "navratri"),
        (date(2027, 10, 11), date(2027, 11, 2), "diwali"),  # Diwali 29 Oct
    ],
}
# A campaign pitched today goes live a couple of weeks later: plan for that moment.
LEAD_DAYS = 14


def season_for_date(today: date) -> str | None:
    """Season the campaign will run in: the calendar at ``today`` + LEAD_DAYS."""
    when = today + timedelta(days=LEAD_DAYS)
    for start, end, key in _BY_YEAR.get(when.year, []):
        if start <= when <= end:
            return key
    md = (when.month, when.day)
    for start, end, key in _FIXED:
        if start <= md <= end:
            return key
    return None


def detect_season(today: date, text: str) -> str | None:
    """Season key for a campaign: explicit keywords in ``text`` first, else the calendar."""
    return detect_season_detail(today, text)[0]


def detect_season_detail(today: date, text: str) -> tuple[str | None, str | None]:
    """(season key, "text" | "calendar" | None): where the season came from."""
    low = (text or "").lower()
    best, best_hits, best_pos = None, 0, 10**9
    for s in SEASONS.values():
        hits, first = 0, 10**9
        for kw in s.keywords:
            for m in re.finditer(r"(?<![a-z])" + re.escape(kw) + r"(?![a-z])", low):
                hits += 1
                first = min(first, m.start())
        # More mentions win; ties go to the one mentioned first (the brief comes first in the text).
        if hits and (hits > best_hits or (hits == best_hits and first < best_pos)):
            best, best_hits, best_pos = s.key, hits, first
    if best:
        return best, "text"
    cal = season_for_date(today)
    return cal, ("calendar" if cal else None)


def get_season(key: str | None) -> Season | None:
    return SEASONS.get(key or "")


# --------------------------------------------------------------------------- calendar truth for prompts/checks

_MONTH_NAMES = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
_MONTH = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?"
_DAY = r"(?:\s*\d{1,2}(?!\d))?"
_MONTH_SPAN = rf"{_MONTH}{_DAY}(?:\s*(?:-|–|—|to)\s*(?:{_MONTH})?{_DAY})?(?:,?\s*20\d\d)?"


def season_window(key: str | None, today: date) -> tuple[date, date] | None:
    """The live or next real window for a season (approximate for fixed-calendar moments)."""
    cands: list[tuple[date, date]] = []
    for year in (today.year, today.year + 1):
        cands += [(s, e) for s, e, k in _BY_YEAR.get(year, []) if k == key]
        for (sm, sd), (em, ed), k in _FIXED:
            if k == key:
                cands.append((date(year, sm, sd), date(year, em, ed)))
    upcoming = [(s, e) for s, e in cands if e >= today]
    return min(upcoming) if upcoming else None


def _span(start: date, end: date) -> str:
    if start.year == end.year:
        return f"{start.day} {start:%b}–{end.day} {end:%b} {end.year}"
    return f"{start.day} {start:%b} {start.year}–{end.day} {end:%b} {end.year}"


def season_timing(key: str | None, today: date) -> str | None:
    """'IPL season is NOT live now: next window ~20 Mar–31 May 2027, ~23 weeks away' (or live now)."""
    season, window = SEASONS.get(key or ""), season_window(key, today)
    if not season or not window:
        return None
    start, end = window
    if start <= today:
        return f"{season.label} is live now (~{_span(start, end)})"
    weeks = max(1, (start - today).days // 7)
    return f"{season.label} is NOT live now: next window ~{_span(start, end)}, ~{weeks} week{'s' if weeks > 1 else ''} away"


def _months_in(text: str) -> set[int]:
    return {_MONTH_NAMES.index(m.group(0)[:3].lower()) + 1 for m in re.finditer(_MONTH, text)}


def _window_months(start: date, end: date) -> set[int]:
    months, d = set(), date(start.year, start.month, 1)
    while d <= end:
        months.add(d.month)
        d = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
    return months


def fix_dates_for(text: str, keyword: str, start: date, end: date, today: date, label: str | None = None) -> str:
    """Within ~60 chars after ``keyword``: a month span outside [start, end] becomes that window, and a
    year before the window's year next to the keyword is bumped to it."""
    kw_re = r"(?<![A-Za-z])" + re.escape(keyword) + r"(?![A-Za-z])"
    if not text or not re.search(kw_re, text, re.IGNORECASE):
        return text
    ok_months = _window_months(start, end)

    def bump_year(m: re.Match) -> str:
        # The next real window is in a later year, so "<moment> <past year>" is a season that's over.
        return f"{m.group(1)}{start.year}" if int(m.group(2)) < start.year else m.group(0)

    def fix_span(m: re.Match) -> str:
        months = _months_in(m.group(2))
        return m.group(1) + (label or _span(start, end)) if months and not months <= ok_months else m.group(0)

    text = re.sub(rf"({kw_re}\s+)(20\d\d)\b", bump_year, text, flags=re.IGNORECASE)
    return re.sub(rf"({kw_re}[^.;\n]{{0,60}}?)({_MONTH_SPAN})", fix_span, text, flags=re.IGNORECASE)


def fix_event_dates(text: str, today: date, skip: set[str] | None = None) -> str:
    """Calendar fallback: correct dates attached to a known moment, e.g. "IPL 2026 (matches Oct-Nov 2026)".

    ``skip`` holds lower-cased keywords already corrected from verified, sourced dates.
    """
    if not text:
        return text
    for season in SEASONS.values():
        if skip and any(kw in skip for kw in season.keywords):
            continue
        window = season_window(season.key, today)
        if window:
            for kw in season.keywords:
                text = fix_dates_for(text, kw, window[0], window[1], today)
    return text
