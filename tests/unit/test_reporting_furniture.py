from jarvis.reporting.furniture import (
    WATERMARK,
    code_sha,
    header_lines,
    is_suppressed,
    sample_size_note,
    utc_now_iso,
)


def test_watermark_is_ascii_only():
    # WP-015/D-065: a Windows cp1252 console cannot encode arbitrary
    # Unicode. This watermark is written to files and may later be
    # echoed to a console (as probe's own WATERMARK already is) --
    # matching that established ASCII-only convention avoids repeating
    # the exact class of bug WP-015 fixed.
    WATERMARK.encode("ascii")


def test_sample_size_note_suppresses_below_10():
    assert sample_size_note(0) == "n<10 suppressed"
    assert sample_size_note(9) == "n<10 suppressed"
    assert is_suppressed(9) is True


def test_sample_size_note_warns_between_10_and_30():
    assert sample_size_note(10) == "n=10 (small sample)"
    assert sample_size_note(29) == "n=29 (small sample)"
    assert is_suppressed(10) is False


def test_sample_size_note_empty_at_or_above_30():
    assert sample_size_note(30) == ""
    assert sample_size_note(1000) == ""
    assert is_suppressed(30) is False


def test_code_sha_returns_a_real_looking_git_sha(tmp_path):
    from jarvis.core.config import repo_root

    sha = code_sha(repo_root())
    assert sha != "unknown"
    assert len(sha) == 40
    assert all(c in "0123456789abcdef" for c in sha)


def test_code_sha_returns_unknown_for_a_non_repo_directory(tmp_path):
    sha = code_sha(tmp_path)
    assert sha == "unknown"


def test_utc_now_iso_ends_in_z_not_offset():
    stamp = utc_now_iso()
    assert stamp.endswith("Z")
    assert "+00:00" not in stamp


def test_header_lines_discloses_unavailable_fields_rather_than_omitting():
    lines = header_lines(
        title="Test Report",
        data_period="2007-01-01 to 2015-01-01",
        code_sha_value="deadbeef",
        generated_at="2026-01-01T00:00:00Z",
    )
    joined = "\n".join(lines)
    assert "not yet available (Stage 1B not built)" in joined
    assert "not used by this report" in joined
    assert "Vault years (2023-present): excluded" in joined
