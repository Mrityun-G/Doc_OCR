"""Direct extraction from PDFs filled in on the reference template's own form fields.

Record binding comes from the template's field naming (e.g. med_3_dosage -> medication row 3),
so relationships are exact; confidence is high and the label recorded is the template label.
"""
from __future__ import annotations

from collections import defaultdict

from .models import CONF, ExtractionResult, Observation, SectionEvidence, union_bbox
from .template import Template

MIN_MATCHED = 5


def is_template_form(widgets, template: Template) -> bool:
    names = template.all_acroform_names()
    matched = sum(1 for w in widgets if w.name in names)
    return matched >= MIN_MATCHED and matched >= 0.5 * max(1, len(widgets))


def _label(template: Template, b) -> str:
    s = template.section_by_id[b.section_id]
    if b.part == "checklist" and b.kind == "option":
        return next(o["label"] for o in s["checklist"]["options"] if o["key"] == b.record_key)
    it = template.item(b.section_id, b.part, b.key)
    if it:
        return it["label"]
    if b.part == "row_table":
        return b.key
    return b.key


def extract_acroform(src, widgets, template: Template, res: ExtractionResult):
    idx = template.acroform_index
    order = 0
    yes_no: dict[tuple, dict] = defaultdict(dict)
    sec_pages: dict[str, set] = defaultdict(set)
    sec_boxes: dict[str, list] = defaultdict(list)
    row_labels_done: set = set()

    def add(**kw):
        nonlocal order
        order += 1
        res.observations.append(Observation(order=order, method="acroform", confidence=CONF["acroform"], **kw))

    for w in widgets:
        b = idx.get(w.name)
        if not b:
            continue
        sec_pages[b.section_id].add(w.page)
        sec_boxes[b.section_id].append((w.page, w.rect))
        common = dict(section_id=b.section_id, part=b.part, page=w.page, bbox=tuple(w.rect),
                      entity_ref=b.entity_ref, extra={"acroform_field": w.name})
        if b.kind == "yes_no":
            yes_no[(b.section_id, b.part, b.key, b.record_key, b.entity_ref)][b.choice] = w
            continue
        if b.kind == "option":
            if w.checked:
                lbl = _label(template, b)
                add(key="option", record_key=b.record_key, raw=lbl, label_text=lbl,
                    **dict(common, extra={"acroform_field": w.name, "option_key": b.record_key}))
            continue
        val = w.value
        if w.kind in ("CheckBox", "RadioButton"):
            val = w.checked
        elif w.kind == "Signature":
            val = True if w.value else False
        else:
            val = str(val or "").strip()
            if not val:
                continue
        it = template.item(b.section_id, b.part, b.key)
        if it and it.get("type") == "signature":
            common["extra"] = {"acroform_field": w.name, "signature_evidence":
                               "signature_field" if w.kind == "Signature" else "typed_text"}
        add(key=b.key, record_key=b.record_key, raw=val, label_text=_label(template, b), **common)
        # a filled family-history row implies its printed relation label (Mother, Father ...)
        if b.part == "row_table" and (b.section_id, b.record_key) not in row_labels_done:
            row_labels_done.add((b.section_id, b.record_key))
            part = template.section_by_id[b.section_id]["row_table"]
            rdef = next(r for r in part["rows"] if r["row_key"] == b.record_key)
            add(key=part["row_attribute"], record_key=b.record_key, raw=rdef["label"],
                label_text=part["row_header"]["label"],
                **dict(common, extra={"acroform_field": w.name, "row_key_known": True,
                                      "derived_from": "template row label"}))

    for (sid, part, key, rk, eref), d in yes_no.items():
        yes, no = d.get("yes"), d.get("no")
        ys, ns = bool(yes and yes.checked), bool(no and no.checked)
        if not ys and not ns:
            continue
        ref = yes or no
        bbox = union_bbox([x.rect for x in (yes, no) if x])
        raw = "yes+no" if ys and ns else ("yes" if ys else "no")
        it = template.item(sid, part, key)
        o = Observation(section_id=sid, part=part, key=key, record_key=rk, raw=raw, label_text=it["label"],
                        page=ref.page, bbox=bbox, method="acroform", confidence=CONF["acroform"],
                        entity_ref=eref, order=order + 1,
                        extra={"decision": None if ys and ns else raw,
                               "acroform_field": [x.name for x in (yes, no) if x]})
        order += 1
        if ys and ns:
            o.flags.add("ambiguous_choice")
            o.notes.append("both 'Yes' and 'No' boxes are ticked")
        res.observations.append(o)

    for sid, pages in sec_pages.items():
        ev = res.sections.get(sid)
        if not ev:
            ev = res.sections[sid] = SectionEvidence(sid, "acroform_field_names", CONF["acroform"])
        ev.pages |= pages
        if not ev.heading_bboxes:
            p = min(pages)
            ev.heading_bboxes.append((p, union_bbox([r for pg, r in sec_boxes[sid] if pg == p])))
