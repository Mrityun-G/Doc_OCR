"""Loads and indexes a template definition (template.json).

The template is the single source of truth for: section names/aliases, which entity each
section produces, attribute labels/aliases, table layouts, AcroForm field names of the
reference PDF and the allowed relationship predicates.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .textnorm import normalize, tokens

PARTS = ("fields", "table", "row_table", "checklist")


@dataclass(frozen=True)
class Binding:
    """Where an AcroForm field of the reference template lands in the data model."""
    section_id: str
    part: str
    key: str
    record_key: str
    kind: str                 # text | yes_no | option | other
    choice: str | None = None  # 'yes' / 'no' for yes_no checkboxes
    entity_ref: str | None = None


class TemplateError(ValueError):
    pass


def _alias_list(defn: dict) -> list[tuple[str, ...]]:
    seen, out = set(), []
    for a in [defn.get("label", "")] + list(defn.get("aliases", [])):
        t = tokens(a)
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return sorted(out, key=len, reverse=True)


class Template:
    def __init__(self, path: str | Path):
        p = Path(path)
        if p.is_dir():
            p = p / "template.json"
        self.path = p
        self.dir = p.parent
        self.data = json.loads(p.read_text(encoding="utf-8"))
        self.template_id = self.data["template_id"]
        self.document_type = self.data["document_type"]
        self.sections: list[dict] = self.data["sections"]
        self.section_by_id = {s["section_id"]: s for s in self.sections}
        self.predicates: dict = self.data["predicates"]
        self.review_threshold: float = float(self.data.get("review_threshold", 0.7))
        self.subject = self.data["subject_entity"]
        self.reference_pdf = self.dir / self.data.get("reference_pdf", "reference_template.pdf")

        self.heading_index: dict[tuple, str] = {}
        for s in self.sections:
            for a in _alias_list({"label": s["title"], "aliases": s.get("aliases", [])}):
                other = self.heading_index.get(a)
                if other and other != s["section_id"]:
                    raise TemplateError(f"heading alias {' '.join(a)!r} used by {other} and {s['section_id']}")
                self.heading_index[a] = s["section_id"]

        self.acroform_index: dict[str, Binding] = {}
        self._build_acroform_index()

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def aliases(defn: dict) -> list[tuple[str, ...]]:
        return _alias_list(defn)

    def match_heading(self, text: str) -> str | None:
        t = re.sub(r"\(.*?\)", " ", text)
        t = re.sub(r"\bcontinued\b|\bcont\b\.?", " ", t, flags=re.I)
        t = re.sub(r"^\s*(section\s+)?([0-9]{1,2}|[A-Za-z])[.):]\s+", "", t, flags=re.I)
        t = t.strip().rstrip(":").strip()
        key = tuple(normalize(t).split())
        if not key or len(key) > 9:
            return None
        return self.heading_index.get(key)

    def items(self, section_id: str, part: str) -> list[dict]:
        s = self.section_by_id[section_id]
        p = s.get(part)
        if not p:
            return []
        if part == "fields":
            return p["items"]
        if part == "checklist":
            return [p["other"]] if p.get("other") else []
        return p["columns"]

    def item(self, section_id: str, part: str, key: str) -> dict | None:
        for it in self.items(section_id, part):
            if it["key"] == key:
                return it
        return None

    def all_acroform_names(self) -> set[str]:
        return set(self.acroform_index)

    # ------------------------------------------------------------------ index
    def _add(self, name: str, b: Binding):
        if name in self.acroform_index:
            raise TemplateError(f"duplicate acroform name {name!r}")
        self.acroform_index[name] = b

    def _build_acroform_index(self):
        for s in self.sections:
            sid = s["section_id"]
            if "fields" in s:
                per_field = s["fields"]["entity"].get("per_field", False)
                for it in s["fields"]["items"]:
                    af = it.get("acroform")
                    if not af:
                        continue
                    rk = it["key"] if per_field else "main"
                    if it.get("type") == "yes_no":
                        for choice in ("yes", "no"):
                            if af.get(choice):
                                self._add(af[choice], Binding(sid, "fields", it["key"], rk, "yes_no", choice,
                                                              it.get("entity_ref")))
                    else:
                        self._add(af, Binding(sid, "fields", it["key"], rk, "text", None, it.get("entity_ref")))
            if "table" in s:
                t = s["table"]
                for row in range(1, int(t["rows"]) + 1):
                    for c in t["columns"]:
                        self._add(t["acroform_pattern"].format(row=row, key=c["key"]),
                                  Binding(sid, "table", c["key"], f"row{row}", "text"))
            if "row_table" in s:
                t = s["row_table"]
                for r in t["rows"]:
                    for c in t["columns"]:
                        self._add(t["acroform_pattern"].format(row=r["row_key"], key=c["key"]),
                                  Binding(sid, "row_table", c["key"], r["row_key"], "text"))
            if "checklist" in s:
                c = s["checklist"]
                for o in c["options"]:
                    self._add(c["acroform_pattern"].format(key=o["key"]),
                              Binding(sid, "checklist", "option", o["key"], "option"))
                if c.get("other", {}).get("acroform"):
                    self._add(c["other"]["acroform"], Binding(sid, "checklist", c["other"]["key"], "other", "other"))
