"""Intermediate data structures passed between extractors and the entity builder."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Extraction confidence by method. Kept in one place so scoring is auditable.
CONF = {
    "acroform": 0.98,
    "section_context": 0.95,
    "kv_same_line": 0.92,
    "kv_next_segment": 0.88,
    "kv_value_below": 0.72,
    "table_cell": 0.90,
    "table_cell_overflow": 0.60,
    "table_continuation": 0.55,
    "checkbox": 0.90,
    "checkbox_weak": 0.78,
    "yes_no_text": 0.85,
    "signature_text": 0.90,
    "signature_image": 0.80,
    "signature_ink": 0.75,
    "derived_split_comma": 0.85,
    "derived_split_space": 0.78,
    "family_kv": 0.85,
}

BBox = tuple[float, float, float, float]


def union_bbox(boxes) -> BBox | None:
    boxes = [b for b in boxes if b]
    if not boxes:
        return None
    return (min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes))


def round_bbox(b: BBox | None) -> list[float] | None:
    return [round(v, 1) for v in b] if b else None


@dataclass
class Observation:
    """One value found in the source, already bound to a template slot and a record.

    record_key is what preserves relationships: every observation with the same
    (section_id, part, record_key) becomes an attribute of the same entity.
    """
    section_id: str
    part: str                 # fields | table | row_table | checklist
    key: str                  # template attribute key (dot path allowed)
    record_key: str           # 'main', item key (per-field), 'row3', 'mother', option key ...
    raw: Any
    label_text: str
    page: int
    bbox: BBox | None
    method: str
    confidence: float
    label_bbox: BBox | None = None
    entity_ref: str | None = None      # sub-entity name (e.g. 'signature')
    flags: set = field(default_factory=set)
    notes: list = field(default_factory=list)
    extra: dict = field(default_factory=dict)
    order: int = 0


@dataclass
class SectionEvidence:
    section_id: str
    method: str                       # heading | acroform_field_names
    confidence: float
    source_heading: str | None = None
    pages: set = field(default_factory=set)
    heading_bboxes: list = field(default_factory=list)   # (page, bbox)
    continuation_pages: set = field(default_factory=set)


@dataclass
class ExtractionResult:
    mode: str                                  # template_acroform | layout | none
    observations: list[Observation] = field(default_factory=list)
    sections: dict[str, SectionEvidence] = field(default_factory=dict)
    unmapped: list[dict] = field(default_factory=list)
    warnings: list[dict] = field(default_factory=list)
    pages_without_text: list[int] = field(default_factory=list)
    ocr_pages: list[int] = field(default_factory=list)
