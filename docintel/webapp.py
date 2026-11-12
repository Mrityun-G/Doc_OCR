"""Local web UI.

    python -m docintel serve            ->  http://localhost:8000

Upload PDFs, browse entities / relationships / review items, click any value to see it
highlighted on the source page, ask relationship questions, download JSON and the
standardized PDF. Binds to 127.0.0.1 only. Each upload is processed independently and
stored under <data>/ (sources/, json/, standardized/, index.json).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import threading
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

import pymupdf

from .generate import generate_standardized_pdf
from .pipeline import _counts, _now, process_document
from .query import DocQuery
from .template import Template

WEB_DIR = Path(__file__).resolve().parent / "web"
MAX_UPLOAD = 50 * 1024 * 1024
DOC_RE = r"(DOC_\d{3,})"


PUBLIC_DEMO = os.environ.get("DOCINTEL_PUBLIC_DEMO", "").lower() in ("1", "true", "yes")


class Store:
    def __init__(self, root: Path, template: Template):
        self.root, self.t = root, template
        self.lock = threading.Lock()
        for d in ("sources", "json", "standardized"):
            (root / d).mkdir(parents=True, exist_ok=True)
        self.index_path = root / "index.json"
        self.docs: list[dict] = []
        if self.index_path.exists():
            self.docs = json.loads(self.index_path.read_text(encoding="utf-8")).get("documents", [])

    def _save(self):
        self.index_path.write_text(json.dumps({"template_id": self.t.template_id, "updated_at": _now(),
                                               "documents": self.docs}, indent=2), encoding="utf-8")

    def entry(self, doc_id: str) -> dict | None:
        return next((d for d in self.docs if d["document_id"] == doc_id), None)

    def source_path(self, doc_id: str) -> Path | None:
        e = self.entry(doc_id)
        return self.root / "sources" / doc_id / e["source_filename"] if e else None

    def json_path(self, doc_id: str) -> Path:
        return self.root / "json" / f"{doc_id}.json"

    def std_path(self, doc_id: str) -> Path:
        return self.root / "standardized" / f"{doc_id}_standardized.pdf"

    def add(self, filename: str, data: bytes, seeded: bool = False) -> dict:
        safe = re.sub(r"[^A-Za-z0-9._() -]", "_", Path(filename).name).strip()[:120] or "upload.pdf"
        if not safe.lower().endswith(".pdf"):
            safe += ".pdf"
        with self.lock:
            n = max([int(d["document_id"][4:]) for d in self.docs] or [0]) + 1
            doc_id = f"DOC_{n:03d}"
            entry = {"document_id": doc_id, "source_filename": safe, "status": "processing", "uploaded_at": _now()}
            if seeded:
                entry["sample"] = True
            self.docs.append(entry)
            self._save()
        src = self.root / "sources" / doc_id / safe
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_bytes(data)
        try:
            doc = process_document(src, doc_id, self.t)
            self.json_path(doc_id).write_text(json.dumps(doc, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
            upd = {"status": doc["processing"]["status"], "extraction_mode": doc["processing"]["extraction_mode"],
                   "source_sha256": doc["source"]["sha256"], "counts": _counts(doc)}
            if doc["processing"]["status"] != "needs_ocr":
                rep = generate_standardized_pdf(doc, self.t, self.std_path(doc_id))
                upd["fill_report"] = {"filled_fields": rep["filled_fields"],
                                      "withheld_pending_review": len(rep["skipped_for_review"]),
                                      "not_placed": rep["not_placed"]}
        except Exception as e:  # keep the entry so the user sees why
            upd = {"status": "failed", "error": f"{type(e).__name__}: {e}"}
        with self.lock:
            entry.update(upd)
            self._save()
        return entry

    def delete(self, doc_id: str) -> bool:
        with self.lock:
            e = self.entry(doc_id)
            if not e:
                return False
            self.docs.remove(e)
            self._save()
        shutil.rmtree(self.root / "sources" / doc_id, ignore_errors=True)
        for p in (self.json_path(doc_id), self.std_path(doc_id)):
            p.unlink(missing_ok=True)
        render_page.cache_clear()
        return True


@lru_cache(maxsize=128)
def render_page(path: str, page: int, mtime: float, dpi: int = 110) -> bytes:
    with pymupdf.open(path) as d:
        return d[page - 1].get_pixmap(dpi=dpi).tobytes("png")


def page_sizes(path: Path) -> list[list[float]]:
    if not path or not path.exists():
        return []
    with pymupdf.open(path) as d:
        return [[round(p.rect.width, 2), round(p.rect.height, 2)] for p in d]


def make_handler(store: Store):
    class Handler(BaseHTTPRequestHandler):
        server_version = "docintel"

        def log_message(self, fmt, *args):
            pass

        # -------------------------------------------------------- helpers
        def _send(self, code: int, body: bytes, ctype: str, headers: dict | None = None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code: int = 200):
            self._send(code, json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"),
                       "application/json; charset=utf-8")

        def _err(self, code: int, msg: str):
            self._json({"error": msg}, code)

        # -------------------------------------------------------- GET
        def do_GET(self):
            u = urlparse(self.path)
            p, q = u.path, parse_qs(u.query)
            try:
                if p in ("/", "/index.html"):
                    return self._send(200, (WEB_DIR / "index.html").read_bytes(), "text/html; charset=utf-8")
                if p == "/api/documents":
                    return self._json({"template_id": store.t.template_id, "public_demo": PUBLIC_DEMO,
                                       "documents": store.docs})
                m = re.fullmatch(f"/api/doc/{DOC_RE}", p)
                if m:
                    jp = store.json_path(m[1])
                    if not jp.exists():
                        return self._err(404, "document not processed")
                    return self._json({"entry": store.entry(m[1]),
                                       "doc": json.loads(jp.read_text(encoding="utf-8")),
                                       "source_pages": page_sizes(store.source_path(m[1])),
                                       "standardized_pages": page_sizes(store.std_path(m[1]))})
                m = re.fullmatch(f"/api/doc/{DOC_RE}/page/(\\d+)\\.png", p)
                if m:
                    which = q.get("which", ["source"])[0]
                    path = store.std_path(m[1]) if which == "standardized" else store.source_path(m[1])
                    if not path or not path.exists():
                        return self._err(404, "file not found")
                    try:
                        png = render_page(str(path), int(m[2]), path.stat().st_mtime)
                    except IndexError:
                        return self._err(404, "no such page")
                    return self._send(200, png, "image/png")
                m = re.fullmatch(f"/api/doc/{DOC_RE}/ask", p)
                if m:
                    jp = store.json_path(m[1])
                    if not jp.exists():
                        return self._err(404, "document not processed")
                    dq = DocQuery(json.loads(jp.read_text(encoding="utf-8")))
                    fn = {"entity": dq.entity, "related": dq.related, "owner": dq.owner, "section": dq.section,
                          "between": dq.between, "evidence": dq.evidence, "find": dq.find}.get(q.get("q", [""])[0])
                    if not fn:
                        return self._err(400, "unknown question")
                    try:
                        return self._json({"answer": fn(*[a.strip() for a in q.get("a", [])])})
                    except (KeyError, TypeError) as e:
                        return self._err(400, str(e).strip("'\""))
                m = re.fullmatch(f"/files/{DOC_RE}/(json|standardized|source)", p)
                if m:
                    doc_id, kind = m[1], m[2]
                    path = {"json": store.json_path(doc_id), "standardized": store.std_path(doc_id),
                            "source": store.source_path(doc_id)}[kind]
                    if not path or not path.exists():
                        return self._err(404, "file not found")
                    ctype = "application/json; charset=utf-8" if kind == "json" else "application/pdf"
                    disp = "attachment" if kind == "json" else "inline"
                    return self._send(200, path.read_bytes(), ctype,
                                      {"Content-Disposition": f"{disp}; filename*=UTF-8''{quote(path.name)}"})
                return self._err(404, "not found")
            except (ConnectionError, BrokenPipeError):
                pass
            except Exception as e:
                return self._err(500, f"{type(e).__name__}: {e}")

        # -------------------------------------------------------- POST / DELETE
        def do_POST(self):
            if urlparse(self.path).path != "/api/upload":
                return self._err(404, "not found")
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0:
                return self._err(400, "empty upload")
            if n > MAX_UPLOAD:
                return self._err(413, "file larger than 50 MB")
            data = self.rfile.read(n)
            if not data.startswith(b"%PDF"):
                return self._err(400, "not a PDF file")
            name = unquote(self.headers.get("X-Filename") or "upload.pdf")
            return self._json(store.add(name, data))

        def do_DELETE(self):
            m = re.fullmatch(f"/api/doc/{DOC_RE}", urlparse(self.path).path)
            if m and PUBLIC_DEMO and (store.entry(m[1]) or {}).get("sample"):
                return self._err(403, "sample documents can't be deleted on the public demo")
            if not m or not store.delete(m[1]):
                return self._err(404, "not found")
            return self._json({"deleted": m[1]})

    return Handler


def serve(port: int = 8000, data_dir: str | Path = "web_data", template: Template | None = None,
          seed: Path | None = None, host: str = "127.0.0.1"):
    store = Store(Path(data_dir), template)
    if seed and not store.docs and seed.exists():
        for f in sorted(seed.glob("*.pdf")):
            e = store.add(f.name, f.read_bytes(), seeded=True)
            print(f"seeded {e['document_id']}  {f.name}  [{e['status']}]", flush=True)
    httpd = ThreadingHTTPServer((host, port), make_handler(store))
    shown = "localhost" if host in ("127.0.0.1", "0.0.0.0") else host
    print(f"docintel UI running at http://{shown}:{port}   (listening on {host}; data: {Path(data_dir).resolve()}"
          f"{'; public demo mode' if PUBLIC_DEMO else ''})", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
