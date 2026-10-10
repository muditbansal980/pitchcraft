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


SEASONS: dict[str, Season] = {s.key: s for s in [
    Season("diwali", "Diwali", ("#FFB300", "#FF6F00", "#C2185B"), "diyas",
           "Diwali festive night, glowing diyas, marigold garlands, rangoli, warm fairy lights",
           ("diwali", "deepavali", "festival of lights", "dhanteras", "diya", "diyas", "rangoli", "bhai dooj")),
    Season("navratri", "Navratri / Durga Puja", ("#E53935", "#FFB300", "#8E24AA"), "confetti",
           "Navratri garba night, colourful chaniya choli, dandiya sticks, festive lights, Durga Puja pandal",
           ("navratri", "garba", "dandiya", "durga puja", "pujo", "dussehra", "dasara", "vijayadashami")),
    Season("holi", "Holi", ("#E91E63", "#00BCD4", "#FFEB3B"), "confetti",
           "Holi colour festival, clouds of gulal powder, water balloons, joyful crowd",
           ("holi", "gulal", "rang barse", "dhulandi")),
    Season("rakhi", "Raksha Bandhan", ("#F57C00", "#C2185B", "#FFC107"), "dots",
           "Raksha Bandhan, sibling tying a rakhi, sweets box, warm family home",
           ("rakhi", "raksha bandhan", "rakshabandhan")),
    Season("eid", "Eid", ("#00897B", "#FFD54F", "#3949AB"), "dots",
           "Eid celebration, crescent moon, lanterns, sheer khurma, festive family feast",
           ("eid", "ramadan", "ramzan", "iftar", "eid ul fitr", "bakrid")),
    Season("christmas", "Christmas & New Year", ("#C62828", "#2E7D32", "#FFD54F"), "confetti",
           "Christmas and New Year party, fairy lights, gifts, confetti, countdown",
           ("christmas", "xmas", "new year", "nye", "secret santa", "year end")),
    Season("valentines", "Valentine's", ("#E91E63", "#F48FB1", "#B71C1C"), "dots",
           "Valentine's Day, hearts, roses, couple date night, pink and red tones",
           ("valentine", "valentines", "galentine", "14 feb")),
    Season("ipl", "IPL season", ("#1565C0", "#FFC107", "#E53935"), "pitch_lines",
           "IPL cricket night match, stadium floodlights, fans cheering, cricket bat and ball",
           ("ipl", "indian premier league", "t20", "csk", "rcb", "mumbai indians")),
    Season("world_cup", "Cricket World Cup", ("#1E88E5", "#FF9800", "#43A047"), "pitch_lines",
           "cricket World Cup, Indian fans in blue jerseys, tricolour face paint, stadium",
           ("world cup", "icc", "champions trophy", "asia cup", "team india", "men in blue")),
    Season("monsoon", "Monsoon", ("#26A69A", "#5C6BC0", "#90A4AE"), "waves",
           "Indian monsoon rain, chai and pakoras by the window, wet streets, umbrellas",
           ("monsoon", "barish", "baarish", "rainy season", "rains")),
    Season("summer", "Summer", ("#FFB300", "#FF7043", "#29B6F6"), "dots",
           "Indian summer heat, cold drinks, mango, sunlight, beach and pool vibes",
           ("summer", "heatwave", "garmi", "mango season", "summer vacation")),
    Season("back_to_school", "Back to school", ("#1E88E5", "#FDD835", "#43A047"), "dots",
           "back to school and college, notebooks, backpacks, campus corridors",
           ("back to school", "back-to-school", "exam season", "board exams", "college fest", "admissions")),
    Season("independence_day", "Independence Day", ("#FF9933", "#138808", "#000080"), "stripes",
           "Indian Independence Day, tricolour flags, kites in the sky, patriotic celebration",
           ("independence day", "15 august", "republic day", "26 january", "har ghar tiranga")),
    Season("big_sale", "Festive sale", ("#FF6F00", "#FFC107", "#D500F9"), "confetti",
           "festive online shopping sale, parcels and shopping bags, price tags, phone checkout",
           ("great indian festival", "big billion days", "prime day", "big deal days", "end of season sale",
            "eoss", "mega sale", "sale event", "flash sale", "black friday", "cyber monday")),
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
