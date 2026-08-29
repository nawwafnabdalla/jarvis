from pathlib import Path

import pytest
import yaml

from jarvis.core.errors import SessionError
from jarvis.sessions.definitions import load_session_set_def


def _write_session_set(
    tmp_path: Path,
    session_set_id: str,
    version: int,
    sessions: dict,
    *,
    fold_ambiguous: str = "later",
    fold_nonexistent: str = "later",
    thin_day_threshold: float = 0.60,
) -> Path:
    config_dir = tmp_path / "config" / "sessions"
    config_dir.mkdir(parents=True, exist_ok=True)
    content = {
        "session_set_id": session_set_id,
        "version": version,
        "tzdata_version_at_authoring": "2026.3",
        "fold_policy": {"ambiguous": fold_ambiguous, "nonexistent": fold_nonexistent},
        "exclude_partial": True,
        "thin_day_threshold": thin_day_threshold,
        "sessions": sessions,
    }
    path = config_dir / f"{session_set_id}.v{version}.yaml"
    path.write_text(yaml.dump(content), encoding="utf-8")
    return path


@pytest.fixture
def isolated_repo_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr("jarvis.sessions.definitions.repo_root", lambda: tmp_path)
    return tmp_path


def test_loads_fx_core_v1():
    definition = load_session_set_def("fx_core", 1)
    assert definition.session_set_id == "fx_core"
    assert definition.version == 1
    assert len(definition.sessions) == 6


def test_missing_file_raises():
    with pytest.raises(SessionError):
        load_session_set_def("nope", 1)


def test_end_before_start_rejected(isolated_repo_root: Path):
    _write_session_set(
        isolated_repo_root,
        "test_set",
        1,
        {"bad": {"tz": "Europe/London", "start": "16:00", "end": "08:00"}},
    )
    with pytest.raises(SessionError):
        load_session_set_def("test_set", 1)


def test_unknown_timezone_rejected(isolated_repo_root: Path):
    _write_session_set(
        isolated_repo_root,
        "test_set",
        1,
        {"bad": {"tz": "Mars/Olympus_Mons", "start": "09:00", "end": "10:00"}},
    )
    with pytest.raises(SessionError):
        load_session_set_def("test_set", 1)


def test_derived_with_concrete_fields_rejected(isolated_repo_root: Path):
    _write_session_set(
        isolated_repo_root,
        "test_set",
        1,
        {
            "a": {"tz": "Europe/London", "start": "08:00", "end": "12:00"},
            "b": {"tz": "Europe/London", "start": "08:00", "end": "12:00"},
            "bad": {
                "derived": "intersection",
                "of": ["a", "b"],
                "tz": "Europe/London",
            },
        },
    )
    with pytest.raises(SessionError):
        load_session_set_def("test_set", 1)


def test_derived_of_unknown_session_rejected(isolated_repo_root: Path):
    _write_session_set(
        isolated_repo_root,
        "test_set",
        1,
        {
            "london": {"tz": "Europe/London", "start": "08:00", "end": "12:00"},
            "bad": {"derived": "intersection", "of": ["london", "nonexistent"]},
        },
    )
    with pytest.raises(SessionError):
        load_session_set_def("test_set", 1)


def test_nested_derived_rejected(isolated_repo_root: Path):
    _write_session_set(
        isolated_repo_root,
        "test_set",
        1,
        {
            "a": {"tz": "Europe/London", "start": "08:00", "end": "12:00"},
            "b": {"tz": "Europe/London", "start": "09:00", "end": "13:00"},
            "c": {"derived": "intersection", "of": ["a", "b"]},
            "bad": {"derived": "intersection", "of": ["c", "a"]},
        },
    )
    with pytest.raises(SessionError):
        load_session_set_def("test_set", 1)


def test_fold_policy_raise_rejected(isolated_repo_root: Path):
    _write_session_set(
        isolated_repo_root,
        "test_set",
        1,
        {"london": {"tz": "Europe/London", "start": "08:00", "end": "12:00"}},
        fold_nonexistent="raise",
    )
    with pytest.raises(SessionError):
        load_session_set_def("test_set", 1)


def test_out_of_bounds_session_rejected(isolated_repo_root: Path):
    """The anchoring guard. Tokyo 00:00 on 2023-06-05 is 2023-06-04 15:00Z;
    trading day 2023-06-05 begins 2023-06-04 21:00Z -- the window falls
    before the trading day even starts."""
    _write_session_set(
        isolated_repo_root,
        "test_set",
        1,
        {"tokyo_early": {"tz": "Asia/Tokyo", "start": "00:00", "end": "06:00"}},
    )
    with pytest.raises(SessionError):
        load_session_set_def("test_set", 1)


def test_thin_day_threshold_range_validated(isolated_repo_root: Path):
    _write_session_set(
        isolated_repo_root,
        "test_set",
        1,
        {"london": {"tz": "Europe/London", "start": "08:00", "end": "12:00"}},
        thin_day_threshold=1.5,
    )
    with pytest.raises(SessionError):
        load_session_set_def("test_set", 1)


def test_cache_invalidates_when_file_changes(isolated_repo_root: Path):
    """Proves the load_session_set_def cache is keyed on file content
    (mtime + size), not just (session_set_id, version): overwriting the
    same path with different content must not return the earlier call's
    cached, now-stale result.

    The replacement content is deliberately a different byte length (an
    extra padding field), not just a same-length field swap -- two
    same-length HH:MM time values would leave the file size identical,
    and on a filesystem with coarse mtime resolution the cache key could
    then fail to change at all, making this test pass by luck rather than
    by actually exercising invalidation."""
    path = _write_session_set(
        isolated_repo_root,
        "cache_test",
        1,
        {"london": {"tz": "Europe/London", "start": "08:00", "end": "12:00"}},
    )
    size_before = path.stat().st_size
    definition = load_session_set_def("cache_test", 1)
    assert "london" in definition.sessions

    # Overwrite the SAME path with invalid content (end before start), and
    # a padding note that guarantees a different file size regardless of
    # mtime granularity.
    path.write_text(
        yaml.dump(
            {
                "session_set_id": "cache_test",
                "version": 1,
                "tzdata_version_at_authoring": "2026.3",
                "fold_policy": {"ambiguous": "later", "nonexistent": "later"},
                "exclude_partial": True,
                "thin_day_threshold": 0.60,
                "sessions": {
                    "london": {
                        "tz": "Europe/London",
                        "start": "16:00",
                        "end": "08:00",
                        "note": "padding to guarantee a different file size than the original write",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    assert path.stat().st_size != size_before

    with pytest.raises(SessionError):
        load_session_set_def("cache_test", 1)
