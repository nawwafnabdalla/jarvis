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
