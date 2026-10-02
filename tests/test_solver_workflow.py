"""solver를 챔피언 엔진으로 워크플로우 한 바퀴 (M4 완료 기준: 엔진을 바꿔도 워크플로우 코드는 그대로).

테스트 속도를 위해 임시 레지스트리의 solver@v1 시간 한도를 줄이고, 케이스 1+1건 세트를 쓴다.
"""

import pytest
import yaml

from engines import get_engine
from engines.solver import clear_cache
from modelreg import Registry
from tests.conftest import P1_P4, write_set
from workflow import runner
from workflow.compare_engines import compare_engines
from workflow.rehearsal import rehearsal_llm
from workflow.scenarios import load_set
from workflow.stages import run_stage
from workflow.storage import RunStore
from workflow.warm import warm


def _fast_solver(ws):
    path = ws["models"] / "solver" / "v1" / "params.yaml"
    params = yaml.safe_load(path.read_text(encoding="utf-8"))
    params["objective"]["time_limit"] = 0.02
    path.write_text(yaml.safe_dump(params, allow_unicode=True), encoding="utf-8")


def test_solver_runs_through_the_same_workflow(ws, tmp_path):
    _fast_solver(ws)
    ws["settings"]["engines"]["solver"]["search"]["max_evals"] = 4      # 테스트 속도 (엔진별 예외가 기본값보다 우선)
    train = write_set(tmp_path / "s_train.yaml", "s_train", [(1, P1_P4)])
    holdout = write_set(tmp_path / "s_holdout.yaml", "s_holdout", [(201, ["P1", "P2", "P3"])])
    run = runner.run_workflow(get_engine("solver"), models_dir=ws["models"], runs_dir=ws["runs"], train_path=train,
                              holdout_path=holdout, llm=rehearsal_llm(), llm_config=ws["llm_config"],
                              settings=ws["settings"], rehearsal=True)
    assert run["status"] == "awaiting_approval" and run["model"] == "solver@v1"
    assert run["limits"]["validation"]["max_seconds"] == 1800                  # 엔진별 예외가 적용됨
    assert run["limits"]["search"]["max_evals"] == 4
    store = RunStore(ws["runs"])
    proposals = store.load_stage(run["run_id"], "proposals")["proposals"]
    assert [p["state"] for p in proposals] == ["valid", "valid", "invalid", "unsupported", "valid"]
    assert proposals[-1]["proposal"]["params_changes"][0]["path"] == "objective.time_diff_per_min"
    results = store.load_stage(run["run_id"], "validation")["results"]
    found = store.load_stage(run["run_id"], "search")
    assert [d["path"] for d in found["space"]][-1] == "objective.time_diff_per_min"   # solver면 가중치도 탐색
    assert len(found["evals"]) == 4 and all(c["id"] >= 6 for c in found["candidates"])
    assert [r["id"] for r in results] == [1, 2, 5] + [c["id"] for c in found["candidates"]]
    assert all(r["violations"] == 0 for r in results)

    pick = next((r for r in results if r["verdict"]["pass"]), results[-1])
    runner.approve(runs_dir=ws["runs"], run_id=run["run_id"], proposal_id=pick["id"],
                   override_reason=None if pick["verdict"]["pass"] else "테스트: solver 승인 흐름 확인")
    solver_reg, rule_reg = Registry(ws["models"], "solver"), Registry(ws["models"], "rule")
    assert solver_reg.champion() == 2 and solver_reg.card(2)["parent"] == 1
    assert rule_reg.versions() == [1] and rule_reg.champion() == 1               # 다른 엔진 계보는 그대로


def test_compare_engines_on_the_same_cases(ws, tmp_path):
    _fast_solver(ws)
    sset = write_set(tmp_path / "cmp.yaml", "cmp", [(5, P1_P4)])
    result = compare_engines([get_engine("rule"), get_engine("solver")], models_dir=ws["models"], set_path=sset)
    assert result["engines"] == ["rule", "solver"] and result["base"] == "rule"
    s = result["summary"]
    assert s["rule"]["model"] == "rule@v1" and s["solver"]["model"] == "solver@v1"
    assert s["rule"]["violations"] == s["solver"]["violations"] == 0
    assert s["solver"]["metrics"]["assignment_rate"] >= s["rule"]["metrics"]["assignment_rate"]   # 규칙 엔진 해에서 출발
    assert set(s["solver"]["delta_vs_rule"]) == set(s["rule"]["metrics"])


def test_warm_fills_the_disk_cache_for_the_champion(ws, tmp_path, monkeypatch):
    _fast_solver(ws)
    monkeypatch.setenv("SOLVER_CACHE_DIR", str(tmp_path / "cache"))
    clear_cache()
    sset = write_set(tmp_path / "w_train.yaml", "w_train", [(1, P1_P4), (2, P1_P4)])
    engine = get_engine("solver")
    result = warm(engine, models_dir=ws["models"], set_paths=[sset])
    assert result["model"] == "solver@v1" and [c["seed"] for c in result["sets"][0]["cases"]] == [1, 2]
    assert len(list((tmp_path / "cache").glob("*.json"))) == 2
    clear_cache()                                               # 다음 실행(새 프로세스)과 같다
    monkeypatch.setattr("engines.solver.solve_instance", lambda *a, **k: pytest.fail("캐시를 쓰지 않았다"))
    params = engine.load_params(Registry(ws["models"], "solver").params_path(1))
    run_stage(engine, params, load_set(sset)["cases"])          # 1단계 실행이 다시 풀지 않는다
