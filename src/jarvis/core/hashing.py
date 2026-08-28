"""Canonical serialisation and hashing."""

import datetime
import hashlib
import json
from pathlib import Path

from jarvis.core.errors import HashingError


def _reject_non_canonical(obj: object) -> None:
    if isinstance(obj, (set, frozenset)):
        raise HashingError("sets have no stable order and cannot be canonically serialised")
    if isinstance(obj, (datetime.date, datetime.datetime, datetime.time)):
        raise HashingError(
            f"{type(obj).__name__} values are not canonically serialisable; "
            "convert to Nanos or an ISO string explicitly"
        )
    raise HashingError(f"object of type {type(obj).__name__!r} is not canonically serialisable")


def canonical_json(obj: object) -> str:
    """Deterministic JSON: sorted keys, no whitespace, ensure_ascii=False,
    floats rendered with repr(), None/True/False as JSON literals.
    Raises HashingError on any non-serialisable type (including sets,
    which have no stable order)."""
    try:
        return json.dumps(
            obj,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
            default=_reject_non_canonical,
        )
    except ValueError as exc:
        raise HashingError(f"value is not representable in canonical JSON: {exc}") from exc


def sha256_canonical(obj: object) -> str:
    """Hex SHA-256 of canonical_json(obj).encode('utf-8')."""
    return sha256_bytes(canonical_json(obj).encode("utf-8"))


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path, chunk_size: int = 1 << 20) -> str:
    """Hex SHA-256 of a file, read in chunks. Raises HashingError if missing."""
    if not path.is_file():
        raise HashingError(f"file not found: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()
