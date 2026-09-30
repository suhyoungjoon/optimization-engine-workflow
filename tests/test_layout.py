from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CORE_NAMES = {"core", "domains", "api", "scripts", "configs"}


def test_no_top_level_names_that_shadow_core():
    assert not CORE_NAMES & {p.name for p in ROOT.iterdir()}


def test_core_imports_resolve_to_installed_package():
    import core
    import domains
    assert ROOT not in Path(core.__file__).resolve().parents
    assert ROOT not in Path(domains.__file__).resolve().parents
