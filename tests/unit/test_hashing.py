import datetime
import subprocess
import sys
from pathlib import Path

import pytest

from jarvis.core.errors import HashingError
from jarvis.core.hashing import canonical_json, sha256_canonical, sha256_file


def test_canonical_json_key_order_invariant():
    a = {"b": 1, "a": 2}
    b = {"a": 2, "b": 1}
    assert canonical_json(a) == canonical_json(b)

    nested_a = {"outer": {"z": 1, "y": {"b": 2, "a": 3}}}
    nested_b = {"outer": {"y": {"a": 3, "b": 2}, "z": 1}}
    assert canonical_json(nested_a) == canonical_json(nested_b)


def test_canonical_json_rejects_set():
    with pytest.raises(HashingError):
        canonical_json({"a", "b"})


def test_canonical_json_rejects_nan_and_inf():
    with pytest.raises(HashingError):
        canonical_json(float("nan"))
    with pytest.raises(HashingError):
        canonical_json(float("inf"))
    with pytest.raises(HashingError):
        canonical_json(float("-inf"))


def test_canonical_json_rejects_datetime():
    with pytest.raises(HashingError):
        canonical_json(datetime.datetime(2024, 1, 1))
    with pytest.raises(HashingError):
        canonical_json(datetime.date(2024, 1, 1))


def test_sha256_canonical_stable_across_processes():
    obj = {"b": 1, "a": [1, 2, 3], "c": {"x": 1.5}}
    src_path = str(Path(__file__).resolve().parents[2] / "src")
    script = (
        "import sys; sys.path.insert(0, sys.argv[1]); "
        "from jarvis.core.hashing import sha256_canonical; "
        f"print(sha256_canonical({obj!r}))"
    )
    result = subprocess.run(
        [sys.executable, "-c", script, src_path],
        capture_output=True,
        text=True,
        check=True,
    )
    assert sha256_canonical(obj) == result.stdout.strip()


def test_sha256_file_missing_raises(tmp_path: Path):
    with pytest.raises(HashingError):
        sha256_file(tmp_path / "does_not_exist.bin")


def test_non_string_dict_key_rejected():
    with pytest.raises(HashingError):
        canonical_json({1: "x"})


def test_nested_non_string_dict_key_rejected():
    with pytest.raises(HashingError):
        canonical_json({"a": {"b": {2: "x"}}})


def test_non_string_dict_key_nested_inside_list_rejected():
    """A dict-only recursion would miss this: the offending dict is inside
    a list, not directly inside another dict. json.dumps coerces the key
    just as silently either way."""
    with pytest.raises(HashingError):
        canonical_json({"a": [{"b": {2: "x"}}]})


def test_bool_dict_key_rejected():
    # bool is not str, and json would otherwise silently render it "true"/
    # "false", colliding with an actual string key "true"/"false".
    with pytest.raises(HashingError):
        canonical_json({True: "x"})


def test_mixed_type_keys_raise_hashing_error_not_typeerror():
    try:
        canonical_json({1: "a", "b": 2})
    except HashingError:
        pass
    except TypeError:
        pytest.fail("canonical_json leaked a raw TypeError instead of HashingError")
    else:
        pytest.fail("canonical_json did not raise for a mixed-type-key dict")


def test_string_keys_still_work():
    """Confirms no existing hash changed: a fixed structure of all-string
    keys must still produce this exact digest. Verified directly against
    the pre-A-6-fix implementation (commit 57024e3), not just reasoned
    about: both produce fa57f877...4bff81 for this object, byte-for-byte
    identical. The A-6 fix only adds a pre-check that a valid
    (all-string-key) structure always passes; it does not alter
    json.dumps's own arguments."""
    obj = {"b": 1, "a": {"nested": [1, 2, 3], "z": "x"}, "c": 2.5}
    assert (
        sha256_canonical(obj)
        == "fa57f8773c06f227023f14720b72d15dc77c793495d6983cb853d66a754bff81"
    )

    nested_a = {"outer": {"z": 1, "y": {"b": 2, "a": 3}}}
    nested_b = {"outer": {"y": {"a": 3, "b": 2}, "z": 1}}
    assert sha256_canonical(nested_a) == sha256_canonical(nested_b)


def test_tuple_list_hash_identically_documented_behavior():
    """Documented, deliberately not fixed (A-6): tuples and lists serialise
    to the same JSON array, so they hash identically. This is a semantic
    decision (JSON has no tuple type), not a bug."""
    assert sha256_canonical((1, 2)) == sha256_canonical([1, 2])
