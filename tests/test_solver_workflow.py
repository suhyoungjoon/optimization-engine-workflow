"""solver를 챔피언 엔진으로 워크플로우 한 바퀴 (M4 완료 기준: 엔진을 바꿔도 워크플로우 코드는 그대로).

테스트 속도를 위해 임시 레지스트리의 solver@v1 시간 한도를 줄이고, 케이스 1+1건 세트를 쓴다.
"""

import yaml

from engines import get_engine
from modelreg import Registry
from tests.conftest import P1_P4, write_set
from workflow import runner
from workflow.compare_engines import compare_engines
from workflow.rehearsal import rehearsal_llm
from workflow.storage import RunStore


def _fast_solver(ws):
    path = ws["models"] / "solver" / "v1" / "params.yaml"
    params = yaml.safe_load(path.read_text(encoding="utf-8"))
    params["objective"]["time_limit"] = 0.02
    path.write_text(yaml.safe_dump(params, allow_unicode=True), encoding="utf-8")


def test_solver_runs_through_the_same_workflow(ws, tmp_path):
    _fast_solver(ws)
    train = write_set(tmp_path / "s_train.yaml", "s_train", [(1, P1_P4)])
    holdout = write_set(tmp_path / "s_holdout.yaml", "s_holdout", [(201, ["P1", "P2", "P3"])])
    run = runner.run_workflow(get_engine("solver"), models_dir=ws["models"], runs_dir=ws["runs"], train_path=train,
                              holdout_path=holdout, llm=rehearsal_llm(), llm_config=ws["llm_config"],
                              settings=ws["settings"], rehearsal=True)
    assert run["status"] == "awaiting_approval" and run["model"] == "solver@v1"
    assert run["limits"]["validation"]["max_seconds"] == 1200                  # 엔진별 예외가 적용됨
    store = RunStore(ws["runs"])
    proposals = store.load_stage(run["run_id"], "proposals")["proposals"]
    assert [p["state"] for p in proposals] == ["valid", "valid", "invalid", "unsupported", "valid"]
    assert proposals[-1]["proposal"]["params_changes"][0]["path"] == "objective.time_diff_per_min"
    results = store.load_stage(run["run_id"], "validation")["results"]
    assert [r["id"] for r in results] == [1, 2, 5] and all(r["violations"] == 0 for r in results)

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
