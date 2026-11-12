"""Per-document processing and batch orchestration."""
from __future__ import annotations

import datetime as dt
import json
from collections import Counter
from pathlib import Path

from . import SCHEMA_VERSION, __version__
from .builder import DocumentBuilder, iter_traces
from .extract_acroform import extract_acroform, is_template_form
from .extract_layout import LayoutExtractor, widget_words
from .layout import build_lines, detect_headings, strip_page_furniture
from .models import ExtractionResult, round_bbox
from .ocr import ocr_page_words
from .pdfsource import SourcePDF
from .template import Template
from .validate import validate_document


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def _annotate_headings(src: SourcePDF, template: Template, res: ExtractionResult):
    """In AcroForm mode, still locate the printed headings so section evidence has a bbox."""
    lines = {p: build_lines(src.text_words(p)) for p in range(1, src.page_count + 1)}
    lines = strip_page_furniture(lines, src.page_sizes)
    for h in detect_headings(lines, template):
        ev = res.sections.get(h.section_id)
        if ev and not ev.source_heading:
            ev.source_heading = h.text
            ev.heading_bboxes.insert(0, (h.page, h.bbox))


def _sections_json(template: Template, res: ExtractionResult, b: DocumentBuilder) -> list[dict]:
    out = []
    for s in template.sections:
        sid = s["section_id"]
        ev = res.sections.get(sid)
        ents = [eid for eid, e in b.by_id.items() if e["section_id"] == sid]
        if not ev and not ents:
            continue
        pages = set(ev.pages | ev.continuation_pages) if ev else set()
        for eid in ents:
            pages |= set(b.by_id[eid].get("source_pages", []))
        hp, hb = (ev.heading_bboxes[0] if ev and ev.heading_bboxes else (None, None))
        out.append({"section_id": sid, "title": s["title"],
                    "source_heading": ev.source_heading if ev else None,
                    "identified_by": ev.method if ev else "values_only",
                    "pages": sorted(p for p in pages if p),
                    "heading_location": {"page": hp, "bbox": round_bbox(hb)} if hp else None,
                    "entity_ids": ents})
    return out


def process_document(path: str | Path, doc_id: str, template: Template, *, use_ocr: bool = True) -> dict:
    src = SourcePDF(path)
    try:
        res = ExtractionResult(mode="none")
        widgets = src.widgets()
        if widgets and is_template_form(widgets, template):
            res.mode = "template_acroform"
            extract_acroform(src, widgets, template, res)
            _annotate_headings(src, template, res)
        else:
            words = {}
            for p in range(1, src.page_count + 1):
                ws = src.text_words(p)
                if not ws and src.images(p):
                    ocr = ocr_page_words(src.doc, p) if use_ocr else None
                    if ocr:
                        ws = ocr
                        res.ocr_pages.append(p)
                    else:
                        res.pages_without_text.append(p)
                if ws:
                    boxes, inside = src.checkbox_words(p, ws)
                    ws = [w for w in ws if id(w) not in inside] + boxes
                words[p] = ws
            res.mode = "layout"
            if widgets:
                for p, ws in widget_words(widgets).items():
                    words.setdefault(p, []).extend(ws)
                res.mode = "layout+foreign_acroform"
            if res.ocr_pages:
                res.mode += "+ocr"
            if any(words.values()):
                LayoutExtractor(src, template, res).run(words)
        if res.pages_without_text:
            res.warnings.append({"code": "needs_ocr",
                                 "message": f"page(s) {res.pages_without_text} contain only images; install Tesseract "
                                            f"and pytesseract to OCR them"})

        b = DocumentBuilder(template, doc_id, src.filename, res)
        b.build()
        doc = {
            "schema_version": SCHEMA_VERSION,
            "document_id": doc_id,
            "document_type": template.document_type,
            "template_id": template.template_id,
            "source": {"filename": src.filename, "sha256": src.sha256, "page_count": src.page_count,
                       "has_acroform": bool(widgets), "pages_without_text_layer": res.pages_without_text,
                       "ocr_pages": res.ocr_pages},
            "processing": {"engine": f"docintel {__version__}", "processed_at": _now(), "extraction_mode": res.mode,
                           "review_threshold": template.review_threshold, "status": None,
                           "coordinate_system": "PDF points; origin top-left; bbox = [x0, y0, x1, y1]"},
            "sections": _sections_json(template, res, b),
            "entities": b.entities_json(),
            "relationships": b.rels,
            "missing_information": b.missing,
            "validation": {},
            "manual_review": [],
            "unmapped_content": res.unmapped,
        }
        validate_document(doc, template, b)
        return doc
    finally:
        src.close()


def _counts(doc: dict) -> dict:
    n_ent = sum(len(v) if isinstance(v, list) else (1 if v else 0) for v in doc["entities"].values())
    n_attr = 0
    for v in doc["entities"].values():
        for e in ([v] if isinstance(v, dict) else (v or [])):
            n_attr += sum(1 for _ in iter_traces(e))
    return {"entities": n_ent, "attributes": n_attr, "relationships": len(doc["relationships"]),
            "uncertain_relationships": sum(1 for r in doc["relationships"] if r["support"] == "uncertain"),
            "manual_review_items": len(doc["manual_review"]), "missing_items": len(doc["missing_information"]),
            "warnings": len(doc["validation"]["warnings"]), "errors": len(doc["validation"]["errors"])}


def collect_inputs(inputs: list[str | Path]) -> list[Path]:
    files: list[Path] = []
    for i in inputs:
        p = Path(i)
        if p.is_dir():
            files += [f for f in p.iterdir() if f.suffix.lower() == ".pdf"]
        elif p.suffix.lower() == ".pdf":
            files.append(p)
    seen, out = set(), []
    for f in sorted(files, key=lambda f: f.name.lower()):
        if f.resolve() not in seen:
            seen.add(f.resolve())
            out.append(f)
    return out


def process_batch(inputs, out_dir, template: Template, *, generate_pdf: bool = True,
                  include_uncertain: bool = False, use_ocr: bool = True, log=print) -> dict:
    from .generate import generate_standardized_pdf

    out = Path(out_dir)
    (out / "json").mkdir(parents=True, exist_ok=True)
    if generate_pdf:
        (out / "standardized").mkdir(parents=True, exist_ok=True)
    files = collect_inputs(inputs)
    started = _now()
    index = {"batch_id": "BATCH_" + started.replace(":", "").replace("-", "")[:15], "created_at": started,
             "template_id": template.template_id, "engine": f"docintel {__version__}",
             "output_dir": str(out), "documents": []}
    queue = []
    for i, f in enumerate(files, 1):
        doc_id = f"DOC_{i:03d}"
        entry = {"document_id": doc_id, "source_filename": f.name}
        try:
            doc = process_document(f, doc_id, template, use_ocr=use_ocr)
            jp = out / "json" / f"{doc_id}.json"
            jp.write_text(json.dumps(doc, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
            entry.update({"status": doc["processing"]["status"], "source_sha256": doc["source"]["sha256"],
                          "extraction_mode": doc["processing"]["extraction_mode"],
                          "json": str(jp.relative_to(out)), "counts": _counts(doc)})
            if generate_pdf and doc["processing"]["status"] != "needs_ocr":
                pp = out / "standardized" / f"{doc_id}_standardized.pdf"
                rep = generate_standardized_pdf(doc, template, pp, include_uncertain=include_uncertain)
                entry["standardized_pdf"] = str(pp.relative_to(out))
                entry["fill_report"] = {"filled_fields": rep["filled_fields"],
                                        "withheld_pending_review": len(rep["skipped_for_review"]),
                                        "not_placed": rep["not_placed"]}
            for r in doc["manual_review"]:
                queue.append({"document_id": doc_id, "source_filename": f.name, **r})
            log(f"{doc_id}  {f.name:<40} {entry['status']:<24} "
                f"{entry['counts']['entities']} entities, {entry['counts']['relationships']} relationships, "
                f"{entry['counts']['manual_review_items']} review items")
        except Exception as e:  # one bad file must not stop the batch
            entry.update({"status": "failed", "error": f"{type(e).__name__}: {e}"})
            log(f"{doc_id}  {f.name:<40} FAILED: {e}")
        index["documents"].append(entry)
    index["completed_at"] = _now()
    index["summary"] = dict(Counter(d["status"] for d in index["documents"]))
    (out / "batch_index.json").write_text(json.dumps(index, indent=2, ensure_ascii=False), encoding="utf-8")
    (out / "review_queue.json").write_text(json.dumps(queue, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    return index
