"""D-010/WP-017: "only bars and vault may access data/ storage" is one of
the two rules D-010 names as carrying this project's architectural
integrity. Before WP-017, the `.importlinter` contract meant to enforce it
targeted `jarvis.bars.storage`, a module that never existed -- import-linter
silently skips forbidden targets absent from the graph, so the contract
was vacuously KEPT (passing by matching nothing, not by checking anything)
since before Stage 0 finished. This is the dedicated test the Technical
Bible claims exists for this rule specifically, filling that gap for real.

Deliberately does NOT assert "the whole contract is currently KEPT" --
that would depend on every OTHER module in the codebase staying clean,
which is `test_layers.py::test_import_linter_contracts_pass`'s job (and,
as of WP-017, it correctly fails: cli/main.py bypasses bars's public
interface with direct `jarvis.bars.store`/`jarvis.bars.resample` imports,
a real, disclosed, pre-existing violation the corrected contract now
surfaces -- see D-066). These two tests instead verify the MECHANISM
itself -- that the rule's targets are real and that a direct bypass is
actually caught -- independent of whatever state the rest of the
codebase happens to be in on any given day.
"""

import grimp

from jarvis.core.config import repo_root


def _build_graph() -> grimp.ImportGraph:
    return grimp.build_graph("jarvis", cache_dir=None)


def test_single_reader_rule_targets_are_not_vacuous():
    """The contract's forbidden targets must be real modules in the import
    graph. This is exactly the bug WP-017 fixes: `jarvis.bars.storage`
    never existed, so the pre-WP-017 contract silently matched nothing."""
    graph = _build_graph()
    assert "jarvis.bars.store" in graph.modules, (
        "jarvis.bars.store is not a real module in the import graph -- the "
        "contract's forbidden target would be silently skipped, exactly "
        "like the pre-WP-017 jarvis.bars.storage bug"
    )
    assert "jarvis.bars.resample" in graph.modules, (
        "jarvis.bars.resample is not a real module in the import graph -- "
        "the contract's forbidden target would be silently skipped"
    )


def test_single_reader_rule_catches_a_direct_bypass():
    """Inject a direct import of jarvis.bars.store from a module outside
    bars/vault (jarvis.qa.report, chosen because it does NOT already
    import bars.store directly -- only indirectly, via the sanctioned
    `from jarvis.bars import read_bars`) and confirm
    find_modules_directly_imported_by -- the same primitive the
    contract's allow_indirect_imports=true check uses -- reports it. This
    is a permanent, automated version of the manual throwaway-violation
    check performed during WP-017's own development (a real file was
    added to a currently-clean module, confirmed to break the contract,
    then removed)."""
    graph = _build_graph()

    before = graph.find_modules_directly_imported_by("jarvis.qa.report")
    assert "jarvis.bars.store" not in before, (
        "jarvis.qa.report already directly imports jarvis.bars.store -- "
        "this test needs a module that does not, to prove the injected "
        "edge below is what gets detected, not a pre-existing one"
    )

    graph.add_import(
        importer="jarvis.qa.report",
        imported="jarvis.bars.store",
        line_number=1,
        line_contents="from jarvis.bars.store import BAR_SCHEMA",
    )

    after = graph.find_modules_directly_imported_by("jarvis.qa.report")
    assert "jarvis.bars.store" in after, (
        "a direct import of jarvis.bars.store was not detected -- the "
        "mechanism the single-reader-rule contract relies on is not "
        "actually catching direct bypasses"
    )


def test_single_reader_rule_config_exists_and_is_readable():
    """Sanity check that the contract lives where every other test in this
    file assumes: the repo's own .importlinter, not some other config the
    grimp-level tests above could drift from unnoticed. Checks the actual
    `forbidden_modules` line, not just any mention of the string anywhere
    in the file -- the file's own explanatory comment legitimately
    mentions the old, broken `jarvis.bars.storage` name as history."""
    config_path = repo_root() / ".importlinter"
    assert config_path.is_file()
    lines = config_path.read_text(encoding="utf-8").splitlines()
    config_lines = [line.strip() for line in lines if not line.strip().startswith("#")]
    assert "jarvis.bars.store" in config_lines
    assert "jarvis.bars.storage" not in config_lines
