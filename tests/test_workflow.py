"""워크플로우 한 바퀴 (가짜 LLM). 승인 전에는 params가 바뀌지 않고, 승인하면 버전이 오른다."""

import pytest
import yaml

from engines import get_engine
from tests.fake_llm import FakeLLM, tool_use
from workflow import runner
from workflow.rehearsal import BOUNDARY_RULE, rehearsal_llm
from workflow.storage import RunStore


def _run(ws, llm=None, seed=42, faults=("P4",)):
    return runner.run_workflow(get_engine("rule"), params_path=ws["params"], runs_dir=ws["runs"], seed=seed,
                               faults=list(faults), llm=llm or rehearsal_llm(), llm_config=ws["llm_config"],
                               settings=ws["settings"], rehearsal=True)


def _version(ws):
    return yaml.safe_load(ws["params"].read_text(encoding="utf-8"))["version"]


def test_rehearsal_stops_at_awaiting_approval_without_touching_params(ws):
    before = ws["params"].read_text(encoding="utf-8")
    run = _run(ws)
    assert run["status"] == "awaiting_approval"
    assert [h["status"] for h in run["history"]] == ["running", "analyzed", "proposed", "validated",
                                                      "awaiting_approval"]
    assert ws["params"].read_text(encoding="utf-8") == before
    # 재현 정보
    assert run["engine"] == "rule" and run["model"] == "rule@v1" and run["params_version"] == 1
    assert run["scenario"] == {"seed": 42, "faults": ["P4"]} and run["llm"]["model"] == "fake-model"
    assert run["core"]["version"] == "0.1.0"
    store = RunStore(ws["runs"])
    assert store.load_stage(run["run_id"], "run")["params"]["version"] == 1
    assert len(store.load_stage(run["run_id"], "decisions")) == 1500


def test_proposals_are_rechecked_and_only_valid_params_are_validated(ws):
    run = _run(ws)
    store = RunStore(ws["runs"])
    states = {p["id"]: p["state"] for p in store.load_stage(run["run_id"], "proposals")["proposals"]}
    assert states == {1: "valid", 2: "invalid", 3: "unsupported"}
    [result] = store.load_stage(run["run_id"], "validation")["results"]
    assert result["id"] == 1 and result["violations_after"] == 0 and result["approvable"]
    assert result["after"]["assignment_rate"] > result["before"]["assignment_rate"]
    assert result["slices"]["F3"]["after"]["fail_rate"] < result["slices"]["F3"]["before"]["fail_rate"]


def test_approve_writes_params_and_bumps_version(ws):
    run = _run(ws)
    done = runner.approve(get_engine("rule"), runs_dir=ws["runs"], run_id=run["run_id"], note="ok")
    assert done["status"] == "applied" and _version(ws) == 2
    assert yaml.safe_load(ws["params"].read_text(encoding="utf-8"))["overrides"]["rules"] == [BOUNDARY_RULE]
    apply = RunStore(ws["runs"]).load_stage(run["run_id"], "apply")
    assert (apply["proposal_id"], apply["model_before"], apply["model_after"]) == (1, "rule@v1", "rule@v2")
    with pytest.raises(runner.WorkflowError, match="승인 대기 상태가 아니다"):
        runner.approve(get_engine("rule"), runs_dir=ws["runs"], run_id=run["run_id"])
    assert _version(ws) == 2


@pytest.mark.parametrize("proposal_id,match", [(2, "검증되지 않았다"), (3, "검증되지 않았다"), (9, "검증되지 않았다")])
def test_approve_refuses_unvalidated_proposals(ws, proposal_id, match):
    run = _run(ws)
    with pytest.raises(runner.WorkflowError, match=match):
        runner.approve(get_engine("rule"), runs_dir=ws["runs"], run_id=run["run_id"], proposal_id=proposal_id)
    assert _version(ws) == 1


def test_approve_refuses_stale_run_after_params_changed(ws):
    first, second = _run(ws), _run(ws)
    runner.approve(get_engine("rule"), runs_dir=ws["runs"], run_id=first["run_id"])
    with pytest.raises(runner.WorkflowError, match="v1에서 v2로"):
        runner.approve(get_engine("rule"), runs_dir=ws["runs"], run_id=second["run_id"])
    assert _version(ws) == 2 and RunStore(ws["runs"]).load(second["run_id"])["status"] == "awaiting_approval"


def test_reject_records_only(ws):
    before = ws["params"].read_text(encoding="utf-8")
    run = _run(ws)
    done = runner.reject(runs_dir=ws["runs"], run_id=run["run_id"], reason="부작용이 크다")
    assert done["status"] == "rejected" and done["history"][-1]["reason"] == "부작용이 크다"
    assert ws["params"].read_text(encoding="utf-8") == before


def test_analysis_without_submission_fails_with_reason(ws):
    silent = FakeLLM(lambda item, n, messages, tools: {"type": "text", "text": "..."})
    run = _run(ws, llm=silent)
    assert run["status"] == "failed" and "결과분석" in run["history"][-1]["reason"]
    assert RunStore(ws["runs"]).load_stage(run["run_id"], "proposals") is None


def test_no_valid_params_proposal_fails(ws):
    def policy(item, n, messages, tools):
        names = {t["name"] for t in tools}
        if "submit_report" in names:
            return rehearsal_llm().policy(item, n, messages, tools)
        return tool_use("submit_proposals", {"proposals": [
            {"title": "x", "kind": "params", "rationale": "x", "target_findings": ["F3"],
             "params_changes": [{"path": "travel.detour_factor", "value": 2.0}]}]})   # bounds 폭 0인 고정값

    run = _run(ws, llm=FakeLLM(policy))
    assert run["status"] == "failed" and "검증할 params 안 없음" in run["history"][-1]["reason"]


def test_llm_call_limits_come_from_settings(ws):
    ws["settings"]["llm"]["analyze_max_calls"] = 2      # 리허설 분석가는 제출까지 4번 호출한다
    llm = rehearsal_llm()
    run = _run(ws, llm=llm)
    assert run["status"] == "failed" and "stop=max_calls" in run["history"][-1]["reason"]
    assert len(llm.calls) == 2


def test_unexpected_error_is_recorded_as_failed(ws):
    def boom(item, n, messages, tools):
        return RuntimeError("api down")

    with pytest.raises(RuntimeError):
        _run(ws, llm=FakeLLM(boom))
    [run] = RunStore(ws["runs"]).list()
    assert run["status"] == "failed" and run["history"][-1]["reason"] == "analysis: RuntimeError: api down"
    assert "traceback" in run["error"]
