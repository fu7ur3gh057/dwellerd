"""Log-line signature — a stable fingerprint used to dedup near-identical
lines, so we report one alert per *kind* of error rather than one per
occurrence.

The line is normalized (timestamps, numbers, hex blobs, UUIDs and quoted
runs become placeholders) and then hashed together with the source name, so
the same message arriving from two different sources stays distinct. The
result is stored in `log_signatures`, which is what makes first-seen
detection survive a daemon restart.
"""
from __future__ import annotations

import hashlib
import re

_RE_UUID = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
# Long hex runs (addresses, hashes, request ids) are collapsed before plain
# numbers so the number rule doesn't shred them into a row of <n>.
_RE_HEX = re.compile(r"\b(?:0x)?[0-9a-fA-F]{8,}\b")
_RE_QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"")
_RE_NUM = re.compile(r"\d+")
_RE_WS = re.compile(r"\s+")

_MAX_NORMALIZED = 300


def normalize(line: str) -> str:
    """Collapse the variable parts of a log line to placeholders."""
    s = line.strip()
    s = _RE_UUID.sub("<uuid>", s)
    s = _RE_HEX.sub("<hex>", s)
    s = _RE_QUOTED.sub("<str>", s)
    s = _RE_NUM.sub("<n>", s)
    s = _RE_WS.sub(" ", s)
    return s[:_MAX_NORMALIZED]


def compute_signature(line: str, source: str = "") -> str:
    """Return a 16-char hex signature for `line`, scoped to `source`."""
    norm = normalize(line)
    digest = hashlib.sha1(f"{source}\x00{norm}".encode("utf-8", "replace"))
    return digest.hexdigest()[:16]
