import pytest

from tests.conftest import ROOT, write_set
from workflow.scenarios import check_disjoint, load_set


def test_repo_sets_load_and_are_disjoint():
    train, holdout = load_set(ROOT / "scenarios" / "train.yaml"), load_set(ROOT / "scenarios" / "holdout.yaml")
    assert (train["name"], len(train["cases"])) == ("train", 5)
    assert (holdout["name"], len(holdout["cases"])) == ("holdout", 10)
    check_disjoint(train, holdout)
    train_faults = {tuple(c["faults"]) for c in train["cases"]}
    assert any(tuple(c["faults"]) not in train_faults for c in holdout["cases"])   # 학습용에 없는 결함 조합


@pytest.mark.parametrize("cases,match", [
    ([], "비어"),
    ([("x", [])], "정수"),
    ([(1, "P1")], "문자열 목록"),
    ([(1, []), (1, ["P1"])], "같은 seed"),
])
def test_invalid_sets(tmp_path, cases, match):
    with pytest.raises(ValueError, match=match):
        load_set(write_set(tmp_path / "s.yaml", "s", cases))


def test_overlapping_seeds_are_rejected(tmp_path):
    a = load_set(write_set(tmp_path / "a.yaml", "a", [(1, []), (2, [])]))
    b = load_set(write_set(tmp_path / "b.yaml", "b", [(2, ["P1"]), (3, [])]))
    with pytest.raises(ValueError, match=r"\[2\]"):
        check_disjoint(a, b)


def test_items_limits_the_instance(tmp_path):
    from engines import get_engine
    from modelreg import Registry
    from workflow.scenarios import make_instance
    path = tmp_path / "s.yaml"
    path.write_text("cases:\n  - {seed: 1, faults: [P4], items: 120}\n  - {seed: 2, faults: []}\n", encoding="utf-8")
    s = load_set(path)
    assert s["cases"] == [{"seed": 1, "faults": ["P4"], "items": 120}, {"seed": 2, "faults": []}]
    engine = get_engine("rule")
    pack = engine.pack_factory(engine.load_params(Registry(ROOT / "models", "rule").params_path(1)))
    assert len(make_instance(pack, s["cases"][0]).orders) == 120
    assert len(make_instance(pack, s["cases"][1]).orders) == 1500
    path.write_text("cases:\n  - {seed: 1, faults: [], items: 0}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="items"):
        load_set(path)


def test_demo_sets_are_small_and_paired():
    train, holdout = load_set(ROOT / "scenarios" / "demo_train.yaml"), load_set(ROOT / "scenarios" / "demo_holdout.yaml")
    check_disjoint(train, holdout)
    assert all(c.get("items") for c in train["cases"] + holdout["cases"])
