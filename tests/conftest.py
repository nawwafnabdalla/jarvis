from pathlib import Path

import pytest


@pytest.fixture
def isolated_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway repo root with an empty config/ directory, isolated from
    the real repository so config-loading tests can write their own fixture
    files without touching config/instruments.yaml or config/periods.yaml."""
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    (tmp_path / "config").mkdir()
    monkeypatch.setattr("jarvis.core.config.repo_root", lambda: tmp_path)
    return tmp_path
