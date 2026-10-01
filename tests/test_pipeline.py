"""워크플로우 정의(workflow/pipeline.yaml)가 스스로 맞고, runner가 실제로 그 순서대로 단계를 부르는지."""

import copy

import pytest

from tests.conftest import SETTINGS
from tests.test_workflow import _run
from workflow import runner, stages
from workflow.pipeline import load_pipeline, pipeline_errors, stage_labels
from workflow.storage import STAGE_FILES

DEFN = load_pipeline()


def test_definition_is_consistent_with_settings():
    assert pipeline_errors(DEFN, SETTINGS) == []


def test_stage_keys_match_run_storage():
    assert [s["key"] for s in DEFN["stages"]] == [k for k in STAGE_FILES if k != "decisions"]
    assert list(stage_labels().values()) == ["실행", "결과분석", "개선안 도출", "검증(비교)", "개선적용"]


def test_runner_calls_stage_impls_in_definition_order(ws, monkeypatch):
    calls = []
    for s in DEFN["stages"]:
        module, _, name = s["impl"].rpartition(".")
        target = {"workflow.stages": stages, "workflow.runner": runner}[module]
        original = getattr(target, name)

        def spy(*args, _name=s["impl"], _fn=original, **kwargs):
            calls.append(_name)
            return _fn(*args, **kwargs)
        monkeypatch.setattr(target, name, spy)
    run = _run(ws)
    runner.approve(runs_dir=ws["runs"], run_id=run["run_id"])
    assert calls == [s["impl"] for s in DEFN["stages"]]


def _broken(change):
    d = copy.deepcopy(DEFN)
    change(d)
    return pipeline_errors(d, SETTINGS)


@pytest.mark.parametrize("change,message", [
    (lambda d: d["stages"].insert(0, d["stages"].pop()), "마지막 단계 하나"),                       # 게이트를 맨 앞으로
    (lambda d: d["stages"][3].update(kind="gate"), "마지막 단계 하나"),                             # 게이트가 둘
    (lambda d: d["stages"][1]["reads"].append("nowhere"), "알 수 없는 입력"),
    (lambda d: d["stages"][0]["reads"].append("report"), "앞 단계가 만들지 않았다"),               # 뒤 단계 산출물을 먼저 읽음
    (lambda d: d["stages"][1]["writes"].append("decisions"), "두 단계가 만든다"),
    (lambda d: d["stages"][2].update(impl="workflow.stages.nope"), "찾을 수 없다"),
    (lambda d: d["stages"][1]["settings"].append("llm.nope"), "설정 llm.nope"),
    (lambda d: d["artifacts"].append({"key": "run", "label": "x", "file": "x"}), "겹치면 안 된다"),
    (lambda d: d["stages"][0].update(actor="robot"), "actor는"),
])
def test_broken_definitions_are_reported(change, message):
    assert any(message in e for e in _broken(change))
