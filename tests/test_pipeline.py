"""End-to-end tests over the generated sample PDFs.

Each test pins one relationship guarantee from the requirements.
Run:  python -m pytest -q
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pymupdf
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from docintel.builder import get_attr, iter_traces  # noqa: E402
from docintel.pipeline import process_batch  # noqa: E402
from docintel.query import DocQuery  # noqa: E402
from docintel.template import Template  # noqa: E402
from docintel.validate import _index, check_insurance, check_isolation  # noqa: E402

import build_reference_template  # noqa: E402
import make_samples  # noqa: E402

TEMPLATE_DIR = ROOT / "templates" / "patient_intake_v1"


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    build_reference_template.build(TEMPLATE_DIR)
    inp = tmp_path_factory.mktemp("in")
    out = tmp_path_factory.mktemp("out")
    make_samples.main(inp)
    t = Template(TEMPLATE_DIR)
    idx = process_batch([inp], out, t, log=lambda *_: None)
    docs = {d["document_id"]: json.loads((out / d["json"]).read_text(encoding="utf-8"))
            for d in idx["documents"] if "json" in d}
    return {"index": idx, "docs": docs, "out": out, "template": t}


def ents(doc, coll):
    v = doc["entities"][coll]
    return [v] if isinstance(v, dict) else (v or [])


def val(ent, key):
    tr = get_attr(ent, key)
    return tr["value"] if tr else None


def rels(doc, pred, subj=None, obj=None):
    return [r for r in doc["relationships"] if r["predicate"] == pred
            and (subj is None or r["subject"] == subj) and (obj is None or r.get("object") == obj)]


# ------------------------------------------------------------------ outputs
def test_one_json_per_pdf_and_batch_index(run):
    idx = run["index"]
    assert [d["document_id"] for d in idx["documents"]] == ["DOC_001", "DOC_002", "DOC_003", "DOC_004"]
    assert {d["status"] for d in idx["documents"]} <= {"completed", "needs_review", "needs_ocr", "completed_with_warnings"}
    for d in idx["documents"]:
        assert (run["out"] / "json" / f"{d['document_id']}.json").exists()
    assert (run["out"] / "batch_index.json").exists() and (run["out"] / "review_queue.json").exists()


def test_outputs_conform_to_json_schema(run):
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((ROOT / "schema" / "document.schema.json").read_text(encoding="utf-8"))
    doc_v = jsonschema.Draft202012Validator(schema)
    trace_v = jsonschema.Draft202012Validator({**schema["$defs"]["trace"], "$defs": schema["$defs"]})
    for doc in run["docs"].values():
        doc_v.validate(doc)
        for e in _index(doc).values():
            for _, tr in iter_traces(e):
                trace_v.validate(tr)


def test_every_value_is_traceable_to_its_own_source_file(run):
    for doc in run["docs"].values():
        fn = doc["source"]["filename"]
        for e in _index(doc).values():
            for _, tr in iter_traces(e):
                assert tr["source"]["filename"] == fn
                assert tr["source"]["page"] is not None
                assert {"field_label", "extraction_method", "confidence", "validation"} <= tr.keys()
        for r in doc["relationships"]:
            assert r["evidence"]["source_file"] == fn and r["evidence"]["pages"]


# ------------------------------------------------------------------ medications
def test_medication_rows_keep_their_own_dosage_and_doctor(run):
    doc = run["docs"]["DOC_003"]          # foreign layout, columns re-ordered: Drug | Prescribed By | Dose | How Often
    by_name = {val(m, "name"): m for m in ents(doc, "medications")}
    assert val(by_name["Metformin"], "dosage") == "500 mg"
    assert val(by_name["Lisinopril"], "dosage") == "10 mg"
    assert val(by_name["Sertraline"], "dosage") == "50 mg"
    doctors = {d["doctor_id"]: val(d, "name") for d in ents(doc, "doctors")}
    for name, dr in [("Metformin", "Dr. Alan Chen"), ("Lisinopril", "Dr. Alan Chen"), ("Sertraline", "Dr. Priya Nair")]:
        m = by_name[name]
        pb = rels(doc, "PRESCRIBED_BY", subj=m["medication_id"])
        assert len(pb) == 1 and doctors[pb[0]["object"]] == dr and pb[0]["support"] == "explicit"
    vit = by_name["Vitamin D3"]
    assert not rels(doc, "PRESCRIBED_BY", subj=vit["medication_id"])      # no doctor on that row: none inferred
    assert get_attr(vit, "frequency")["validation"]["status"] == "needs_review"   # wrapped row
    assert rels(doc, "TAKES_MEDICATION", obj=vit["medication_id"])[0]["support"] == "uncertain"


# ------------------------------------------------------------------ insurance
def test_side_by_side_primary_and_secondary_insurance_stay_separate(run):
    doc = run["docs"]["DOC_003"]
    pols = {val(p, "policy_type"): p for p in ents(doc, "insurance_policies")}
    assert val(pols["primary"], "insurance_id") == "BRH-4471920"
    assert val(pols["secondary"], "insurance_id") == "LKM-88120"
    assert val(pols["primary"], "policy_holder.name") == "Maria Rivera"
    assert val(pols["secondary"], "policy_holder.name") == "Daniel Rivera"
    for p in pols.values():
        for _, tr in iter_traces(p):
            assert tr["template_field"].startswith(p["section_id"] + ".")
    pat, spo = doc["entities"]["patient"]["patient_id"], doc["entities"]["spouse"]["spouse_id"]
    assert rels(doc, "HELD_BY", subj=pols["primary"]["policy_id"])[0]["object"] == pat
    assert rels(doc, "HELD_BY", subj=pols["secondary"]["policy_id"])[0]["object"] == spo


def test_duplicate_insurance_id_is_flagged(run):
    doc = copy.deepcopy(run["docs"]["DOC_001"])
    pols = {val(p, "policy_type"): p for p in ents(doc, "insurance_policies")}
    get_attr(pols["secondary"], "insurance_id")["value"] = val(pols["primary"], "insurance_id")
    res = check_insurance(doc)
    assert res["status"] == "review" and "same Insurance ID" in res["issues"][0]["message"]


# ------------------------------------------------------------------ family history
@pytest.mark.parametrize("doc_id,mother,father", [
    ("DOC_001", {"Hypertension", "Type 2 diabetes"}, {"Heart disease", "Stroke"}),
    ("DOC_002", {"Hypothyroidism"}, {"High cholesterol", "Gout"}),
    ("DOC_003", {"Type 2 diabetes", "hypertension"}, {"Heart disease", "stroke"}),
])
def test_mother_and_father_histories_never_mix(run, doc_id, mother, father):
    doc = run["docs"][doc_id]
    conds = {c["medical_condition_id"]: val(c, "name") for c in ents(doc, "medical_conditions")}
    fam = {val(f, "relation"): f for f in ents(doc, "family_members")}
    got = {}
    for rel_key in ("mother", "father"):
        fid = fam[rel_key]["family_member_id"]
        got[rel_key] = {conds[r["object"]] for r in rels(doc, "HAS_CONDITION", subj=fid)}
    assert got == {"mother": mother, "father": father}
    by_id = {c["medical_condition_id"]: c for c in ents(doc, "medical_conditions")}
    for r in rels(doc, "HAS_CONDITION", subj=doc["entities"]["patient"]["patient_id"]):
        assert by_id[r["object"]]["section_id"] == "medical_history"      # patient's own history only
    assert {r["object"] for r in rels(doc, "HAS_CONDITION")} == set(conds)   # every condition has an owner


# ------------------------------------------------------------------ consents
def test_consent_decisions_keep_their_permission_type(run):
    doc = run["docs"]["DOC_003"]
    by_perm = {val(c, "permission_type"): c for c in ents(doc, "consents")}
    vm, tx, em = by_perm["voicemail"], by_perm["text_message"], by_perm["email"]
    assert rels(doc, "AUTHORIZES_COMMUNICATION", subj=vm["consent_id"])[0]["object_value"] == "voicemail"
    assert rels(doc, "DECLINES_COMMUNICATION", subj=em["consent_id"])[0]["object_value"] == "email"
    assert val(tx, "decision") is None                                  # both boxes ticked
    assert not [r for r in doc["relationships"] if r["subject"] == tx["consent_id"]]
    assert rels(doc, "PROVIDES_CONSENT", obj=tx["consent_id"])[0]["support"] == "uncertain"
    rel = by_perm["release_of_health_information"]
    afm = ents(doc, "authorized_family_members")[0]
    assert rels(doc, "AUTHORIZES_DISCLOSURE_TO", subj=rel["consent_id"], obj=afm["authorized_family_member_id"])


def test_vector_checkboxes_on_flattened_form(run):
    doc = run["docs"]["DOC_002"]
    dec = {val(c, "permission_type"): val(c, "decision") for c in ents(doc, "consents")}
    assert dec == {"voicemail": "no", "text_message": "yes", "email": "yes", "release_of_health_information": "no"}
    pmh = {val(c, "name") for c in ents(doc, "medical_conditions") if c["section_id"] == "medical_history"}
    assert pmh == {"Thyroid Disorder", "Depression/Anxiety"}


# ------------------------------------------------------------------ signatures
def test_signatures_bind_to_their_own_agreement(run):
    doc = run["docs"]["DOC_003"]
    agr = {a["agreement_id"]: a for a in ents(doc, "agreements")}
    for s in ents(doc, "signatures"):
        links = rels(doc, "SIGNATURE_FOR", subj=s["signature_id"])
        assert len(links) == 1 and agr[links[0]["object"]]["section_id"] == s["section_id"]
    fin = next(a for a in agr.values() if val(a, "agreement_type") == "financial_responsibility")
    signs = rels(doc, "SIGNS", obj=fin["agreement_id"])[0]
    assert signs["support"] == "uncertain" and "Ana Lopez" in signs["uncertainty_reasons"][0]
    treat = next(a for a in agr.values() if val(a, "agreement_type") == "consent_to_treatment")
    assert rels(doc, "SIGNS", obj=treat["agreement_id"])[0]["support"] == "explicit"   # drawn ink signature


def test_unsigned_agreement_reported_not_invented(run):
    doc = run["docs"]["DOC_002"]
    fin = next(a for a in ents(doc, "agreements") if val(a, "agreement_type") == "financial_responsibility")
    assert not rels(doc, "SIGNS", obj=fin["agreement_id"])
    assert any(m["ref"] == "financial_agreement.signature" for m in doc["missing_information"])


# ------------------------------------------------------------------ status / isolation
def test_fillable_form_is_complete_and_scanned_needs_ocr(run):
    assert run["docs"]["DOC_001"]["processing"]["status"] == "completed"
    assert run["docs"]["DOC_001"]["processing"]["extraction_mode"] == "template_acroform"
    d4 = run["docs"]["DOC_004"]
    assert d4["processing"]["status"] == "needs_ocr" and d4["source"]["pages_without_text_layer"] == [1, 2]


def test_cross_document_contamination_is_detected(run):
    doc = copy.deepcopy(run["docs"]["DOC_001"])
    med = ents(doc, "medications")[0]
    get_attr(med, "dosage")["source"]["filename"] = run["docs"]["DOC_003"]["source"]["filename"]
    assert check_isolation(doc, _index(doc))["status"] == "failed"
    doc = copy.deepcopy(run["docs"]["DOC_001"])
    doc["relationships"][0]["object"] = "DR_999"
    assert check_isolation(doc, _index(doc))["status"] == "failed"


def test_ids_are_document_scoped(run):
    # DR_001 exists in several documents but is a different person in each: nothing is shared.
    names = {d: val(ents(doc, "doctors")[0], "name") for d, doc in run["docs"].items() if ents(doc, "doctors")}
    assert names["DOC_001"] == "Dr. Helen Park" and names["DOC_003"] == "Dr. Alan Chen"


# ------------------------------------------------------------------ query
def test_query_answers_the_seven_questions(run):
    doc = run["docs"]["DOC_003"]
    q = DocQuery(doc)
    mother = next(f for f in ents(doc, "family_members") if val(f, "relation") == "mother")
    cid = mother["conditions_ids"][0]
    assert q.owner(cid)["person"] == mother["family_member_id"]                    # Q3
    assert q.section(f"{cid}.name")["section_id"] == "family_history"              # Q4
    med = ents(doc, "medications")[0]
    assert q.related(f"{med['medication_id']}.dosage")["sibling_attributes"]["name"] == "Metformin"   # Q2
    b = q.between(med["medication_id"], med["prescribing_doctor_id"])              # Q5
    assert b["relation"] == "direct" and b["support"] == "explicit"
    rid = rels(doc, "PRESCRIBED_BY", subj=med["medication_id"])[0]["relationship_id"]
    ev = q.evidence(rid)                                                            # Q6 / Q7
    assert ev["pages"] == [1] and ev["support"] == "explicit"
    assert q.owner("INS_002.policy_holder.name")["person_type"] == "spouse"


# ------------------------------------------------------------------ standardized PDF
def _fields(path):
    d = pymupdf.open(path)
    out = {w.field_name: w.field_value for p in d for w in p.widgets()}
    d.close()
    return out


def test_standardized_pdf_is_populated_by_structure(run):
    a = _fields(run["out"] / "standardized" / "DOC_001_standardized.pdf")
    assert a["med_2_name"] == "Metformin" and a["med_2_dosage"] == "500 mg"
    assert a["med_3_prescribing_doctor"] == "Dr. Samir Gupta"
    assert a["sec_ins_id"] == "LMI-2209381" and a["pri_ins_id"] == "HHP-55821907"
    assert a["fh_father_conditions"] == "Heart disease, Stroke"
    assert a["cons_email_no"] not in ("Off", False, None) and a["cons_email_yes"] in ("Off", False, None)
    c = _fields(run["out"] / "standardized" / "DOC_003_standardized.pdf")
    assert c["med_1_prescribing_doctor"] == "Dr. Alan Chen"
    assert c["med_4_name"] == "Vitamin D3" and c["med_4_frequency"] == ""        # withheld pending review
    assert c["cons_text_yes"] in ("Off", False, None) and c["cons_text_no"] in ("Off", False, None)
    assert c["sec_ins_holder_name"] == "Daniel Rivera" and c["pri_ins_holder_name"] == "Maria Rivera"
