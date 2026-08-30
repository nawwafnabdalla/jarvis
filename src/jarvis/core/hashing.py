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


def _check_string_keys(obj: object, path: str = "$") -> None:
    """Recursively reject any non-string dict key, at any nesting depth.
    json.dumps silently coerces a non-string key to a string (int 1 ->
    "1", bool True -> "true"), which makes {1: 'x'} and {'1': 'x'} hash
    identically -- a genuine collision in a system whose integrity model
    rests on content hashes. Walked before json.dumps ever runs, so this
    is the sole source of truth for key-type rejection; sort_keys=True
    would otherwise raise a raw TypeError on a mixed-type key set, which
    this function makes unreachable in practice (json.dumps's own
    TypeError is still caught below as defence in depth)."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            if not isinstance(key, str):
                raise HashingError(
                    f"{path}: dict key {key!r} of type {type(key).__name__!r} is not "
                    "a string; only string keys are canonically serialisable"
                )
            _check_string_keys(value, f"{path}.{key}")
    elif isinstance(obj, (list, tuple)):
        for index, item in enumerate(obj):
            _check_string_keys(item, f"{path}[{index}]")


def canonical_json(obj: object) -> str:
    """Deterministic JSON: sorted keys, no whitespace, ensure_ascii=False,
    floats rendered with repr(), None/True/False as JSON literals.
    Raises HashingError on any non-serialisable type (including sets,
    which have no stable order) and on any non-string dict key at any
    nesting depth.

    NOTE ON CONTAINER IDENTITY: this hash does not distinguish Python
    container types. A tuple and a list with the same elements serialise
    to the same JSON array, so sha256_canonical((1, 2)) ==
    sha256_canonical([1, 2]). This is arguably correct for JSON
    round-tripping (JSON has no tuple type) and is a deliberate, documented
    decision -- not a defect. Do not change it; a caller that needs to
    distinguish tuples from lists must encode that distinction itself."""
    _check_string_keys(obj)
    try:
        return json.dumps(
            obj,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
            default=_reject_non_canonical,
        )
    except (TypeError, ValueError) as exc:
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
