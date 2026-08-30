import time

import pytest

from jarvis.core.errors import IdError
from jarvis.core.ids import new_sequential_id, new_ulid

_CROCKFORD_CHARSET = set("0123456789ABCDEFGHJKMNPQRSTVWXYZ")


def test_ulid_length_and_charset():
    ulid = new_ulid()
    assert len(ulid) == 26
    assert set(ulid) <= _CROCKFORD_CHARSET


def test_ulid_time_ordering():
    generated = []
    for _ in range(1000):
        generated.append(new_ulid())
        time.sleep(0.001)
    assert generated == sorted(generated)


def test_new_sequential_id_empty_returns_001():
    assert new_sequential_id("FAM", []) == "FAM-001"


def test_new_sequential_id_increments():
    assert new_sequential_id("FAM", ["FAM-001", "FAM-002"]) == "FAM-003"
    assert new_sequential_id("FAM", ["FAM-007"]) == "FAM-008"


def test_new_sequential_id_malformed_raises():
    with pytest.raises(IdError):
        new_sequential_id("FAM", ["FAM-abc"])


def test_mismatched_prefix_rejected():
    with pytest.raises(IdError):
        new_sequential_id("FAM", ["HYP-009"])


def test_mixed_prefixes_rejected():
    with pytest.raises(IdError):
        new_sequential_id("FAM", ["FAM-001", "DSV-042"])


def test_matching_prefix_still_increments():
    assert new_sequential_id("FAM", ["FAM-001", "FAM-007"]) == "FAM-008"
