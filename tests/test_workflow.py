"""워크플로우 한 바퀴 (가짜 LLM, 작은 시나리오 세트).

M2 완료 기준: 검증용 세트에서 효과가 사라지는 개선안(경계 지역 안)을 판정이 걸러내고,
판정을 통과한 안은 승인으로 새 챔피언이 되며, rollback으로 이전 챔피언에 돌아간다.
"""

import json

import pytest
import yaml

from engines import get_engine
from modelreg import Registry
from tests.conftest import SETTINGS as SETTINGS_FOR_TEST
from tests.conftest import write_set
from tests.fake_llm import FakeLLM, tool_use
from workflow import runner
from workflow.rehearsal import BOUNDARY_RULE, rehearsal_llm
from workflow.storage import RunStore


def _run(ws, llm=None, **overrides):
    kw = {"models_dir": ws["models"], "runs_dir": ws["runs"], "train_path": ws["train"],
          "holdout_path": ws["holdout"], "llm": llm or rehearsal_llm(), "llm_config": ws["llm_config"],
          "settings": ws["settings"], "rehearsal": True, **overrides}
    return runner.run_workflow(get_engine("rule"), **kw)


def _reg(ws):
    return Registry(ws["models"], "rule")


def _results(ws, run):
    return {r["id"]: r for r in RunStore(ws["runs"]).load_stage(run["run_id"], "validation")["results"]}


@pytest.fixture
def awaiting(ws):
    return _run(ws)


def test_run_stops_at_awaiting_approval_and_records_reproduction_info(ws, awaiting):
    run = awaiting
    assert run["status"] == "awaiting_approval"
    assert [h["status"] for h in run["history"]] == ["running", "analyzed", "proposed", "searched",
                                                      "validated", "awaiting_approval"]
    assert _reg(ws).versions() == [1] and _reg(ws).champion() == 1          # 승인 전에는 레지스트리 그대로
    assert run["engine"] == "rule" and run["model"] == "rule@v1" and run["champion_version"] == 1
    assert [c["seed"] for c in run["scenarios"]["train"]["cases"]] == [1, 2]
    assert [c["seed"] for c in run["scenarios"]["holdout"]["cases"]] == [201, 202]
    assert run["llm"]["model"] == "fake-model" and run["core"]["version"] == "0.1.0"
    stage1 = RunStore(ws["runs"]).load_stage(run["run_id"], "run")
    assert [c["seed"] for c in stage1["cases"]] == [1, 2] and stage1["representative"]["seed"] == 1
    assert stage1["summary"]["violations"] == 0 and stage1["params"]["version"] == 1


def test_judgment_filters_effect_that_vanishes_on_holdout(ws, awaiting):
    states = {p["id"]: p["state"] for p in RunStore(ws["runs"]).load_stage(awaiting["run_id"], "proposals")["proposals"]}
    assert states == {1: "valid", 2: "valid", 3: "invalid", 4: "unsupported"}
    boundary, window = _results(ws, awaiting)[1], _results(ws, awaiting)[2]
    # 경계 지역 안: 학습용에서는 할당이 오르지만 P4가 없는 검증용에서는 효과가 사라진다
    assert boundary["train"]["summary"]["metrics"]["assignment_rate"]["delta_mean"] > 0.05
    assert boundary["holdout"]["summary"]["metrics"]["assignment_rate"]["delta_mean"] == 0
    assert not boundary["verdict"]["pass"] and boundary["verdict"]["overfit"]
    assert "검증용: assignment_rate 평균 Δ +0.000 (최소 개선 +0.010)" in boundary["verdict"]["reasons"]
    # 시간 허용 오차 안: 두 세트 모두에서 효과가 남는다
    assert window["verdict"]["pass"] and window["violations"] == 0
    assert len(window["train"]["cases"]) == 2 and len(window["holdout"]["cases"]) == 2


def test_approve_registers_new_champion_and_rollback_restores(ws, awaiting):
    with pytest.raises(runner.WorkflowError, match=r"판정 통과: \[2, 5, 6\]"):   # 통과 안이 여럿이면 사람이 고른다
        runner.approve(runs_dir=ws["runs"], run_id=awaiting["run_id"])
    done = runner.approve(runs_dir=ws["runs"], run_id=awaiting["run_id"], proposal_id=2, note="ok")
    reg = _reg(ws)
    assert done["status"] == "applied" and reg.versions() == [1, 2] and reg.champion() == 2
    params = yaml.safe_load(reg.params_path(2).read_text(encoding="utf-8"))
    assert params["version"] == 2 and params["matching"]["time_window_min"] == [0, 30, 75]
    card = reg.card(2)
    assert (card["parent"], card["run_id"], card["proposal_id"], card["override"]) == (1, awaiting["run_id"], 2, None)
    assert card["verdict"]["pass"] and set(card["validation"]) == {"train", "holdout"}
    apply = RunStore(ws["runs"]).load_stage(awaiting["run_id"], "apply")
    assert (apply["model_before"], apply["model_after"]) == ("rule@v1", "rule@v2")

    assert runner.rollback(models_dir=ws["models"], engine="rule", reason="현장 반응 나쁨") == (2, 1)
    assert reg.champion() == 1 and reg.versions() == [1, 2]                 # 버전은 지우지 않는다
    assert reg.history()[-1] == {**reg.history()[-1], "action": "rollback", "version": 1, "reason": "현장 반응 나쁨"}


def test_failed_verdict_needs_override_reason(ws, awaiting):
    with pytest.raises(runner.WorkflowError, match="판정을 통과하지 못했다"):
        runner.approve(runs_dir=ws["runs"], run_id=awaiting["run_id"], proposal_id=1)
    assert _reg(ws).versions() == [1]
    runner.approve(runs_dir=ws["runs"], run_id=awaiting["run_id"], proposal_id=1, override_reason="경계 지역 확대 방침")
    card = _reg(ws).card(2)
    assert card["override"] == {"reason": "경계 지역 확대 방침"} and not card["verdict"]["pass"]
    assert yaml.safe_load(_reg(ws).params_path(2).read_text(encoding="utf-8"))["overrides"]["rules"] == [BOUNDARY_RULE]


def test_violations_block_approval_even_with_override(ws, awaiting):
    path = ws["runs"] / awaiting["run_id"] / "4_validation.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["results"][1]["violations"] = 3                   # 규칙 엔진은 위반을 내지 않으므로 기록을 고쳐 재현한다
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(runner.WorkflowError, match="필수조건 위반 3건"):
        runner.approve(runs_dir=ws["runs"], run_id=awaiting["run_id"], proposal_id=2, override_reason="x")
    assert _reg(ws).versions() == [1]


@pytest.mark.parametrize("proposal_id", [3, 4, 9])
def test_unvalidated_proposals_cannot_be_approved(ws, awaiting, proposal_id):
    with pytest.raises(runner.WorkflowError, match="검증되지 않았다"):
        runner.approve(runs_dir=ws["runs"], run_id=awaiting["run_id"], proposal_id=proposal_id, override_reason="x")


def test_champion_change_makes_pending_run_stale(ws, awaiting):
    second = _run(ws)
    runner.approve(runs_dir=ws["runs"], run_id=awaiting["run_id"], proposal_id=2)
    with pytest.raises(runner.WorkflowError, match="v1에서 v2로"):
        runner.approve(runs_dir=ws["runs"], run_id=second["run_id"], proposal_id=2)
    runner.rollback(models_dir=ws["models"], engine="rule", reason="back")
    runner.approve(runs_dir=ws["runs"], run_id=second["run_id"], proposal_id=2)            # 챔피언이 다시 v1이면 유효
    assert _reg(ws).versions() == [1, 2, 3] and _reg(ws).card(3)["parent"] == 1


def test_approve_twice_and_reject(ws, awaiting):
    runner.approve(runs_dir=ws["runs"], run_id=awaiting["run_id"], proposal_id=2)
    with pytest.raises(runner.WorkflowError, match="승인 대기 상태가 아니다"):
        runner.approve(runs_dir=ws["runs"], run_id=awaiting["run_id"], proposal_id=2)
    other = _run(ws)
    done = runner.reject(runs_dir=ws["runs"], run_id=other["run_id"], reason="부작용이 크다")
    assert done["status"] == "rejected" and _reg(ws).versions() == [1, 2]


def test_registry_lock_blocks_concurrent_decisions(ws, awaiting):
    lock = ws["models"] / "rule" / ".lock"
    lock.touch()
    for call in (lambda: runner.approve(runs_dir=ws["runs"], run_id=awaiting["run_id"], proposal_id=2),
                 lambda: runner.reject(runs_dir=ws["runs"], run_id=awaiting["run_id"], reason="x"),
                 lambda: runner.rollback(models_dir=ws["models"], engine="rule", reason="x")):
        with pytest.raises(runner.WorkflowError, match="진행 중"):
            call()
    lock.unlink()
    runner.approve(runs_dir=ws["runs"], run_id=awaiting["run_id"], proposal_id=2)
    assert _reg(ws).champion() == 2 and not lock.exists()


@pytest.mark.parametrize("bad_id", ["..", "../outside", "/tmp/outside", "a/../../outside"])
def test_run_id_cannot_escape_runs_dir(ws, tmp_path, bad_id):
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "run.json").write_text('{"status": "awaiting_approval"}', encoding="utf-8")
    ws["runs"].mkdir()
    with pytest.raises(runner.WorkflowError, match="찾을 수 없음"):
        runner.approve(runs_dir=ws["runs"], run_id=bad_id, proposal_id=2)


# --- 실패 기록 ---

def _failed_reason(ws):
    [run] = RunStore(ws["runs"]).list()
    assert run["status"] == "failed"
    return run["history"][-1]["reason"]


def test_overlapping_scenario_sets_fail_in_setup(ws, tmp_path):
    ws["holdout"] = write_set(tmp_path / "overlap.yaml", "overlap", [(2, [])])
    with pytest.raises(ValueError):
        _run(ws, holdout_path=ws["holdout"])
    assert "seed를 공유한다" in _failed_reason(ws)


def test_bad_judgment_config_fails_in_setup(ws):
    ws["settings"]["judgment"]["target"]["min_improvement"] = "high"
    with pytest.raises(ValueError):
        _run(ws)
    assert _failed_reason(ws).startswith("setup: ValueError: 설정 오류: judgment")


def test_missing_settings_fail_in_setup(ws):
    del ws["settings"]["validation"]
    with pytest.raises(KeyError):
        _run(ws)
    assert _failed_reason(ws).startswith("setup:")


def test_missing_registry_fails_in_setup(ws, tmp_path):
    with pytest.raises(Exception, match="레지스트리가 없다"):
        _run(ws, models_dir=tmp_path / "empty")
    assert _failed_reason(ws).startswith("setup: RegistryError")


def test_validation_time_budget(ws):
    ws["settings"]["validation"]["max_seconds"] = 0
    run = _run(ws)
    assert run["status"] == "failed" and "검증 시간 예산" in run["history"][-1]["reason"]


def test_llm_call_limits_come_from_settings(ws):
    ws["settings"]["llm"]["analyze_max_calls"] = 2      # 리허설 분석가는 제출까지 4번 호출한다
    llm = rehearsal_llm()
    run = _run(ws, llm=llm)
    assert run["status"] == "failed" and "stop=max_calls" in run["history"][-1]["reason"] and len(llm.calls) == 2


def test_no_valid_params_proposal_fails(ws):
    def policy(item, n, messages, tools):
        if "submit_report" in {t["name"] for t in tools}:
            return rehearsal_llm().policy(item, n, messages, tools)
        return tool_use("submit_proposals", {"proposals": [
            {"title": "x", "kind": "params", "rationale": "x", "target_findings": ["F3"],
             "params_changes": [{"path": "travel.detour_factor", "value": 2.0}]}]})   # bounds 폭 0인 고정값

    run = _run(ws, llm=FakeLLM(policy))
    assert run["status"] == "failed" and "검증할 params 안 없음" in run["history"][-1]["reason"]


def test_unexpected_error_is_recorded_as_failed(ws):
    with pytest.raises(RuntimeError):
        _run(ws, llm=FakeLLM(lambda item, n, messages, tools: RuntimeError("api down")))
    assert _failed_reason(ws) == "analysis: RuntimeError: api down"


def test_progress_writes_stage_changes_even_within_the_throttle(ws, tmp_path):
    store = RunStore(tmp_path / "runs")
    run_id = store.create({})
    write = runner._progress_writer(store, run_id, min_interval=60)
    write("run", 1, 5, "a")
    write("run", 2, 5, "b")                      # 같은 단계 중간 값: 건너뛴다
    assert store.load(run_id)["progress"]["detail"] == "a"
    write("run", 5, 5, "끝")
    write("analysis", 0, 1, "분석 agent 실행 중")   # 바로 뒤의 단계 전환도 남는다
    assert store.load(run_id)["progress"]["stage"] == "analysis"


def test_engine_overrides_apply_on_top_of_defaults():
    from workflow.config import for_engine, settings_errors
    s = {"llm": {"analyze_max_calls": 30, "propose_max_calls": 20, "search_max_calls": 10},
         "search": SETTINGS_FOR_TEST["search"], "validation": {"max_seconds": 300},
         "judgment": SETTINGS_FOR_TEST["judgment"], "scenarios": {"train": "train", "holdout": "holdout"},
         "engines": {"solver": {"validation": {"max_seconds": 1200}, "scenarios": {"train": "solver_train", "holdout": "solver_holdout"}}}}
    assert for_engine(s, "solver")["validation"] == {"max_seconds": 1200}
    assert for_engine(s, "solver")["llm"]["propose_max_calls"] == 20 and "engines" not in for_engine(s, "solver")
    assert for_engine(s, "rule")["scenarios"]["train"] == "train"
    assert settings_errors(s) == []
    bad = {**s, "engines": {"solver": {"validation": {"max_seconds": 0}}}}
    assert any(e.startswith("engines.solver:") for e in settings_errors(bad))
    assert settings_errors({**s, "engines": {"solver": 5}})


def test_run_records_effective_limits(ws, awaiting):
    assert awaiting["limits"] == {"llm": SETTINGS_FOR_TEST["llm"], "validation": SETTINGS_FOR_TEST["validation"],
                                  "search": {**SETTINGS_FOR_TEST["search"], "max_evals": 6}}


def test_search_budget_stops_the_search_not_the_run(ws):
    ws["settings"]["search"]["max_seconds"] = 1e-9                 # 인스턴스를 만들기도 전에 예산을 넘긴다
    run = _run(ws)
    found = RunStore(ws["runs"]).load_stage(run["run_id"], "search")
    assert run["status"] == "awaiting_approval" and found["stop"] == "max_seconds" and found["candidates"] == []


def test_approving_a_search_candidate_registers_its_values(ws, awaiting):
    found = RunStore(ws["runs"]).load_stage(awaiting["run_id"], "search")
    top = found["candidates"][0]
    assert top["origin"] == {"by": "search", "rank": 1, "eval": top["origin"]["eval"]}
    runner.approve(runs_dir=ws["runs"], run_id=awaiting["run_id"], proposal_id=top["id"])
    reg = _reg(ws)
    params = yaml.safe_load(reg.params_path(2).read_text(encoding="utf-8"))
    for change in top["proposal"]["params_changes"]:
        section, key = change["path"].split("[")[0].split(".")
        assert params[section][key][2] == change["value"]
    assert reg.card(2)["origin"]["by"] == "search" and reg.card(2)["proposal_id"] == top["id"]
