"""Generate fictional sample intake PDFs that exercise every extraction path.

  A  intake_A_fillable_okafor.pdf      reference template filled in its form fields (AcroForm path)
  B  intake_B_flattened_lindqvist.pdf  reference template filled then flattened (layout path,
                                       vector checkboxes); contains an invalid e-mail
  C  intake_C_other_clinic_rivera.pdf  a different clinic's layout: other headings, side-by-side
                                       primary/secondary insurance, re-ordered medication columns,
                                       a wrapped medication row, 'Mother:/Father:' family lines,
                                       an ambiguous consent (both boxes ticked), a hand-drawn
                                       signature, and an agreement signed by someone else
  D  intake_D_scanned_rivera.pdf       image-only scan of C (no text layer -> needs OCR)

All people, organisations, phone numbers (555-01xx) and IDs are invented.
    python tools/make_samples.py [output_dir]
"""
from __future__ import annotations

import sys
from pathlib import Path

import pymupdf

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_PDF = ROOT / "templates" / "patient_intake_v1" / "reference_template.pdf"

A = {
    "pat_first_name": "Samuel", "pat_last_name": "Okafor", "pat_dob": "04/12/1979", "pat_sex": "Male",
    "pat_marital_status": "Married", "pat_phone": "(312) 555-0148", "pat_address": "1180 Lakeview Ave, Apt 4B",
    "pat_city": "Chicago", "pat_state": "IL", "pat_zip": "60614", "pat_email": "s.okafor@example.com",
    "spo_name": "Adaeze Okafor", "spo_dob": "09/30/1981", "spo_phone": "312-555-0172",
    "emc_name": "Chinedu Okafor", "emc_relationship": "Brother", "emc_phone": "773-555-0119",
    "pri_ins_company": "Harbor Health Plan", "pri_ins_phone": "800-555-0134", "pri_ins_holder_name": "Samuel Okafor",
    "pri_ins_holder_relationship": "Self", "pri_ins_holder_dob": "04/12/1979", "pri_ins_id": "HHP-55821907",
    "pri_ins_group": "GRP-40017",
    "sec_ins_company": "Lakeside Mutual Insurance", "sec_ins_phone": "800-555-0177", "sec_ins_holder_name": "Adaeze Okafor",
    "sec_ins_holder_relationship": "Spouse", "sec_ins_holder_dob": "09/30/1981", "sec_ins_id": "LMI-2209381",
    "sec_ins_group": "7781-B",
    "med_1_name": "Atorvastatin", "med_1_dosage": "20 mg", "med_1_frequency": "Once daily at bedtime",
    "med_1_prescribing_doctor": "Dr. Helen Park",
    "med_2_name": "Metformin", "med_2_dosage": "500 mg", "med_2_frequency": "Twice daily with meals",
    "med_2_prescribing_doctor": "Dr. Helen Park",
    "med_3_name": "Albuterol inhaler", "med_3_dosage": "90 mcg, 2 puffs", "med_3_frequency": "As needed",
    "med_3_prescribing_doctor": "Dr. Samir Gupta",
    "alg_1_allergen": "Penicillin", "alg_1_reaction": "Hives", "alg_1_severity": "Moderate",
    "alg_2_allergen": "Peanuts", "alg_2_reaction": "Anaphylaxis", "alg_2_severity": "Life-threatening",
    "pmh_diabetes": True, "pmh_hypertension": True, "pmh_asthma": True, "pmh_other": "Sleep apnea",
    "surg_1_procedure": "Appendectomy", "surg_1_date": "2004", "surg_1_hospital": "Mercy General Hospital",
    "surg_2_procedure": "Knee arthroscopy", "surg_2_date": "06/2017", "surg_2_hospital": "Northside Surgical Center",
    "fh_mother_living_status": "Living", "fh_mother_age": "71", "fh_mother_conditions": "Hypertension, Type 2 diabetes",
    "fh_father_living_status": "Deceased", "fh_father_age": "68", "fh_father_conditions": "Heart disease, Stroke",
    "fh_siblings_living_status": "Living", "fh_siblings_conditions": "Asthma",
    "cons_voicemail_yes": True, "cons_text_yes": True, "cons_email_no": True,
    "rel_authorized_yes": True,
    "auth_1_name": "Adaeze Okafor", "auth_1_relationship": "Spouse", "auth_1_phone": "312-555-0172",
    "agr_treatment_signature": "Samuel Okafor", "agr_treatment_printed_name": "Samuel Okafor", "agr_treatment_date": "10/28/2026",
    "agr_hipaa_signature": "Samuel Okafor", "agr_hipaa_printed_name": "Samuel Okafor", "agr_hipaa_date": "10/28/2026",
    "agr_financial_signature": "Samuel Okafor", "agr_financial_printed_name": "Samuel Okafor", "agr_financial_date": "10/28/2026",
}

B = {
    "pat_first_name": "Grace", "pat_last_name": "Lindqvist", "pat_dob": "11/02/1990", "pat_sex": "F",
    "pat_marital_status": "Single", "pat_phone": "651-555-0163", "pat_address": "77 Birch Hollow Road",
    "pat_city": "Saint Paul", "pat_state": "Minnesota", "pat_zip": "55105", "pat_email": "grace.lindqvist@example",
    "emc_name": "Ingrid Lindqvist", "emc_relationship": "Mother", "emc_phone": "651-555-0110",
    "pri_ins_company": "Northstar Health Cooperative", "pri_ins_phone": "888-555-0102", "pri_ins_holder_name": "Grace Lindqvist",
    "pri_ins_holder_relationship": "Self", "pri_ins_holder_dob": "11/02/1990", "pri_ins_id": "NHC-30019284",
    "pri_ins_group": "88213",
    "med_1_name": "Levothyroxine", "med_1_dosage": "75 mcg", "med_1_frequency": "Every morning",
    "med_1_prescribing_doctor": "Dr. Erik Holm",
    "med_2_name": "Ibuprofen", "med_2_dosage": "200 mg", "med_2_frequency": "As needed for pain",
    "alg_1_allergen": "Sulfa drugs", "alg_1_reaction": "Rash", "alg_1_severity": "Mild",
    "pmh_thyroid_disorder": True, "pmh_depression_anxiety": True,
    "fh_mother_living_status": "Living", "fh_mother_age": "62", "fh_mother_conditions": "Hypothyroidism",
    "fh_father_living_status": "Living", "fh_father_age": "64", "fh_father_conditions": "High cholesterol, Gout",
    "cons_voicemail_no": True, "cons_text_yes": True, "cons_email_yes": True,
    "rel_authorized_no": True,
    "agr_treatment_signature": "Grace Lindqvist", "agr_treatment_printed_name": "Grace Lindqvist", "agr_treatment_date": "11/05/2026",
    "agr_hipaa_signature": "Grace Lindqvist", "agr_hipaa_date": "11/05/2026",
}


def fill_template(values: dict, out: Path, flatten: bool = False):
    doc = pymupdf.open(TEMPLATE_PDF)
    for page in doc:
        for w in page.widgets():
            if w.field_name in values:
                v = values[w.field_name]
                w.field_value = (w.on_state() if v else "Off") if isinstance(v, bool) else str(v)
                w.update()
    if flatten:
        doc.bake()
    doc.set_metadata({"title": "Patient intake (sample)", "creator": "docintel tools/make_samples.py"})
    doc.save(out, garbage=3, deflate=True)


# ---------------------------------------------------------------- foreign layout (C)
class Page:
    def __init__(self, doc):
        self.p = doc.new_page(width=612, height=792)

    def t(self, x, y, s, size=9.5, bold=False, color=(0.08, 0.08, 0.1)):
        self.p.insert_text((x, y), s, fontsize=size, fontname="hebo" if bold else "helv", color=color)

    def h(self, y, s, x=40, x1=572):
        self.t(x, y, s, 10.5, True, (0.45, 0.12, 0.12))
        self.p.draw_line((x, y + 3.5), (x1, y + 3.5), color=(0.45, 0.12, 0.12), width=0.7)

    def ink(self, x, y):
        """A free-hand signature squiggle drawn as Bezier curves."""
        s = self.p.new_shape()
        pts = [(x, y), (x + 14, y - 12), (x + 22, y + 6), (x + 34, y - 9), (x + 48, y + 4), (x + 60, y - 10),
               (x + 74, y + 2), (x + 88, y - 6), (x + 104, y + 1)]
        for i in range(0, len(pts) - 3, 3):
            s.draw_bezier(pts[i], pts[i + 1], pts[i + 2], pts[i + 3])
        s.draw_bezier(pts[-4], pts[-3], pts[-2], pts[-1])
        s.finish(color=(0.05, 0.1, 0.35), width=1.1, closePath=False)
        s.commit()


def make_other_clinic(out: Path):
    doc = pymupdf.open()
    a = Page(doc)
    a.t(40, 48, "EXAMPLE RIVERSIDE CLINIC", 14, True, (0.45, 0.12, 0.12))
    a.t(40, 62, "New Patient Registration Packet  -  please print clearly", 8.5, color=(0.4, 0.4, 0.4))

    a.h(90, "PATIENT DEMOGRAPHICS")
    a.t(40, 110, "Patient Name: Rivera, Maria L.")
    a.t(330, 110, "DOB: 7/14/1985")
    a.t(460, 110, "Gender: Female")
    a.t(40, 126, "Street Address: 42 Elm Street")
    a.t(330, 126, "Marital Status: Married")
    a.t(40, 142, "City: Springfield   State: IL   Zip: 62704")
    a.t(40, 158, "Mobile Phone: 217.555.0143")
    a.t(330, 158, "E-mail: maria.rivera@example.com")

    a.h(186, "SPOUSE / PARTNER")
    a.t(40, 205, "Name: Daniel Rivera")
    a.t(250, 205, "Date of Birth: 03-22-1983")
    a.t(420, 205, "Phone: (217) 555-0190")

    a.h(232, "IN CASE OF EMERGENCY")
    a.t(40, 251, "Contact Name: Ana Lopez")
    a.t(250, 251, "Relationship: Sister")
    a.t(420, 251, "Phone: 217-555-0177")

    a.h(279, "PRIMARY INSURANCE", 40, 292)
    a.h(279, "SECONDARY INSURANCE", 316, 572)
    left = ["Carrier: Blue River Health", "Subscriber Name: Maria Rivera", "Relationship to Patient: Self",
            "Subscriber DOB: 07/14/1985", "Member ID: BRH-4471920", "Group #: GRP-2210", "Phone: 800-555-0101"]
    right = ["Carrier: Lakeside Mutual", "Subscriber Name: Daniel Rivera", "Relationship to Patient: Spouse",
             "Subscriber DOB: 03/22/1983", "Member ID: LKM-88120", "Group #: 5590-A", "Phone: 800-555-0199"]
    for i, (l, r) in enumerate(zip(left, right)):
        a.t(40, 298 + i * 15, l)
        a.t(316, 298 + i * 15, r)

    a.h(420, "MEDICATION LIST")
    a.t(40, 435, "Please include vitamins and supplements.", 8.5, color=(0.4, 0.4, 0.4))
    cols = [40, 200, 340, 430]
    for x, s in zip(cols, ["Drug", "Prescribed By", "Dose", "How Often"]):
        a.t(x, 452, s, 9, True)
    meds = [("Metformin", "Dr. Alan Chen", "500 mg", "Twice daily"),
            ("Lisinopril", "Dr. Alan Chen", "10 mg", "Once daily"),
            ("Sertraline", "Dr. Priya Nair", "50 mg", "Once daily"),
            ("Vitamin D3", "", "1000 IU", "Daily")]
    y = 470
    for row in meds:
        for x, s in zip(cols, row):
            if s:
                a.t(x, y, s)
        y += 18
    a.t(430, y - 8, "(with breakfast)")          # wrapped continuation of the Vitamin D3 row

    a.h(560, "DRUG ALLERGIES")
    for x, s in zip([40, 200, 360], ["Allergen", "Reaction", "Severity"]):
        a.t(x, 577, s, 9, True)
    for i, row in enumerate([("Penicillin", "Hives", "Moderate"), ("Shellfish", "Throat swelling", "Severe")]):
        for x, s in zip([40, 200, 360], row):
            a.t(x, 595 + i * 18, s)

    a.h(650, "MEDICAL CONDITIONS")
    opts1 = [("[X]", "Diabetes"), ("[X]", "Hypertension"), ("[ ]", "Asthma"), ("[ ]", "Heart Disease")]
    opts2 = [("[ ]", "Cancer"), ("[ ]", "Thyroid Disorder"), ("[X]", "Depression/Anxiety"), ("[ ]", "Arthritis")]
    for row, yy in ((opts1, 669), (opts2, 686)):
        for i, (m, s) in enumerate(row):
            a.t(40 + i * 133, yy, f"{m} {s}")
    a.t(40, 704, "Other: Migraines")
    a.t(40, 770, "Riverside registration packet  |  Page 1 of 2", 7.5, color=(0.5, 0.5, 0.5))

    b = Page(doc)
    b.h(50, "SURGICAL HISTORY")
    for x, s in zip([40, 250, 330], ["Surgery", "Year", "Facility"]):
        b.t(x, 67, s, 9, True)
    for i, row in enumerate([("Appendectomy", "2009", "Springfield General Hospital"),
                             ("Cesarean section", "2016", "St. Mary's Medical Center")]):
        for x, s in zip([40, 250, 330], row):
            b.t(x, 85 + i * 18, s)

    b.h(140, "FAMILY HISTORY")
    b.t(40, 159, "Mother: Living, age 66 - Type 2 diabetes, hypertension")
    b.t(40, 175, "Father: Deceased, age 70 - Heart disease, stroke")

    b.h(205, "COMMUNICATION PREFERENCES")
    b.t(40, 224, "May we leave a voicemail?   [X] Yes   [ ] No")
    b.t(40, 240, "May we send text messages?   [X] Yes   [X] No")
    b.t(40, 256, "May we contact you by email?   [ ] Yes   [X] No")

    b.h(286, "RELEASE OF INFORMATION")
    b.t(40, 305, "I authorize release of my health information to the individuals listed below:   [X] Yes   [ ] No")
    for x, s in zip([40, 250, 400], ["Name", "Relationship", "Phone"]):
        b.t(x, 323, s, 9, True)
    for x, s in zip([40, 250, 400], ["Daniel Rivera", "Husband", "217-555-0190"]):
        b.t(x, 341, s)

    paras = {
        "CONSENT FOR TREATMENT": "I consent to evaluation and treatment by Riverside providers.",
        "PRIVACY PRACTICES ACKNOWLEDGMENT": "I received the clinic's notice describing how health information is used.",
        "FINANCIAL POLICY": "I accept responsibility for charges not covered by insurance.",
    }
    y = 375
    for i, (head, para) in enumerate(paras.items()):
        b.h(y, head)
        b.t(40, y + 17, para, 8.5, color=(0.35, 0.35, 0.35))
        if i == 0:
            b.t(40, y + 40, "Patient Signature:")
            b.ink(128, y + 37)
            b.t(300, y + 40, "Print Name: Maria Rivera")
            b.t(470, y + 40, "Date: 11/03/2026")
        elif i == 1:
            b.t(40, y + 40, "Signature: /s/ Maria Rivera")
            b.t(470, y + 40, "Date: 11/03/2026")
        else:
            b.t(40, y + 40, "Signature:")
            b.ink(95, y + 37)
            b.t(300, y + 40, "Print Name: Ana Lopez")
            b.t(470, y + 40, "Date: 11/03/2026")
        y += 72
    b.t(40, 770, "Riverside registration packet  |  Page 2 of 2", 7.5, color=(0.5, 0.5, 0.5))
    doc.set_metadata({"title": "Riverside registration (sample)", "creator": "docintel tools/make_samples.py"})
    doc.save(out, garbage=3, deflate=True)


def make_scan(src: Path, out: Path):
    s = pymupdf.open(src)
    d = pymupdf.open()
    for p in s:
        pix = p.get_pixmap(dpi=110, colorspace=pymupdf.csGRAY)
        np = d.new_page(width=p.rect.width, height=p.rect.height)
        np.insert_image(np.rect, pixmap=pix)
    d.save(out, garbage=3, deflate=True)


def main(out_dir: Path):
    out_dir.mkdir(parents=True, exist_ok=True)
    fill_template(A, out_dir / "intake_A_fillable_okafor.pdf")
    fill_template(B, out_dir / "intake_B_flattened_lindqvist.pdf", flatten=True)
    make_other_clinic(out_dir / "intake_C_other_clinic_rivera.pdf")
    make_scan(out_dir / "intake_C_other_clinic_rivera.pdf", out_dir / "intake_D_scanned_rivera.pdf")
    for f in sorted(out_dir.glob("*.pdf")):
        print(f"written: {f}")


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "samples" / "input")
