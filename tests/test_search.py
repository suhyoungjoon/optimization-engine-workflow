"""파라미터 탐색(workflow/search.py): 범위 검사, 결정적 표본 추출, 학습용 점수, 상위 K개 선택."""

import pytest

from engines import get_engine
from workflow.compare import BudgetExceeded
from workflow.search import SEARCH_DEFAULTS, pick_top, run_search, sample_points, search_errors, space_errors

ROOT_PARAMS = "models/rule/v1/params.yaml"
JUDGMENT = {"target": {"metric": "assignment_rate", "min_improvement": 0.01},
            "guards": {"desired_time_match_rate": {"max_drop": 0.05}}}
TW = "matching.time_window_min[2]"
AREA = "matching.area_extension_km[2]"


@pytest.fixture(scope="module")
def setup():
    engine = get_engine("rule")
    params = engine.load_params(ROOT_PARAMS)
    return params, engine.pack_factory(params).dimensions()


def test_space_must_be_inside_bounds_and_numeric(setup):
    params, dims = setup
    ok = [{"path": TW, "low": 60, "high": 100}, {"path": AREA, "low": 2, "high": 5}]
    assert space_errors(params, ok, dims, max_dims=3) == []
    assert space_errors(params, [], dims, max_dims=3)                                              # 비어 있음
    assert space_errors(params, ok * 2, dims, max_dims=3)                                          # 개수 초과
    assert space_errors(params, [ok[0], ok[0]], dims, max_dims=3)                                  # 같은 경로 두 번
    assert any("허용 범위" in e for e in space_errors(params, [{"path": TW, "low": 60, "high": 200}], dims, 3))
    assert space_errors(params, [{"path": TW, "low": 90, "high": 60}], dims, 3)                    # low ≥ high
    assert space_errors(params, [{"path": TW, "low": True, "high": 90}], dims, 3)                  # bool은 숫자가 아님
    assert space_errors(params, [{"path": "matching.nope", "low": 1, "high": 2}], dims, 3)
    assert space_errors(params, [{"path": "matching.time_window_min", "low": 1, "high": 2}], dims, 3)  # 목록 전체는 안 됨
    assert space_errors(params, [{"path": TW}], dims, 3) and space_errors(params, ["x"], dims, 3)
    assert space_errors(params, "x", dims, 3)


def test_sampling_is_deterministic_and_respects_types(setup):
    params = {**setup[0], "travel": {**setup[0]["travel"], "avg_speed_kmh": 30.0}}
    space = [{"path": TW, "low": 60, "high": 100}, {"path": "travel.avg_speed_kmh", "low": 20, "high": 40}]
    a = sample_points(params, space, 10, seed=0)
    assert a == sample_points(params, space, 10, seed=0) and a != sample_points(params, space, 10, seed=1)
    assert all(isinstance(p[TW], int) and 60 <= p[TW] <= 100 for p in a)            # 챔피언 값이 정수면 정수
    assert all(isinstance(p["travel.avg_speed_kmh"], float) for p in a)            # 챔피언 값이 실수면 실수
    assert len({tuple(sorted(p.items())) for p in a}) == len(a)                      # 중복 없음


def test_search_finds_the_best_point_on_a_known_objective(setup):
    """평가 함수를 가짜로 두고 알고리즘만 본다: 목표 지표는 TW=84에서 최대, 부작용 한도는 TW>95에서 넘는다."""
    params, _ = setup
    seen = []

    def evaluate(point):
        seen.append(point)
        tw = point[TW]
        return {"metrics": {"assignment_rate": {"delta_mean": 0.05 - abs(tw - 84) / 1000},
                            "desired_time_match_rate": {"delta_mean": -0.06 if tw > 95 else -0.01}},
                "violations": 0, "cases": 1}

    out = run_search(evaluate, params, [{"path": TW, "low": 60, "high": 120}], JUDGMENT,
                     {**SEARCH_DEFAULTS, "max_evals": 20, "top_k": 2})
    assert out["stop"] == "max_evals" and len(out["evals"]) == len(seen) == 20
    assert all(p[TW] != 60 for p in seen)                                           # 챔피언과 같은 점은 풀지 않는다
    best = out["top"][0]
    assert abs(best["point"][TW] - 84) <= 2 and best["pass"]
    assert all(e["point"][TW] <= 95 for e in out["top"])                            # 부작용 한도를 넘는 점은 고르지 않는다
    again = run_search(evaluate, params, [{"path": TW, "low": 60, "high": 120}], JUDGMENT,
                       {**SEARCH_DEFAULTS, "max_evals": 20, "top_k": 2})
    assert again["top"] == out["top"]                                               # 결정적


def test_small_integer_space_stops_when_exhausted(setup):
    params, _ = setup
    out = run_search(lambda p: {"metrics": {"assignment_rate": {"delta_mean": p[AREA] / 100}}, "violations": 0},
                     params, [{"path": AREA, "low": 2, "high": 5}], {"target": JUDGMENT["target"]},
                     {**SEARCH_DEFAULTS, "max_evals": 40})
    assert sorted(e["point"][AREA] for e in out["evals"]) == [2, 4, 5]               # 3은 챔피언 값
    assert out["stop"] == "exhausted" and out["top"][0]["point"][AREA] == 5


def test_budget_stops_the_search_but_keeps_results(setup):
    params, _ = setup
    calls = []

    def evaluate(point):
        calls.append(point)
        if len(calls) == 3:
            raise BudgetExceeded("예산")
        return {"metrics": {"assignment_rate": {"delta_mean": 0.02}}, "violations": 0}

    out = run_search(evaluate, params, [{"path": TW, "low": 60, "high": 120}], {"target": JUDGMENT["target"]},
                     {**SEARCH_DEFAULTS, "max_evals": 10})
    assert out["stop"] == "max_seconds" and len(out["evals"]) == 2 and len(out["top"]) == 2


def test_top_k_takes_only_train_passing_points_best_first_and_apart():
    space = [{"path": "a", "low": 0, "high": 10}]
    evals = [{"i": 1, "point": {"a": 1}, "pass": True, "target": 0.02},
             {"i": 2, "point": {"a": 2}, "pass": False, "target": 0.09},
             {"i": 3, "point": {"a": 3}, "pass": True, "target": 0.03},
             {"i": 4, "point": {"a": 4}, "pass": True, "target": 0.03},
             {"i": 5, "point": {"a": 3.5}, "pass": True, "target": 0.04}]
    assert [e["i"] for e in pick_top(evals[:4], 2, space)] == [3, 4]               # 같은 점수면 먼저 평가한 점
    assert [e["i"] for e in pick_top(evals, 3, space)] == [5, 1]                   # 3.5와 범위의 10% 안인 점은 건너뛴다
    assert pick_top([evals[1]], 2, space) == []


def test_search_keeps_a_margin_from_the_guards(setup):
    """탐색은 부작용 한도의 guard_margin만큼만 쓴다: 한도 −0.05, 여유 0.8이면 학습용에서 −0.04까지."""
    params, _ = setup

    def evaluate(point):
        tw = point[TW]
        return {"metrics": {"assignment_rate": {"delta_mean": (tw - 60) / 1000},
                            "desired_time_match_rate": {"delta_mean": -(tw - 60) / 800}},
                "violations": 0}

    space = [{"path": TW, "low": 60, "high": 120}]
    strict = run_search(evaluate, params, space, JUDGMENT, {**SEARCH_DEFAULTS, "guard_margin": 0.8})
    loose = run_search(evaluate, params, space, JUDGMENT, {**SEARCH_DEFAULTS, "guard_margin": 1.0})
    assert strict["top"][0]["point"][TW] == 92 and loose["top"][0]["point"][TW] == 100
    guard = next(c for c in strict["top"][0]["checks"] if c["name"] == "guard:desired_time_match_rate")
    assert guard["threshold"] == {"max_drop": 0.04}                                  # 기록에도 여유를 둔 한도가 남는다


def test_search_settings_are_checked():
    assert search_errors(SEARCH_DEFAULTS) == []
    assert search_errors({**SEARCH_DEFAULTS, "max_evals": 0}) and search_errors({**SEARCH_DEFAULTS, "top_k": True})
    assert search_errors({**SEARCH_DEFAULTS, "max_seconds": -1}) and search_errors({**SEARCH_DEFAULTS, "max_dims": 9})
    assert search_errors({**SEARCH_DEFAULTS, "guard_margin": 0}) and search_errors({**SEARCH_DEFAULTS, "guard_margin": 1.5})
    assert search_errors("x") and search_errors({**SEARCH_DEFAULTS, "seed": 1.5})


def test_workflow_settings_include_search_budget():
    import copy

    from tests.conftest import SETTINGS
    from workflow.config import EDITABLE, for_engine, settings_errors
    assert settings_errors(SETTINGS) == [] and "search" in EDITABLE
    assert for_engine(SETTINGS, "solver")["search"]["max_evals"] < SETTINGS["search"]["max_evals"]   # 엔진별 예외
    for broken in ({"search": {**SETTINGS["search"], "top_k": 0}}, {"search": 5},
                   {"llm": {**SETTINGS["llm"], "search_max_calls": 0}},
                   {"engines": {"solver": {"search": {"max_dims": 0}}}}):
        s = copy.deepcopy(SETTINGS)
        s.update(broken)
        assert settings_errors(s), broken


def test_refinement_widens_again_instead_of_stopping_early(setup):
    """정수 범위에서 주변 탐색 폭이 한 칸보다 좁아져도 '다 봤다'로 멈추지 않는다 (조합 164개 중 40개를 채운다)."""
    params, _ = setup
    out = run_search(lambda p: {"metrics": {"assignment_rate": {"delta_mean": 0.02 - abs(p[TW] - 89) / 1000}},
                                "violations": 0},
                     params, [{"path": TW, "low": 60, "high": 100}, {"path": AREA, "low": 2, "high": 5}],
                     {"target": JUDGMENT["target"]}, SEARCH_DEFAULTS)
    assert out["stop"] == "max_evals" and len(out["evals"]) == 40
