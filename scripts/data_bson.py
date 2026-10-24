"""Tiny streaming BSON reader (standard library only) for the LifeSnaps `fitbit.bson` dump.

The dump is 9.7 GB inside the zip, so we never extract it: `iter_docs()` reads the zip member as a stream and
yields (type_string, raw_bytes) for every top-level document without decoding it. `decode()` is a minimal decoder
for the BSON types that occur in the dump (it raises on anything else, so a surprise cannot pass silently).

    uv run python scripts/data_bson.py census [--limit-mb N]     # count documents per `type`, keep one example each
"""
from __future__ import annotations

import argparse
import json
import struct
import sys
import time
import zipfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

ROOT = Path(__file__).resolve().parent.parent
LIFESNAPS_ZIP = ROOT / "data" / "raw" / "lifesnaps_v4.zip"
BSON_MEMBER = "rais_anonymized/mongo_rais_anonymized/fitbit.bson"
_TYPE_KEY = b"\x02type\x00"


def decode(buf: bytes, pos: int = 0, is_array: bool = False):
    """Decode one BSON document starting at buf[pos]. Returns a dict (or list for arrays)."""
    (size,) = struct.unpack_from("<i", buf, pos)
    end = pos + size - 1                                  # last byte is the 0x00 terminator
    pos += 4
    out: dict = {}
    while pos < end:
        t = buf[pos]
        pos += 1
        z = buf.index(b"\x00", pos)
        key = buf[pos:z].decode("utf-8", "replace")
        pos = z + 1
        if t == 0x01:
            (val,) = struct.unpack_from("<d", buf, pos); pos += 8
        elif t == 0x02:
            (n,) = struct.unpack_from("<i", buf, pos)
            val = buf[pos + 4:pos + 4 + n - 1].decode("utf-8", "replace"); pos += 4 + n
        elif t in (0x03, 0x04):
            (n,) = struct.unpack_from("<i", buf, pos)
            val = decode(buf, pos, is_array=(t == 0x04)); pos += n
        elif t == 0x05:
            (n,) = struct.unpack_from("<i", buf, pos); val = buf[pos + 5:pos + 5 + n]; pos += 5 + n
        elif t == 0x07:
            val = buf[pos:pos + 12].hex(); pos += 12
        elif t == 0x08:
            val = buf[pos] == 1; pos += 1
        elif t == 0x09:
            (ms,) = struct.unpack_from("<q", buf, pos)
            val = datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat(); pos += 8
        elif t == 0x0A:
            val = None
        elif t == 0x10:
            (val,) = struct.unpack_from("<i", buf, pos); pos += 4
        elif t == 0x12:
            (val,) = struct.unpack_from("<q", buf, pos); pos += 8
        else:
            raise ValueError(f"unsupported BSON type 0x{t:02x} at key {key!r}")
        out[key] = val
    if is_array:
        return [out[k] for k in sorted(out, key=int)]
    return out


def doc_type(raw: bytes) -> str:
    """The value of the top-level `type` field, found without decoding the whole document."""
    i = raw.find(_TYPE_KEY, 0, 400)
    if i < 0:
        return "?"
    (n,) = struct.unpack_from("<i", raw, i + len(_TYPE_KEY))
    j = i + len(_TYPE_KEY) + 4
    return raw[j:j + n - 1].decode("utf-8", "replace")


def pid_hex(raw: bytes) -> str:
    """The participant id (an ObjectId in the `id` field), read from its fixed offset; falls back to a full decode."""
    if raw[21:25] == b"\x07id\x00":
        return raw[25:37].hex()
    return decode(raw)["id"]


def fast_heart_rate(raw: bytes) -> tuple[str, int | float, int]:
    """(dateTime, bpm, confidence) of a `heart_rate` document without a full decode (about 10x faster)."""
    i = raw.index(b"\x02dateTime\x00") + 10
    (n,) = struct.unpack_from("<i", raw, i)
    dt = raw[i + 4:i + 4 + n - 1].decode()
    j = raw.index(b"bpm\x00", i)
    bpm = struct.unpack_from("<i", raw, j + 4)[0] if raw[j - 1] == 0x10 else struct.unpack_from("<d", raw, j + 4)[0]
    k = raw.index(b"confidence\x00", j)
    conf = struct.unpack_from("<i", raw, k + 11)[0] if raw[k - 1] == 0x10 else int(struct.unpack_from("<d", raw, k + 11)[0])
    return dt, bpm, conf


def iter_docs(zip_path: Path = LIFESNAPS_ZIP, limit_bytes: int | None = None) -> Iterator[tuple[str, bytes]]:
    """Stream (type, raw document bytes) from the bson inside the zip (no extraction to disk)."""
    with zipfile.ZipFile(zip_path) as z, z.open(BSON_MEMBER) as f:
        read = 0
        buf = bytearray()
        chunk = 8 * 1024 * 1024
        pos = 0
        while True:
            if len(buf) - pos < 4:
                data = f.read(chunk)
                if not data:
                    return
                buf = buf[pos:] + data
                pos = 0
                continue
            (size,) = struct.unpack_from("<i", buf, pos)
            if len(buf) - pos < size:
                data = f.read(max(chunk, size))
                if not data:
                    raise EOFError("truncated bson")
                buf = buf[pos:] + data
                pos = 0
                continue
            raw = bytes(buf[pos:pos + size])
            pos += size
            read += size
            yield doc_type(raw), raw
            if limit_bytes and read >= limit_bytes:
                return


def census(limit_mb: int | None) -> dict:
    t0 = time.time()
    counts: Counter = Counter()
    examples: dict[str, dict] = {}
    nbytes: Counter = Counter()
    for typ, raw in iter_docs(limit_bytes=limit_mb * 1024 * 1024 if limit_mb else None):
        counts[typ] += 1
        nbytes[typ] += len(raw)
        if typ not in examples:
            examples[typ] = decode(raw)
    return {"seconds": round(time.time() - t0, 1), "documents": sum(counts.values()), "by_type": dict(counts.most_common()),
            "bytes_by_type": dict(nbytes.most_common()), "examples": examples}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["census"])
    ap.add_argument("--limit-mb", type=int, default=None)
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    res = census(a.limit_mb)
    text = json.dumps(res, indent=1, default=str)
    if a.out:
        Path(a.out).write_text(text)
    print(json.dumps({k: v for k, v in res.items() if k != "examples"}, indent=1))
