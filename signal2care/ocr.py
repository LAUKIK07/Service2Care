"""Document reader: turns attached documents into normalized text for the model.

Routing
- Photos and screenshots go straight to the vision model. They only come here when the person
  asks for text to be read out of them.
- Plain text files are read as they are.
- PDFs: the text layer first (pdftotext: local, exact, free). A scanned PDF with no text layer is
  rendered page by page and each page is sent to OCR.space.
- Whatever the source, the result goes through one normalization step before the model sees it.

OCR.space free plan limits (docs checked 2026-10-24): 1 MB per file, 3 pages per PDF, 25,000
requests per month, 500 per day per IP. Engine 2 is the default; Engine 3 is used as a second try
for handwriting and tables (1,000 per month).
"""
from __future__ import annotations

import asyncio
import logging
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import httpx

from .media import fit_under

log = logging.getLogger("s2c.ocr")

OCRSPACE_URL = "https://api.ocr.space/parse/image"
MIN_TEXT_CHARS = 40       # below this a PDF is treated as scanned
MAX_CHARS = 12_000        # per document handed to the model
MAX_SCAN_PAGES = 6        # pages rendered and read per scanned PDF
FILE_LIMIT = 1_000_000    # OCR.space free plan


class OCRError(Exception):
    pass


_NO_TEXT = re.compile(r"^\W*\[?\s*no text detected\s*\]?\W*$", re.I)


_LOOKALIKE = {"\u0430": "a", "\u0435": "e", "\u043e": "o", "\u0440": "p", "\u0441": "c", "\u0445": "x",
              "\u0443": "y", "\u0456": "i", "\u0410": "A", "\u0412": "B", "\u0415": "E", "\u041a": "K",
              "\u041c": "M", "\u041d": "H", "\u041e": "O", "\u0420": "P", "\u0421": "C", "\u0422": "T",
              "\u0425": "X"}


def fix_mixed_script(text: str) -> str:
    """OCR sometimes returns a Cyrillic look-alike inside a Latin word (for example 'T\u0430x'). Map those back."""
    def fix(match: re.Match) -> str:
        word = match.group(0)
        if re.search(r"[A-Za-z]", word) and any(c in _LOOKALIKE for c in word):
            return "".join(_LOOKALIKE.get(c, c) for c in word)
        return word
    return re.sub(r"\S+", fix, text)


def _sniff(data: bytes) -> tuple[str, str]:
    """Content type and OCR.space filetype from the bytes themselves, not from the file name."""
    if data[:4] == b"%PDF":
        return "application/pdf", "PDF"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png", "PNG"
    if data[:3] == b"GIF":
        return "image/gif", "GIF"
    if data[:2] == b"BM":
        return "image/bmp", "BMP"
    return "image/jpeg", "JPG"


def normalize(text: str, limit: int = MAX_CHARS) -> str:
    """One shape for every source: no control characters, tidy whitespace, bounded length."""
    text = re.sub(r"[\x00-\x08\x0b\x0e-\x1f]", "", text or "").replace("\r\n", "\n").replace("\f", "\n\n---\n\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) > limit:
        text = text[:limit].rstrip() + f"\n[truncated, {len(text) - limit} more characters]"
    return text


def _pdftotext(path: Path) -> str:
    exe = shutil.which("pdftotext")
    if not exe:
        return ""
    run = subprocess.run([exe, "-layout", "-enc", "UTF-8", str(path), "-"], capture_output=True, timeout=60)
    return run.stdout.decode("utf-8", "replace") if run.returncode == 0 else ""


def _pdf_pages(path: Path) -> int:
    exe = shutil.which("pdfinfo")
    if not exe:
        return 0
    run = subprocess.run([exe, str(path)], capture_output=True, timeout=30)
    match = re.search(r"Pages:\s+(\d+)", run.stdout.decode("utf-8", "replace"))
    return int(match.group(1)) if match else 0


def _render_pages(path: Path, outdir: Path, pages: int) -> list[Path]:
    exe = shutil.which("pdftoppm")
    if not exe:
        return []
    run = subprocess.run(
        [exe, "-jpeg", "-jpegopt", "quality=75", "-r", "130", "-f", "1", "-l", str(pages), str(path),
         str(outdir / "page")],
        capture_output=True, timeout=120,
    )
    return sorted(outdir.glob("page*.jpg")) if run.returncode == 0 else []


class DocumentReader:
    def __init__(self, provider: str, api_key: str, http: httpx.AsyncClient | None):
        self.provider = provider
        self._key = api_key
        self._http = http

    @property
    def can_ocr(self) -> bool:
        return self.provider == "ocrspace" and bool(self._key) and self._http is not None

    async def read(self, path: Path, mime: str) -> tuple[str, str]:
        """Return (normalized_text, source). Source is 'text', 'text-layer' or 'ocr'; text may be empty."""
        if mime.startswith("text/") or path.suffix.lower() in (".txt", ".md", ".csv", ".json"):
            return normalize(path.read_text(errors="replace")), "text"
        if mime == "application/pdf":
            layer = normalize(await asyncio.to_thread(_pdftotext, path))
            if len(layer) >= MIN_TEXT_CHARS:
                return layer, "text-layer"
            return await self._ocr_pdf(path)
        if mime.startswith("image/"):
            return await self._ocr_image(path)
        return "", ""

    # ---------------------------------------------------------------- scanned PDFs
    async def _ocr_pdf(self, path: Path) -> tuple[str, str]:
        if not self.can_ocr:
            return "", ""
        total = await asyncio.to_thread(_pdf_pages, path)
        want = min(total or MAX_SCAN_PAGES, MAX_SCAN_PAGES)
        with tempfile.TemporaryDirectory() as tmp:
            pages = await asyncio.to_thread(_render_pages, path, Path(tmp), want)
            parts: list[str] = []
            if pages:
                for i, page in enumerate(pages, 1):
                    data = await asyncio.to_thread(fit_under, page, FILE_LIMIT)
                    if data is None:
                        continue
                    try:
                        text = await self._ocrspace(data, page.name)
                    except OCRError as exc:
                        log.info("page %d not read: %s", i, exc)
                        continue
                    if text:
                        parts.append(f"[page {i}]\n{text}")
            elif path.stat().st_size <= FILE_LIMIT:  # no renderer installed: send the PDF itself (first 3 pages)
                try:
                    text = await self._ocrspace(path.read_bytes(), path.name)
                    if text:
                        parts.append(text)
                except OCRError as exc:
                    log.info("pdf not read: %s", exc)
        if not parts:
            return "", ""
        body = "\n\n".join(parts)
        if total > want:
            body += f"\n[only the first {want} of {total} pages were read]"
        return normalize(body), "ocr"

    # ---------------------------------------------------------------------- images
    async def _ocr_image(self, path: Path) -> tuple[str, str]:
        if not self.can_ocr:
            return "", ""
        data = await asyncio.to_thread(fit_under, path, FILE_LIMIT)
        if data is None:
            return "", ""
        try:
            return normalize(await self._ocrspace(data, path.name)), "ocr"
        except OCRError as exc:
            log.info("image not read: %s", exc)
            return "", ""

    # ------------------------------------------------------------------- OCR.space
    async def _ocrspace(self, data: bytes, filename: str) -> str:
        """Engine 2 first. If it finds almost nothing, try Engine 3 (handwriting, tables) once."""
        text = await self._ocrspace_once(data, filename, engine=2)
        if len(text.strip()) < 20:
            try:
                alt = await self._ocrspace_once(data, filename, engine=3)
            except OCRError:
                return text
            return alt if len(alt.strip()) > len(text.strip()) else text
        return text

    async def _ocrspace_once(self, data: bytes, filename: str, *, engine: int) -> str:
        mime, filetype = _sniff(data)
        form = {"OCREngine": str(engine), "scale": "true", "detectOrientation": "true", "isTable": "true",
                "isOverlayRequired": "false", "filetype": filetype}
        resp = None
        for attempt in range(2):
            try:
                resp = await self._http.post(OCRSPACE_URL, headers={"apikey": self._key}, data=form,
                                             files={"file": (filename, data, mime)}, timeout=90)
            except httpx.TransportError as exc:
                if attempt == 1:
                    raise OCRError(f"network error ({type(exc).__name__})") from exc
                await asyncio.sleep(1.5)
                continue
            if resp.status_code >= 500 and attempt == 0:
                await asyncio.sleep(1.5)
                continue
            break
        if resp is None or resp.status_code != 200:
            raise OCRError(f"http {resp.status_code if resp else 'none'}")
        try:
            body = resp.json()
        except ValueError as exc:
            raise OCRError("unreadable response") from exc
        if body.get("IsErroredOnProcessing"):
            raise OCRError(str(body.get("ErrorMessage") or body.get("ErrorDetails") or "processing error")[:200])
        texts = [(p.get("ParsedText") or "").strip() for p in body.get("ParsedResults") or []]
        texts = ["" if _NO_TEXT.match(t) else fix_mixed_script(t) for t in texts]
        return "\n\n---\n\n".join(t for t in texts if t).strip()
