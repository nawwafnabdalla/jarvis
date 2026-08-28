"""ULID generation and prefixed identifiers."""

import re
import secrets
import time
from typing import Literal

from jarvis.core.errors import IdError

IdPrefix = Literal[
    "DSV", "CODE", "FAM", "HYP", "SV", "RUN", "OPP", "TRD",
    "PREREG", "UNLOCK", "ROB", "FWD",
]

_CROCKFORD_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_SEQUENTIAL_ID_RE = re.compile(r"^[A-Za-z]+-(\d+)$")


def _encode_crockford_base32(data: bytes) -> str:
    value = int.from_bytes(data, "big")
    chars = []
    for i in range(26):
        shift = 5 * (25 - i)
        index = (value >> shift) & 0x1F
        chars.append(_CROCKFORD_ALPHABET[index])
    return "".join(chars)


def new_ulid() -> str:
    """26-character Crockford base32 ULID, lexicographically sortable by time."""
    timestamp_ms = int(time.time() * 1000)
    timestamp_bytes = timestamp_ms.to_bytes(6, "big")
    random_bytes = secrets.token_bytes(10)
    return _encode_crockford_base32(timestamp_bytes + random_bytes)


def new_id(prefix: IdPrefix) -> str:
    """Return f'{prefix}-{new_ulid()}'."""
    return f"{prefix}-{new_ulid()}"


def new_sequential_id(prefix: IdPrefix, existing: list[str]) -> str:
    """Return f'{prefix}-{n:03d}' where n is one greater than the highest
    zero-padded integer suffix present in `existing`. Used for FAM, HYP, DSV,
    which are human-referenced and must be short. Raises IdError if `existing`
    contains a malformed id."""
    max_n = 0
    for entry in existing:
        match = _SEQUENTIAL_ID_RE.fullmatch(entry)
        if match is None:
            raise IdError(f"malformed sequential id in existing list: {entry!r}")
        max_n = max(max_n, int(match.group(1)))
    return f"{prefix}-{max_n + 1:03d}"
