# docintel: relationship-aware intake PDF extraction

docintel turns each intake PDF into **one semantically structured JSON document**. Every value in it:

- belongs to the right **entity**: patient, policy, medication, mother, consent, signature, and so on
- is linked by explicit **relationships** such as `PAT_001 TAKES_MEDICATION MED_002`
- is **traceable** to its source: the label as printed, page, bounding box, extraction method, confidence and validation status
- is **validated**, with uncertain links sent to a manual-review queue instead of being guessed

The same structured representation then **populates the reference template PDF**.

```
PDF ─┬─ fillable on reference template ─► AcroForm extractor ─┐   (row binding from field names)
     └─ flattened / foreign layout / OCR ► Layout extractor ───┤   (row binding from geometry)
                                                               ▼
                     record-bound observations (section, record, attribute, trace)
                                                               ▼
                 Entity builder ─► entities + relationships (explicit | uncertain)
                                                               ▼
                 Relationship validation ─► DOC_xxx.json ─► standardized reference PDF
```

## Quick start

```bash
pip install -r requirements-dev.txt            # requirements.txt alone is enough to run the app
python tools/build_reference_template.py        # stand-in reference template (see note below)
python tools/make_samples.py                    # 4 fictional sample PDFs -> samples/input
python -m docintel process samples/input -o samples/output
python -m pytest -q                             # 18 end-to-end tests
```

Outputs:

| Path | Content |
|---|---|
| `output/json/DOC_001.json` … | one JSON per source PDF; never merged across files |
| `output/standardized/DOC_001_standardized.pdf` | reference template populated from that JSON |
| `output/batch_index.json` | document IDs, source file + SHA-256, status, counts, fill report |
| `output/review_queue.json` | every manual-review item across the batch, tagged with its document |

> **The reference PDF was not supplied.** `templates/patient_intake_v1/` holds a stand-in that covers every entity in the requirements. See [Adapting to the real reference PDF](#adapting-to-the-real-reference-pdf); no code changes are needed.

## Web UI

```bash
python -m docintel serve          # http://localhost:8000  (127.0.0.1 only; data kept in ./web_data)
```

- Drag PDFs in to upload them; each file is processed on its own.
- Browse entities, relationships (filterable to the uncertain ones), the review queue, missing items and validation checks.
- Click any value, relationship or review item to see it highlighted on the source page: value boxes are solid, printed labels and headings are dashed.
- Switch the viewer to the standardized PDF.
- Use the Ask tab for the seven relationship questions.
- Download the JSON or the PDFs.

The first start loads the four sample PDFs; use `--no-seed` to start empty.

### Walkthrough video

[video/docintel_how_it_works.mp4](video/docintel_how_it_works.mp4) is a captioned walkthrough of about 2 minutes (1080p, H.264). To regenerate it, keep `serve` running, then:

```bash
pip install playwright imageio-ffmpeg opencv-python
python tools/make_demo_video.py                 # recaptures the live app; add --work DIR --reuse to re-render only
```

## Deploy: Netlify + Render

Netlify serves static files and its Functions don't run Python, so the app is split in two. Both halves deploy automatically from this GitHub repo on every push to `main`.

| Part | Host | Config | What it serves |
|---|---|---|---|
| Web page | Netlify | [netlify.toml](netlify.toml) | `docintel/web/index.html`; `/api/*` and `/files/*` are proxied to Render |
| Python API | Render (free web service) | [render.yaml](render.yaml) | PDF processing, JSON, page images, standardized PDFs |

1. **Render first**, because Netlify needs its address:
   1. On dashboard.render.com, choose **New → Blueprint**, connect GitHub and pick this repo.
   2. Click **Apply**. The service `docintel-api` builds and starts, and is checked at `/api/documents`.
   3. Note its URL. If it isn't `https://docintel-api.onrender.com`, put the real one in both `to =` lines of `netlify.toml` and push.
2. **Netlify:**
   1. On app.netlify.com, choose **Add new project → Import an existing project → GitHub**.
   2. Authorize the Netlify GitHub app (all repos, or just this one) and pick the repo.
   3. The settings are read from `netlify.toml` (publish directory `docintel/web`), so click **Deploy**.
   4. Optionally rename the site under **Project configuration → Change project name**.
3. **After that:** every `git push` redeploys both. Pull requests get Netlify deploy previews.

Notes:
- **Cold starts:** Render's free tier sleeps after about 15 idle minutes. The page shows "Starting the server…" and retries until it wakes, which takes up to about a minute.
- **Storage:** the free tier's disk is temporary. Uploads disappear on restart or redeploy, and the four sample PDFs are re-seeded.
- **Public demo mode:** `render.yaml` sets `DOCINTEL_PUBLIC_DEMO=1`. The page then tells visitors to upload only fictional data, and the sample documents can't be deleted. There is no login, so never upload real patient documents to a public deployment.

## How relationships are preserved

The rule: an attribute joins an entity only if it was observed in the **same section and the same record**.

- **Section** is the page region under a recognised heading. Regions are rectangles, so headings printed side by side (Primary | Secondary Insurance) get separate regions and cannot merge.
- **Record** is the table row, the family-history row (Mother/Father…), the consent question, the checklist option, or the AcroForm row encoded in the field name (`med_3_dosage`).
- Table columns are located from the source's own header text, so `Drug | Prescribed By | Dose | How Often` maps correctly even when re-ordered.
- **Same label, different owner:** `Name`, `Phone` and `Date of Birth` resolve by section context. A phone under *Emergency Contact* belongs to the emergency contact, not the patient.
- **Nothing is inferred across records.** A medication row with no doctor gets no `PRESCRIBED_BY`. A wrapped row, an overflowing cell, a both-ticked Yes/No, or a signature whose printed name differs from the patient produces a relationship with `"support": "uncertain"` and a review item.

### Entities and predicates (from `template.json`)

`patient · spouse · emergency_contacts · insurance_policies · insurance_companies · medications · doctors · allergies · medical_conditions · surgeries · hospitals · family_members · consents · authorized_family_members · agreements · signatures`

| Predicate | Subject → Object |
|---|---|
| `HAS_INSURANCE`, `HELD_BY`, `ISSUED_BY` | patient → policy; policy → patient/spouse; policy → company |
| `TAKES_MEDICATION`, `PRESCRIBED_BY` | patient → medication; medication → doctor |
| `HAS_ALLERGY`, `HAD_SURGERY`, `PERFORMED_AT` | patient → allergy / surgery; surgery → hospital |
| `HAS_CONDITION` | patient **or** family member → condition (each condition has exactly one owner) |
| `HAS_FAMILY_HISTORY`, `HAS_SPOUSE`, `HAS_EMERGENCY_CONTACT` | patient → person |
| `PROVIDES_CONSENT`, `AUTHORIZES_COMMUNICATION` / `DECLINES_COMMUNICATION` | patient → consent; consent → channel (literal) |
| `AUTHORIZES_DISCLOSURE_TO`, `HAS_AUTHORIZED_FAMILY_MEMBER` | consent → authorized person; patient → authorized person |
| `SIGNATURE_FOR`, `SIGNS` | signature → agreement; patient → agreement |

## Output format (abridged `DOC_003.json`)

```jsonc
{
  "document_id": "DOC_003", "document_type": "patient_intake",
  "source": {"filename": "intake_C_other_clinic_rivera.pdf", "sha256": "…", "page_count": 2},
  "processing": {"status": "needs_review", "extraction_mode": "layout", "coordinate_system": "PDF points; origin top-left"},
  "sections": [{"section_id": "primary_insurance", "source_heading": "PRIMARY INSURANCE", "pages": [1],
                "heading_location": {"page": 1, "bbox": [40.0, 267.8, 153.7, 282.2]}, "entity_ids": ["INS_001", "INSCO_001"]}],
  "entities": {
    "medications": [{
      "medication_id": "MED_001", "entity_type": "medication", "section_id": "medications",
      "record": {"part": "table", "record_key": "row1", "index": 1},
      "name":   {"value": "Metformin", "field_label": "Drug",
                 "source": {"filename": "…rivera.pdf", "page": 1, "bbox": [40.0, 459.8, 82.2, 472.8], "label_bbox": [40.0, 442.4, 61.0, 454.8]},
                 "extraction_method": "layout_table", "confidence": 0.9, "validation": {"status": "valid", "messages": []}},
      "dosage": {"value": "500 mg", "field_label": "Dose", "…": "…"},
      "prescribing_doctor": {"value": "Dr. Alan Chen", "field_label": "Prescribed By", "…": "…"},
      "prescribing_doctor_id": "DR_001"
    }]
  },
  "relationships": [{
    "relationship_id": "REL_007", "subject": "MED_001", "predicate": "PRESCRIBED_BY", "object": "DR_001",
    "support": "explicit", "confidence": 0.9,
    "basis": "'Prescribed By' recorded on the same table row as the medication",
    "evidence": {"source_file": "…rivera.pdf", "pages": [1], "locations": [ "heading", "name cell", "doctor cell (bboxes)" ]}
  }],
  "missing_information": [{"ref": "MED_004", "severity": "incomplete_record", "reason": "record 4 has no value for: Prescribing Doctor"}],
  "validation": {"status": "needs_review", "checks": [ … 7 checks … ], "errors": [], "warnings": [ … ]},
  "manual_review": [{"review_id": "REV_007", "kind": "relationship", "ref": "REL_045",
                     "relationship": "PAT_001 SIGNS AGR_003",
                     "reason": "printed name 'Ana Lopez' differs from patient 'Maria Rivera'; the signer may be a guardian or representative"}],
  "unmapped_content": []
}
```

The formal contract is [schema/document.schema.json](schema/document.schema.json); the tests validate every output against it.

## Relationship validation

Each document runs these independent checks, listed in `validation.checks`:

| Check | Guarantees |
|---|---|
| `document_isolation` | every value and relationship comes from this file; every referenced ID exists here |
| `predicate_signatures` | e.g. `PRESCRIBED_BY` only links a medication to a doctor |
| `medication_record_integrity` | name, dosage and doctor share one row/page; one doctor per medication |
| `insurance_policy_separation` | at most one primary/secondary; no value crosses from one policy's section to the other; duplicate IDs flagged |
| `family_history_separation` | every condition has exactly one owner; family conditions never attach to the patient and vice versa |
| `consent_decision_integrity` | each decision keeps its permission type; yes/no matches `AUTHORIZES_`/`DECLINES_COMMUNICATION` |
| `signature_agreement_binding` | each signature belongs to exactly one agreement from its own section; date sanity |

Document status is one of `completed`, `completed_with_warnings`, `needs_review`, `needs_ocr` or `failed_validation`.

## Asking questions of a document

```bash
J=samples/output/json/DOC_003.json
python -m docintel summary $J                       # readable overview
python -m docintel ask $J entity  MED_002           # 1 what belongs to this entity
python -m docintel ask $J related MED_003.dosage    # 2 what is related to this field
python -m docintel ask $J owner   COND_006          # 3 which person (-> FAM_001 Mother)
python -m docintel ask $J section COND_006.name     # 4 which section
python -m docintel ask $J between MED_002 DR_001    # 5 relationship between two elements
python -m docintel ask $J evidence REL_045          # 6 source page + 7 explicit/uncertain
python -m docintel ask $J find hypertension         # patient's vs mother's, kept apart
```

## Standardized PDF

`process` fills the reference template from each JSON. You can also run it alone: `python -m docintel generate DOC_003.json`.

- Values are placed by **structure**: medication row *i* goes to `med_i_*`, the mother record to `fh_mother_*`, the secondary policy to `sec_ins_*`.
- Values pending review are **withheld** (pass `--include-uncertain` to print them). The fill report in `batch_index.json` counts withheld fields and records that did not fit (for example a 6th medication when the template has 5 rows).

## Adapting to the real reference PDF

1. Inventory it:
   `python -m docintel scan-template path/to/reference.pdf -o scan.json`
   For a fillable PDF this lists every field with its page, rectangle, printed label, the section it sits in, and a suggested template slot. On the stand-in, all 119 suggestions match the true bindings.
2. In `templates/<id>/template.json`, set each item's `acroform` name (or a table's `acroform_pattern`) to the real field names. Then add the real headings and labels to `aliases`.
3. Copy the real PDF to `templates/<id>/reference_template.pdf`, or point `reference_pdf` at it, and stop running `build_reference_template.py`.
4. For new source-form vendors, add their headings and labels as `aliases`. Unrecognised headings are never guessed: their content is reported as unsectioned or unmapped.

## Known limitations

- **OCR:** scanned pages need Tesseract plus `pytesseract`. Without them a scanned PDF is reported as `needs_ocr` and queued for review. The OCR adapter (`docintel/ocr.py`) was not exercised here because Tesseract is not installed. Handwriting is outside what Tesseract reads well.
- **Layout:** these are not yet handled:
  - table headers wrapped over two lines
  - right-aligned numeric columns (assignment uses the column's left edge)
  - key/value forms without section headings
- **Dates** are read as US `MM/DD/YYYY`; ambiguous ones are annotated in `validation.messages`.
- **Signatures:** only presence is detected (typed text, image or ink strokes). They are not verified.
- **Checkboxes:** printed (`[X]`, `☒`, `✔`) and vector-drawn ones (detected by measuring the box interior) are supported. Checkboxes that exist only inside a scanned image need OCR plus a checkbox detector.

## Layout

```
docintel/        template.py  pdfsource.py  layout.py  extract_acroform.py  extract_layout.py
                 validators.py  builder.py  validate.py  generate.py  query.py  scan.py  pipeline.py  cli.py  ocr.py
templates/patient_intake_v1/   template.json (definition) + reference_template.pdf (stand-in)
schema/          document.schema.json
tools/           build_reference_template.py  make_samples.py
tests/           test_pipeline.py
samples/         input/ (fictional PDFs)  output/ (their JSON, standardized PDFs, index, review queue)
```
