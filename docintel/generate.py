"""Populate the reference template PDF from a document's structured JSON.

Values are placed by *structure*, not by text: medication row i of the JSON fills med_i_*,
the 'mother' family-history record fills fh_mother_*, the primary policy fills pri_ins_*.
Attributes whose validation status is needs_review/invalid are withheld unless
include_uncertain=True, so an unconfirmed dosage is never printed on the wrong row.
"""
from __future__ import annotations

import re
from pathlib import Path

import pymupdf

from .builder import get_attr
from .template import Template

OK = ("valid", "warning")


def _usable(tr, include_uncertain: bool) -> bool:
    return bool(tr) and tr["value"] not in (None, "", []) and (include_uncertain or tr["validation"]["status"] in OK)


def _fmt(tr: dict, typ: str) -> str:
    v = tr["value"]
    if isinstance(v, list):
        return ", ".join(str(x) for x in v)
    s = str(v)
    if typ in ("date", "dob", "date_or_year") and re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        y, m, d = s.split("-")
        return f"{m}/{d}/{y}"
    if typ == "date_or_year" and re.fullmatch(r"\d{4}-\d{2}", s):
        y, m = s.split("-")
        return f"{m}/{y}"
    if typ == "enum":
        return s.replace("_", " ").title()
    return s


def _list(ents, coll):
    v = ents.get(coll)
    return [v] if isinstance(v, dict) else (v or [])


def build_fill_map(doc: dict, template: Template, include_uncertain: bool = False):
    ents = doc["entities"]
    values: dict[str, object] = {}
    sources: dict[str, str] = {}
    skipped, not_placed = [], []

    def put(name, tr, value, ref):
        values[name] = value
        sources[name] = ref

    def skip(name, tr, ref):
        skipped.append({"field": name, "ref": ref, "value": tr["value"], "status": tr["validation"]["status"]})

    for s in template.sections:
        sid = s["section_id"]
        if "fields" in s:
            part = s["fields"]
            cfg = part["entity"]
            pool = [e for e in _list(ents, cfg["collection"]) if e["section_id"] == sid]
            if cfg.get("per_field"):
                for it in part["items"]:
                    e = next((x for x in pool if x["record"]["record_key"] == it["key"]), None)
                    tr = get_attr(e, cfg["value_attribute"]) if e else None
                    if not tr:
                        continue
                    ref = f"{e['consent_id']}.{cfg['value_attribute']}"
                    if _usable(tr, include_uncertain) and tr["value"] in ("yes", "no") and it["acroform"].get(tr["value"]):
                        put(it["acroform"][tr["value"]], tr, True, ref)
                    else:
                        skip(it["acroform"].get("yes"), tr, ref)
            else:
                main = ents.get("patient") if cfg.get("subject") else next(
                    (x for x in pool if x["record"]["record_key"] == "main"), None)
                subs = {}
                for ref_name, scfg in cfg.get("sub_entities", {}).items():
                    subs[ref_name] = next((x for x in _list(ents, scfg["collection"]) if x["section_id"] == sid), None)
                for it in part["items"]:
                    af = it.get("acroform")
                    if not af:
                        continue
                    tgt = subs.get(it["entity_ref"]) if it.get("entity_ref") else main
                    if not tgt:
                        continue
                    tid = next(v for k, v in tgt.items() if k.endswith("_id"))
                    if it.get("type") == "signature":
                        tr = get_attr(tgt, "signature_present")
                        if tr and tr["value"] and _usable(tr, include_uncertain):
                            put(af, tr, f"(signed - source p.{tr['source']['page']})", f"{tid}.signature_present")
                        elif tr and tr["value"]:
                            skip(af, tr, f"{tid}.signature_present")
                        continue
                    tr = get_attr(tgt, it["key"])
                    if _usable(tr, include_uncertain):
                        put(af, tr, _fmt(tr, it.get("type", "text")), f"{tid}.{it['key']}")
                    elif tr:
                        skip(af, tr, f"{tid}.{it['key']}")
        if "table" in s:
            part = s["table"]
            rows = sorted((e for e in _list(ents, part["entity"]["collection"])
                           if e["section_id"] == sid and e["record"]["part"] == "table"),
                          key=lambda e: e["record"]["index"])
            for i, e in enumerate(rows, 1):
                eid = next(v for k, v in e.items() if k.endswith("_id"))
                if i > int(part["rows"]):
                    not_placed.append({"ref": eid, "reason": f"template has only {part['rows']} rows in '{s['title']}'"})
                    continue
                for c in part["columns"]:
                    tr = get_attr(e, c["key"])
                    name = part["acroform_pattern"].format(row=i, key=c["key"])
                    if _usable(tr, include_uncertain):
                        put(name, tr, _fmt(tr, c.get("type", "text")), f"{eid}.{c['key']}")
                    elif tr:
                        skip(name, tr, f"{eid}.{c['key']}")
        if "row_table" in s:
            part = s["row_table"]
            keys = {r["row_key"] for r in part["rows"]}
            for e in _list(ents, part["entity"]["collection"]):
                if e["section_id"] != sid:
                    continue
                eid = e["family_member_id"]
                rel = get_attr(e, part["row_attribute"])
                if not rel or rel["value"] not in keys:
                    not_placed.append({"ref": eid, "reason": f"relation {rel and rel['raw_value']!r} has no row on the template"})
                    continue
                for c in part["columns"]:
                    tr = get_attr(e, c["key"])
                    name = part["acroform_pattern"].format(row=rel["value"], key=c["key"])
                    if _usable(tr, include_uncertain):
                        put(name, tr, _fmt(tr, c.get("type", "text")), f"{eid}.{c['key']}")
                    elif tr:
                        skip(name, tr, f"{eid}.{c['key']}")
        if "checklist" in s:
            part = s["checklist"]
            others = []
            for e in _list(ents, part["entity"]["collection"]):
                if e["section_id"] != sid or e["record"]["part"] != "checklist":
                    continue
                tr, eid = e["name"], e["medical_condition_id"]
                if not _usable(tr, include_uncertain):
                    skip(part["acroform_pattern"].format(key=e.get("checklist_option") or "other"), tr, f"{eid}.name")
                    continue
                if e.get("checklist_option"):
                    put(part["acroform_pattern"].format(key=e["checklist_option"]), tr, True, f"{eid}.name")
                else:
                    others.append(str(tr["value"]))
            if others and part.get("other", {}).get("acroform"):
                values[part["other"]["acroform"]] = ", ".join(others)
                sources[part["other"]["acroform"]] = "medical_conditions (other)"
    return values, sources, skipped, not_placed


def generate_standardized_pdf(doc: dict, template: Template, out_path: str | Path, include_uncertain: bool = False) -> dict:
    if not template.reference_pdf.exists():
        raise FileNotFoundError(f"reference template PDF not found: {template.reference_pdf}")
    values, sources, skipped, not_placed = build_fill_map(doc, template, include_uncertain)
    pdf = pymupdf.open(template.reference_pdf)
    filled, pending = 0, set(values)
    for page in pdf:
        for w in page.widgets() or []:
            name = w.field_name
            if name not in values:
                continue
            v = values[name]
            pending.discard(name)
            if w.field_type in (pymupdf.PDF_WIDGET_TYPE_CHECKBOX, pymupdf.PDF_WIDGET_TYPE_RADIOBUTTON):
                w.field_value = w.on_state() if v else "Off"
            else:
                w.field_value = str(v)
            w.update()
            filled += 1
    pdf.set_metadata({"title": f"{template.data.get('title', 'Standardized form')} - {doc['document_id']}",
                      "subject": f"Standardized from {doc['source']['filename']} (sha256 {doc['source']['sha256'][:12]})",
                      "creator": doc["processing"]["engine"], "producer": "PyMuPDF",
                      "keywords": f"{doc['document_id']};{template.template_id}"})
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    pdf.save(out_path, garbage=3, deflate=True)
    pdf.close()
    return {"filled_fields": filled, "field_sources": sources, "skipped_for_review": skipped,
            "not_placed": not_placed, "fields_missing_in_template_pdf": sorted(pending)}
