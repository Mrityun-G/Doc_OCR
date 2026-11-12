"""Optional OCR adapter for image-only pages.

Uses Tesseract through pytesseract when both are installed; OCR words are converted to PDF
coordinates and fed into the same layout engine as native text, with each word's OCR
confidence carried into the field confidence. When OCR is unavailable the page is reported
as 'needs_ocr' and routed to manual review instead of being silently skipped.
"""
from __future__ import annotations

from .layout import Word


def ocr_available() -> bool:
    try:
        import pytesseract  # noqa: F401
        pytesseract.get_tesseract_version()
        return True
    except Exception:
        return False


def ocr_page_words(doc, pno: int, dpi: int = 300) -> list[Word] | None:
    """Return OCR'd words for page `pno` (1-based) or None when OCR is unavailable."""
    if not ocr_available():
        return None
    import pytesseract
    from PIL import Image

    page = doc[pno - 1]
    pix = page.get_pixmap(dpi=dpi, alpha=False)
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)
    scale = 72.0 / dpi
    words = []
    for i, txt in enumerate(data["text"]):
        txt = (txt or "").strip()
        try:
            conf = float(data["conf"][i])
        except (TypeError, ValueError):
            conf = -1
        if not txt or conf < 0:
            continue
        x, y, w, h = data["left"][i], data["top"][i], data["width"][i], data["height"][i]
        words.append(Word(x * scale, y * scale, (x + w) * scale, (y + h) * scale, txt, pno,
                          conf=max(0.0, min(1.0, conf / 100.0)), origin="ocr"))
    return words
