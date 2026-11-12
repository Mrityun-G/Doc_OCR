"""Text normalisation and checkbox-marker helpers shared by every extractor."""
from __future__ import annotations

import re
import unicodedata

# Glyphs / tokens that mean "this box is ticked" or "this box is empty".
CHECK_GLYPHS = set("☒☑✔✓✗✘⊠■▣")
CHECKED_TOKENS = {"[x]", "(x)", "[✓]", "[✔]", "[v]", "[/]"}
UNCHECKED_TOKENS = {"[ ]", "( )", "[]", "()", "☐", "□", "❏", "▢"}
WEAK_CHECKED = {"x"}  # a bare "X" right before an option (typical of typed/OCR'd forms)

_FILLER = re.compile(r"^[_\-–—.·:]+$")


def normalize(text: str) -> str:
    """Lower-case, ASCII-fold and reduce to space-separated alphanumeric tokens.

    '#' becomes 'number' so 'Group #' == 'Group Number'; '&' becomes 'and'.
    """
    if not text:
        return ""
    s = unicodedata.normalize("NFKC", str(text)).lower()
    s = s.replace("&", " and ").replace("#", " number ")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return s.strip()


def tokens(text: str) -> tuple[str, ...]:
    return tuple(normalize(text).split())


def is_filler(text: str) -> bool:
    """Underscore lines, dashes, lone colons: layout furniture rather than values."""
    t = text.strip()
    return not t or bool(_FILLER.match(t))


def marker_state(text: str) -> tuple[str | None, str]:
    """Classify a word as a checkbox marker.

    Returns (state, remainder) where state is 'checked', 'unchecked', 'weak_checked' or None,
    and remainder is any text glued to the marker (e.g. '☒Yes' -> ('checked', 'Yes')).
    """
    t = text.strip()
    if not t:
        return None, ""
    low = t.lower()
    if low in CHECKED_TOKENS:
        return "checked", ""
    if low in UNCHECKED_TOKENS:
        return "unchecked", ""
    if low in WEAK_CHECKED:
        return "weak_checked", ""
    if t[0] in CHECK_GLYPHS:
        return "checked", t[1:].strip()
    if t[0] in "☐□❏▢":
        return "unchecked", t[1:].strip()
    m = re.match(r"^\[(x|X|✓|✔)\](.*)$", t)
    if m:
        return "checked", m.group(2).strip()
    m = re.match(r"^\[\s?\](.*)$", t)
    if m:
        return "unchecked", m.group(1).strip()
    return None, ""


def split_list(text: str) -> list[str]:
    """Split 'Diabetes, hypertension; asthma' into items. Drops none/n-a.

    Deliberately does NOT split on 'and' or '/', which occur inside condition names.
    """
    if not text:
        return []
    parts = re.split(r"\s*(?:,|;|\n|\s\+\s)\s*", text)
    out = []
    for p in parts:
        p = p.strip(" .-–—:()")
        if not p or normalize(p) in {"none", "n a", "na", "unknown", "no", "nil", "not applicable"}:
            continue
        out.append(p)
    return out


def name_key(text: str) -> str:
    """Key used to de-duplicate people/organisations *within one document*."""
    t = normalize(text)
    t = re.sub(r"^(dr|doctor|prof)\s+", "", t)
    t = re.sub(r"\s+(md|do|np|pa|phd|rn|dds)$", "", t)
    return t


def names_match(a: str, b: str) -> bool:
    """Loose person-name equality: 'Rivera, Maria L.' == 'Maria Rivera'."""
    ta, tb = set(name_key(a).split()), set(name_key(b).split())
    ta = {x for x in ta if len(x) > 1}
    tb = {x for x in tb if len(x) > 1}
    if not ta or not tb:
        return False
    small, big = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    return small <= big
