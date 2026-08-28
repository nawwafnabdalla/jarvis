import shutil
import subprocess
from pathlib import Path

import pytest

from jarvis.core.config import repo_root

_EXPECTED_MODULES = [
    "jarvis.core",
    "jarvis.provenance",
    "jarvis.timeengine",
    "jarvis.sessions",
    "jarvis.ingest",
    "jarvis.bars",
    "jarvis.qa",
    "jarvis.features",
    "jarvis.strategies",
    "jarvis.strategy_impls",
    "jarvis.opportunities",
    "jarvis.execution",
    "jarvis.vault",
    "jarvis.experiments",
    "jarvis.describe",
    "jarvis.backtest",
    "jarvis.statistics",
    "jarvis.robustness",
    "jarvis.forward",
    "jarvis.reporting",
    "jarvis.cli",
]


def test_import_linter_contracts_pass():
    lint_imports = shutil.which("lint-imports")
    if lint_imports is None:
        pytest.fail("lint-imports executable not found on PATH; install the dev extras")
    result = subprocess.run(
        [lint_imports],
        cwd=repo_root(),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("module_name", _EXPECTED_MODULES)
def test_every_expected_module_exists(module_name):
    module_path = repo_root() / "src" / Path(*module_name.split("."))
    assert (module_path / "__init__.py").is_file()
