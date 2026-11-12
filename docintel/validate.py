"""Relationship validation over a finished document graph, plus the manual-review queue.

Each check is independent and reports {check, status, checked, issues[]}. An issue has a
severity: 'error' (graph is inconsistent), 'review' (a human must confirm) or 'warning'.
"""
from __future__ import annotations

from collections import Counter, defaultdict

from .builder import get_attr, iter_traces
from .textnorm import normalize


def _index(doc: dict) -> dict[str, dict]:
    idx = {}
    for val in doc["entities"].values():
        for ent in ([val] if isinstance(val, dict) else (val or [])):
            idx[_eid(ent)] = ent
    return idx


def _eid(ent: dict) -> str:
    """The entity's own id field is the first *_id key (ids are written first)."""
    for k, v in ent.items():
        if k.endswith("_id") and isinstance(v, str):
            return v
    raise KeyError("entity without id")


def _check(name, checked, issues):
    sev = {i["severity"] for i in issues}
    status = "failed" if "error" in sev else ("review" if "review" in sev else ("warning" if issues else "passed"))
    return {"check": name, "status": status, "checked": checked, "issues": issues}


def _issue(sev, ref, msg):
    return {"severity": sev, "ref": ref, "message": msg}


# ------------------------------------------------------------------ checks
def check_isolation(doc, idx):
    fn = doc["source"]["filename"]
    issues, n = [], 0
    for eid, ent in idx.items():
        for path, tr in iter_traces(ent):
            n += 1
            if tr["source"]["filename"] != fn:
                issues.append(_issue("error", f"{eid}.{path}", f"value originates from {tr['source']['filename']!r}, not {fn!r}"))
        for k, v in ent.items():
            if k == "section_id":
                continue
            refs = v if isinstance(v, list) and k.endswith("_ids") else ([v] if k.endswith("_id") and isinstance(v, str) else [])
            if k == "referenced_by":
                refs = v
            for r in refs:
                if r not in idx:
                    issues.append(_issue("error", eid, f"{k} references {r!r}, which does not exist in this document"))
    ids = [_eid(e) for e in idx.values()]
    for d, c in Counter(ids).items():
        if c > 1:
            issues.append(_issue("error", d, "duplicate entity id"))
    for r in doc["relationships"]:
        n += 1
        if r["evidence"]["source_file"] != fn:
            issues.append(_issue("error", r["relationship_id"], "relationship evidence comes from another file"))
        for end in ("subject", "object"):
            if end in r and r[end] not in idx:
                issues.append(_issue("error", r["relationship_id"], f"{end} {r[end]!r} is not an entity of this document"))
    return _check("document_isolation", n, issues)


def check_predicates(doc, idx, template):
    issues = []
    for r in doc["relationships"]:
        spec = template.predicates.get(r["predicate"])
        if not spec:
            issues.append(_issue("error", r["relationship_id"], f"unknown predicate {r['predicate']}"))
            continue
        st = idx.get(r["subject"], {}).get("entity_type")
        if st not in spec["subject"]:
            issues.append(_issue("error", r["relationship_id"], f"{r['predicate']} cannot have a {st} subject"))
        if "object" in r:
            ot = idx.get(r["object"], {}).get("entity_type")
            if ot not in spec["object"]:
                issues.append(_issue("error", r["relationship_id"], f"{r['predicate']} cannot point to a {ot}"))
        elif "@literal" not in spec["object"]:
            issues.append(_issue("error", r["relationship_id"], f"{r['predicate']} requires an entity object"))
    return _check("predicate_signatures", len(doc["relationships"]), issues)


def _rels(doc, pred=None, subj=None, obj=None):
    return [r for r in doc["relationships"] if (pred is None or r["predicate"] == pred)
            and (subj is None or r["subject"] == subj) and (obj is None or r.get("object") == obj)]


def check_medications(doc, idx):
    meds = doc["entities"].get("medications") or []
    issues = []
    for m in meds:
        mid = m["medication_id"]
        if not get_attr(m, "name"):
            issues.append(_issue("review", mid, "medication record has no name; its dosage/doctor cannot be attributed"))
        for path, tr in iter_traces(m):
            if not tr["template_field"].startswith("medications."):
                issues.append(_issue("error", f"{mid}.{path}", "attribute did not come from the medications section"))
        pages = {tr["source"]["page"] for _, tr in iter_traces(m)}
        if len(pages) > 1:
            issues.append(_issue("review", mid, "medication attributes come from different pages"))
        for msg in m["record"].get("integrity_issues", []):
            issues.append(_issue("review", mid, msg))
        pb = _rels(doc, "PRESCRIBED_BY", subj=mid)
        if len(pb) > 1:
            issues.append(_issue("error", mid, "medication linked to more than one prescribing doctor"))
        if pb and pb[0]["object"] != m.get("prescribing_doctor_id"):
            issues.append(_issue("error", mid, "PRESCRIBED_BY relationship disagrees with prescribing_doctor_id"))
        if pb:
            dr = idx.get(pb[0]["object"])
            if dr and normalize(str(dr["name"]["value"])).replace("dr ", "") != normalize(str(m["prescribing_doctor"]["value"])).replace("dr ", ""):
                issues.append(_issue("warning", mid, "doctor entity name differs from the value on the medication row"))
    return _check("medication_record_integrity", len(meds), issues)


def check_insurance(doc):
    pols = doc["entities"].get("insurance_policies") or []
    issues = []
    types = Counter(get_attr(p, "policy_type")["value"] for p in pols if get_attr(p, "policy_type"))
    for t, c in types.items():
        if c > 1:
            issues.append(_issue("error", t, f"{c} policies are typed '{t}'; primary/secondary must be distinct"))
    for p in pols:
        pid, sid = p["policy_id"], p["section_id"]
        for path, tr in iter_traces(p):
            if not tr["template_field"].startswith(sid + "."):
                issues.append(_issue("error", f"{pid}.{path}", f"value from {tr['template_field']} merged into a {sid} policy"))
    by_type = {get_attr(p, "policy_type")["value"]: p for p in pols if get_attr(p, "policy_type")}
    pri, sec = by_type.get("primary"), by_type.get("secondary")
    if sec and not pri:
        issues.append(_issue("warning", sec["policy_id"], "secondary insurance present without a primary policy"))
    if pri and sec:
        a, b = get_attr(pri, "insurance_id"), get_attr(sec, "insurance_id")
        if a and b and normalize(str(a["value"])) == normalize(str(b["value"])):
            issues.append(_issue("review", sec["policy_id"], "secondary policy has the same Insurance ID as the primary "
                                                             "policy - possible duplicated or merged entry"))
    for p in pols:
        if not get_attr(p, "insurance_id"):
            issues.append(_issue("warning", p["policy_id"], "policy has no insurance ID"))
    return _check("insurance_policy_separation", len(pols), issues)


def check_family(doc, idx):
    fam = doc["entities"].get("family_members") or []
    conds = doc["entities"].get("medical_conditions") or []
    issues = []
    rel_counts = Counter(get_attr(f, "relation")["value"] for f in fam if get_attr(f, "relation"))
    for r, c in rel_counts.items():
        if c > 1 and r in ("mother", "father"):
            issues.append(_issue("review", r, f"{c} separate '{r}' rows found"))
    for f in fam:
        if not get_attr(f, "relation"):
            issues.append(_issue("review", f["family_member_id"], "family history record without a family member label"))
    owners = defaultdict(list)
    for r in _rels(doc, "HAS_CONDITION"):
        owners[r["object"]].append(r["subject"])
    for c in conds:
        cid = c["medical_condition_id"]
        own = owners.get(cid, [])
        if len(own) != 1:
            issues.append(_issue("error", cid, f"condition has {len(own)} owners; it must belong to exactly one person"))
            continue
        otype = idx.get(own[0], {}).get("entity_type")
        if c["section_id"] == "family_history" and otype != "family_member":
            issues.append(_issue("error", cid, "family-history condition attached to someone other than a family member"))
        if c["section_id"] == "medical_history" and otype != "patient":
            issues.append(_issue("error", cid, "patient medical-history condition attached to a family member"))
        if c.get("owner_id") and c["owner_id"] != own[0]:
            issues.append(_issue("error", cid, "owner_id disagrees with HAS_CONDITION relationship"))
    return _check("family_history_separation", len(fam) + len(conds), issues)


def check_consents(doc):
    cons = doc["entities"].get("consents") or []
    issues = []
    for c in cons:
        cid = c["consent_id"]
        pt, dec = get_attr(c, "permission_type"), get_attr(c, "decision")
        if not pt:
            issues.append(_issue("error", cid, "consent without a permission type"))
        if not dec or dec["value"] not in ("yes", "no"):
            issues.append(_issue("review", cid, f"consent decision for '{pt and pt['value']}' could not be resolved"))
            continue
        if get_attr(c, "category") and get_attr(c, "category")["value"] == "communication":
            want = "AUTHORIZES_COMMUNICATION" if dec["value"] == "yes" else "DECLINES_COMMUNICATION"
            got = [r for r in doc["relationships"] if r["subject"] == cid and r["predicate"].endswith("_COMMUNICATION")]
            if not got and dec["validation"]["status"] in ("valid", "warning"):
                issues.append(_issue("error", cid, f"decision '{dec['value']}' has no {want} relationship"))
            for r in got:
                if r["predicate"] != want or r.get("object_value") != pt["value"]:
                    issues.append(_issue("error", cid, f"relationship {r['relationship_id']} does not match decision/permission"))
    return _check("consent_decision_integrity", len(cons), issues)


def check_signatures(doc, idx):
    sigs = doc["entities"].get("signatures") or []
    agrs = doc["entities"].get("agreements") or []
    issues = []
    dob = get_attr(doc["entities"].get("patient") or {}, "date_of_birth")
    per_agr = Counter()
    for s in sigs:
        sid = s["signature_id"]
        links = _rels(doc, "SIGNATURE_FOR", subj=sid)
        if len(links) != 1:
            issues.append(_issue("error", sid, f"signature linked to {len(links)} agreements; must be exactly one"))
            continue
        agr = idx.get(links[0]["object"])
        per_agr[links[0]["object"]] += 1
        if agr and agr["section_id"] != s["section_id"]:
            issues.append(_issue("error", sid, "signature and agreement come from different sections"))
        d = get_attr(s, "signed_date")
        if d and dob and isinstance(d["value"], str) and isinstance(dob["value"], str) and len(d["value"]) == 10 \
                and len(dob["value"]) == 10 and d["value"] < dob["value"]:
            issues.append(_issue("review", sid, "signature date is before the patient's date of birth"))
    for a, c in per_agr.items():
        if c > 1:
            issues.append(_issue("error", a, "agreement has more than one signature block"))
    return _check("signature_agreement_binding", len(sigs) + len(agrs), issues)


# ------------------------------------------------------------------ driver
def validate_document(doc: dict, template, builder) -> None:
    idx = _index(doc)
    checks = [
        check_isolation(doc, idx),
        check_predicates(doc, idx, template),
        check_medications(doc, idx),
        check_insurance(doc),
        check_family(doc, idx),
        check_consents(doc),
        check_signatures(doc, idx),
    ]
    warnings = list(builder.warnings)
    errors = []
    review: list[dict] = []

    for c in checks:
        for i in c["issues"]:
            entry = {"code": c["check"], "ref": i["ref"], "message": i["message"]}
            if i["severity"] == "error":
                errors.append(entry)
            else:
                warnings.append(entry)
            if i["severity"] in ("error", "review"):
                review.append({"kind": "validation", "ref": i["ref"], "reason": f"[{c['check']}] {i['message']}"})

    for eid, ent in idx.items():
        for path, tr in iter_traces(ent):
            st = tr["validation"]["status"]
            if st in ("needs_review", "invalid"):
                review.append({"kind": "attribute", "ref": f"{eid}.{path}", "field_label": tr["field_label"],
                               "value": tr["value"], "status": st,
                               "reason": "; ".join(tr["validation"]["messages"] + tr.get("notes", [])) or st,
                               "page": tr["source"]["page"], "bbox": tr["source"]["bbox"]})
            elif st == "warning":
                warnings.append({"code": "attribute_warning", "ref": f"{eid}.{path}",
                                 "message": "; ".join(tr["validation"]["messages"])})
    for r in doc["relationships"]:
        if r["support"] == "uncertain":
            review.append({"kind": "relationship", "ref": r["relationship_id"],
                           "relationship": f"{r['subject']} {r['predicate']} {r.get('object', r.get('object_value'))}",
                           "reason": "; ".join(r.get("uncertainty_reasons", [])), "pages": r["evidence"]["pages"]})
    review.extend(builder.review_extra)
    for w in builder.warnings:
        if w.get("code") in ("table_header_not_recognised", "family_row_without_relation", "no_sections_recognised"):
            review.append({"kind": "section", "ref": w.get("section_id"), "reason": w["message"]})
    for p in doc["source"]["pages_without_text_layer"]:
        review.append({"kind": "page", "ref": f"page {p}",
                       "reason": "page is an image without a text layer and OCR is unavailable; content not extracted"})

    seen, dedup = set(), []
    for r in review:
        k = (r["kind"], r["ref"], r["reason"])
        if k not in seen:
            seen.add(k)
            dedup.append(r)
    for i, r in enumerate(dedup, 1):
        r["review_id"] = f"REV_{i:03d}"
        dedup[i - 1] = {"review_id": r.pop("review_id"), **r}

    extracted = sum(1 for e in idx.values() for _ in iter_traces(e))
    if doc["source"]["pages_without_text_layer"] and extracted == 0:
        status = "needs_ocr"
    elif errors:
        status = "failed_validation"
    elif dedup:
        status = "needs_review"
    elif warnings:
        status = "completed_with_warnings"
    else:
        status = "completed"

    doc["validation"] = {"status": status, "checks": checks, "errors": errors, "warnings": warnings}
    doc["manual_review"] = dedup
    doc["processing"]["status"] = status
