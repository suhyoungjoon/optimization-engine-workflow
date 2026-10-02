import pytest

from workflow.judge import check_config, judge

CFG = {"target": {"metric": "rate", "min_improvement": 0.01},
       "guards": {"match": {"max_drop": 0.05}, "travel": {"max_increase": 2.0}}}


def _summary(rate=0.02, match=-0.01, travel=0.5, violations=0):
    return {"cases": 3, "violations": violations,
            "metrics": {k: {"delta_mean": v} for k, v in (("rate", rate), ("match", match), ("travel", travel))}}


def test_config_validation():
    assert check_config(CFG) == []
    assert check_config({}) and check_config({"target": {"metric": "rate", "min_improvement": "x"}})
    assert check_config({**CFG, "guards": {"m": {"max_drop": 0.1, "max_increase": 1}}})
    assert check_config({**CFG, "guards": {"m": {"max_drop": -0.1}}})
    assert check_config({"target": {"metric": "rate", "min_improvement": True}})        # YAML true
    assert check_config({**CFG, "guards": {"m": {"max_drop": False}}})
    assert check_config({"target": {"metric": "rate", "min_improvement": float("nan")}})
    for bad in ([], {"target": "rate"}, {**CFG, "guards": ["m"]}, {**CFG, "guards": {"m": 0.1}}):
        assert check_config(bad)                                                       # 모양이 틀려도 예외가 아니라 오류 목록


def test_pass_requires_both_sets():
    assert judge(CFG, {"train": _summary(), "holdout": _summary()})["pass"]
    v = judge(CFG, {"train": _summary(), "holdout": _summary(match=-0.06)})
    assert not v["pass"] and not v["overfit"]
    assert v["reasons"] == ["검증용: match 평균 Δ -0.060 (하락 한도 0.050)"]


def test_effect_that_vanishes_on_holdout_is_overfit():
    v = judge(CFG, {"train": _summary(rate=0.08), "holdout": _summary(rate=0.0)})
    assert not v["pass"] and v["overfit"]
    assert v["reasons"] == ["검증용: rate 평균 Δ +0.000 (최소 개선 +0.010)"]


@pytest.mark.parametrize("summary,reason", [
    (_summary(violations=1), "필수조건 위반 1건"),
    (_summary(travel=2.5), "travel 평균 Δ +2.500 (증가 한도 2.000)"),
    ({"cases": 1, "violations": 0, "metrics": {}}, "목표 지표 rate가 없다"),
])
def test_single_failures(summary, reason):
    v = judge(CFG, {"train": _summary(), "holdout": summary})
    assert not v["pass"] and f"검증용: {reason}" in v["reasons"]


def test_thresholds_are_inclusive():
    assert judge(CFG, {"train": _summary(rate=0.01, match=-0.05, travel=2.0),
                       "holdout": _summary(rate=0.01, match=-0.05, travel=2.0)})["pass"]
