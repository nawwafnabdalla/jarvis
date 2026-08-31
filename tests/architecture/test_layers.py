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


def test_import_linter_contracts_pass(capsys):
    # Calls the same underlying function the `lint-imports` console script
    # wraps (importlinter.cli.lint_imports, which is a plain function
    # returning an exit code -- not click-decorated) directly in-process,
    # rather than shelling out to a script resolved via shutil.which(PATH).
    # The previous subprocess-based version passed or failed depending on
    # whether the venv's Scripts/bin directory happened to be on PATH for
    # the invoking shell, which is a property of how the test was run, not
    # of the codebase -- exactly the class of flakiness that must not gate
    # something this test is meant to guarantee unconditionally. An
    # explicit config_filename makes the result independent of the
    # process's current working directory too.
    from importlinter.cli import EXIT_STATUS_SUCCESS, lint_imports

    exit_code = lint_imports(config_filename=str(repo_root() / ".importlinter"))
    output = capsys.readouterr()
    assert exit_code == EXIT_STATUS_SUCCESS, output.out + output.err


@pytest.mark.parametrize("module_name", _EXPECTED_MODULES)
def test_every_expected_module_exists(module_name):
    module_path = repo_root() / "src" / Path(*module_name.split("."))
    assert (module_path / "__init__.py").is_file()
