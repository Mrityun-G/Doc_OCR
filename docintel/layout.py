"""Geometry layer: words -> lines -> segments; section headings -> page regions.

Coordinates are PDF points with the origin at the top-left of the page (PyMuPDF convention).
Regions are rectangles, so two sections printed side by side (e.g. Primary and Secondary
Insurance columns) get separate regions and their values can never be merged.
"""
from __future__ import annotations

import re
import statistics
from collections import defaultdict
from dataclasses import dataclass, field

from .textnorm import marker_state, normalize


@dataclass(eq=False)
class Word:
    x0: float
    y0: float
    x1: float
    y1: float
    text: str
    page: int
    conf: float = 1.0
    origin: str = "text"   # text | ocr | widget

    @property
    def xc(self):
        return (self.x0 + self.x1) / 2

    @property
    def yc(self):
        return (self.y0 + self.y1) / 2

    @property
    def h(self):
        return max(1.0, self.y1 - self.y0)

    @property
    def bbox(self):
        return (self.x0, self.y0, self.x1, self.y1)


@dataclass
class Tok:
    text: str
    wi: int
    first: bool
    last: bool


class Line:
    """Words sharing a baseline, left-to-right, split into gap-separated segments."""

    def __init__(self, words: list[Word]):
        self.words = sorted(words, key=lambda w: w.x0)
        self._merge_markers()
        self.page = self.words[0].page
        self.rx1: float | None = None   # right edge of the region this line was built in
        self._build_segments()
        self._build_tokens()

    # geometry
    @property
    def y0(self):
        return min(w.y0 for w in self.words)

    @property
    def y1(self):
        return max(w.y1 for w in self.words)

    @property
    def yc(self):
        return statistics.median(w.yc for w in self.words)

    @property
    def h(self):
        return statistics.median(w.h for w in self.words)

    @property
    def text(self):
        return " ".join(w.text for w in self.words)

    def bbox(self, i0=0, i1=None):
        ws = self.words[i0:(len(self.words) if i1 is None else i1 + 1)]
        return (min(w.x0 for w in ws), min(w.y0 for w in ws), max(w.x1 for w in ws), max(w.y1 for w in ws))

    def segment_of(self, wi: int) -> int:
        for k, (a, b) in enumerate(self.segments):
            if a <= wi <= b:
                return k
        return len(self.segments) - 1

    # construction
    def _merge_markers(self):
        out: list[Word] = []
        for w in self.words:
            if out and w.text.strip() in ("]", ")") and out[-1].text.strip() in ("[", "(") and w.x0 - out[-1].x1 < 10:
                p = out.pop()
                out.append(Word(p.x0, min(p.y0, w.y0), w.x1, max(p.y1, w.y1), p.text.strip() + " " + w.text.strip(),
                                w.page, min(p.conf, w.conf), p.origin))
            else:
                out.append(w)
        self.words = out

    def _build_segments(self):
        hs = [w.h for w in self.words]
        gap_limit = max(10.0, 1.0 * statistics.median(hs))
        segs, start = [], 0
        for i in range(1, len(self.words)):
            if self.words[i].x0 - self.words[i - 1].x1 > gap_limit:
                segs.append((start, i - 1))
                start = i
        segs.append((start, len(self.words) - 1))
        self.segments = segs

    def _build_tokens(self):
        toks: list[Tok] = []
        for wi, w in enumerate(self.words):
            if marker_state(w.text)[0]:
                toks.append(Tok("\x00marker", wi, True, True))
                continue
            sub = normalize(w.text).split()
            for k, s in enumerate(sub):
                toks.append(Tok(s, wi, k == 0, k == len(sub) - 1))
        self.tokens = toks


def build_lines(words: list[Word]) -> list[Line]:
    if not words:
        return []
    ws = sorted(words, key=lambda w: (w.page, w.yc, w.x0))
    groups: list[list[Word]] = []
    for w in ws:
        if groups:
            g = groups[-1]
            gy = statistics.median(x.yc for x in g)
            gh = statistics.median(x.h for x in g)
            if g[0].page == w.page and abs(w.yc - gy) <= max(2.5, 0.45 * min(w.h, gh)):
                g.append(w)
                continue
        groups.append([w])
    return [Line(g) for g in groups]


# ---------------------------------------------------------------- page furniture
_PAGE_NO = re.compile(r"\bpage\s+\d+(\s+of\s+\d+)?\b", re.I)


def strip_page_furniture(lines_by_page: dict[int, list[Line]], page_sizes: dict) -> dict[int, list[Line]]:
    """Drop page numbers and headers/footers repeated in the top/bottom 8% of pages."""
    def in_margin(ln, p):
        H = page_sizes[p][1]
        return ln.y1 < 0.08 * H or ln.y0 > 0.92 * H

    sig = defaultdict(set)
    for p, lines in lines_by_page.items():
        for ln in lines:
            if in_margin(ln, p):
                sig[re.sub(r"\d+", "#", normalize(ln.text))].add(p)
    repeated = {k for k, ps in sig.items() if len(ps) >= 2}
    out = {}
    for p, lines in lines_by_page.items():
        keep = []
        for ln in lines:
            if _PAGE_NO.search(ln.text) and len(ln.words) <= 10:
                continue
            if in_margin(ln, p) and re.sub(r"\d+", "#", normalize(ln.text)) in repeated:
                continue
            keep.append(ln)
        out[p] = keep
    return out


# ---------------------------------------------------------------- headings & regions
@dataclass(eq=False)
class Heading:
    section_id: str
    page: int
    bbox: tuple
    text: str
    word_ids: set = field(default_factory=set)

    @property
    def yc(self):
        return (self.bbox[1] + self.bbox[3]) / 2


@dataclass(eq=False)
class Region:
    section_id: str
    page: int
    x0: float
    y0: float
    x1: float
    y1: float
    heading: Heading | None

    def contains(self, w: Word) -> bool:
        return w.page == self.page and self.x0 <= w.xc < self.x1 and self.y0 <= w.yc <= self.y1


def detect_headings(lines_by_page: dict[int, list[Line]], template) -> list[Heading]:
    """A line is a heading line only if EVERY gap-separated segment on it is a section title.

    That rule keeps table header rows such as 'Relation | Age | Medical Conditions' from being
    mistaken for the 'Medical Conditions' section heading.
    """
    out = []
    for p, lines in lines_by_page.items():
        for ln in lines:
            found = []
            for a, b in ln.segments:
                txt = " ".join(w.text for w in ln.words[a:b + 1])
                sid = template.match_heading(txt)
                if not sid:
                    found = None
                    break
                found.append(Heading(sid, p, ln.bbox(a, b), txt, {id(w) for w in ln.words[a:b + 1]}))
            if found:
                out.extend(found)
    return out


def _overlap(a, b) -> bool:
    return min(a[1], b[1]) - max(a[0], b[0]) > 1


def build_regions(headings: list[Heading], page_sizes: dict) -> list[Region]:
    by_page = defaultdict(list)
    for h in headings:
        by_page[h.page].append(h)
    regions: list[Region] = []
    open_regions: list[tuple] = []          # (section_id, x0, x1) still open at end of previous page
    for p in sorted(page_sizes):
        W, H = page_sizes[p]
        hs = sorted(by_page.get(p, []), key=lambda h: (h.bbox[1], h.bbox[0]))
        ext = {}
        for h in hs:
            row = [g for g in hs if g is not h and abs(g.yc - h.yc) < 6]
            left = [g for g in row if g.bbox[0] < h.bbox[0]]
            right = [g for g in row if g.bbox[0] > h.bbox[0]]
            x0 = 0.0 if not left else h.bbox[0] - 4
            x1 = W if not right else min(g.bbox[0] for g in right) - 4
            ext[id(h)] = (x0, x1)

        for sid, x0, x1 in open_regions:      # continuation of a section from the previous page
            tops = [h.bbox[1] for h in hs if _overlap(ext[id(h)], (x0, x1))]
            yb = (min(tops) - 1) if tops else H
            if yb > 5:
                regions.append(Region(sid, p, x0, 0.0, x1, yb, None))

        new_open = []
        for h in hs:
            x0, x1 = ext[id(h)]
            below = [g.bbox[1] for g in hs if g is not h and g.bbox[1] > h.bbox[3] - 2 and _overlap(ext[id(g)], (x0, x1))]
            yb = (min(below) - 1) if below else H
            regions.append(Region(h.section_id, p, x0, h.bbox[1] - 1, x1, yb, h))
            if not below:
                new_open.append((h.section_id, x0, x1))
        for sid, x0, x1 in open_regions:
            if not any(_overlap(ext[id(h)], (x0, x1)) for h in hs):
                new_open.append((sid, x0, x1))
        open_regions = new_open
    return regions


def find_region(regions: list[Region], w: Word) -> Region | None:
    for r in regions:
        if r.contains(w):
            return r
    return None
