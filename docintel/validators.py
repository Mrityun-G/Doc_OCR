"""Field-level normalisation and validation.

normalize(type, raw, item) -> (value, status, messages)
status: valid | warning | invalid   (needs_review is decided later from confidence/flags)
"""
from __future__ import annotations

import datetime as _dt
import re

from .textnorm import normalize as _norm, split_list

ENUMS = {
    "sex": {"male": ["m", "male", "man"], "female": ["f", "female", "woman"],
            "other": ["other", "non binary", "nonbinary", "x"],
            "unknown": ["unknown", "prefer not to say", "declined", "undisclosed"]},
    "marital": {"single": ["single", "never married", "s"], "married": ["married", "m"],
                "divorced": ["divorced", "d"], "widowed": ["widowed", "widow", "widower", "w"],
                "separated": ["separated"], "domestic_partner": ["domestic partner", "partner", "partnered"]},
    "holder_relationship": {"self": ["self", "patient", "same", "insured"], "spouse": ["spouse", "husband", "wife", "partner"],
                            "child": ["child", "son", "daughter", "dependent"],
                            "parent": ["parent", "mother", "father", "guardian", "legal guardian"],
                            "other": ["other"]},
    "severity": {"mild": ["mild", "low", "minor"], "moderate": ["moderate", "mod", "medium"],
                 "severe": ["severe", "high", "serious"],
                 "life_threatening": ["life threatening", "anaphylaxis", "anaphylactic"]},
    "living": {"living": ["living", "alive", "l", "yes"], "deceased": ["deceased", "dead", "passed away", "died", "d"]},
}

US_STATES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA", "colorado": "CO",
    "connecticut": "CT", "delaware": "DE", "district of columbia": "DC", "florida": "FL", "georgia": "GA",
    "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD", "massachusetts": "MA",
    "michigan": "MI", "minnesota": "MN", "mississippi": "MS", "missouri": "MO", "montana": "MT",
    "nebraska": "NE", "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM",
    "new york": "NY", "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC", "south dakota": "SD",
    "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY", "puerto rico": "PR",
}

DATE_FORMATS = ["%m/%d/%Y", "%m-%d-%Y", "%m.%d.%Y", "%Y-%m-%d", "%m/%d/%y", "%m-%d-%y",
                "%B %d, %Y", "%B %d %Y", "%b %d, %Y", "%b %d %Y", "%d %B %Y", "%d %b %Y"]

DOSAGE_RE = re.compile(r"\d+(\.\d+)?\s*(mg|mcg|µg|ug|g|ml|mL|units?|iu|IU|%|tabs?|tablets?|caps?|capsules?|puffs?|drops?|meq|mEq)\b",
                       re.I)


def today() -> _dt.date:
    return _dt.date.today()


def parse_date(raw: str) -> tuple[_dt.date | None, list[str]]:
    s = re.sub(r"\s+", " ", str(raw).strip()).rstrip(".")
    s = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", s)
    for f in DATE_FORMATS:
        try:
            d = _dt.datetime.strptime(s, f).date()
        except ValueError:
            continue
        msgs = []
        if "%y" in f and d > today():
            d = d.replace(year=d.year - 100)
            msgs.append("two-digit year interpreted in the 1900s")
        if f.startswith("%m") and d.day <= 12 and d.month != d.day:
            msgs.append("numeric date interpreted as MM/DD (US form convention)")
        return d, msgs
    return None, []


def _enum(name: str, raw: str):
    n = _norm(raw)
    for canon, syns in ENUMS[name].items():
        if n == canon.replace("_", " ") or n in syns:
            return canon
    return None


def normalize(typ: str, raw, item: dict | None = None):
    item = item or {}
    msgs: list[str] = []
    if typ in ("signature",):
        return bool(raw), "valid", msgs
    if typ in ("checkbox",):
        return bool(raw), "valid", msgs
    if isinstance(raw, bool):
        return raw, "valid", msgs
    s = re.sub(r"\s+", " ", str(raw)).strip()
    if not s:
        return None, "invalid", ["empty value"]

    if typ == "name":
        if not re.fullmatch(r"[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ .,'’\-/]*", s):
            return s, "warning", ["unexpected characters for a person/organisation name"]
        return s, "valid", msgs

    if typ in ("date", "dob", "date_or_year"):
        if typ == "date_or_year" and re.fullmatch(r"(19|20)\d{2}", s):
            y = int(s)
            if y > today().year:
                return s, "invalid", ["year is in the future"]
            return s, "valid", ["year only"]
        if typ == "date_or_year" and re.fullmatch(r"\d{1,2}/(19|20)\d{2}", s):
            m, y = s.split("/")
            return f"{int(y):04d}-{int(m):02d}", "valid", ["month/year only"]
        d, msgs = parse_date(s)
        if not d:
            return s, "invalid", [f"unrecognised date format {s!r}"]
        if d > today():
            return d.isoformat(), "invalid", msgs + ["date is in the future"]
        if typ == "dob":
            age = (today() - d).days / 365.25
            if age > 120:
                return d.isoformat(), "invalid", msgs + [f"implausible age ({age:.0f} years)"]
        return d.isoformat(), "valid", msgs

    if typ == "phone":
        digits = re.sub(r"\D", "", re.sub(r"(?i)\s*(ext|x)\.?\s*\d+$", "", s))
        if len(digits) == 11 and digits[0] == "1":
            digits = digits[1:]
        if len(digits) == 10:
            return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}", "valid", msgs
        if 7 <= len(digits) <= 15:
            return s, "warning", ["not a 10-digit North-American number"]
        return s, "invalid", ["phone number has too few/many digits"]

    if typ == "email":
        if re.fullmatch(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}", s):
            return s.lower(), "valid", msgs
        return s, "invalid", ["not a valid e-mail address"]

    if typ == "state":
        if re.fullmatch(r"[A-Za-z]{2}", s) and s.upper() in US_STATES.values():
            return s.upper(), "valid", msgs
        code = US_STATES.get(_norm(s))
        if code:
            return code, "valid", [f"state name normalised to {code}"]
        return s, "warning", ["not a recognised US state"]

    if typ == "zip":
        if re.fullmatch(r"\d{5}(-\d{4})?", s):
            return s, "valid", msgs
        if re.fullmatch(r"\d{9}", s):
            return f"{s[:5]}-{s[5:]}", "valid", msgs
        return s, "invalid", ["ZIP code must be 5 or 9 digits"]

    if typ == "identifier":
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9\-\s./]{1,30}", s):
            return s, "valid", msgs
        return s, "warning", ["identifier contains unexpected characters"]

    if typ == "integer":
        m = re.fullmatch(r"(\d{1,3})(\s*(yrs?|years?))?", s, re.I)
        if m and 0 <= int(m.group(1)) <= 120:
            return int(m.group(1)), "valid", msgs
        return s, "invalid", ["not a plausible age"]

    if typ == "dosage":
        if DOSAGE_RE.search(s):
            return s, "valid", msgs
        return s, "warning", ["dosage has no recognisable amount + unit"]

    if typ == "enum":
        v = _enum(item.get("enum", ""), s) if item.get("enum") in ENUMS else None
        if v:
            return v, "valid", msgs
        return s, "warning", [f"value not in the {item.get('enum')} vocabulary"]

    if typ == "list":
        return split_list(s), "valid", msgs

    if typ == "yes_no":
        n = _norm(s)
        if n in ("yes", "y"):
            return "yes", "valid", msgs
        if n in ("no", "n"):
            return "no", "valid", msgs
        return None, "invalid", [f"could not resolve yes/no from {s!r}"]

    return s, "valid", msgs
