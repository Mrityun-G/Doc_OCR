"""Render the STAND-IN reference template (fillable PDF) from template.json.

The real project should use the client's reference PDF instead; this generator exists so the
pipeline can be exercised end-to-end and so template.json and the PDF never drift apart.

    python tools/build_reference_template.py [templates/patient_intake_v1]
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import pymupdf

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from docintel.template import Template  # noqa: E402

W, H = 612, 792
L, R = 40, 572
CW = R - L
FS = 9
FIELD_H = 14
ROW_H = 21
TROW_H = 18
BOTTOM = 742
INK = (0.1, 0.1, 0.12)
MUTED = (0.35, 0.37, 0.42)
ACCENT = (0.12, 0.25, 0.45)


def tw(s, size=FS, font="helv"):
    return pymupdf.get_text_length(s, fontname=font, fontsize=size)


def wrap(text, width, size):
    lines, cur = [], ""
    for word in text.split():
        cand = f"{cur} {word}".strip()
        if tw(cand, size) <= width:
            cur = cand
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


def pack(items):
    rows, cur, acc = [], [], 0.0
    for it in items:
        if it.get("render") is False:
            continue
        if it.get("type") == "yes_no":
            if cur:
                rows.append(cur)
            rows.append([it])
            cur, acc = [], 0.0
            continue
        w = it.get("w", 0.5)
        if acc + w > 1.001 and cur:
            rows.append(cur)
            cur, acc = [], 0.0
        cur.append(it)
        acc += w
    if cur:
        rows.append(cur)
    return rows


class Renderer:
    def __init__(self, t: Template):
        self.t = t
        self.doc = pymupdf.open()
        self.page = None
        self.y = 0.0

    # primitives
    def new_page(self):
        self.page = self.doc.new_page(width=W, height=H)
        if self.doc.page_count == 1:
            self.text(L, 50, self.t.data.get("title", "Form").upper(), 16, "hebo", ACCENT)
            self.text(L, 64, "Sample Family Clinic  -  standard reference template (stand-in)", 8.5, "helv", MUTED)
            self.y = 80
        else:
            self.y = 44

    def text(self, x, y, s, size=FS, font="helv", color=INK):
        self.page.insert_text((x, y), s, fontsize=size, fontname=font, color=color)

    def widget(self, name, rect, kind="text"):
        w = pymupdf.Widget()
        w.field_name = name
        w.rect = pymupdf.Rect(rect)
        if kind == "text":
            w.field_type = pymupdf.PDF_WIDGET_TYPE_TEXT
            w.text_fontsize = 9
            w.text_font = "Helv"
        else:
            w.field_type = pymupdf.PDF_WIDGET_TYPE_CHECKBOX
            w.field_value = False
        w.border_color = (0.45, 0.5, 0.6)
        w.border_width = 0.6
        w.fill_color = (0.96, 0.97, 1.0)
        self.page.add_widget(w)

    # measurement
    def height(self, s):
        h = 24
        if s.get("text"):
            h += len(wrap(s["text"], CW, 8.5)) * 11 + 6
        if "fields" in s:
            h += len(pack(s["fields"]["items"])) * ROW_H + 2
        for pn in ("table", "row_table"):
            if pn in s:
                p = s[pn]
                n = int(p["rows"]) if pn == "table" else len(p["rows"])
                h += (13 if p.get("instructions") else 0) + 14 + n * TROW_H + 6
        if "checklist" in s:
            c = s["checklist"]
            h += (13 if c.get("instructions") else 0) + math.ceil(len(c["options"]) / 4) * TROW_H + ROW_H + 4
        return h + 8

    # sections
    def section(self, s):
        if self.page is None or self.y + self.height(s) > BOTTOM:
            self.new_page()
        y = self.y
        self.text(L, y + 12, s["title"], 11.5, "hebo", ACCENT)
        self.page.draw_line((L, y + 16), (R, y + 16), color=ACCENT, width=0.8)
        self.y = y + 26
        if s.get("text"):
            for ln in wrap(s["text"], CW, 8.5):
                self.text(L, self.y + 8, ln, 8.5, "helv", MUTED)
                self.y += 11
            self.y += 6
        if "fields" in s:
            self.fields(s["fields"])
        if "table" in s:
            self.table(s["table"])
        if "row_table" in s:
            self.row_table(s["row_table"])
        if "checklist" in s:
            self.checklist(s["checklist"])
        self.y += 8

    def fields(self, part):
        for row in pack(part["items"]):
            x = L
            for it in row:
                if it.get("type") == "yes_no":
                    lbl = it["label"] if it["label"].endswith("?") else it["label"] + ":"
                    self.text(x, self.y + 10, lbl)
                    bx = x + tw(lbl) + 14
                    for choice, txt in (("yes", "Yes"), ("no", "No")):
                        self.widget(it["acroform"][choice], (bx, self.y + 1, bx + 10, self.y + 11), "check")
                        self.text(bx + 14, self.y + 10, txt)
                        bx += 48
                    continue
                cw = it.get("w", 0.5) * CW
                lbl = it["label"] + ":"
                self.text(x, self.y + 10, lbl)
                lw = tw(lbl)
                self.widget(it["acroform"], (x + lw + 3, self.y, x + cw - 8, self.y + FIELD_H))
                x += cw
            self.y += ROW_H
        self.y += 2

    def _header(self, cols):
        xs, x = [], L
        for c in cols:
            xs.append(x)
            self.text(x + 2, self.y + 10, c["label"], 8.5, "hebo", INK)
            x += c["w"] * CW
        self.page.draw_line((L, self.y + 13), (R, self.y + 13), color=(0.7, 0.72, 0.78), width=0.5)
        self.y += 15
        return xs

    def table(self, p):
        if p.get("instructions"):
            self.text(L, self.y + 8, p["instructions"], 8, "helv", MUTED)
            self.y += 13
        xs = self._header(p["columns"])
        for r in range(1, int(p["rows"]) + 1):
            for c, x in zip(p["columns"], xs):
                self.widget(p["acroform_pattern"].format(row=r, key=c["key"]),
                            (x, self.y, x + c["w"] * CW - 6, self.y + FIELD_H))
            self.y += TROW_H
        self.y += 6

    def row_table(self, p):
        cols = [dict(p["row_header"], key="__row__")] + p["columns"]
        xs = self._header(cols)
        for r in p["rows"]:
            self.text(L + 2, self.y + 10, r["label"])
            for c, x in zip(p["columns"], xs[1:]):
                self.widget(p["acroform_pattern"].format(row=r["row_key"], key=c["key"]),
                            (x, self.y, x + c["w"] * CW - 6, self.y + FIELD_H))
            self.y += TROW_H
        self.y += 6

    def checklist(self, c):
        if c.get("instructions"):
            self.text(L, self.y + 8, c["instructions"], 8, "helv", MUTED)
            self.y += 13
        for i, o in enumerate(c["options"]):
            col = i % 4
            if i and col == 0:
                self.y += TROW_H
            x = L + col * CW / 4
            self.widget(c["acroform_pattern"].format(key=o["key"]), (x, self.y + 1, x + 10, self.y + 11), "check")
            self.text(x + 14, self.y + 10, o["label"])
        self.y += TROW_H
        if c.get("other"):
            lbl = c["other"]["label"] + ":"
            self.text(L, self.y + 10, lbl)
            self.widget(c["other"]["acroform"], (L + tw(lbl) + 3, self.y, R - 8, self.y + FIELD_H))
            self.y += ROW_H
        self.y += 4

    def finish(self, out: Path):
        n = self.doc.page_count
        for i, page in enumerate(self.doc, 1):
            page.insert_text((L, 772), f"Form {self.t.template_id}  -  Page {i} of {n}", fontsize=7.5,
                             fontname="helv", color=MUTED)
        self.doc.set_metadata({"title": f"{self.t.data.get('title')} - reference template (stand-in)",
                               "subject": self.t.template_id, "creator": "docintel tools/build_reference_template.py"})
        self.doc.save(out, garbage=3, deflate=True)


def build(template_dir: Path) -> Path:
    t = Template(template_dir)
    r = Renderer(t)
    for s in t.sections:
        r.section(s)
    r.finish(t.reference_pdf)
    return t.reference_pdf


if __name__ == "__main__":
    d = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "templates" / "patient_intake_v1"
    print(f"written: {build(d)}")
