"""Small shared utilities: identifiers, timestamps, and stream hashing."""

from __future__ import annotations

import hashlib
import os
import secrets
import uuid
from datetime import datetime, timezone
from typing import Any, Iterator

__all__ = [
    "new_id",
    "now_iso",
    "utc_now",
    "parse_iso",
    "seconds_from_now_iso",
    "sha256_file",
    "sha256_bytes",
    "constant_time_equals",
    "new_token",
    "chunked",
]

_PREFIXES = {
    "instance": "inst",
    "document": "doc",
    "batch": "batch",
    "operation": "op",
    "job": "job",
    "task": "task",
    "note": "note",
    "profile": "prof",
    "conversation": "conv",
    "message": "msg",
    "evidence": "ev",
    "request": "req",
    "session": "sess",
    "filter": "flt",
}


def new_id(kind: str) -> str:
    """Opaque, unguessable identifier.

    A document ID is never derived from a candidate name, a path, or a hash of
    either (PRD section 4). It is stable for the life of the submission, across
    renames and managed moves.
    """
    prefix = _PREFIXES.get(kind, kind)
    return f"{prefix}_{uuid.uuid4().hex}"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    """ISO-8601 UTC with an explicit offset, microsecond precision."""
    return utc_now().isoformat()


def parse_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def seconds_from_now_iso(seconds: float) -> str:
    from datetime import timedelta

    return (utc_now() + timedelta(seconds=seconds)).isoformat()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str | os.PathLike[str], *, chunk: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


def constant_time_equals(a: str, b: str) -> bool:
    return secrets.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def new_token(nbytes: int = 32) -> str:
    """URL-safe secret used for pairing links and CSRF tokens."""
    return secrets.token_urlsafe(nbytes)


def chunked(items: list[Any], size: int) -> Iterator[list[Any]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]
