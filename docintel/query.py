"""Question answering over one document's structured JSON.

  1. entity(id)            What information belongs to this entity?
  2. related(ref)          What information is related to this field / entity?
  3. owner(ref)            Which person does this information belong to?
  4. section(ref)          Which section contains this information?
  5. between(a, b)         What is the relationship between these two data elements?
  6/7. evidence(rel_id)    Which source page supports it, and is it explicit or uncertain?
  find(text)               Locate entities/attributes whose value matches some text.

A ref is an entity id ('MED_002') or an attribute path ('MED_002.dosage',
'INS_002.policy_holder.name').
"""
from __future__ import annotations

from collections import deque

from .builder import get_attr, iter_traces
from .textnorm import normalize
from .validate import _eid, _index

PERSON_TYPES = {"patient", "spouse", "family_member", "emergency_contact", "authorized_family_member", "doctor"}
# predicate -> True when the SUBJECT owns the object; False when the OBJECT owns the subject
OWNERSHIP = {"HAS_INSURANCE": True, "HAS_SPOUSE": True, "HAS_EMERGENCY_CONTACT": True, "TAKES_MEDICATION": True,
             "HAS_ALLERGY": True, "HAS_CONDITION": True, "HAD_SURGERY": True, "HAS_FAMILY_HISTORY": True,
             "PROVIDES_CONSENT": True, "HAS_AUTHORIZED_FAMILY_MEMBER": True, "SIGNS": True, "SIGNATURE_FOR": False}


def _short(tr: dict) -> dict:
    return {"value": tr["value"], "label": tr["field_label"], "page": tr["source"]["page"],
            "bbox": tr["source"]["bbox"], "method": tr["extraction_method"], "confidence": tr["confidence"],
            "status": tr["validation"]["status"]}


class DocQuery:
    def __init__(self, doc: dict):
        self.doc = doc
        self.idx = _index(doc)
        self.sections = {s["section_id"]: s for s in doc["sections"]}

    # ------------------------------------------------------------ helpers
    def _split(self, ref: str):
        eid, _, path = ref.partition(".")
        if eid not in self.idx:
            raise KeyError(f"{eid!r} is not an entity of {self.doc['document_id']}")
        return eid, path

    def _rels(self, eid):
        out = [r for r in self.doc["relationships"] if r["subject"] == eid]
        inc = [r for r in self.doc["relationships"] if r.get("object") == eid]
        return out, inc

    @staticmethod
    def _rel_line(r):
        return (f"{r['relationship_id']}: {r['subject']} -{r['predicate']}-> {r.get('object', repr(r.get('object_value')))}"
                f"  [{r['support']}, conf {r['confidence']}, pages {r['evidence']['pages']}]")

    # ------------------------------------------------------------ 1
    def entity(self, eid: str) -> dict:
        eid, _ = self._split(eid)
        e = self.idx[eid]
        out, inc = self._rels(eid)
        return {"entity_id": eid, "entity_type": e["entity_type"], "section": self.section(eid)["title"],
                "record": e["record"], "source_pages": e.get("source_pages"),
                "attributes": {p: _short(t) for p, t in iter_traces(e)},
                "links": {k: v for k, v in e.items() if k.endswith(("_id", "_ids")) and v != eid},
                "relationships_out": [self._rel_line(r) for r in out],
                "relationships_in": [self._rel_line(r) for r in inc]}

    # ------------------------------------------------------------ 2
    def related(self, ref: str) -> dict:
        eid, path = self._split(ref)
        e = self.idx[eid]
        out, inc = self._rels(eid)
        res = {"ref": ref, "belongs_to_entity": eid, "entity_type": e["entity_type"],
               "record": e["record"],
               "sibling_attributes": {p: t["value"] for p, t in iter_traces(e) if p != path},
               "relationships": [self._rel_line(r) for r in out + inc]}
        if path:
            tr = get_attr(e, path)
            if not tr:
                raise KeyError(f"{ref!r} has no such attribute")
            res["attribute"] = _short(tr)
            linked = e.get(f"{path.split('.')[-1]}_id")
            if linked:
                res["linked_entity"] = {"entity_id": linked, "attributes": {p: t["value"] for p, t in iter_traces(self.idx[linked])}}
        return res

    # ------------------------------------------------------------ 3
    def owner(self, ref: str) -> dict:
        eid, path = self._split(ref)
        e = self.idx[eid]
        if path.startswith("policy_holder"):
            held = [r for r in self.doc["relationships"] if r["subject"] == eid and r["predicate"] == "HELD_BY"]
            if held:
                r = held[0]
                return {"ref": ref, "person": r["object"], "person_type": self.idx[r["object"]]["entity_type"],
                        "person_name": self._name(r["object"]), "chain": [self._rel_line(r)], "support": r["support"]}
            nm = get_attr(e, "policy_holder.name")
            return {"ref": ref, "person": None, "person_name": nm and nm["value"], "support": "unlinked",
                    "note": "policy holder is recorded on the policy but not linked to a person entity"}
        chain, cur, support = [], eid, "explicit"
        for _ in range(6):
            if self.idx[cur]["entity_type"] in PERSON_TYPES:
                return {"ref": ref, "person": cur, "person_type": self.idx[cur]["entity_type"],
                        "person_name": self._name(cur), "chain": chain, "support": support}
            step = None
            for r in self.doc["relationships"]:
                own = OWNERSHIP.get(r["predicate"])
                if own is True and r.get("object") == cur:
                    step = (r, r["subject"])
                elif own is False and r["subject"] == cur:
                    step = (r, r.get("object"))
                if step:
                    break
            if not step:
                break
            chain.append(self._rel_line(step[0]))
            if step[0]["support"] == "uncertain":
                support = "uncertain"
            cur = step[1]
        return {"ref": ref, "person": None, "chain": chain, "support": "not_established",
                "note": "no ownership relationship leads to a person"}

    def _name(self, eid):
        e = self.idx[eid]
        fn, ln = get_attr(e, "first_name"), get_attr(e, "last_name")
        if fn or ln:
            return " ".join(str(x["value"]) for x in (fn, ln) if x)
        for k in ("name", "full_name"):
            if get_attr(e, k):
                return get_attr(e, k)["value"]
        rel = get_attr(e, "relation")
        return rel["raw_value"] if rel else None

    # ------------------------------------------------------------ 4
    def section(self, ref: str) -> dict:
        eid, path = self._split(ref)
        e = self.idx[eid]
        s = self.sections.get(e["section_id"], {"section_id": e["section_id"], "title": e["section_id"]})
        res = {"ref": ref, "section_id": s["section_id"], "title": s.get("title"),
               "source_heading": s.get("source_heading"), "section_pages": s.get("pages")}
        if path:
            tr = get_attr(e, path)
            res["value_page"] = tr["source"]["page"] if tr else None
            res["value_bbox"] = tr["source"]["bbox"] if tr else None
        return res

    # ------------------------------------------------------------ 5
    def between(self, a: str, b: str) -> dict:
        ea, _ = self._split(a)
        eb, _ = self._split(b)
        if ea == eb:
            return {"a": a, "b": b, "relation": "same_entity",
                    "explanation": f"both are attributes of {ea} ({self.idx[ea]['entity_type']})"}
        direct = [r for r in self.doc["relationships"]
                  if {r["subject"], r.get("object")} == {ea, eb}]
        if direct:
            return {"a": a, "b": b, "relation": "direct", "relationships": [self._rel_line(r) for r in direct],
                    "support": "uncertain" if any(r["support"] == "uncertain" for r in direct) else "explicit"}
        adj: dict[str, list] = {}
        for r in self.doc["relationships"]:
            if "object" in r:
                adj.setdefault(r["subject"], []).append((r["object"], r))
                adj.setdefault(r["object"], []).append((r["subject"], r))
        q, prev = deque([ea]), {ea: None}
        while q:
            cur = q.popleft()
            if cur == eb:
                break
            for nxt, r in adj.get(cur, []):
                if nxt not in prev:
                    prev[nxt] = (cur, r)
                    q.append(nxt)
        if eb not in prev:
            return {"a": a, "b": b, "relation": "none", "explanation": "no relationship path in this document"}
        path, cur = [], eb
        while prev[cur]:
            cur, r = prev[cur][0], prev[cur][1]
            path.append(r)
        path.reverse()
        return {"a": a, "b": b, "relation": "indirect", "path": [self._rel_line(r) for r in path],
                "support": "uncertain" if any(r["support"] == "uncertain" for r in path) else "explicit"}

    # ------------------------------------------------------------ 6 / 7
    def evidence(self, rel_id: str) -> dict:
        r = next((x for x in self.doc["relationships"] if x["relationship_id"] == rel_id), None)
        if not r:
            raise KeyError(f"{rel_id!r} not found")
        return {"relationship": self._rel_line(r), "support": r["support"], "confidence": r["confidence"],
                "basis": r["basis"], "source_file": r["evidence"]["source_file"], "pages": r["evidence"]["pages"],
                "locations": r["evidence"]["locations"], "uncertainty_reasons": r.get("uncertainty_reasons", [])}

    # ------------------------------------------------------------ search
    def find(self, text: str) -> list[dict]:
        n = normalize(text)
        hits = []
        for eid, e in self.idx.items():
            for p, t in iter_traces(e):
                if n and n in normalize(str(t["value"])):
                    hits.append({"ref": f"{eid}.{p}", "value": t["value"], "page": t["source"]["page"],
                                 "owner": self.owner(eid).get("person_name")})
        return hits


def render_summary(doc: dict) -> str:
    """Readable overview of one document's graph (for terminals and quick review)."""
    q = DocQuery(doc)
    L = [f"{doc['document_id']}  {doc['source']['filename']}  [{doc['processing']['status']}]  "
         f"mode={doc['processing']['extraction_mode']}"]
    for coll, v in doc["entities"].items():
        ents = [v] if isinstance(v, dict) else (v or [])
        if not ents:
            continue
        L.append(f"\n{coll}:")
        for e in ents:
            eid = _eid(e)
            vals = []
            for p, t in iter_traces(e):
                flag = "" if t["validation"]["status"] in ("valid",) else f" <{t['validation']['status']}>"
                vals.append(f"{p}={t['value']!r}{flag}")
            L.append(f"  {eid}: " + ", ".join(vals))
    L.append("\nrelationships:")
    for r in doc["relationships"]:
        L.append("  " + q._rel_line(r))
    if doc["manual_review"]:
        L.append("\nmanual review:")
        for r in doc["manual_review"]:
            L.append(f"  {r['review_id']} [{r['kind']}] {r['ref']}: {r['reason']}")
    if doc["missing_information"]:
        L.append("\nmissing information:")
        for m in doc["missing_information"]:
            L.append(f"  [{m['severity']}] {m['ref']}: {m['reason']}")
    return "\n".join(L)
