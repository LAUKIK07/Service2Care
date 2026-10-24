"""Attachment helpers: file names, HEIC conversion, resizing, data URLs (macOS sips)."""
from __future__ import annotations

import base64
import mimetypes
import subprocess
import tempfile
from pathlib import Path

_EXT = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif", "image/webp": ".webp",
    "image/heic": ".heic", "image/heif": ".heif", "application/pdf": ".pdf",
}

NEEDS_CONVERSION = {"image/heic", "image/heif"}
MAX_PLAIN_BYTES = 2_000_000


def ext_for(mime: str | None, filename: str | None) -> str:
    if mime in _EXT:
        return _EXT[mime]
    if filename and Path(filename).suffix:
        return Path(filename).suffix.lower()[:8]
    return mimetypes.guess_extension(mime or "") or ".bin"


def mime_for_path(path: Path) -> str:
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def _sips_to_jpeg(path: Path) -> bytes | None:
    """Convert to JPEG and cap the long edge at 1600 px. Returns None if sips is unavailable."""
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "out.jpg"
        try:
            run = subprocess.run(
                ["sips", "-s", "format", "jpeg", "-Z", "1600", str(path), "--out", str(out)],
                capture_output=True, timeout=30,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return None
        if run.returncode != 0 or not out.exists():
            return None
        return out.read_bytes()


def to_data_url(path: Path, mime: str | None = None) -> str:
    """Return a data URL a vision model can read. HEIC and large images become JPEG."""
    mime = mime or mime_for_path(path)
    raw = path.read_bytes()
    if mime in NEEDS_CONVERSION or len(raw) > MAX_PLAIN_BYTES:
        converted = _sips_to_jpeg(path)
        if converted is not None:
            raw, mime = converted, "image/jpeg"
    return f"data:{mime};base64,{base64.b64encode(raw).decode()}"
