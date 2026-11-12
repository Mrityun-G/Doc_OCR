"""Read-only access to one source PDF: words with boxes, form widgets, images and ink."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import pymupdf

from .layout import Word


@dataclass
class WidgetInfo:
    name: str
    kind: str          # Text | CheckBox | RadioButton | Signature | ComboBox | ListBox ...
    value: object
    checked: bool
    page: int
    rect: tuple[float, float, float, float]


class SourcePDF:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.filename = self.path.name
        data = self.path.read_bytes()
        self.sha256 = hashlib.sha256(data).hexdigest()
        self.doc = pymupdf.open(stream=data, filetype="pdf")
        if self.doc.needs_pass:
            raise ValueError("PDF is password protected")
        self.page_count = len(self.doc)
        self.page_sizes = {i + 1: (p.rect.width, p.rect.height) for i, p in enumerate(self.doc)}
        self._ink: dict[int, list] = {}
        self._img: dict[int, list] = {}

    def close(self):
        self.doc.close()

    # -------------------------------------------------------------- text
    def text_words(self, pno: int) -> list[Word]:
        page = self.doc[pno - 1]
        out = []
        for x0, y0, x1, y1, txt, *_ in page.get_text("words"):
            if txt.strip():
                out.append(Word(x0, y0, x1, y1, txt, pno))
        return out

    def has_text(self, pno: int) -> bool:
        return bool(self.doc[pno - 1].get_text("text").strip())

    # -------------------------------------------------------------- widgets
    def widgets(self) -> list[WidgetInfo]:
        out = []
        for i, page in enumerate(self.doc, 1):
            for w in page.widgets() or []:
                kind = w.field_type_string
                val = w.field_value
                checked = False
                if kind in ("CheckBox", "RadioButton"):
                    checked = val is True or val not in (None, False, "", "Off", "off", "0")
                out.append(WidgetInfo(w.field_name or "", kind, val, checked, i, tuple(w.rect)))
        return out

    # -------------------------------------------------------------- graphics
    def images(self, pno: int) -> list[tuple]:
        if pno not in self._img:
            self._img[pno] = [tuple(i["bbox"]) for i in self.doc[pno - 1].get_image_info()]
        return self._img[pno]

    def ink(self, pno: int) -> list[tuple]:
        """Bounding boxes of free-hand-looking vector paths (signature strokes).

        Straight rules and rectangles (form furniture) are ignored: only paths that contain
        at least two Bezier curve segments count as ink.
        """
        if pno not in self._ink:
            boxes = []
            for d in self.doc[pno - 1].get_drawings():
                curves = sum(1 for it in d.get("items", []) if it[0] == "c")
                if curves >= 2:
                    boxes.append(tuple(d["rect"]))
            self._ink[pno] = boxes
        return self._ink[pno]

    def page_is_image_only(self, pno: int) -> bool:
        return not self.has_text(pno) and bool(self.images(pno))

    def checkbox_words(self, pno: int, words: list[Word], dark_ratio: float = 0.05):
        """Find vector-drawn checkboxes and decide ticked/empty by rendering their interior.

        Flattened forms draw ticks as graphics or symbol-font glyphs that do not survive text
        extraction, so the box itself is inspected. Returns (pseudo_words, ids_of_words_inside_boxes):
        each box becomes a '[X]' or '[ ]' word that the layout engine treats like a printed marker,
        and stray glyph characters found inside a box are dropped.
        """
        page = self.doc[pno - 1]
        boxes: list = []
        for d in page.get_drawings():
            for it in d.get("items", []):
                if it[0] == "re":
                    r = pymupdf.Rect(it[1])
                elif it[0] == "qu":
                    r = pymupdf.Quad(it[1]).rect
                else:
                    continue
                if 6 <= r.width <= 18 and 6 <= r.height <= 18 and abs(r.width - r.height) <= 2.5:
                    if not any(abs(r.x0 - b.x0) < 2.5 and abs(r.y0 - b.y0) < 2.5 for b in boxes):
                        boxes.append(r)
        out, inside = [], set()
        for b in boxes:
            clip = pymupdf.Rect(b.x0 + 1.5, b.y0 + 1.5, b.x1 - 1.5, b.y1 - 1.5)
            pix = page.get_pixmap(clip=clip, matrix=pymupdf.Matrix(4, 4), colorspace=pymupdf.csGRAY, annots=False)
            px = pix.samples
            ratio = sum(1 for v in px if v < 140) / max(1, len(px))
            for w in words:
                if b.x0 - 1 <= w.xc <= b.x1 + 1 and b.y0 - 1 <= w.yc <= b.y1 + 1:
                    inside.add(id(w))
            out.append(Word(b.x0, b.y0, b.x1, b.y1, "[X]" if ratio > dark_ratio else "[ ]", pno,
                            origin="vector_checkbox"))
        return out, inside
