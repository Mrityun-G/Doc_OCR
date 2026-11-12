"""Layout-based extraction for flattened, typed, OCR'd or foreign-layout PDFs.

Every value is bound to (section, part, record) *by geometry*:
  * section  = the heading region the words fall in
  * record   = the table row / family row / consent item / checklist option they sit on
so a dosage can only ever be attached to the medication printed on the same row.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass

from .layout import (Line, Word, build_lines, build_regions, detect_headings, find_region,
                     strip_page_furniture)
from .models import CONF, ExtractionResult, Observation, SectionEvidence, union_bbox
from .template import Template
from .textnorm import is_filler, marker_state, normalize

TABLE_TOL = 6.0


# ============================================================== label matching
@dataclass
class Match:
    item: dict
    t0: int
    t1: int
    w0: int
    w1: int
    line: Line

    @property
    def label_text(self) -> str:
        return " ".join(w.text for w in self.line.words[self.w0:self.w1 + 1]).rstrip(":?").strip()

    @property
    def label_bbox(self):
        return self.line.bbox(self.w0, self.w1)


class Matcher:
    def __init__(self, items: list[dict]):
        self.entries = []
        for it in items:
            for a in Template.aliases(it):
                self.entries.append((a, it))
        self.entries.sort(key=lambda e: len(e[0]), reverse=True)

    def find(self, line: Line) -> list[Match]:
        toks, out, i = line.tokens, [], 0
        while i < len(toks):
            hit = None
            if toks[i].first:
                for a, it in self.entries:
                    n = len(a)
                    if i + n <= len(toks) and toks[i + n - 1].last and tuple(t.text for t in toks[i:i + n]) == a:
                        hit = Match(it, i, i + n - 1, toks[i].wi, toks[i + n - 1].wi, line)
                        break
            if hit:
                out.append(hit)
                i = hit.t1 + 1
            else:
                i += 1
        return out


def accepted(m: Match, allow_marker: bool = False) -> bool:
    """A label match counts only if it is *used as a label*: followed by ':'/'?', followed by a
    checkbox (yes/no items), or standing alone as its own segment. This keeps words such as
    'date' or 'email' inside consent paragraphs from being read as field labels."""
    words = m.line.words
    last = words[m.w1].text.rstrip()
    if last.endswith((":", "?")):
        return True
    if m.w1 + 1 < len(words) and words[m.w1 + 1].text.strip() in (":", "?"):
        return True
    if allow_marker and m.w1 + 1 < len(words) and marker_state(words[m.w1 + 1].text)[0]:
        return True
    a, b = m.line.segments[m.line.segment_of(m.w0)]
    return a == m.w0 and b == m.w1


def _strip(idx: list[int], words: list[Word]) -> list[int]:
    while idx and is_filler(words[idx[0]].text):
        idx = idx[1:]
    while idx and is_filler(words[idx[-1]].text):
        idx = idx[:-1]
    return idx


def collect_value(m: Match, nxt: Match | None, until_label: bool = False) -> tuple[list[int], str]:
    line, words = m.line, m.line.words
    limit = nxt.w0 if nxt else len(words)
    s = line.segment_of(m.w1)
    seg_end = line.segments[s][1]
    end = limit if until_label else min(limit, seg_end + 1)
    idx = _strip(list(range(m.w1 + 1, end)), words)
    how = "kv_same_line"
    if not idx and not until_label and s + 1 < len(line.segments):
        a, b = line.segments[s + 1]
        if a < limit:
            idx = _strip(list(range(a, min(limit, b + 1))), words)
            how = "kv_next_segment"
    return idx, how


def _method(words: list[Word], base: str) -> str:
    origins = {w.origin for w in words}
    if "ocr" in origins:
        return base + "+ocr"
    if "widget" in origins:
        return base + "+acroform_widget"
    if "vector_checkbox" in origins:
        return base + "+vector_checkbox"
    return base


def _wconf(words: list[Word]) -> float:
    return min((w.conf for w in words), default=1.0)


@dataclass
class YesNo:
    decision: str | None
    ambiguous: bool
    raw: str
    conf: float


def parse_yes_no(ws: list[Word]) -> YesNo | None:
    raw = " ".join(w.text for w in ws)
    states = {"yes": [], "no": []}
    for i, w in enumerate(ws):
        st, rest = marker_state(w.text)
        if not st:
            continue
        opt = normalize(rest or (ws[i + 1].text if i + 1 < len(ws) else ""))
        if opt in ("yes", "y"):
            states["yes"].append(st)
        elif opt in ("no", "n"):
            states["no"].append(st)
    if states["yes"] or states["no"]:
        on = {k: [s for s in v if s in ("checked", "weak_checked")] for k, v in states.items()}
        weak = any(s == "weak_checked" for v in on.values() for s in v)
        conf = CONF["checkbox_weak"] if weak else CONF["checkbox"]
        if on["yes"] and on["no"]:
            return YesNo(None, True, raw, conf)
        if on["yes"]:
            return YesNo("yes", False, raw, conf)
        if on["no"]:
            return YesNo("no", False, raw, conf)
        return None                                    # boxes present, none ticked: unanswered
    words = normalize(raw).split()
    if words in (["yes"], ["y"]):
        return YesNo("yes", False, raw, CONF["yes_no_text"])
    if words in (["no"], ["n"]):
        return YesNo("no", False, raw, CONF["yes_no_text"])
    if not words or sorted(set(words)) == ["no", "yes"]:
        return None                                    # bare 'Yes  No' options with no marks
    return YesNo(None, True, raw, 0.5)


# ============================================================== extractor
class LayoutExtractor:
    def __init__(self, src, template: Template, result: ExtractionResult):
        self.src, self.t, self.res = src, template, result
        self._order = 0

    def _obs(self, **kw) -> Observation:
        self._order += 1
        o = Observation(order=self._order, **kw)
        self.res.observations.append(o)
        return o

    # ---------------------------------------------------------- driver
    def run(self, words_by_page: dict[int, list[Word]]):
        lines_by_page = {p: build_lines(ws) for p, ws in words_by_page.items()}
        lines_by_page = strip_page_furniture(lines_by_page, self.src.page_sizes)
        headings = detect_headings(lines_by_page, self.t)
        regions = build_regions(headings, self.src.page_sizes)
        if not headings:
            self.res.warnings.append({"code": "no_sections_recognised",
                                      "message": "No template section headings were recognised; the document "
                                                 "may not be a patient intake form or uses unknown headings."})
        for h in headings:
            ev = self.res.sections.get(h.section_id)
            if not ev:
                ev = self.res.sections[h.section_id] = SectionEvidence(h.section_id, "heading", CONF["section_context"],
                                                                        source_heading=h.text)
            ev.pages.add(h.page)
            ev.heading_bboxes.append((h.page, h.bbox))
        for r in regions:
            if r.heading is None and r.section_id in self.res.sections:
                self.res.sections[r.section_id].continuation_pages.add(r.page)

        heading_words = set().union(*(h.word_ids for h in headings)) if headings else set()
        region_words: dict[int, list[Word]] = defaultdict(list)
        unsectioned: list[Word] = []
        for p in sorted(lines_by_page):
            for ln in lines_by_page[p]:
                for w in ln.words:
                    if id(w) in heading_words:
                        continue
                    r = find_region(regions, w)
                    if r:
                        region_words[id(r)].append(w)
                    else:
                        unsectioned.append(w)

        for s in self.t.sections:
            regs = sorted((r for r in regions if r.section_id == s["section_id"]), key=lambda r: (r.page, r.y0, r.x0))
            lines: list[Line] = []
            for r in regs:
                for ln in build_lines(region_words[id(r)]):
                    ln.rx1 = r.x1
                    lines.append(ln)
            if lines:
                self._section(s, lines)

        for ln in build_lines(unsectioned):
            self._unmapped_pairs(ln, set(), None)

    # ---------------------------------------------------------- per section
    def _section(self, s: dict, lines: list[Line]):
        header_li, tpart = None, None
        for pn in ("table", "row_table"):
            if pn in s:
                tpart = pn
                header_li = self._find_header(s, pn, lines)
        fields_matcher = Matcher(s["fields"]["items"]) if "fields" in s else None
        if "fields" in s:
            self._fields(s, lines[:header_li] if header_li is not None else lines)
        if tpart:
            if header_li is not None:
                self._table(s, tpart, lines, header_li, fields_matcher)
            elif tpart == "row_table":
                self._row_table_fallback(s, lines)
            else:
                self._report_unparsed_table(s, lines)
        if "checklist" in s:
            self._checklist(s, lines)

    # ---------------------------------------------------------- key / value
    def _fields(self, s: dict, lines: list[Line]):
        part = s["fields"]
        per_field = part["entity"].get("per_field", False)
        matcher = Matcher(part["items"])
        skip = set()
        for li, line in enumerate(lines):
            if li in skip:
                continue
            found = matcher.find(line)
            acc = [m for m in found if accepted(m, allow_marker=m.item.get("type") == "yes_no")]
            used: set[int] = set()
            for k, m in enumerate(acc):
                nxt = acc[k + 1] if k + 1 < len(acc) else None
                it, typ = m.item, m.item.get("type", "text")
                used.update(range(m.w0, m.w1 + 1))
                rk = it["key"] if per_field else "main"
                common = dict(section_id=s["section_id"], part="fields", key=it["key"], record_key=rk,
                              label_text=m.label_text, page=line.page, label_bbox=m.label_bbox,
                              entity_ref=it.get("entity_ref"))
                if typ == "yes_no":
                    idx, _ = collect_value(m, nxt, until_label=True)
                    used.update(idx)
                    ws = [line.words[i] for i in idx]
                    yn = parse_yes_no(ws)
                    if yn is None:
                        continue
                    o = self._obs(raw=yn.raw, bbox=union_bbox([w.bbox for w in ws]) or m.label_bbox,
                                  method=_method(ws, "layout_checkbox"), confidence=yn.conf * _wconf(ws),
                                  extra={"decision": yn.decision}, **common)
                    if yn.ambiguous:
                        o.flags.add("ambiguous_choice")
                        o.notes.append(f"could not resolve a single yes/no answer from {yn.raw!r}")
                    continue

                idx, how = collect_value(m, nxt)
                if typ == "signature":
                    used.update(idx)
                    self._signature(m, nxt, idx, common)
                    continue
                if not idx and nxt is None and m.w1 == len(line.words) - 1:
                    below = self._value_below(lines, li, m, matcher)
                    if below:
                        bl, bidx = below
                        skip.add(bl)
                        ws = [lines[bl].words[i] for i in bidx]
                        self._obs(raw=" ".join(w.text for w in ws), bbox=union_bbox([w.bbox for w in ws]),
                                  method=_method(ws, "layout_key_value"),
                                  confidence=CONF["kv_value_below"] * _wconf(ws),
                                  notes=["value read from the line below its label"], **common)
                    continue
                if not idx:
                    continue
                used.update(idx)
                ws = [line.words[i] for i in idx]
                self._obs(raw=" ".join(w.text for w in ws), bbox=union_bbox([w.bbox for w in ws]),
                          method=_method(ws, "layout_key_value"), confidence=CONF[how] * _wconf(ws), **common)
            self._unmapped_pairs(line, used, s["section_id"])

    def _value_below(self, lines, li, m: Match, matcher: Matcher):
        if li + 1 >= len(lines):
            return None
        nl, line = lines[li + 1], lines[li]
        if nl.page != line.page or nl.y0 - line.y1 > 1.2 * line.h:
            return None
        if any(accepted(x) for x in matcher.find(nl)):
            return None
        x0 = m.label_bbox[0] - 6
        idx = [i for i, w in enumerate(nl.words) if x0 <= w.x0 < m.label_bbox[2] + 250]
        idx = _strip(idx, nl.words)
        return (li + 1, idx) if idx else None

    def _signature(self, m: Match, nxt: Match | None, idx: list[int], common: dict):
        line = m.line
        if idx:
            ws = [line.words[i] for i in idx]
            self._obs(raw=" ".join(w.text for w in ws), bbox=union_bbox([w.bbox for w in ws]),
                      method=_method(ws, "signature_typed_text"), confidence=CONF["signature_text"] * _wconf(ws),
                      extra={"signature_evidence": "typed_text"}, **common)
            return
        lb = m.label_bbox
        x1 = nxt.label_bbox[0] if nxt else (line.rx1 or lb[2] + 250)
        area = (lb[2], lb[1] - 16, x1, lb[3] + 6)

        def hit(b):
            return min(b[2], area[2]) > max(b[0], area[0]) and min(b[3], area[3]) > max(b[1], area[1])

        imgs = [b for b in self.src.images(line.page) if hit(b)]
        ink = [b for b in self.src.ink(line.page) if hit(b)]
        if imgs:
            self._obs(raw=True, bbox=union_bbox(imgs), method="signature_image_detection",
                      confidence=CONF["signature_image"], extra={"signature_evidence": "image"}, **common)
        elif ink:
            self._obs(raw=True, bbox=union_bbox(ink), method="signature_ink_detection",
                      confidence=CONF["signature_ink"], extra={"signature_evidence": "ink_strokes"}, **common)
        else:
            self._obs(raw=False, bbox=area, method="signature_detection", confidence=CONF["signature_ink"],
                      extra={"signature_evidence": "none"}, notes=["signature line found but empty"], **common)

    def _unmapped_pairs(self, line: Line, used: set[int], section_id: str | None):
        for a, b in line.segments:
            idx = [i for i in range(a, b + 1) if i not in used]
            if not idx:
                continue
            ws = [line.words[i] for i in idx]
            text = " ".join(w.text for w in ws)
            mm = re.match(r"^([^:]{2,60}):\s*(\S.*)$", text)
            if mm and not marker_state(ws[0].text)[0]:
                self.res.unmapped.append({"section_id": section_id, "label": mm.group(1).strip(),
                                          "value": mm.group(2).strip(), "page": line.page,
                                          "bbox": [round(v, 1) for v in union_bbox([w.bbox for w in ws])]})

    # ---------------------------------------------------------- tables
    def _columns(self, s, pn):
        part = s[pn]
        cols = list(part["columns"])
        if pn == "row_table":
            rh = dict(part["row_header"])
            rh["key"] = "__row__"
            cols = [rh] + cols
        return cols

    def _find_header(self, s, pn, lines) -> int | None:
        cols = self._columns(s, pn)
        matcher = Matcher(cols)
        need = 2 if len(cols) > 1 else 1
        for li, line in enumerate(lines):
            if len({m.item["key"] for m in matcher.find(line)}) >= need:
                return li
        return None

    def _table(self, s, pn, lines, header_li, stop_matcher: Matcher | None):
        part, sid = s[pn], s["section_id"]
        cols = self._columns(s, pn)
        header = lines[header_li]
        pos, self._hdr = {}, {}
        for m in Matcher(cols).find(header):
            if m.item["key"] not in pos:
                pos[m.item["key"]] = header.words[m.w0].x0
                self._hdr[m.item["key"]] = (m.label_text, m.label_bbox)   # label as printed in the source
        colpos = sorted(pos.items(), key=lambda kv: kv[1])
        if pn == "row_table" and "__row__" not in pos:
            colpos.insert(0, ("__row__", -1e9))
        starts = [x for _, x in colpos]
        key_col = "__row__" if pn == "row_table" else part["key_column"]

        records: list[dict] = []
        prev_line = None
        for li in range(header_li + 1, len(lines)):
            line = lines[li]
            if stop_matcher and any(accepted(m, True) for m in stop_matcher.find(line)):
                break
            cells: dict[str, list[Word]] = defaultdict(list)
            over: set[str] = set()
            for wi, w in enumerate(line.words):
                if wi == 0 and re.fullmatch(r"\d{1,2}[.)]", w.text) and len(starts) > 1 and w.x1 < starts[1]:
                    continue                              # row numbering such as '1.' or '2)'
                j = 0
                for k, st in enumerate(starts):
                    if w.x0 >= st - TABLE_TOL:
                        j = k
                key = colpos[j][0]
                if j + 1 < len(starts) and w.x1 > starts[j + 1] + 1:
                    over.add(key)
                cells[key].append(w)
            cells = {k: [w for w in v if not is_filler(w.text)] for k, v in cells.items()}
            cells = {k: v for k, v in cells.items() if v}
            if not cells:
                continue
            if key_col not in cells and records and prev_line is not None \
                    and line.page == prev_line.page and line.y0 - prev_line.y1 < 0.5 * line.h:
                rec = records[-1]                          # wrapped text: continuation of previous row
                for k, v in cells.items():
                    rec["cells"].setdefault(k, []).extend(v)
                    rec["flags"][k].add("continuation")
                prev_line = line
                continue
            rec = {"cells": dict(cells), "flags": defaultdict(set), "page": line.page}
            for k in over:
                rec["flags"][k].add("overflow")
            if key_col not in cells:
                rec["flags"]["__record__"].add("missing_key")
            records.append(rec)
            prev_line = line

        for n, rec in enumerate(records, 1):
            if pn == "row_table":
                self._emit_row_table_record(s, part, rec)
            else:
                self._emit_table_record(s, pn, part, rec, f"row{n}")

    def _cell_obs(self, sid, pn, key, rk, ws, flags, label, extra=None):
        notes, conf = [], CONF["table_cell"]
        if "overflow" in flags:
            conf = CONF["table_cell_overflow"]
            notes.append("cell text crosses into the next column; column assignment uncertain")
        if "continuation" in flags:
            conf = CONF["table_continuation"]
            notes.append("value continues on a following line with no row key; merged into the row above")
        hdr_key = "__row__" if pn == "row_table" and extra and "row_key_known" in extra else key
        label, label_bbox = getattr(self, "_hdr", {}).get(hdr_key, (label, None))
        o = self._obs(section_id=sid, part=pn, key=key, record_key=rk, raw=" ".join(w.text for w in ws),
                      label_text=label, label_bbox=label_bbox, page=ws[0].page, bbox=union_bbox([w.bbox for w in ws]),
                      method=_method(ws, "layout_table"), confidence=conf * _wconf(ws), notes=notes,
                      extra=extra or {})
        o.flags |= set(flags)
        return o

    def _emit_table_record(self, s, pn, part, rec, rk):
        sid = s["section_id"]
        labels = {c["key"]: c["label"] for c in part["columns"]}
        for key, ws in rec["cells"].items():
            if key in labels:
                self._cell_obs(sid, pn, key, rk, ws, rec["flags"][key], labels[key])
        if "missing_key" in rec["flags"]["__record__"]:
            self.res.warnings.append({"code": "row_without_key", "section_id": sid, "record_key": rk,
                                      "message": f"a row in '{s['title']}' has no {part['key_column']}"})

    def _resolve_row(self, part, text) -> tuple[str, bool]:
        n = tuple(normalize(text).split())
        for r in part["rows"]:
            if n in Template.aliases(r):
                return r["row_key"], True
        return "x:" + "_".join(n), False

    def _emit_row_table_record(self, s, part, rec):
        sid = s["section_id"]
        row_ws = rec["cells"].get("__row__")
        if row_ws and not any(k != "__row__" for k in rec["cells"]):
            return            # printed row label (e.g. 'Children') with every cell left blank: unanswered
        if not row_ws:
            self.res.warnings.append({"code": "family_row_without_relation", "section_id": sid,
                                      "message": "a family-history row has no relation label; values not attached"})
            for key, ws in rec["cells"].items():
                self.res.unmapped.append({"section_id": sid, "label": key, "value": " ".join(w.text for w in ws),
                                          "page": ws[0].page, "bbox": None})
            return
        label = " ".join(w.text for w in row_ws).rstrip(":")
        rk, known = self._resolve_row(part, label)
        self._cell_obs(sid, "row_table", part["row_attribute"], rk, row_ws, rec["flags"]["__row__"],
                       part["row_header"]["label"], extra={"row_key_known": known})
        labels = {c["key"]: c["label"] for c in part["columns"]}
        for key, ws in rec["cells"].items():
            if key in labels:
                self._cell_obs(sid, "row_table", key, rk, ws, rec["flags"][key], labels[key])

    def _row_table_fallback(self, s, lines):
        """'Mother: living, age 66 - diabetes, hypertension' style family history."""
        part, sid = s["row_table"], s["section_id"]
        rows = [dict(r, key=r["row_key"]) for r in part["rows"]]
        matcher = Matcher(rows)
        for line in lines:
            acc = [m for m in matcher.find(line) if accepted(m)]
            for k, m in enumerate(acc):
                nxt = acc[k + 1] if k + 1 < len(acc) else None
                idx, _ = collect_value(m, nxt, until_label=True)
                if not idx:
                    continue
                ws = [line.words[i] for i in idx]
                text = " ".join(w.text for w in ws)
                bbox = union_bbox([w.bbox for w in ws])
                rk = m.item["row_key"]
                base = dict(section_id=sid, part="row_table", record_key=rk, page=line.page, bbox=bbox,
                            label_bbox=m.label_bbox, method=_method(ws, "layout_key_value"))
                conf = CONF["family_kv"] * _wconf(ws)
                self._obs(key=part["row_attribute"], raw=m.label_text, label_text=m.label_text,
                          confidence=conf, extra={"row_key_known": True}, **dict(base, bbox=m.label_bbox))
                rest = text
                st = re.search(r"\b(living|alive|deceased|dead|passed away|died)\b", rest, re.I)
                if st:
                    self._obs(key="living_status", raw=st.group(1), label_text=m.label_text, confidence=conf, **base)
                    rest = rest[:st.start()] + " " + rest[st.end():]
                ag = re.search(r"\b(?:aged?|age at death)\s*(?:of\s*)?(\d{1,3})\b", rest, re.I)
                if ag:
                    self._obs(key="age", raw=ag.group(1), label_text=m.label_text, confidence=conf, **base)
                    rest = rest[:ag.start()] + " " + rest[ag.end():]
                rest = re.sub(r"^[\s,;:\-–—()]+|[\s,;:\-–—()]+$", "", rest)
                rest = re.sub(r"^\s*[,;\-–—]+\s*", "", rest)
                if rest:
                    self._obs(key=part["default_column"], raw=rest, label_text=m.label_text, confidence=conf, **base)

    def _report_unparsed_table(self, s, lines):
        text = [ln.text for ln in lines if ln.text.strip()]
        if text:
            self.res.warnings.append({"code": "table_header_not_recognised", "section_id": s["section_id"],
                                      "message": f"section '{s['title']}' was found but its table header was not "
                                                 f"recognised; {len(text)} line(s) were not extracted",
                                      "lines": text[:10]})

    # ---------------------------------------------------------- checklist
    def _checklist(self, s, lines):
        part, sid = s["checklist"], s["section_id"]
        opts = [dict(o) for o in part["options"]]
        other = part.get("other")
        other_matcher = Matcher([other]) if other else None
        for line in lines:
            words = line.words
            oth = [m for m in other_matcher.find(line) if accepted(m)] if other_matcher else []
            stop = oth[0].w0 if oth else len(words)
            i = 0
            while i < stop:
                st, rest = marker_state(words[i].text)
                if not st:
                    i += 1
                    continue
                seg_end = line.segments[line.segment_of(i)][1]
                j, opt_idx = i + 1, []
                while j <= seg_end and j < stop and not marker_state(words[j].text)[0]:
                    opt_idx.append(j)
                    j += 1
                text = (rest + " " + " ".join(words[k].text for k in opt_idx)).strip()
                if st in ("checked", "weak_checked") and text:
                    n = tuple(normalize(text).split())
                    opt = next((o for o in opts if n in Template.aliases(o)), None)
                    ws = [words[i]] + [words[k] for k in opt_idx]
                    conf = CONF["checkbox_weak" if st == "weak_checked" else "checkbox"] * _wconf(ws)
                    self._obs(section_id=sid, part="checklist", key="option",
                              record_key=opt["key"] if opt else "x:" + "_".join(n), raw=text,
                              label_text=text, page=line.page, bbox=union_bbox([w.bbox for w in ws]),
                              method=_method(ws, "layout_checkbox"), confidence=conf,
                              extra={"option_key": opt["key"] if opt else None})
                i = j
            for k, m in enumerate(oth):
                idx, how = collect_value(m, oth[k + 1] if k + 1 < len(oth) else None)
                if idx:
                    ws = [words[x] for x in idx]
                    self._obs(section_id=sid, part="checklist", key=other["key"], record_key="other",
                              raw=" ".join(w.text for w in ws), label_text=m.label_text, page=line.page,
                              bbox=union_bbox([w.bbox for w in ws]), label_bbox=m.label_bbox,
                              method=_method(ws, "layout_key_value"), confidence=CONF[how] * _wconf(ws))


def widget_words(widgets) -> dict[int, list[Word]]:
    """Turn a *foreign* fillable form's widget values into positioned pseudo-words so the
    layout engine can pair them with the printed labels next to them."""
    out: dict[int, list[Word]] = defaultdict(list)
    for w in widgets:
        x0, y0, x1, y1 = w.rect
        if w.kind in ("CheckBox", "RadioButton"):
            out[w.page].append(Word(x0, y0, x1, y1, "[X]" if w.checked else "[ ]", w.page, origin="widget"))
            continue
        val = str(w.value or "").strip()
        if not val:
            continue
        parts = val.split()
        total = sum(len(p) for p in parts) + len(parts) - 1
        cx, width = x0 + 2, max(1.0, (x1 - x0 - 4))
        for p in parts:
            pw = width * len(p) / max(total, 1)
            out[w.page].append(Word(cx, y0, cx + pw, y1, p, w.page, origin="widget"))
            cx += pw + width / max(total, 1)
    return out
