"""solver 엔진: 결정성, 필수조건, 매칭 범위, 규칙 엔진 해에서 출발, 캐시, 실패 사유."""

import copy

import pytest
import yaml

from core import check_params
from domains.dispatch import rule_engine as R

from engines import get_engine
from engines.solver import clear_cache, model
from modelreg import Registry
from tests.conftest import ROOT

SOLVER_V1 = Registry(ROOT / "models", "solver").params_path(1)
CODES = {"NO_SKILL", "NO_CERT", "OUT_OF_AREA", "NO_TIME_MATCH", "CAPACITY"}


@pytest.fixture(scope="module")
def setup():
    engine = get_engine("solver")
    params = engine.load_params(SOLVER_V1)
    params["objective"]["time_limit"] = 0.05             # 테스트는 짧게
    pack = engine.pack_factory(params)
    inst, _ = pack.generate(1, ["P1", "P2", "P3", "P4"])
    small = pack.subset(inst, pack.items(inst)[:90])      # 1일차 앞 90건 (3개 지점 문제)
    return engine, params, pack, small


def _key(records):
    return [(d.item_id, d.decision, d.status, d.reason_code) for d in records]


def test_v1_params_extend_rule_params_and_pass_bounds():
    solver = yaml.safe_load(SOLVER_V1.read_text(encoding="utf-8"))
    rule = yaml.safe_load((ROOT / "models" / "rule" / "v1" / "params.yaml").read_text(encoding="utf-8"))
    assert {k: v for k, v in solver.items() if k != "objective"} == rule        # 검증·지표가 쓰는 섹션은 같다
    assert set(solver["objective"]) >= {"assign", "time_diff_per_min", "home_km", "master", "time_limit", "bounds", "docs"}
    assert check_params(solver, get_engine("solver").pack_factory(solver).dimensions()) == []


def test_deterministic_and_valid(setup):
    _engine, params, pack, small = setup
    clear_cache()
    a = pack.solve(small, params)
    clear_cache()
    b = pack.solve(small, params)
    assert _key(a) == _key(b)
    assert pack.validate(small, a) == []
    assert [d.item_id for d in a] == pack.items(small)


def test_respects_matching_ranges_and_reasons(setup):
    _engine, params, pack, small = setup
    records = pack.solve(small, params)
    for d in records:
        if d.status == "success":
            order, worker = small.order_index[d.item_id], small.worker_index[d.decision["worker_id"]]
            stage = R.actual_stage(small, order, worker, R.parse_start(d.decision), params)
            assert stage is not None and stage == d.decision["matching_stage"]   # 3단계 범위 안
        else:
            assert d.reason_code in CODES and d.evidence.startswith("solver:")


def test_starts_from_rule_solution(setup):
    _engine, params, pack, small = setup
    rule = R.solve(small, params)
    solver = pack.solve(small, params)
    assert sum(d.status == "success" for d in solver) >= sum(d.status == "success" for d in rule)


def test_same_instance_and_params_are_solved_once(setup, monkeypatch):
    _engine, params, pack, small = setup
    clear_cache()
    calls = []
    real = model.solve_instance
    monkeypatch.setattr("engines.solver.solve_instance", lambda *a, **k: calls.append(1) or real(*a, **k))
    first = pack.solve(small, params)
    again = get_engine("solver").pack_factory(params).solve(small, params)   # 새 팩이어도 캐시를 쓴다
    other = copy.deepcopy(params)
    other["objective"]["time_diff_per_min"] += 1
    pack.solve(small, other)
    assert len(calls) == 2 and _key(first) == _key(again)


def test_metrics_and_validate_come_from_core_pack(setup):
    _engine, params, pack, small = setup
    records = pack.solve(small, params)
    core = get_engine("rule").pack_factory(params)
    assert pack.metrics(small, records) == core.metrics(small, records)
    assert pack.dimensions() == core.dimensions()
