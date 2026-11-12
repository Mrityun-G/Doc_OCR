"""Builds the entity graph for ONE document from its record-bound observations.

Guarantees
* An attribute joins an entity only if its observation has the same (section, part, record).
* Every attribute is a *trace*: value + original label + page + bbox + method + confidence
  + validation status.
* Relationships are emitted only when the document structure supports them; when support
  is weak they are kept but marked support='uncertain' and queued for manual review.
* A builder instance holds the state of exactly one source file; nothing is shared between
  documents, so entities from different PDFs can never be linked.
"""
from __future__ import annotations

import copy
from collections import OrderedDict, defaultdict

from . import validators
from .models import CONF, ExtractionResult, Observation, round_bbox
from .template import Template
from .textnorm import name_key, names_match, normalize

OK_STATUSES = ("valid", "warning")


def is_trace(d) -> bool:
    return isinstance(d, dict) and "extraction_method" in d


def get_attr(ent: dict, key: str):
    cur = ent
    for p in key.split("."):
        if not isinstance(cur, dict) or p not in cur:
            return None
        cur = cur[p]
    return cur if is_trace(cur) else None


def iter_traces(obj, path=""):
    if is_trace(obj):
        yield path, obj
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (dict, list)):
                yield from iter_traces(v, f"{path}.{k}" if path else k)


class DocumentBuilder:
    def __init__(self, template: Template, doc_id: str, filename: str, res: ExtractionResult):
        self.t, self.doc_id, self.filename, self.res = template, doc_id, filename, res
        self.thr = template.review_threshold
        self.counters: dict[str, int] = defaultdict(int)
        self.collections: dict[str, object] = OrderedDict()
        self.by_id: dict[str, dict] = {}
        self.meta: dict[str, dict] = {}
        self.rels: list[dict] = []
        self.warnings: list[dict] = list(res.warnings)
        self.missing: list[dict] = []
        self.review_extra: list[dict] = []
        self.link_index: dict[tuple, str] = {}
        self.part_entities: dict[tuple, str] = {}
        self.patient_id: str | None = None
        self._declare_collections()

    # ================================================================ scaffolding
    def _declare_collections(self):
        """Every collection the template can produce is present in the output (stable schema)."""
        def declare(cfg):
            self.collections.setdefault(cfg["collection"], None if cfg.get("container") == "object" else [])

        declare(self.t.subject)
        for s in self.t.sections:
            for pn in ("fields", "table", "row_table", "checklist"):
                p = s.get(pn)
                if not p:
                    continue
                declare(p["entity"])
                for sub in p["entity"].get("sub_entities", {}).values():
                    declare(sub)
                for it in p.get("items", []) + p.get("columns", []):
                    for k in ("link", "split_to"):
                        if it.get(k):
                            declare(it[k])

    def new_entity(self, cfg: dict, section_id: str, part: str, record_key, index: int = 1):
        prefix = cfg["prefix"]
        self.counters[prefix] += 1
        eid = f"{prefix}_{self.counters[prefix]:03d}"
        id_field = cfg.get("id_field") or f"{cfg['type']}_id"
        ent = {id_field: eid, "entity_type": cfg["type"], "section_id": section_id,
               "record": {"part": part, "record_key": record_key, "index": index}}
        coll = cfg["collection"]
        if cfg.get("container") == "object":
            if self.collections.get(coll) is not None:
                raise ValueError(f"second {coll} entity in one document")
            self.collections[coll] = ent
        else:
            self.collections.setdefault(coll, []).append(ent)
        self.by_id[eid] = ent
        self.meta[eid] = {"type": cfg["type"], "collection": coll, "id_field": id_field}
        return eid, ent

    # ================================================================ traces
    def trace(self, o: Observation, item: dict | None, typ: str | None = None, value=...):
        item = item or {}
        typ = typ or item.get("type", "text")
        if typ == "yes_no":
            v = o.extra.get("decision") if "decision" in o.extra else validators.normalize("yes_no", o.raw)[0]
            status, msgs = ("valid", []) if v in ("yes", "no") else ("needs_review", ["yes/no answer could not be resolved"])
        else:
            v, status, msgs = validators.normalize(typ, o.raw, item)
            msgs = list(msgs)
        if value is not ...:
            v = value
        if o.flags & {"ambiguous_choice", "continuation", "overflow"}:
            status = "needs_review"
        if o.confidence < self.thr and status in OK_STATUSES:
            status = "needs_review"
            msgs.append(f"confidence {o.confidence:.2f} is below the review threshold {self.thr}")
        attr = o.extra.get("option_key") or o.key
        tr = {
            "value": v,
            "raw_value": o.raw,
            "field_label": o.label_text,
            "template_field": f"{o.section_id}.{attr}",
            "source": {"filename": self.filename, "page": o.page, "bbox": round_bbox(o.bbox),
                       "label_bbox": round_bbox(o.label_bbox)},
            "extraction_method": o.method,
            "confidence": round(o.confidence, 3),
            "validation": {"status": status, "messages": msgs},
        }
        notes = list(o.notes)
        if o.extra.get("acroform_field"):
            tr["source"]["acroform_field"] = o.extra["acroform_field"]
        if o.extra.get("signature_evidence"):
            tr["signature_evidence"] = o.extra["signature_evidence"]
        if o.extra.get("derived_from"):
            notes.append(f"derived from {o.extra['derived_from']}")
        if notes:
            tr["notes"] = notes
        return tr

    def context_trace(self, value, sid: str, attr: str):
        """Attributes implied by WHERE the values were found (e.g. policy_type=primary)."""
        ev = self.res.sections.get(sid)
        page, bbox = (ev.heading_bboxes[0] if ev and ev.heading_bboxes else (None, None))
        heading = ev.source_heading if ev and ev.source_heading else self.t.section_by_id[sid]["title"]
        method = "section_context" if ev and ev.method == "heading" else "template_section"
        return {"value": value, "raw_value": heading, "field_label": f"section heading '{heading}'",
                "template_field": f"{sid}.{attr}",
                "source": {"filename": self.filename, "page": page, "bbox": round_bbox(bbox), "label_bbox": None},
                "extraction_method": method, "confidence": ev.confidence if ev else CONF["section_context"],
                "validation": {"status": "valid", "messages": []}}

    def set_attr(self, ent: dict, key: str, tr: dict):
        parts = key.split(".")
        tgt = ent
        for p in parts[:-1]:
            tgt = tgt.setdefault(p, {})
        k = parts[-1]
        cur = tgt.get(k)
        if cur is None:
            tgt[k] = tr
            return
        where = {"page": tr["source"]["page"], "bbox": tr["source"]["bbox"], "field_label": tr["field_label"]}
        if cur["value"] == tr["value"]:
            cur.setdefault("also_found_at", []).append(where)
            return
        cur.setdefault("alternate_values", []).append(dict(where, value=tr["value"]))
        if cur["validation"]["status"] == "valid":
            cur["validation"]["status"] = "warning"
        cur["validation"]["messages"].append(
            f"a different value {tr['value']!r} was also found for this attribute on page {where['page']}")

    # ================================================================ relationships
    def section_basis(self, sid: str):
        ev = self.res.sections.get(sid)
        title = self.t.section_by_id[sid]["title"]
        if not ev:
            return None, 0.0, f"no section evidence for '{title}'"
        if ev.method == "heading":
            p = ev.heading_bboxes[0][0]
            return ev, ev.confidence, f"inside section '{title}' (heading '{ev.source_heading}', page {p})"
        return ev, ev.confidence, f"entered in the reference template's '{title}' form fields"

    def rel(self, subj: str, pred: str, obj: str | None = None, *, value=None, traces=(), basis: str,
            confidence: float = 1.0, reasons=(), section_id: str | None = None):
        traces = [t for t in traces if t]
        reasons = [r for r in reasons if r]
        conf = min([confidence] + [t["confidence"] for t in traces])
        pages = sorted({t["source"]["page"] for t in traces if t["source"]["page"]})
        boxes = [{"page": t["source"]["page"], "bbox": t["source"]["bbox"], "attribute": t["template_field"]}
                 for t in traces if t["source"]["bbox"]]
        ev = self.res.sections.get(section_id) if section_id else None
        if ev and ev.heading_bboxes and ev.source_heading:
            hp, hb = ev.heading_bboxes[0]
            boxes.insert(0, {"page": hp, "bbox": round_bbox(hb), "attribute": "section_heading"})
            pages = sorted(set(pages) | {hp})
        if conf < self.thr:
            reasons.append(f"relationship confidence {conf:.2f} is below the review threshold")
        r = {"relationship_id": f"REL_{len(self.rels) + 1:03d}", "subject": subj, "predicate": pred}
        if obj is not None:
            r["object"] = obj
        else:
            r["object_value"] = value
        r.update({"support": "uncertain" if reasons else "explicit", "confidence": round(conf, 3),
                  "basis": basis,
                  "evidence": {"source_file": self.filename, "section_id": section_id, "pages": pages, "locations": boxes},
                  "review_required": bool(reasons)})
        if reasons:
            r["uncertainty_reasons"] = reasons
        self.rels.append(r)
        return r

    @staticmethod
    def _status_reasons(tr: dict | None, what: str) -> list[str]:
        if tr and tr["validation"]["status"] not in OK_STATUSES:
            return [f"{what} needs review ({'; '.join(tr['validation']['messages']) or tr['validation']['status']})"]
        return []

    # ================================================================ build
    def build(self):
        cfg = self.t.subject
        self.patient_id, _ = self.new_entity(cfg, cfg["section_id"], "fields", "main")
        self.part_entities[(cfg["section_id"], "fields", "main")] = self.patient_id
        for s in self.t.sections:
            sid = s["section_id"]
            obs = sorted((o for o in self.res.observations if o.section_id == sid), key=lambda o: o.order)
            if not obs and sid not in self.res.sections:
                self.missing.append({"ref": sid, "section_id": sid, "label": s["title"],
                                     "severity": "required" if sid == cfg["section_id"] else "section_not_found",
                                     "reason": "section not present in the source document"})
                continue
            if "fields" in s:
                self._fields(s, [o for o in obs if o.part == "fields"])
            for pn in ("table", "row_table"):
                if pn in s:
                    self._table(s, pn, [o for o in obs if o.part == pn])
            if "checklist" in s:
                self._checklist(s, [o for o in obs if o.part == "checklist"])
            if sid == cfg["section_id"]:
                self._derive_names()
        self._insurance_holders()
        self._signers()
        self._missing_pass()
        self._source_pages()

    # ---------------------------------------------------------------- fields part
    def _fields(self, s: dict, obs: list[Observation]):
        part, sid = s["fields"], s["section_id"]
        cfg = part["entity"]
        items = {it["key"]: it for it in part["items"]}
        ev, sconf, sbasis = self.section_basis(sid)

        if cfg.get("per_field"):
            for it in part["items"]:
                olist = [o for o in obs if o.key == it["key"]]
                if not olist:
                    self.missing.append({"ref": f"{sid}.{it['key']}", "section_id": sid, "label": it["label"],
                                         "severity": "required" if it.get("required") else "not_provided",
                                         "reason": "question not answered in the source document"})
                    continue
                eid, ent = self.new_entity(cfg, sid, "fields", it["key"])
                for k, v in {**cfg.get("fixed", {}), **it.get("fixed", {})}.items():
                    self.set_attr(ent, k, self.context_trace(v, sid, k))
                ent["question"] = it["label"]
                trs = [self.trace(o, it) for o in olist]
                for tr in trs:
                    self.set_attr(ent, cfg["value_attribute"], tr)
                tr = ent[cfg["value_attribute"]]
                self.part_entities[(sid, "fields", it["key"])] = eid
                reasons = self._status_reasons(tr, f"answer to '{it['label']}'")
                if tr.get("alternate_values"):
                    reasons.append("conflicting answers found for this consent question")
                self.rel(self.patient_id, cfg["patient_predicate"], eid, traces=[tr], confidence=sconf,
                         basis=f"answer to '{olist[0].label_text}' {sbasis}", reasons=reasons, section_id=sid)
                dp = cfg.get("decision_predicates", {})
                if tr["value"] in dp and not reasons:
                    perm = it.get("fixed", {}).get("permission_type")
                    self.rel(eid, dp[tr["value"]], value=perm, traces=[tr], confidence=sconf,
                             basis=f"'{tr['value'].upper()}' marked for '{it['label']}'", section_id=sid)
            return

        main = [o for o in obs if not o.entity_ref]
        subs: dict[str, list] = defaultdict(list)
        for o in obs:
            if o.entity_ref:
                subs[o.entity_ref].append(o)

        eid = None
        if cfg.get("subject"):
            eid = self.patient_id
        elif main or (cfg.get("sub_entities") and (ev or subs)):
            eid, ent = self.new_entity(cfg, sid, "fields", "main")
            for k, v in cfg.get("fixed", {}).items():
                self.set_attr(ent, k, self.context_trace(v, sid, k))
        if eid:
            ent = self.by_id[eid]
            for o in main:
                self.set_attr(ent, o.key, self.trace(o, items.get(o.key)))
            self.part_entities[(sid, "fields", "main")] = eid
            self._links(ent, eid, part["items"], sid, row=False)
            if cfg.get("patient_predicate") and not cfg.get("subject"):
                trs = [t for _, t in iter_traces(ent) if t["extraction_method"] not in ("section_context", "template_section")]
                key_item = next((it for it in part["items"] if it.get("required")), None)
                reasons = []
                if key_item and not get_attr(ent, key_item["key"]):
                    reasons.append(f"no {key_item['label']} found for this {cfg['type'].replace('_', ' ')}")
                self.rel(self.patient_id, cfg["patient_predicate"], eid, traces=trs[:6], confidence=sconf,
                         basis=f"{cfg['type'].replace('_', ' ')} details {sbasis} of the patient's own intake form",
                         reasons=reasons, section_id=sid)

        for ref, olist in subs.items():
            scfg = cfg["sub_entities"][ref]
            seid, sent = self.new_entity(scfg, sid, "fields", ref)
            for o in olist:
                it = items[o.key]
                key = "signature_present" if it.get("type") == "signature" else o.key
                self.set_attr(sent, key, self.trace(o, it))
            self.part_entities[(sid, "fields", ref)] = seid
            if eid:
                trs = [t for _, t in iter_traces(sent)]
                self.rel(seid, scfg["parent_predicate"], eid, traces=trs, confidence=sconf,
                         basis=f"signature block printed under '{s['title']}' {sbasis}", section_id=sid)

    # ---------------------------------------------------------------- linked entities
    def _links(self, ent: dict, eid: str, items: list[dict], sid: str, row: bool, reasons_extra=(), anchor=None):
        """anchor = the record's key attribute (e.g. medication name): included in the evidence so the
        relationship shows both cells that share the row."""
        for it in items:
            lk = it.get("link")
            if not lk:
                continue
            tr = get_attr(ent, it["key"])
            if not tr or not tr["value"]:
                continue
            key = (lk["collection"], name_key(str(tr["value"])))
            lid = self.link_index.get(key)
            if not lid:
                lid, lent = self.new_entity(lk, sid, "derived", None)
                ntr = copy.deepcopy(tr)
                ntr.setdefault("notes", []).append(f"entity created from {eid}.{it['key']}")
                lent["name"] = ntr
                lent["referenced_by"] = [eid]
                self.link_index[key] = lid
            else:
                self.by_id[lid]["referenced_by"].append(eid)
            ent[f"{it['key'].split('.')[-1]}_id"] = lid
            where = "on the same table row as" if row else "in the same section as"
            reasons = list(reasons_extra) + self._status_reasons(tr, it["label"])
            self.rel(eid, lk["predicate"], lid, traces=[anchor, tr] if anchor is not tr else [tr],
                     basis=f"'{tr['field_label']}' recorded {where} the {self.meta[eid]['type'].replace('_', ' ')}",
                     reasons=reasons, section_id=sid)

    def _splits(self, ent: dict, eid: str, items: list[dict], sid: str, owner_desc: str, reasons_extra=()):
        for it in items:
            sp = it.get("split_to")
            if not sp:
                continue
            tr = get_attr(ent, it["key"])
            if not tr or not tr["value"]:
                continue
            ids = []
            for nm in tr["value"]:
                cid, cent = self.new_entity(sp, sid, "derived", None)
                ctr = copy.deepcopy(tr)
                ctr["value"] = nm
                ctr.setdefault("notes", []).append(f"item split from list value {tr['raw_value']!r}")
                cent["name"] = ctr
                cent["owner_id"] = eid
                ids.append(cid)
                self.rel(eid, sp["predicate"], cid, traces=[ctr],
                         basis=f"'{nm}' listed in the '{it['label']}' cell of the {owner_desc}",
                         reasons=list(reasons_extra) + self._status_reasons(ctr, it["label"]), section_id=sid)
            ent[f"{it['key']}_ids"] = ids

    # ---------------------------------------------------------------- tables
    def _row_integrity(self, olist: list[Observation]) -> list[str]:
        reasons = []
        if any("continuation" in o.flags for o in olist):
            reasons.append("row text wraps onto a following line; merged values must be confirmed")
        if any("overflow" in o.flags for o in olist):
            reasons.append("a cell crosses a column boundary; column assignment must be confirmed")
        cells = [o for o in olist if o.bbox and "continuation" not in o.flags and o.method.startswith("layout")]
        if len({o.page for o in olist}) > 1:
            reasons.append("cells of this record are on different pages")
        elif cells:
            ycs = [(o.bbox[1] + o.bbox[3]) / 2 for o in cells]
            if max(ycs) - min(ycs) > 8:
                reasons.append("cells of this record are not vertically aligned on one row")
        return reasons

    def _table(self, s: dict, pn: str, obs: list[Observation]):
        part, sid = s[pn], s["section_id"]
        cfg = part["entity"]
        cols = {c["key"]: c for c in part["columns"]}
        groups: OrderedDict[str, list] = OrderedDict()
        for o in obs:
            groups.setdefault(o.record_key, []).append(o)
        if not groups:
            self.missing.append({"ref": f"{sid}.{pn}", "section_id": sid, "label": s["title"], "severity": "no_records",
                                 "reason": "section present but no records were found"})
            return
        ev, sconf, sbasis = self.section_basis(sid)
        for idx, (rk, olist) in enumerate(groups.items(), 1):
            eid, ent = self.new_entity(cfg, sid, pn, rk, idx)
            for o in olist:
                if pn == "row_table" and o.key == part["row_attribute"]:
                    known = o.extra.get("row_key_known", False)
                    tr = self.trace(o, {"type": "text"}, value=rk if known else normalize(str(o.raw)).replace(" ", "_"))
                    if not known:
                        tr["validation"]["status"] = "warning"
                        tr["validation"]["messages"].append("relation is not one of the template's rows; it is kept "
                                                            "in JSON but cannot be placed on the standardized form")
                    self.set_attr(ent, part["row_attribute"], tr)
                    continue
                self.set_attr(ent, o.key, self.trace(o, cols.get(o.key)))
            integrity = self._row_integrity(olist)
            if integrity:
                ent["record"]["integrity_issues"] = integrity
            owner = (f"'{ent[part['row_attribute']]['raw_value']}' row" if pn == "row_table" and part["row_attribute"] in ent
                     else f"row {idx}")
            key_col = part.get("key_column") or part.get("row_attribute")
            self._links(ent, eid, part["columns"], sid, row=True, reasons_extra=integrity, anchor=get_attr(ent, key_col))
            self._splits(ent, eid, part["columns"], sid, owner_desc=f"{owner} of '{s['title']}'", reasons_extra=integrity)
            reasons = list(integrity)
            if not get_attr(ent, key_col):
                reasons.append(f"record has no {key_col.replace('_', ' ')}; it cannot be identified reliably")
            trs = [t for _, t in iter_traces(ent)]
            if cfg.get("patient_predicate"):
                self.rel(self.patient_id, cfg["patient_predicate"], eid, traces=trs, confidence=sconf,
                         basis=f"{owner} {sbasis}", reasons=reasons, section_id=sid)
            if part.get("parent_link"):
                self._parent_link(s, part["parent_link"], eid, trs)

    def _parent_link(self, s: dict, pl: dict, eid: str, trs: list):
        sid = s["section_id"]
        parent = self.part_entities.get((sid, pl["part"], pl["field_key"]))
        if not parent:
            msg = f"{eid} is listed under '{s['title']}' but no authorization decision was recorded"
            self.warnings.append({"code": "authorization_decision_missing", "ref": eid, "message": msg})
            self.review_extra.append({"kind": "relationship", "ref": f"{eid}<-{pl['predicate']}", "reason": msg,
                                      "pages": sorted({t['source']['page'] for t in trs})})
            return
        dtr = self.by_id[parent].get("decision")
        decision = dtr["value"] if dtr else None
        if decision == pl["requires_decision"] and dtr["validation"]["status"] in OK_STATUSES:
            self.rel(parent, pl["predicate"], eid, traces=[dtr] + trs[:3], section_id=sid,
                     basis=f"authorization answered '{decision.upper()}' and the person is listed in the same section")
        elif decision in ("yes", "no"):
            msg = (f"{eid} is listed as an authorized person but the authorization question was answered "
                   f"'{decision.upper()}'")
            self.warnings.append({"code": "authorization_conflict", "ref": eid, "message": msg})
            self.review_extra.append({"kind": "relationship", "ref": f"{parent}-{pl['predicate']}->{eid}", "reason": msg,
                                      "pages": sorted({t['source']['page'] for t in trs})})
        else:
            self.rel(parent, pl["predicate"], eid, traces=[dtr] + trs[:3], section_id=sid,
                     basis="person listed in the authorization section",
                     reasons=["the authorization decision is ambiguous or unanswered"])

    # ---------------------------------------------------------------- checklist
    def _checklist(self, s: dict, obs: list[Observation]):
        part, sid = s["checklist"], s["section_id"]
        cfg = part["entity"]
        opts = {o["key"]: o for o in part["options"]}
        other = part.get("other")
        ev, sconf, sbasis = self.section_basis(sid)
        if not obs:
            self.missing.append({"ref": sid, "section_id": sid, "label": s["title"], "severity": "not_provided",
                                 "reason": "no condition ticked or listed"})
        n = 0
        for o in obs:
            if o.key == "option":
                n += 1
                ok = o.extra.get("option_key")
                eid, ent = self.new_entity(cfg, sid, "checklist", o.record_key, n)
                tr = self.trace(o, {"type": "text"}, value=opts[ok]["label"] if ok else str(o.raw))
                if not ok:
                    tr["validation"]["status"] = "warning" if tr["validation"]["status"] == "valid" else tr["validation"]["status"]
                    tr["validation"]["messages"].append("ticked item is not one of the template's checklist options")
                ent["name"] = tr
                ent["checklist_option"] = ok
                self.rel(self.patient_id, cfg["patient_predicate"], eid, traces=[tr], confidence=sconf,
                         basis=f"box ticked next to '{o.label_text}' {sbasis}",
                         reasons=self._status_reasons(tr, "ticked condition"), section_id=sid)
            elif other and o.key == other["key"]:
                ltr = self.trace(o, other)
                for nm in ltr["value"] or []:
                    n += 1
                    eid, ent = self.new_entity(cfg, sid, "checklist", "other", n)
                    tr = copy.deepcopy(ltr)
                    tr["value"] = nm
                    tr.setdefault("notes", []).append(f"item split from list value {ltr['raw_value']!r}")
                    ent["name"] = tr
                    ent["checklist_option"] = None
                    self.rel(self.patient_id, cfg["patient_predicate"], eid, traces=[tr], confidence=sconf,
                             basis=f"listed under '{o.label_text}' {sbasis}",
                             reasons=self._status_reasons(tr, other["label"]), section_id=sid)

    # ---------------------------------------------------------------- post passes
    def patient(self) -> dict:
        return self.by_id[self.patient_id]

    def patient_name(self) -> str | None:
        p = self.patient()
        fn, ln = get_attr(p, "first_name"), get_attr(p, "last_name")
        if fn and ln:
            return f"{fn['value']} {ln['value']}"
        full = get_attr(p, "full_name")
        return full["value"] if full else None

    def _derive_names(self):
        p = self.patient()
        full = get_attr(p, "full_name")
        if not full or (get_attr(p, "first_name") and get_attr(p, "last_name")):
            return
        raw = str(full["value"]).strip()
        first = last = None
        if "," in raw:
            last, rest = [x.strip() for x in raw.split(",", 1)]
            first = rest.split()[0] if rest else None
            how, fmt = "derived_split_comma", "Last, First"
        else:
            parts = raw.split()
            if len(parts) >= 2:
                first, last = parts[0], parts[-1]
            how, fmt = "derived_split_space", "First Last"
        for key, val in (("first_name", first), ("last_name", last)):
            if val and not get_attr(p, key):
                tr = copy.deepcopy(full)
                tr.update(value=val.strip(" ,."), extraction_method="derived_from_full_name",
                          template_field=f"{p['section_id']}.{key}",
                          confidence=round(min(full["confidence"], CONF[how]), 3))
                tr.setdefault("notes", []).append(f"derived by splitting {raw!r} as '{fmt}'")
                p[key] = tr

    def _insurance_holders(self):
        pname = self.patient_name()
        spouse = self.collections.get("spouse")
        for pol in self.collections.get("insurance_policies") or []:
            eid = pol["policy_id"]
            rtr = get_attr(pol, "policy_holder.relationship_to_patient")
            ntr = get_attr(pol, "policy_holder.name")
            if not rtr:
                continue
            if rtr["value"] == "self":
                reasons = self._status_reasons(rtr, "relationship to patient")
                if ntr and pname and not names_match(str(ntr["value"]), pname):
                    reasons.append(f"policy holder {ntr['value']!r} does not match patient {pname!r} although "
                                   f"relationship is 'self'")
                self.rel(eid, "HELD_BY", self.patient_id, traces=[rtr, ntr], section_id=pol["section_id"],
                         basis="policy holder 'Relationship to Patient' is 'Self'", reasons=reasons)
            elif rtr["value"] == "spouse" and spouse and ntr and get_attr(spouse, "name"):
                sname = get_attr(spouse, "name")["value"]
                if names_match(str(ntr["value"]), str(sname)):
                    self.rel(eid, "HELD_BY", spouse["spouse_id"], traces=[rtr, ntr, get_attr(spouse, "name")],
                             section_id=pol["section_id"],
                             basis="relationship is 'Spouse' and the policy holder name matches the spouse section")
                else:
                    self.warnings.append({"code": "holder_spouse_mismatch", "ref": eid,
                                          "message": f"policy holder {ntr['value']!r} is marked as spouse but the "
                                                     f"spouse section names {sname!r}; no link created"})

    def _signers(self):
        pname = self.patient_name()
        for agr in self.collections.get("agreements") or []:
            sid, aid = agr["section_id"], agr["agreement_id"]
            title = agr["title"]["value"]
            sig = self.by_id.get(self.part_entities.get((sid, "fields", "signature"), ""))
            sp = get_attr(sig, "signature_present") if sig else None
            if not sp or not sp["value"]:
                self.warnings.append({"code": "agreement_not_signed", "ref": aid,
                                      "message": f"no signature detected for '{title}'"})
                self.missing.append({"ref": f"{sid}.signature", "section_id": sid, "entity_id": aid,
                                     "label": f"{title} signature", "severity": "required",
                                     "reason": "signature line empty or not found"})
                continue
            pn = get_attr(sig, "printed_name")
            reasons = self._status_reasons(sp, "signature")
            if pn and pname and not names_match(str(pn["value"]), pname):
                reasons.append(f"printed name {pn['value']!r} differs from patient {pname!r}; the signer may be a "
                               f"guardian or representative")
            elif not pn:
                typed = sp["raw_value"] if isinstance(sp["raw_value"], str) else None
                if typed and pname and names_match(typed.replace("/s/", ""), pname):
                    pass
                elif "patient" not in normalize(sp["field_label"]):
                    reasons.append("signature line is not labelled as the patient's and no printed name is given")
            sec = self.t.section_by_id[sid]["fields"]["entity"]
            self.rel(self.patient_id, sec.get("signer_predicate", "SIGNS"), aid,
                     traces=[sp, pn, get_attr(sig, "signed_date")], section_id=sid,
                     basis=f"signature found on the '{sp['field_label']}' line of '{title}' (page {sp['source']['page']})",
                     reasons=reasons)

    def _missing_pass(self):
        found_sections = {sid for sid in self.res.sections} | {o.section_id for o in self.res.observations}
        for s in self.t.sections:
            sid = s["section_id"]
            is_subject = sid == self.t.subject["section_id"]
            if "fields" not in s or (sid not in found_sections and not is_subject):
                continue
            part = s["fields"]
            if part["entity"].get("per_field"):
                continue
            ent = self.by_id.get(self.part_entities.get((sid, "fields", "main"), ""))
            for it in part["items"]:
                if it.get("render") is False or it.get("type") == "signature":
                    continue
                tgt = ent
                if it.get("entity_ref"):
                    tgt = self.by_id.get(self.part_entities.get((sid, "fields", it["entity_ref"]), ""))
                    if tgt is None:
                        continue            # whole signature block missing: reported by _signers
                if tgt is None or not get_attr(tgt, it["key"]):
                    self.missing.append({"ref": f"{sid}.{it['key']}", "section_id": sid,
                                         "entity_id": tgt[self.meta_id(tgt)] if tgt else None,
                                         "label": it["label"], "severity": "required" if it.get("required") else "not_provided",
                                         "reason": "no value found in the source document"})
        for coll in ("medications", "allergies", "surgeries", "authorized_family_members", "family_members"):
            for ent in self.collections.get(coll) or []:
                s = self.t.section_by_id[ent["section_id"]]
                part = s.get(ent["record"]["part"])
                if not part or "columns" not in part:
                    continue
                gaps = [c["label"] for c in part["columns"] if not get_attr(ent, c["key"])]
                if gaps:
                    eid = ent[self.meta_id(ent)]
                    self.missing.append({"ref": eid, "section_id": ent["section_id"], "entity_id": eid,
                                         "label": ", ".join(gaps), "severity": "incomplete_record",
                                         "reason": f"record {ent['record']['index']} has no value for: {', '.join(gaps)}"})

    def meta_id(self, ent: dict) -> str:
        for k in ent:
            if k.endswith("_id") and isinstance(ent[k], str) and ent[k] in self.by_id and self.by_id[ent[k]] is ent:
                return k
        raise KeyError("entity without id")

    def _source_pages(self):
        for ent in self.by_id.values():
            ent["source_pages"] = sorted({t["source"]["page"] for _, t in iter_traces(ent) if t["source"]["page"]})

    # ================================================================ output
    def entities_json(self) -> dict:
        return dict(self.collections)
