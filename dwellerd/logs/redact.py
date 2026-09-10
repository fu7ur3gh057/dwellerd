"""Best-effort removal of credentials from captured log lines.

Log lines leave the host twice: they are persisted in SQLite and samples are
sent to Telegram. Redaction therefore happens before either operation. The
rules intentionally favour hiding a suspicious value over preserving an exact
copy of a log line.
"""
from __future__ import annotations

import re

_MASK = "<redacted>"

# postgres://user:pass@db/app or http://user:pass@proxy:3128. Keep the
# scheme and username because they are useful context and normally not secret.
_URL_CREDENTIALS = re.compile(
    r"(?i)\b([a-z][a-z0-9+.-]*://[^\s/:@]+:)([^\s/@]+)(@)"
)

_AUTHORIZATION = re.compile(
    r"(?i)\b((?:proxy[-_])?authorization\s*[:=]\s*)"
    r"(?:bearer|basic)\s+[^\s,;]+"
)

# password=..., "access_token": "...", api-key: ... . Quoted values may
# contain spaces; unquoted values stop at a normal key/value delimiter.
_NAMED_SECRET = re.compile(
    r"""(?ix)
    (\b[\"']?(?:
        password|passwd|pwd|secret|token|access[_-]?token|refresh[_-]?token|
        api[_-]?key|apikey|client[_-]?secret|private[_-]?key|
        authorization|proxy[_-]?authorization|cookie|set[_-]?cookie
    )[\"']?\s*[:=]\s*)
    (
        \"(?:\\.|[^\"\\])*\" |
        '(?:\\.|[^'\\])*' |
        [^\s,;&}\]]+
    )
    """
)

# Standalone credentials which often appear without a key name.
_TELEGRAM_TOKEN = re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{20,}\b")
_JWT = re.compile(
    r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\b"
)
_AWS_ACCESS_KEY = re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")
_PREFIXED_TOKEN = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
    r"sk-[A-Za-z0-9_-]{20,}|xox[baprs]-[A-Za-z0-9-]{10,})\b"
)


def redact_secrets(line: str) -> str:
    """Replace likely credentials with a deterministic marker."""
    text = str(line)
    text = _URL_CREDENTIALS.sub(
        lambda match: f"{match.group(1)}{_MASK}{match.group(3)}", text,
    )
    text = _AUTHORIZATION.sub(lambda match: f"{match.group(1)}{_MASK}", text)
    text = _NAMED_SECRET.sub(lambda match: f"{match.group(1)}{_MASK}", text)
    text = _TELEGRAM_TOKEN.sub(_MASK, text)
    text = _JWT.sub(_MASK, text)
    text = _AWS_ACCESS_KEY.sub(_MASK, text)
    text = _PREFIXED_TOKEN.sub(_MASK, text)
    return text
