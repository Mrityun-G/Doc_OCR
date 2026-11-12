"""Inventory a reference PDF to help map it into template.json.

For a fillable PDF: every widget with its page, rectangle, the printed label beside/above
it, the section region it sits in and suggested template attributes. For a flat PDF: every
'Label:' candidate and heading. Also lists template fields that the PDF does not contain.
"""
from __future__ import annotations

from .layout import Word, build_lines, build_regions, detect_headings, find_region, strip_page_furniture
from .pdfsource import SourcePDF
from .template import Template
from .textnorm import normalize


def _label_near(lines, rect):
    x0, y0, x1, y1 = rect
    yc = (y0 + y1) / 2
    best = None
    for ln in lines:
        if abs(ln.yc - yc) <= max(4, (y1 - y0) / 2):
            left = [w for w in ln.words if w.x1 <= x0 + 2 and x0 - w.x1 < 220]
            if left:
                # walk back from the widget to the previous large gap
                left.sort(key=lambda w: w.x0)
                grp = [left[-1]]
                for w in reversed(left[:-1]):
                    if grp[0].x0 - w.x1 > 12:
                        break
                    grp.insert(0, w)
                best = " ".join(w.text for w in grp)
    if best:
        return best.rstrip(":").strip()
    above = [ln for ln in lines if 0 < y0 - ln.y1 < 14]
    for ln in above:
        ws = [w for w in ln.words if w.x1 > x0 - 4 and w.x0 < x1]
        if ws:
            return " ".join(w.text for w in ws).rstrip(":").strip()
    # table cell further down a column: nearest header text left-aligned with the cell
    return _column_header(lines, rect)


def _same_line(lines, rect):
    yc = (rect[1] + rect[3]) / 2
    return next((ln for ln in lines if abs(ln.yc - yc) <= max(4, (rect[3] - rect[1]) / 2)), None)


def _right_text(lines, rect):
    ln = _same_line(lines, rect)
    if not ln:
        return None
    ws = [w for w in ln.words if w.x0 >= rect[2] - 1]
    if not ws or ws[0].x0 - rect[2] > 30:
        return None
    grp = [ws[0]]
    for w in ws[1:]:
        if w.x0 - grp[-1].x1 > 10:
            break
        grp.append(w)
    return " ".join(w.text for w in grp)


def _describe(lines, w, section_id, template: Template):
    """(printed label, suggested template slots) for one widget."""
    if w.kind in ("CheckBox", "RadioButton"):
        opt = _right_text(lines, w.rect)
        if opt and normalize(opt) in ("yes", "no"):
            ln = _same_line(lines, w.rect)
            a, b = ln.segments[0]
            q = " ".join(x.text for x in ln.words[a:b + 1]).rstrip(":").strip()
            return f"{q} -> {opt}", [f"{s} [{normalize(opt)}]" for s in _suggest(q, section_id, template)]
        return opt, _suggest(opt, section_id, template)
    label = _label_near(lines, w.rect)
    sec = template.section_by_id.get(section_id) if section_id else None
    if sec and "row_table" in sec and label:
        rt = sec["row_table"]
        row = next((r for r in rt["rows"] if tuple(normalize(label).split()) in Template.aliases(r)), None)
        if row:
            col = _column_header(lines, w.rect)
            slots = [f"{section_id}.row_table.{c['key']} [row={row['row_key']}]" for c in rt["columns"]
                     if col and tuple(normalize(col).split()) in Template.aliases(c)]
            return f"{label} / {col}", slots
    return label, _suggest(label, section_id, template)


def _column_header(lines, rect):
    x0, y0 = rect[0], rect[1]
    for ln in sorted((ln for ln in lines if 0 < y0 - ln.y1 < 140), key=lambda ln: -ln.y1):
        ws = [w for w in ln.words if abs(w.x0 - x0) <= 8]
        if ws:
            start = ln.words.index(ws[0])
            seg = ln.segments[ln.segment_of(start)]
            return " ".join(w.text for w in ln.words[start:seg[1] + 1]).rstrip(":").strip()
    return None


def _suggest(label, section_id, template: Template):
    if not label:
        return []
    n = tuple(normalize(label).split())
    out = []
    for s in template.sections:
        if section_id and s["section_id"] != section_id:
            continue
        for pn in ("fields", "table", "row_table", "checklist"):
            if pn not in s:
                continue
            items = template.items(s["section_id"], pn)
            if pn == "checklist":
                items = items + s["checklist"]["options"]
            for it in items:
                if n in Template.aliases(it):
                    out.append(f"{s['section_id']}.{pn}.{it['key']}")
    return out


def scan_template_pdf(path, template: Template) -> dict:
    src = SourcePDF(path)
    try:
        lines = {p: build_lines(src.text_words(p)) for p in range(1, src.page_count + 1)}
        lines = strip_page_furniture(lines, src.page_sizes)
        heads = detect_headings(lines, template)
        regions = build_regions(heads, src.page_sizes)
        widgets = []
        for w in src.widgets():
            c = Word(*w.rect, "", w.page)
            reg = find_region(regions, c)
            label, sugg = _describe(lines[w.page], w, reg.section_id if reg else None, template)
            widgets.append({"name": w.name, "type": w.kind, "page": w.page, "rect": [round(v, 1) for v in w.rect],
                            "nearby_label": label, "section_region": reg.section_id if reg else None,
                            "known_to_template": w.name in template.acroform_index,
                            "suggested_mapping": sugg})
        labels = []
        for p, lns in lines.items():
            for ln in lns:
                for a, b in ln.segments:
                    ws = ln.words[a:b + 1]
                    for i, w in enumerate(ws):
                        if w.text.endswith(":"):
                            txt = " ".join(x.text for x in ws[:i + 1]).rstrip(":")
                            reg = find_region(regions, w)
                            labels.append({"label": txt, "page": p, "bbox": [round(v, 1) for v in ln.bbox(a, a + i)],
                                           "section_region": reg.section_id if reg else None,
                                           "suggested_mapping": _suggest(txt, reg.section_id if reg else None, template)})
                            break
        names = {w["name"] for w in widgets}
        return {"file": src.filename, "pages": src.page_count, "fillable": bool(widgets),
                "recognised_headings": [{"section_id": h.section_id, "text": h.text, "page": h.page,
                                         "bbox": [round(v, 1) for v in h.bbox]} for h in heads],
                "template_sections_not_found": [s["section_id"] for s in template.sections
                                                if s["section_id"] not in {h.section_id for h in heads}],
                "widgets": widgets,
                "widgets_unknown_to_template": [w["name"] for w in widgets if not w["known_to_template"]],
                "template_fields_absent_from_pdf": sorted(set(template.acroform_index) - names) if widgets else None,
                "label_candidates": labels}
    finally:
        src.close()
