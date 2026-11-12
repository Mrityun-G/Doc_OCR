"""Command-line interface.

  python -m docintel process <pdf|dir>... [-o output] [--template DIR] [--no-pdf] [--include-uncertain]
  python -m docintel generate <DOC.json> [-o out.pdf] [--include-uncertain]
  python -m docintel ask <DOC.json> entity|related|owner|section|evidence|find <ref> | between <a> <b>
  python -m docintel summary <DOC.json>
  python -m docintel scan-template <reference.pdf> [-o scan.json]
  python -m docintel serve [--host 127.0.0.1] [--port 8000] [--data web_data] [--no-seed]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

DEFAULT_TEMPLATE = Path(__file__).resolve().parent.parent / "templates" / "patient_intake_v1"


def _template(path):
    from .template import Template
    return Template(path or DEFAULT_TEMPLATE)


def cmd_process(a):
    from .pipeline import process_batch
    idx = process_batch(a.inputs, a.output, _template(a.template), generate_pdf=not a.no_pdf,
                        include_uncertain=a.include_uncertain, use_ocr=not a.no_ocr)
    print(f"\n{len(idx['documents'])} document(s): {idx['summary']}")
    print(f"batch index : {Path(a.output) / 'batch_index.json'}")
    print(f"review queue: {Path(a.output) / 'review_queue.json'}")


def cmd_generate(a):
    from .generate import generate_standardized_pdf
    doc = json.loads(Path(a.json).read_text(encoding="utf-8"))
    out = a.output or Path(a.json).with_name(f"{doc['document_id']}_standardized.pdf")
    rep = generate_standardized_pdf(doc, _template(a.template), out, include_uncertain=a.include_uncertain)
    print(json.dumps({k: v for k, v in rep.items() if k != "field_sources"}, indent=2, default=str))
    print(f"written: {out}")


def cmd_ask(a):
    from .query import DocQuery
    q = DocQuery(json.loads(Path(a.json).read_text(encoding="utf-8")))
    fn = {"entity": q.entity, "related": q.related, "owner": q.owner, "section": q.section,
          "evidence": q.evidence, "find": q.find, "between": q.between}[a.question]
    args = a.args
    if a.question == "between" and len(args) != 2:
        sys.exit("between needs two refs")
    if a.question != "between" and len(args) != 1:
        sys.exit(f"{a.question} needs exactly one argument")
    print(json.dumps(fn(*args), indent=2, ensure_ascii=False, default=str))


def cmd_summary(a):
    from .query import render_summary
    print(render_summary(json.loads(Path(a.json).read_text(encoding="utf-8"))))


def cmd_scan(a):
    from .scan import scan_template_pdf
    res = scan_template_pdf(a.pdf, _template(a.template))
    text = json.dumps(res, indent=2, ensure_ascii=False)
    if a.output:
        Path(a.output).write_text(text, encoding="utf-8")
        print(f"written: {a.output}  ({len(res.get('widgets', []))} widgets, "
              f"{len(res.get('label_candidates', []))} label candidates)")
    else:
        print(text)


def cmd_serve(a):
    from .webapp import serve
    seed = None if a.no_seed else Path(__file__).resolve().parent.parent / "samples" / "input"
    serve(port=a.port, data_dir=a.data, template=_template(a.template), seed=seed, host=a.host)


def main(argv=None):
    p = argparse.ArgumentParser(prog="docintel", description="Relationship-aware PDF intake extraction")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("process", help="extract one JSON per PDF (+ standardized PDFs and a batch index)")
    s.add_argument("inputs", nargs="+")
    s.add_argument("-o", "--output", default="output")
    s.add_argument("--template")
    s.add_argument("--no-pdf", action="store_true", help="skip standardized PDF generation")
    s.add_argument("--no-ocr", action="store_true")
    s.add_argument("--include-uncertain", action="store_true",
                   help="also print values awaiting review on the standardized PDF")
    s.set_defaults(func=cmd_process)

    s = sub.add_parser("generate", help="populate the reference template from a DOC_xxx.json")
    s.add_argument("json")
    s.add_argument("-o", "--output")
    s.add_argument("--template")
    s.add_argument("--include-uncertain", action="store_true")
    s.set_defaults(func=cmd_generate)

    s = sub.add_parser("ask", help="query relationships in a DOC_xxx.json")
    s.add_argument("json")
    s.add_argument("question", choices=["entity", "related", "owner", "section", "between", "evidence", "find"])
    s.add_argument("args", nargs="+")
    s.set_defaults(func=cmd_ask)

    s = sub.add_parser("summary", help="print a readable overview of a DOC_xxx.json")
    s.add_argument("json")
    s.set_defaults(func=cmd_summary)

    s = sub.add_parser("scan-template", help="inventory a real reference PDF to help map it into template.json")
    s.add_argument("pdf")
    s.add_argument("-o", "--output")
    s.add_argument("--template")
    s.set_defaults(func=cmd_scan)

    s = sub.add_parser("serve", help="run the local web UI (http://localhost:8000)")
    s.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8000)),
                   help="default: $PORT (set by hosts like Render) or 8000")
    s.add_argument("--host", default=os.environ.get("DOCINTEL_HOST", "127.0.0.1"),
                   help="127.0.0.1 = this computer only; 0.0.0.0 when deployed")
    s.add_argument("--data", default="web_data", help="where uploads and outputs are stored")
    s.add_argument("--template")
    s.add_argument("--no-seed", action="store_true", help="start empty instead of loading the sample PDFs")
    s.set_defaults(func=cmd_serve)

    a = p.parse_args(argv)
    a.func(a)


if __name__ == "__main__":
    main()
