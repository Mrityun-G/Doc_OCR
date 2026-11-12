"""Render a captioned walkthrough video of docintel from the running web UI.

    python -m docintel serve                  # terminal 1 (http://localhost:8000)
    python tools/make_demo_video.py           # -> video/docintel_how_it_works.mp4

Phase 1 drives the real UI with Playwright (installed Microsoft Edge) and takes 4K
screenshots. A copy of a sample PDF is uploaded for the demo and deleted afterwards.
Phase 2 composes title cards, camera moves, a pointer, click ripples and captions at
1920x1080 / 30 fps and encodes H.264 with OpenCV.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from functools import lru_cache
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
W, H, FPS = 1920, 1080, 30
SW, SH = 3840, 2160                    # screenshots: 1920x1080 viewport at device scale 2
XF = int(0.35 * FPS)                   # cross-fade between segments
FULL = (0.0, 0.0, float(SW), float(SH))

PAL = {"bg": (18, 20, 23), "panel": (28, 31, 37), "ink": (232, 234, 237), "muted": (150, 158, 170),
       "line": (54, 59, 69), "accent": (125, 155, 255), "warn": (239, 180, 92), "ok": (98, 205, 143),
       "code": (22, 24, 28)}
FONTS = {"reg": "segoeui.ttf", "semi": "seguisb.ttf", "bold": "segoeuib.ttf", "black": "seguibl.ttf",
         "mono": "consola.ttf", "monob": "consolab.ttf"}


# ============================================================ helpers
@lru_cache(None)
def font(kind: str, size: int):
    for base in (Path("C:/Windows/Fonts"), Path("/usr/share/fonts/truetype/dejavu")):
        if (base / FONTS[kind]).exists():
            return ImageFont.truetype(str(base / FONTS[kind]), size)
    return ImageFont.load_default()


def ease(x: float) -> float:
    x = min(1.0, max(0.0, x))
    return x * x * (3 - 2 * x)


def interp(keys, t, lerp):
    if t <= keys[0][0]:
        return keys[0][1]
    for (t0, v0), (t1, v1) in zip(keys, keys[1:]):
        if t <= t1:
            return lerp(v0, v1, ease((t - t0) / max(1e-6, t1 - t0)))
    return keys[-1][1]


def lerp_seq(a, b, k):
    return tuple(a[i] + (b[i] - a[i]) * k for i in range(len(a)))


def mix(c0, c1, a):
    return tuple(int(c0[i] + (c1[i] - c0[i]) * a) for i in range(3))


def center(box):
    return ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)


def union(*boxes):
    boxes = [b for b in boxes if b]
    return (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))


def cam_for(box, pad=1.6, max_zoom=2.0, anchor_y=0.42):
    """Camera rect (x, y, w, h) in screenshot pixels that frames `box` above the caption area."""
    x0, y0, x1, y1 = box
    cw = max((x1 - x0) * pad, (y1 - y0) * pad * 16 / 9, SW / max_zoom)
    cw = min(cw, SW)
    ch = cw * 9 / 16
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    rx = min(max(0.0, cx - cw / 2), SW - cw)
    ry = min(max(0.0, cy - ch * anchor_y), SH - ch)
    return (rx, ry, cw, ch)


def wrap(text, f, width):
    words, lines, cur = text.split(), [], ""
    for w in words:
        cand = f"{cur} {w}".strip()
        if f.getlength(cand) <= width:
            cur = cand
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def to_bgr(img: Image.Image) -> np.ndarray:
    return cv2.cvtColor(np.asarray(img), cv2.COLOR_RGB2BGR)


# ============================================================ phase 1: capture
def capture(base: str, work: Path, sample_pdf: Path) -> dict:
    from playwright.sync_api import sync_playwright

    shots: dict = {}
    up = work / "new_patient_riverside.pdf"
    shutil.copy(sample_pdf, up)
    new_id = None

    def bx(pg, sel, max_h=None):
        b = pg.locator(sel).first.bounding_box()
        h = b["height"] if not max_h else min(b["height"], max_h)
        return [b["x"] * 2, b["y"] * 2, (b["x"] + b["width"]) * 2, (b["y"] + h) * 2]

    def hl(pg):
        r = pg.evaluate("""() => { const els=[...document.querySelectorAll('#overlay .hl')]; if(!els.length) return null;
            const rs=els.map(e=>e.getBoundingClientRect()); const pw=document.querySelector('#pagewrap').getBoundingClientRect();
            const vis=rs.filter(r=>r.bottom>pw.top && r.top<pw.bottom); const u=(vis.length?vis:rs);
            return [Math.min(...u.map(r=>r.left)),Math.min(...u.map(r=>r.top)),Math.max(...u.map(r=>r.right)),Math.max(...u.map(r=>r.bottom))]; }""")
        return [v * 2 for v in r] if r else None

    def snap(name, pg, **boxes):
        p = work / f"{name}.jpg"
        pg.screenshot(path=str(p), type="jpeg", quality=92)
        shots[name] = {"img": str(p), **boxes}

    def click(pg, name, sel, wait=1400, focus=True):
        loc = pg.locator(sel).first
        loc.scroll_into_view_if_needed()
        pg.wait_for_timeout(400)
        target = bx(pg, sel)
        snap(name + "_pre", pg, target=target)
        loc.click()
        pg.wait_for_timeout(wait)
        snap(name + "_post", pg, target=target, focus=hl(pg) if focus else None)

    with sync_playwright() as p:
        br = p.chromium.launch(channel="msedge", headless=True)
        pg = br.new_page(viewport={"width": 1920, "height": 1080}, device_scale_factor=2, color_scheme="dark")
        try:
            pg.goto(base + "/#DOC_003")
            pg.wait_for_selector("#tab-body .card")
            pg.wait_for_timeout(1500)
            snap("overview", pg, upload=bx(pg, "label[for=file]"), side=bx(pg, ".side"), viewer=bx(pg, ".pane.right"))

            pg.set_input_files("#file", str(up))
            pg.wait_for_timeout(120)
            snap("uploading", pg, side=bx(pg, ".side"))
            pg.wait_for_function("() => location.hash && location.hash !== '#DOC_003'", timeout=60000)
            new_id = pg.evaluate("location.hash.slice(1)")
            pg.wait_for_selector("#tab-body .card")
            pg.wait_for_timeout(1600)
            snap("uploaded", pg, newdoc=bx(pg, f'#doclist li[data-id="{new_id}"]'), stats=bx(pg, "#stats"),
                 head=bx(pg, ".dochead"), viewer=bx(pg, ".pane.right"), page=bx(pg, "#pagewrap"))

            click(pg, "dose", 'tr.attr[data-eid="MED_003"][data-path="dosage"]')
            click(pg, "chip", '#card-MED_003 .chip:has-text("PRESCRIBED_BY")')
            click(pg, "ins", 'tr.attr[data-eid="INS_002"][data-path="insurance_id"]')
            click(pg, "fam", 'tr.attr[data-eid="FAM_001"][data-path="conditions"]')

            click(pg, "asktab", '#tabs button[data-tab="ask"]', wait=600, focus=False)
            pg.select_option("#aq", "find")
            pg.fill("#a1", "hypertension")
            pg.wait_for_timeout(300)
            click(pg, "askgo", '#askf button[type="submit"]', wait=1200, focus=False)
            shots["askgo_post"]["focus"] = bx(pg, "#ans", max_h=520)

            click(pg, "relstab", '#tabs button[data-tab="rels"]', wait=700, focus=False)
            click(pg, "relsunc", '#relf button[data-f="uncertain"]', wait=700, focus=False)
            shots["relsunc_post"]["focus"] = bx(pg, "#tab-body", max_h=520)
            click(pg, "rel45", '.relrow[data-rel="REL_045"]')

            click(pg, "revtab", '#tabs button[data-tab="review"]', wait=700, focus=False)
            shots["revtab_post"]["focus"] = bx(pg, "#tab-body", max_h=560)
            click(pg, "revitem", '.item:has-text("CONS_002.decision")')

            click(pg, "checks", '#tabs button[data-tab="checks"]', wait=700, focus=False)
            shots["checks_post"]["focus"] = bx(pg, "#tab-body", max_h=640)

            pg.click("#prev")                      # back to page 1, where the medication table is
            pg.wait_for_timeout(900)
            click(pg, "std", '#which button[data-w="standardized"]', wait=1500, focus=False)
            pg.evaluate("() => { const w=document.querySelector('#pagewrap'); w.scrollTo({top: w.scrollHeight*0.47}); }")
            pg.wait_for_timeout(700)
            snap("std_meds", pg, focus=bx(pg, "#pagewrap"))

            with urllib.request.urlopen(f"{base}/api/doc/{new_id}") as r:
                doc = json.loads(r.read())["doc"]
        finally:
            if new_id:
                urllib.request.urlopen(urllib.request.Request(f"{base}/api/doc/{new_id}", method="DELETE")).read()
            br.close()
    return {"shots": shots, "doc": doc, "doc_id": new_id}


# ============================================================ phase 2: cards
@lru_cache(None)
def backdrop() -> np.ndarray:
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    d = np.sqrt(((xx - W * 0.22) / W) ** 2 + ((yy - H * 0.2) / H) ** 2)
    k = np.clip(1 - d / 1.1, 0, 1)[..., None]
    lo, hi = np.array(PAL["bg"], np.float32), np.array((30, 35, 48), np.float32)
    return (lo + (hi - lo) * k ** 2).astype(np.uint8)


def canvas():
    img = Image.fromarray(backdrop().copy())
    return img, ImageDraw.Draw(img)


def text(d, xy, s, f, color, a=1.0, under=PAL["bg"], rise=16):
    if a <= 0:
        return
    d.text((xy[0], xy[1] + (1 - a) * rise), s, font=f, fill=mix(under, color, a))


def card_title(t):
    img, d = canvas()
    a1, a2, a3 = ease(t / 0.8), ease((t - 0.5) / 0.8), ease((t - 1.1) / 0.8)
    x = 170
    s = 78
    cx, cy = x + s / 2, 352 + (1 - a1) * 16
    d.rounded_rectangle([x, cy - s / 2, x + s, cy + s / 2], radius=18, fill=mix(PAL["bg"], PAL["accent"], a1))
    d.polygon([(cx, cy - 22), (cx + 22, cy), (cx, cy + 22), (cx - 22, cy)], fill=mix(PAL["accent"], (255, 255, 255), a1))
    text(d, (x + 110, 290), "docintel", font("black", 112), PAL["ink"], a1)
    text(d, (x, 450), "How relationship-aware intake extraction works", font("semi", 50), PAL["ink"], a2)
    text(d, (x, 528), "PDF  \u2192  entities + relationships  \u2192  validated JSON  \u2192  standardized form",
         font("mono", 32), PAL["accent"], a3)
    text(d, (x, 960), "Walkthrough of the local web app  \u00b7  all people and data shown are fictional",
         font("reg", 26), PAL["muted"], a3)
    return to_bgr(img)


PROBLEMS = [
    ("Metformin   Dr. Alan Chen   500 mg", "Which dose and doctor belong to which drug?"),
    ("Member ID: BRH-4471920    Member ID: LKM-88120", "Which policy is primary, which is secondary?"),
    ("Mother: \u2026hypertension     [X] Hypertension", "Is it the patient's condition or the mother's?"),
    ("Signature: ~~~     Print Name: Ana Lopez", "Did the patient actually sign?"),
]


def card_problem(t):
    img, d = canvas()
    text(d, (160, 150), "Plain text extraction loses who owns what", font("bold", 64), PAL["ink"], ease(t / 0.7))
    text(d, (160, 245), "The same words mean different things depending on where they sit on the page.",
         font("reg", 34), PAL["muted"], ease((t - 0.3) / 0.7))
    for i, (raw, q) in enumerate(PROBLEMS):
        a = ease((t - 1.0 - i * 1.1) / 0.6)
        if a <= 0:
            continue
        y = 370 + i * 150
        d.rounded_rectangle([160, y, 1010, y + 96], radius=14, fill=mix(PAL["bg"], PAL["panel"], a),
                            outline=mix(PAL["bg"], PAL["line"], a), width=2)
        text(d, (192, y + 28), raw, font("mono", 30), PAL["ink"], a, PAL["panel"], 0)
        text(d, (1050, y + 22), "\u2192", font("semi", 42), PAL["muted"], a, rise=0)
        text(d, (1120, y + 26), q, font("semi", 36), PAL["warn"], a, rise=0)
    return to_bgr(img)


STAGES = [
    ("Read", ["Form fields", "Page text + checkboxes", "OCR for scans"]),
    ("Locate", ["Headings become", "page regions", "Table rows found"]),
    ("Bind", ["A value joins an entity", "only in the same", "section and row"]),
    ("Relate", ["TAKES_MEDICATION", "PRESCRIBED_BY, SIGNS", "explicit or uncertain"]),
    ("Validate", ["7 relationship checks", "Missing information", "Manual review queue"]),
]
OUTPUTS = ["DOC_xxx.json  (one per PDF)", "Standardized reference PDF", "Batch index + review queue"]


def card_pipeline(t):
    img, d = canvas()
    text(d, (110, 120), "How a PDF becomes structured, validated data", font("bold", 58), PAL["ink"], ease(t / 0.7))
    bw, gap, x0, y0, bh = 316, 30, 110, 300, 290
    for i, (title, lines) in enumerate(STAGES):
        a = ease((t - 0.7 - i * 1.25) / 0.6)
        if a <= 0:
            continue
        x = x0 + i * (bw + gap)
        d.rounded_rectangle([x, y0, x + bw, y0 + bh], radius=16, fill=mix(PAL["bg"], PAL["panel"], a),
                            outline=mix(PAL["bg"], PAL["accent"] if i == 2 else PAL["line"], a), width=2)
        text(d, (x + 28, y0 + 26), f"{i + 1}", font("monob", 30), PAL["accent"], a, PAL["panel"], 0)
        text(d, (x + 28, y0 + 66), title, font("bold", 44), PAL["ink"], a, PAL["panel"], 0)
        for k, ln in enumerate(lines):
            text(d, (x + 28, y0 + 142 + k * 38), ln, font("reg", 26), PAL["muted"], a, PAL["panel"], 0)
        if i:
            ax = x - gap + 4
            d.polygon([(ax, y0 + bh / 2 - 9), (ax + gap - 10, y0 + bh / 2), (ax, y0 + bh / 2 + 9)],
                      fill=mix(PAL["bg"], PAL["muted"], a))
    a = ease((t - 0.7 - 5 * 1.25) / 0.7)
    if a > 0:
        text(d, (110, 668), "Outputs", font("semi", 30), PAL["muted"], a)
        x = 110
        for o in OUTPUTS:
            w = font("semi", 30).getlength(o) + 56
            d.rounded_rectangle([x, 720, x + w, 790], radius=35, fill=mix(PAL["bg"], PAL["panel"], a),
                                outline=mix(PAL["bg"], PAL["ok"], a), width=2)
            text(d, (x + 28, 735), o, font("semi", 30), PAL["ok"], a, PAL["panel"], 0)
            x += w + 26
        text(d, (110, 880), "Step 3 is the key rule: values are tied together by where they sit, never by guesswork.",
             font("reg", 32), PAL["ink"], ease((t - 8.4) / 0.7))
    return to_bgr(img)


TOKEN = re.compile(r'"[^"]*"|-?\d+(?:\.\d+)?|[^"\d]+')


def code_block(d, x, y, lines, a, size=27, lh=38):
    f = font("mono", size)
    for i, ln in enumerate(lines):
        cx = x
        for m in TOKEN.finditer(ln):
            tok = m.group(0)
            rest = ln[m.end():].lstrip()
            if tok.startswith('"'):
                col = PAL["accent"] if rest.startswith(":") else PAL["ok"]
            elif re.fullmatch(r"-?\d+(?:\.\d+)?", tok):
                col = PAL["warn"]
            else:
                col = PAL["muted"]
            d.text((cx, y + i * lh), tok, font=f, fill=mix(PAL["code"], col, a))
            cx += f.getlength(tok)


def json_lines(doc):
    med = next(m for m in doc["entities"]["medications"] if m["name"]["value"] == "Sertraline")
    rel = next(r for r in doc["relationships"] if r["predicate"] == "PRESCRIBED_BY" and r["subject"] == med["medication_id"])
    n, ds = med["name"], med["dosage"]
    e = [
        f'"{med["medication_id"]}": {{',
        f'  "name":   {{"value": "{n["value"]}", "field_label": "{n["field_label"]}", "page": {n["source"]["page"]},',
        f'             "bbox": {n["source"]["bbox"]}, "confidence": {n["confidence"]}}},',
        f'  "dosage": {{"value": "{ds["value"]}", "field_label": "{ds["field_label"]}", "page": {ds["source"]["page"]},',
        f'             "extraction_method": "{ds["extraction_method"]}", "status": "{ds["validation"]["status"]}"}},',
        f'  "prescribing_doctor_id": "{med["prescribing_doctor_id"]}"',
        "}",
    ]
    r = [
        f'{{"relationship_id": "{rel["relationship_id"]}", "subject": "{rel["subject"]}",',
        f' "predicate": "{rel["predicate"]}", "object": "{rel["object"]}",',
        f' "support": "{rel["support"]}", "confidence": {rel["confidence"]},',
        f' "basis": "{rel["basis"]}",',
        f' "evidence": {{"pages": {rel["evidence"]["pages"]}, "locations": [heading, name cell, doctor cell]}}}}',
    ]
    return e, r


def make_card_json(doc):
    ent_lines, rel_lines = json_lines(doc)

    def card(t):
        img, d = canvas()
        text(d, (160, 96), "One JSON file per PDF", font("bold", 58), PAL["ink"], ease(t / 0.7))
        text(d, (160, 178), "Every value carries its evidence. Every link says why it exists.", font("reg", 32),
             PAL["muted"], ease((t - 0.3) / 0.7))
        for (y0, lines, label, delay) in ((262, ent_lines, "entity  (traced attributes)", 0.8),
                                          (660, rel_lines, "relationship  (with evidence)", 2.4)):
            a = ease((t - delay) / 0.7)
            if a <= 0:
                continue
            hgt = 56 + len(lines) * 38
            d.rounded_rectangle([160, y0, 1760, y0 + hgt], radius=14, fill=mix(PAL["bg"], PAL["code"], a),
                                outline=mix(PAL["bg"], PAL["line"], a), width=2)
            text(d, (1760 - 30 - font("semi", 24).getlength(label), y0 + 14), label, font("semi", 24), PAL["muted"],
                 a, PAL["code"], 0)
            code_block(d, 190, y0 + 30, lines, a)
        return to_bgr(img)
    return card


def card_outro(t):
    img, d = canvas()
    text(d, (160, 130), "Run it yourself", font("bold", 62), PAL["ink"], ease(t / 0.6))
    rows = [("python -m docintel serve", "web app at http://localhost:8000"),
            ("python -m docintel process samples/input -o output", "one JSON + standardized PDF per file"),
            ("python -m docintel ask <DOC.json> owner COND_006", "relationship questions from the terminal"),
            ("python -m docintel scan-template <reference.pdf>", "map your real reference form")]
    for i, (cmd, what) in enumerate(rows):
        a = ease((t - 0.6 - i * 0.6) / 0.6)
        y = 270 + i * 140
        d.rounded_rectangle([160, y, 1110, y + 88], radius=12, fill=mix(PAL["bg"], PAL["code"], a),
                            outline=mix(PAL["bg"], PAL["line"], a), width=2)
        text(d, (190, y + 24), cmd, font("mono", 30), PAL["ok"], a, PAL["code"], 0)
        text(d, (1150, y + 24), what, font("reg", 32), PAL["ink"], a, rise=0)
    text(d, (160, 880), "docintel  \u00b7  relationships you can trace back to the page", font("semi", 34),
         PAL["accent"], ease((t - 3.4) / 0.8))
    return to_bgr(img)


# ============================================================ phase 2: segments
class Seg:
    def __init__(self, dur, caption=None, card=None):
        self.dur, self.caption, self.card = dur, caption, card
        self.imgs: list = []     # (t, path)
        self.cam: list = []      # (t, rect)
        self.cur: list = []      # (t, (x, y) | None)  None = where the previous segment left the pointer
        self.clicks: list = []

    def click(self, t, pre, post, travel=0.9):
        tgt = center(pre["target"])
        self.imgs += [(max(0.0, t - travel - 0.1), pre["img"]), (t + 0.1, post["img"])]
        self.cur += [(t - travel, None if not self.cur else self.cur[-1][1]), (t - 0.12, tgt), (t + 0.3, tgt)]
        self.clicks.append(t)
        return self


@lru_cache(maxsize=10)
def levels(path):
    full = cv2.imread(path)
    if full.shape[1] != SW:
        full = cv2.resize(full, (SW, SH), interpolation=cv2.INTER_AREA)
    return full, cv2.resize(full, (SW // 2, SH // 2), interpolation=cv2.INTER_AREA)


def view(path, rect):
    full, half = levels(path)
    x, y, w, _ = rect
    s = W / w
    src, f = (half, 0.5) if s < 0.8 else (full, 1.0)
    sc = W / (w * f)
    m = np.float32([[sc, 0, -x * f * sc], [0, sc, -y * f * sc]])
    return cv2.warpAffine(src, m, (W, H), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)


@lru_cache(None)
def caption_layer(kicker, body):
    pw = 1500
    fk, fb = font("semi", 24), font("reg", 35)
    lines = wrap(body, fb, pw - 84)
    ph = 74 + len(lines) * 47 + 22
    img = Image.new("RGBA", (pw, ph), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, pw - 1, ph - 1], radius=18, fill=(15, 17, 21, 236), outline=(64, 71, 84, 255), width=2)
    x = 42
    for ch in kicker.upper():
        d.text((x, 24), ch, font=fk, fill=PAL["accent"] + (255,))
        x += fk.getlength(ch) + 2.5
    for i, ln in enumerate(lines):
        d.text((42, 62 + i * 47), ln, font=fb, fill=PAL["ink"] + (255,))
    arr = np.asarray(img).astype(np.float32)
    return arr[..., 2::-1].copy(), arr[..., 3:4] / 255.0, (W - pw) // 2, H - ph - 46


CURSOR = np.array([(0, 0), (0, 30), (8, 23), (14, 36), (20, 33), (14, 21), (24, 21)], np.float32) * 1.25


def draw_pointer(f, x, y):
    pts = (CURSOR + (x, y)).astype(np.int32)
    cv2.fillPoly(f, [pts], (255, 255, 255), cv2.LINE_AA)
    cv2.polylines(f, [pts], True, (25, 25, 25), 2, cv2.LINE_AA)


def draw_ripple(f, x, y, k):
    r, a = int(12 + 42 * k), 0.65 * (1 - k)
    x0, y0, x1, y1 = max(0, int(x) - 60), max(0, int(y) - 60), min(W, int(x) + 60), min(H, int(y) + 60)
    if x1 <= x0 or y1 <= y0:
        return
    roi = f[y0:y1, x0:x1]
    ov = roi.copy()
    cv2.circle(ov, (int(x) - x0, int(y) - y0), r, PAL["accent"][::-1], 5, cv2.LINE_AA)
    cv2.addWeighted(ov, a, roi, 1 - a, 0, roi)


def seg_frames(s: Seg, start_ptr):
    n = int(round(s.dur * FPS))
    cur = [(t, start_ptr if p is None else p) for t, p in s.cur]
    imgs = sorted(s.imgs)
    for i in range(n):
        t = i / FPS
        if s.card:
            f = s.card(t)
        else:
            rect = interp(s.cam, t, lerp_seq)
            k = max(j for j, (ts, _) in enumerate(imgs) if ts <= t) if any(ts <= t for ts, _ in imgs) else 0
            f = view(imgs[k][1], rect)
            if k and t - imgs[k][0] < 0.18 and imgs[k][1] != imgs[k - 1][1]:
                a = (t - imgs[k][0]) / 0.18
                f = cv2.addWeighted(view(imgs[k - 1][1], rect), 1 - a, f, a, 0)
            if cur:
                sx, sy = interp(cur, t, lerp_seq)
                ox, oy = (sx - rect[0]) * W / rect[2], (sy - rect[1]) * H / rect[3]
                for tc in s.clicks:
                    if 0 <= t - tc < 0.55:
                        draw_ripple(f, ox, oy, (t - tc) / 0.55)
                pressed = any(0 <= t - tc < 0.12 for tc in s.clicks)
                draw_pointer(f, ox + (1 if pressed else 0), oy + (1 if pressed else 0))
        if s.caption:
            bgr, al, cx, cy = caption_layer(*s.caption)
            al = al * ease((t - 0.25) / 0.45)
            roi = f[cy:cy + bgr.shape[0], cx:cx + bgr.shape[1]].astype(np.float32)
            f[cy:cy + bgr.shape[0], cx:cx + bgr.shape[1]] = (roi * (1 - al) + bgr * al).astype(np.uint8)
        yield f
    s.end_ptr = cur[-1][1] if cur else start_ptr


class Encoder:
    """H.264 via ffmpeg (imageio-ffmpeg's bundled binary or ffmpeg on PATH); OpenCV mp4v as a last resort."""

    def __init__(self, out: Path):
        exe = None
        try:
            import imageio_ffmpeg
            exe = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            exe = shutil.which("ffmpeg")
        self.p = self.vw = None
        if exe:
            self.p = subprocess.Popen(
                [exe, "-hide_banner", "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "bgr24",
                 "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-", "-c:v", "libx264", "-preset", "medium", "-crf", "20",
                 "-profile:v", "high", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out)],
                stdin=subprocess.PIPE)
        else:
            print("ffmpeg not found (pip install imageio-ffmpeg); writing a larger MPEG-4 file with OpenCV")
            self.vw = cv2.VideoWriter(str(out), cv2.VideoWriter_fourcc(*"mp4v"), FPS, (W, H))

    def write(self, f):
        if self.p:
            self.p.stdin.write(f.tobytes())
        else:
            self.vw.write(f)

    def close(self):
        if self.p:
            self.p.stdin.close()
            if self.p.wait():
                raise RuntimeError("ffmpeg failed")
        else:
            self.vw.release()


def render(segs, out: Path):
    total = sum(int(round(s.dur * FPS)) for s in segs) - XF * (len(segs) - 1)
    out.parent.mkdir(parents=True, exist_ok=True)
    vw = Encoder(out)
    written, tail, ptr = 0, [], (SW * 0.7, SH * 0.55)
    for si, s in enumerate(segs):
        n = int(round(s.dur * FPS))
        nxt = []
        for i, f in enumerate(seg_frames(s, ptr)):
            if i < len(tail):
                a = (i + 1) / (len(tail) + 1)
                f = cv2.addWeighted(tail[i], 1 - a, f, a, 0)
            if si < len(segs) - 1 and i >= n - XF:
                nxt.append(f)
                continue
            p = written / max(1, total - 1)
            cv2.rectangle(f, (0, H - 5), (W, H), (40, 44, 52), -1)
            cv2.rectangle(f, (0, H - 5), (int(W * p), H), PAL["accent"][::-1], -1)
            vw.write(f)
            written += 1
        tail = nxt
        ptr = getattr(s, "end_ptr", ptr)
        print(f"  segment {si + 1}/{len(segs)} rendered", flush=True)
    vw.close()
    return written


def build(cap) -> list[Seg]:
    S = cap["shots"]
    segs = [Seg(4.6, card=card_title), Seg(8.6, card=card_problem), Seg(11.0, card=card_pipeline)]

    s = Seg(5.0, ("The app", "Every uploaded PDF becomes its own document: extracted entities on the left, "
                             "the original page on the right."))
    s.imgs, s.cam = [(0, S["overview"]["img"])], [(0, FULL), (5, (96.0, 54.0, SW - 192.0, SH - 108.0))]
    segs.append(s)

    s = Seg(6.4, ("Upload", "Drop in a registration form from another clinic. It is read, structured and "
                            "validated in about a second."))
    up = {"img": S["overview"]["img"], "target": S["overview"]["upload"]}
    s.imgs = [(0, S["overview"]["img"])]
    s.click(1.6, up, {"img": S["uploading"]["img"]})
    s.imgs.append((2.4, S["uploaded"]["img"]))
    s.cam = [(0, FULL), (2.6, FULL), (4.0, cam_for(union(S["uploaded"]["newdoc"], S["uploaded"]["head"],
                                                         S["uploaded"]["stats"]), pad=1.15, max_zoom=1.7))]
    segs.append(s)

    s = Seg(6.0, ("A different layout", "This clinic uses its own headings, prints Primary and Secondary insurance "
                                        "side by side, and orders the medication columns differently."))
    s.imgs = [(0, S["uploaded"]["img"])]
    s.cam = [(0, cam_for(S["uploaded"]["viewer"], pad=1.05, max_zoom=1.6)),
             (6, cam_for(S["uploaded"]["page"], pad=1.0, max_zoom=1.75, anchor_y=0.3))]
    segs.append(s)

    def click_seg(name, caption, dur=8.0, t=2.0, zoom_post=1.9):
        pre, post = S[name + "_pre"], S[name + "_post"]
        sg = Seg(dur, caption).click(t, pre, post)
        sg.imgs.insert(0, (0, pre["img"]))
        a = cam_for(pre["target"], pad=1.25, max_zoom=1.6)
        b = cam_for(post["focus"], pad=2.4, max_zoom=zoom_post) if post.get("focus") else a
        sg.cam = [(0, a), (t + 0.5, a), (t + 1.9, b), (dur, b)]
        return sg

    segs.append(click_seg("dose", ("Every value is traceable", "Click any value and it is highlighted on the source "
                                   "page, with its printed label, page, method and confidence.")))
    segs.append(click_seg("chip", ("Relationships from structure", "Sertraline is PRESCRIBED_BY Dr. Priya Nair because "
                                   "the drug cell and the doctor cell sit on the same table row.")))
    segs.append(click_seg("ins", ("Policies never merge", "Side-by-side columns become separate page regions, so the "
                                  "secondary policy cannot pick up values from the primary one.")))
    segs.append(click_seg("fam", ("Whose information is it?", "The mother's conditions belong to the Mother record. "
                                  "The patient's own history is a separate set of entities.")))

    s = Seg(9.0, ("Ask the document", "Who owns 'hypertension'? The patient on page 1, and the patient's mother on "
                                      "page 2. Same word, two different people."))
    s.imgs = [(0, S["asktab_pre"]["img"])]
    s.click(1.4, S["asktab_pre"], S["asktab_post"]).click(3.4, S["askgo_pre"], S["askgo_post"])
    a = cam_for(union(S["asktab_pre"]["target"], S["askgo_pre"]["target"]), pad=1.4, max_zoom=1.7)
    b = cam_for(S["askgo_post"]["focus"], pad=1.15, max_zoom=1.8, anchor_y=0.36)
    s.cam = [(0, a), (3.9, a), (5.2, b), (9, b)]
    segs.append(s)

    s = Seg(9.6, ("Uncertain, not guessed", "The financial policy was signed by Ana Lopez, not the patient. SIGNS is "
                                            "kept but marked uncertain and sent for review."))
    s.imgs = [(0, S["relstab_pre"]["img"])]
    s.click(1.2, S["relstab_pre"], S["relstab_post"]).click(2.8, S["relsunc_pre"], S["relsunc_post"])
    s.click(5.0, S["rel45_pre"], S["rel45_post"])
    a = cam_for(union(S["relstab_pre"]["target"], S["relsunc_post"]["focus"]), pad=1.2, max_zoom=1.7)
    b = cam_for(S["rel45_post"]["focus"], pad=2.0, max_zoom=1.9)
    s.cam = [(0, a), (5.5, a), (6.9, b), (9.6, b)]
    segs.append(s)

    s = Seg(8.4, ("Review queue", "Both Yes and No are ticked for text messages, so the answer stays unresolved and "
                                  "is queued for a person instead of being picked."))
    s.imgs = [(0, S["revtab_pre"]["img"])]
    s.click(1.2, S["revtab_pre"], S["revtab_post"]).click(3.4, S["revitem_pre"], S["revitem_post"])
    a = cam_for(S["revtab_post"]["focus"], pad=1.15, max_zoom=1.7)
    b = cam_for(S["revitem_post"]["focus"], pad=2.6, max_zoom=1.9)
    s.cam = [(0, a), (3.9, a), (5.3, b), (8.4, b)]
    segs.append(s)

    s = Seg(6.6, ("Validation", "Seven independent checks run on every document, from file isolation to "
                                "signature-to-agreement binding."))
    s.imgs = [(0, S["checks_pre"]["img"])]
    s.click(1.2, S["checks_pre"], S["checks_post"])
    a = cam_for(S["checks_pre"]["target"], pad=1.4, max_zoom=1.6)
    b = cam_for(S["checks_post"]["focus"], pad=1.1, max_zoom=1.75, anchor_y=0.36)
    s.cam = [(0, a), (1.6, a), (3.0, b), (6.6, b)]
    segs.append(s)

    s = Seg(8.6, ("Standardized form", "The JSON fills the reference template by structure. Values awaiting review, "
                                       "like the wrapped Vitamin D3 frequency, are left blank."))
    s.imgs = [(0, S["std_pre"]["img"])]
    s.click(1.2, S["std_pre"], S["std_post"])
    s.imgs.append((4.2, S["std_meds"]["img"]))
    a = cam_for(S["std_pre"]["target"], pad=1.4, max_zoom=1.6)
    b = cam_for(S["std_meds"]["focus"], pad=1.0, max_zoom=1.75, anchor_y=0.3)
    s.cam = [(0, a), (1.7, a), (3.6, b), (8.6, b)]
    segs.append(s)

    segs.append(Seg(8.6, card=make_card_json(cap["doc"])))
    segs.append(Seg(6.6, card=card_outro))
    return segs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8000")
    ap.add_argument("-o", "--output", default=str(ROOT / "video" / "docintel_how_it_works.mp4"))
    ap.add_argument("--work", help="folder for screenshots (default: a temp folder)")
    ap.add_argument("--sample", default=str(ROOT / "samples" / "input" / "intake_C_other_clinic_rivera.pdf"))
    ap.add_argument("--reuse", action="store_true", help="re-render from the screenshots already in --work")
    a = ap.parse_args()
    work = Path(a.work) if a.work else Path(tempfile.mkdtemp(prefix="docintel_video_"))
    work.mkdir(parents=True, exist_ok=True)
    if a.reuse:
        cap = json.loads((work / "manifest.json").read_text(encoding="utf-8"))
    else:
        try:
            urllib.request.urlopen(a.base + "/api/documents").read()
        except Exception:
            sys.exit(f"The web app is not reachable at {a.base}. Start it with: python -m docintel serve")
        print("capturing the web app ...", flush=True)
        cap = capture(a.base, work, Path(a.sample))
        (work / "manifest.json").write_text(json.dumps(cap, default=str), encoding="utf-8")
    print(f"captured {len(cap['shots'])} screenshots; rendering ...", flush=True)
    segs = build(cap)
    n = render(segs, Path(a.output))
    print(f"written: {a.output}  ({n} frames, {n / FPS:.1f} s)")


if __name__ == "__main__":
    main()
