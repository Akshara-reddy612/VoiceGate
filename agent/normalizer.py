"""Rule-based date/time/service extraction for English, Tenglish and Hinglish (romanized).

Deliberately small and predictable. It only sees text, so it is only as good as the speech recognizer's
transcript; spelling variants it doesn't know are simply not recognized (no guessing).

    normalize("repu sayantram 5 ki", today)  -> date=tomorrow, time=17:00
    normalize("kal subah 10 baje", today)    -> date=tomorrow, time=10:00
    normalize("parso shaam ko", today)       -> date=day after tomorrow, time=None, issues=["needs_hour"]
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, time, timedelta

from booking.models import BusinessConfig

# ---------------------------------------------------------------- vocabulary

RELATIVE_DAYS: dict[str, int] = {
    # English
    "today": 0, "tomorrow": 1, "yesterday": -1,
    # Hindi
    "aaj": 0, "kal": 1, "parso": 2, "parson": 2,  # kal/parso are ambiguous (past/future): booking context = future
    # Telugu
    "ivala": 0, "ivvala": 0, "ivaala": 0, "eeroju": 0, "eroju": 0, "neeroju": 0,
    "repu": 1, "reypu": 1, "ellundi": 2, "elundi": 2,
    "ninna": -1, "monna": -2,
}
MULTIWORD_DAYS = {"day after tomorrow": 2, "day before yesterday": -2, "ee roju": 0, "e roju": 0}

WEEKDAY_WORDS: dict[str, int] = {}
for idx, names in enumerate([
    ("monday", "somvar", "somwar", "somavaram", "somvaram"),
    ("tuesday", "mangalvar", "mangalwar", "mangalavaram"),
    ("wednesday", "budhvar", "budhwar", "budhavaram", "budavaram"),
    ("thursday", "guruvar", "guruwar", "brihaspativar", "guruvaram", "brihaspathivaram"),
    ("friday", "shukravar", "shukrawar", "sukravar", "shukravaram", "sukravaram"),
    ("saturday", "shanivar", "shaniwar", "shanivaram", "sanivaram"),
    ("sunday", "ravivar", "raviwar", "itvaar", "itwar", "aadivaram", "adivaram", "aadhivaram"),
]):
    for n in names:
        WEEKDAY_WORDS[n] = idx

MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
# "next week" in English / Hindi / Telugu (a weekday is still needed to pick the day).
NEXT_WEEK_RE = re.compile(r"\b(next week|agle hafte|agle hafta|agla hafta|vacche vaaram|vachhe vaaram|vache varam|vacche varam)\b")
# Two times offered as alternatives: "5 or 6", "5pm ya 6pm", "between 5 and 6".
TIME_ALTERNATIVES_RE = re.compile(
    r"\b\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)?\s*(?:or|ya|leda|ledha|/)\s*\d{1,2}\b"
    r"|\bbetween\s+\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)?\s+and\s+\d{1,2}\b"
)

PARTS_OF_DAY: dict[str, str] = {
    "morning": "morning", "subah": "morning", "subha": "morning", "savere": "morning",
    "poddunne": "morning", "poddunna": "morning", "podduna": "morning", "poddhunne": "morning",
    "afternoon": "afternoon", "dopahar": "afternoon", "dopehar": "afternoon", "dupahar": "afternoon",
    "madhyanam": "afternoon", "madhyaanam": "afternoon", "madhyahnam": "afternoon",
    "evening": "evening", "shaam": "evening", "sham": "evening", "saam": "evening",
    "sayantram": "evening", "saayantram": "evening", "sayantharam": "evening", "sayankalam": "evening",
    "night": "night", "raat": "night", "raatri": "night", "ratri": "night", "rathri": "night",
}

NUMBER_WORDS: dict[str, int] = {
    # Hindi
    "ek": 1, "do": 2, "teen": 3, "char": 4, "chaar": 4, "paanch": 5, "panch": 5, "chhe": 6, "che": 6, "chheh": 6,
    "saat": 7, "aath": 8, "nau": 9, "das": 10, "gyarah": 11, "gyaarah": 11, "barah": 12, "baarah": 12,
    # Telugu
    "okati": 1, "rendu": 2, "moodu": 3, "mudu": 3, "naalugu": 4, "nalugu": 4, "aidu": 5, "ayidu": 5,
    "aaru": 6, "edu": 7, "eedu": 7, "enimidi": 8, "tommidi": 9, "padi": 10, "padakondu": 11, "pannendu": 12,
    # English
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
    "ten": 10, "eleven": 11, "twelve": 12,
}
# Words that mark "N o'clock": Hindi baje, Telugu ki/ku/gantalaki, English o'clock.
OCLOCK = r"(?:baje|bajay|bje|gantalaki|gantalaku|gantala\s+ki|gantaki|ki|ku|o'?clock)"
NUM = r"(\d{1,2}|" + "|".join(sorted(NUMBER_WORDS, key=len, reverse=True)) + r")"


@dataclass
class Normalized:
    date: date | None = None
    time: time | None = None
    service: str | None = None
    part_of_day: str | None = None
    issues: list[str] = field(default_factory=list)
    candidates: list[time] = field(default_factory=list)  # the options, when issues has ambiguous_time


def _num(token: str) -> int:
    return int(token) if token.isdigit() else NUMBER_WORDS[token]


def _clean(text: str) -> str:
    t = text.lower().replace("’", "'")
    t = re.sub(r"[^\w:'\s-]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


# ---------------------------------------------------------------- date


def extract_date(t: str, today: date, issues: list[str]) -> date | None:
    if m := re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", t):
        try:
            d = date(int(m[1]), int(m[2]), int(m[3]))
        except ValueError:
            issues.append("invalid_date")
            return None
        return _past_check(d, today, issues)

    month_re = "(" + "|".join(MONTHS) + r")[a-z]*"
    m = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?" + month_re, t) or re.search(
        r"\b" + month_re + r"\s+(\d{1,2})(?:st|nd|rd|th)?\b", t
    )
    if m:
        day_s, mon_s = (m[1], m[2]) if m[1].isdigit() else (m[2], m[1])
        try:
            d = date(today.year, MONTHS.index(mon_s[:3]) + 1, int(day_s))
        except ValueError:
            issues.append("invalid_date")
            return None
        return d if d >= today else d.replace(year=today.year + 1)

    if NEXT_WEEK_RE.search(t):
        # "next week tuesday" = the Tuesday of the following calendar week, never this week's.
        day = next((WEEKDAY_WORDS[w] for w in t.split() if w in WEEKDAY_WORDS), None)
        if day is None:
            issues.append("needs_day")
            return None
        next_monday = today + timedelta(days=7 - today.weekday())
        return next_monday + timedelta(days=day)

    for phrase, offset in MULTIWORD_DAYS.items():
        if re.search(rf"\b{phrase}\b", t):
            return _past_check(today + timedelta(days=offset), today, issues)
    for word in t.split():
        if word in RELATIVE_DAYS:
            return _past_check(today + timedelta(days=RELATIVE_DAYS[word]), today, issues)
        if word in WEEKDAY_WORDS:
            ahead = (WEEKDAY_WORDS[word] - today.weekday()) % 7 or 7  # same weekday = next week
            return today + timedelta(days=ahead)
    return None


def _past_check(d: date, today: date, issues: list[str]) -> date:
    if d < today:
        issues.append("past_date")
    return d


# ---------------------------------------------------------------- time


def _apply_part(h: int, part: str | None) -> int:
    if part in ("afternoon", "evening", "night") and h < 12:
        return h + 12
    if part == "morning":
        return h
    if part is None and 1 <= h <= 7:
        return h + 12  # "5 ki" / "at 5" at a clinic means 5 PM
    return h


def _make(h: int, m: int, part: str | None, meridiem: str | None, issues: list[str]) -> time | None:
    if meridiem:
        if not 1 <= h <= 12:
            issues.append("invalid_time")
            return None
        h = h % 12 + (12 if meridiem.startswith("p") else 0)
    else:
        if h > 23:
            issues.append("invalid_time")
            return None
        if h <= 12:
            h = _apply_part(h, part)
    if not 0 <= m <= 59:
        issues.append("invalid_time")
        return None
    return time(h, m)


def _telugu_and_half(word: str) -> int | None:
    """naalugunnara -> 4, padinnara -> 10 (Telugu '-nnara' = 'and a half')."""
    if not word.endswith("nnara") or len(word) <= 5:
        return None
    root = word[:-5]
    for cand in (root, root + "u", root + "i", root.rstrip("u"), root.rstrip("u") + "u"):
        if cand in NUMBER_WORDS:
            return NUMBER_WORDS[cand]
    return None


def time_candidates(t: str, part: str | None, issues: list[str]) -> list[time | None]:
    """Every time expression in the text, most specific pattern first. Matched text is blanked out so one
    phrase is never read twice ("4:30 pm" is one time, not 4:30 and "30 pm"). None = an invalid time."""
    work = re.sub(r"\b\d{4}-\d{2}-\d{2}\b", " ", t)
    found: list[time | None] = []
    mer = r"\s*(a\.?m\.?|p\.?m\.?)?"
    part_words = "|".join(sorted(PARTS_OF_DAY, key=len, reverse=True))
    patterns = [
        (r"\b(\d{1,2}):(\d{2})" + mer, lambda m: _make(int(m[1]), int(m[2]), part, m[3], issues)),
        (r"\b(\d{1,2})\s*(a\.?m\.?|p\.?m\.?)(?!\w)", lambda m: _make(int(m[1]), 0, part, m[2], issues)),
        (r"\b(?:12\s+)?(?:noon|midday)\b", lambda m: time(12, 0)),
        # Hindi fractions: saadhe N = N:30, sawa N = N:15, paune N = (N-1):45, dedh = 1:30, dhai = 2:30
        (r"\b(?:saadhe|sadhe|saade|sade)\s+" + NUM + r"\b", lambda m: _make(_num(m[1]), 30, part, None, issues)),
        (r"\b(?:sawa|sava)\s+" + NUM + r"\b", lambda m: _make(_num(m[1]), 15, part, None, issues)),
        (r"\b(?:paune|pone)\s+" + NUM + r"\b", lambda m: _make(_num(m[1]) - 1, 45, part, None, issues)),
        (r"\b(?:dedh|derh)\b", lambda m: _make(1, 30, part, None, issues)),
        (r"\b(?:dhai|dhaai|adhai|dhaee)\b", lambda m: _make(2, 30, part, None, issues)),
        (r"\b\w+nnara\b", lambda m: _make(h, 30, part, None, issues) if (h := _telugu_and_half(m[0])) is not None else "skip"),
        (r"\b" + NUM + r"\s*" + OCLOCK + r"\b", lambda m: _make(_num(m[1]), 0, part, None, issues)),
        (r"\b(?:at|@)\s*(\d{1,2})\b(?![:/-]|\s*(?:st|nd|rd|th)\b|\s+(?:" + "|".join(MONTHS) + "))",
         lambda m: _make(int(m[1]), 0, part, None, issues)),
        (r"\b(?:" + part_words + r")\s+(?:ko\s+)?(\d{1,2})\b(?!\s*(?:st|nd|rd|th)\b)",
         lambda m: _make(int(m[1]), 0, part, None, issues)),
    ]
    for pattern, make in patterns:
        def take(m: re.Match) -> str:
            value = make(m)
            if value == "skip":
                return m[0]
            found.append(value)
            return " " * len(m[0])

        work = re.sub(pattern, take, work)
    return found


def extract_time(t: str, part: str | None, issues: list[str]) -> time | None:
    """One unambiguous time, or None. Two different times ("5pm or 6pm") -> ambiguous_time, never a guess."""
    cands = time_candidates(t, part, issues)
    if any(c is None for c in cands):
        return None  # invalid_time already recorded
    distinct = list(dict.fromkeys(cands))
    if len(distinct) > 1 or TIME_ALTERNATIVES_RE.search(t):
        if "ambiguous_time" not in issues:
            issues.append("ambiguous_time")
        return None
    return distinct[0] if distinct else None


def extract_part_of_day(t: str, issues: list[str]) -> str | None:
    found = []
    for word in t.split():
        p = PARTS_OF_DAY.get(word)
        if p and p not in found:
            found.append(p)
    if len(found) > 1:
        issues.append("ambiguous_time")
        return None
    return found[0] if found else None


# ---------------------------------------------------------------- service


def match_service(text: str, cfg: BusinessConfig) -> str | None:
    t = text.lower()
    pairs = [(a.lower(), k) for k, s in cfg.services.items() for a in [s.label, *s.aliases]]
    for alias, key in sorted(pairs, key=lambda p: -len(p[0])):
        if re.search(rf"\b{re.escape(alias)}\b", t):
            return key
    return None


# ---------------------------------------------------------------- entry point


def normalize(text: str, today: date, cfg: BusinessConfig | None = None) -> Normalized:
    t = _clean(text)
    issues: list[str] = []
    part = extract_part_of_day(t, issues)
    ambiguous = "ambiguous_time" in issues
    d = extract_date(t, today, issues)
    candidates: list[time] = []
    if ambiguous:
        tm = None
    else:
        tm = extract_time(t, part, issues)
        if "ambiguous_time" in issues:
            candidates = list(dict.fromkeys(c for c in time_candidates(t, part, []) if c))
    if tm is None and part and "ambiguous_time" not in issues and "invalid_time" not in issues:
        issues.append("needs_hour")
    service = match_service(t, cfg) if cfg else None
    return Normalized(date=d, time=tm, service=service, part_of_day=part, issues=issues, candidates=candidates)


def coerce_date(value: str, today: date) -> str:
    """Tool-layer helper: accept ISO or a spoken phrase ('repu', 'kal'); return ISO if understood, else as-is."""
    try:
        return date.fromisoformat(value.strip()).isoformat()
    except ValueError:
        d = normalize(value, today).date
        return d.isoformat() if d else value


def coerce_time(value: str, today: date) -> str:
    """Tool-layer helper: accept HH:MM or a spoken phrase ('sayantram 5', 'saadhe das'); return HH:MM if understood."""
    v = value.strip()
    if re.fullmatch(r"\d{1,2}:\d{2}", v):
        return v
    tm = normalize(v, today).time
    return tm.strftime("%H:%M") if tm else value
