"""AI 개선안의 값만 사람이 고쳐 새 안으로 추가하고, 같은 기준으로 다시 검증한다."""

import pytest
import yaml

from modelreg import Registry
from tests.test_workflow import _run
from workflow import runner
from workflow.storage import RunStore

TW = lambda v: {"params_changes": [{"path": "matching.time_window_min[2]", "value": v}]}   # noqa: E731


@pytest.fixture
def awaiting(ws):
    return _run(ws)


def _revise(ws, run, pid, changes, note=None):
    return runner.revise(runs_dir=ws["runs"], run_id=run["run_id"], proposal_id=pid, changes=changes,
                         max_seconds=300, note=note)


def test_revised_proposal_is_validated_like_the_original(ws, awaiting):
    out = _revise(ws, awaiting, 2, TW(90), note="더 완화")
    entry, result = out["proposal"], out["result"]
    assert entry["id"] == 7 and entry["origin"]["revised_from"] == 2   # 개선안 1~4, 탐색 후보 5·6 다음 and entry["origin"]["note"] == "더 완화"
    assert entry["origin"]["before"]["params_changes"][0]["value"] == 75
    assert entry["proposal"]["title"].endswith("(사람 수정)")
    store = RunStore(ws["runs"])
    assert [p["id"] for p in store.load_stage(awaiting["run_id"], "proposals")["proposals"]] == [1, 2, 3, 4, 7]
    saved = {r["id"]: r for r in store.load_stage(awaiting["run_id"], "validation")["results"]}
    assert saved[7]["origin"]["revised_from"] == 2
    assert len(saved[7]["train"]["cases"]) == 2 and len(saved[7]["holdout"]["cases"]) == 2
    # 90분은 75분보다 할당을 더 올린다
    t = lambda r: r["train"]["summary"]["metrics"]["assignment_rate"]["delta_mean"]   # noqa: E731
    assert t(saved[7]) > t(saved[2])
    assert store.load(awaiting["run_id"])["status"] == "awaiting_approval"


def test_approving_revised_proposal_records_origin(ws, awaiting):
    _revise(ws, awaiting, 2, TW(90), note="더 완화")
    verdict = {r["id"]: r for r in RunStore(ws["runs"]).load_stage(awaiting["run_id"], "validation")["results"]}[7]["verdict"]
    runner.approve(runs_dir=ws["runs"], run_id=awaiting["run_id"], proposal_id=7,
                   override_reason=None if verdict["pass"] else "사람 판단")
    reg = Registry(ws["models"], "rule")
    card = reg.card(2)
    assert card["origin"]["revised_from"] == 2 and card["proposal_id"] == 7
    assert yaml.safe_load(reg.params_path(2).read_text(encoding="utf-8"))["matching"]["time_window_min"] == [0, 30, 90]


@pytest.mark.parametrize("pid,changes,match", [
    (2, {"params_changes": [{"path": "matching.time_window_min[1]", "value": 45}]}, "값만 고칠 수 있다"),
    (2, {"params_changes": [{"path": "matching.time_window_min[2]", "value": 75}]}, "값이 같다"),
    (2, {"params_changes": []}, "값만 고칠 수 있다"),
    (2, TW(999), "허용 범위"),
    (1, {"override_rules": [{"when": {"area_zone": ["core"]}, "set": {"matching.area_extension_km[2]": 5}}]}, "값만 고칠 수 있다"),
    (3, TW(80), "수정할 수 없다"),      # invalid 안
    (4, TW(80), "수정할 수 없다"),      # spec 안
])
def test_invalid_revisions_are_rejected(ws, awaiting, pid, changes, match):
    with pytest.raises(runner.WorkflowError, match=match):
        _revise(ws, awaiting, pid, changes)
    assert len(RunStore(ws["runs"]).load_stage(awaiting["run_id"], "proposals")["proposals"]) == 4


def test_search_candidate_values_can_be_revised(ws, awaiting):
    found = RunStore(ws["runs"]).load_stage(awaiting["run_id"], "search")["candidates"][0]
    changes = {"params_changes": [{**c, "value": c["value"] - 1} for c in found["proposal"]["params_changes"]]}
    entry = _revise(ws, awaiting, found["id"], changes)["proposal"]
    assert entry["origin"]["revised_from"] == found["id"] and entry["proposal"]["title"].startswith("탐색 1위")


def test_override_rule_values_can_be_revised(ws, awaiting):
    rule = {"override_rules": [{"when": {"area_zone": ["boundary"]}, "set": {"matching.area_extension_km[2]": 5}}]}
    assert _revise(ws, awaiting, 1, rule)["proposal"]["proposal"]["override_rules"] == rule["override_rules"]


def test_revise_only_while_awaiting(ws, awaiting):
    runner.approve(runs_dir=ws["runs"], run_id=awaiting["run_id"], proposal_id=2)
    with pytest.raises(runner.WorkflowError, match="승인 대기 상태가 아니다"):
        _revise(ws, awaiting, 2, TW(80))


def test_progress_is_recorded(ws, awaiting):
    progress = RunStore(ws["runs"]).load(awaiting["run_id"])["progress"]
    assert progress["stage"] == "validation" and progress["done"] == progress["total"] == 4 * 5   # 생성 4 + (개선안 2 + 탐색 후보 2) × 4


def test_revalidation_runs_outside_the_registry_lock(ws, awaiting, monkeypatch):
    """재검증 중에도 승인·반려가 막히지 않고, 그 사이 승인되면 수정안은 기록하지 않는다."""
    from workflow import stages
    original = stages.validation_stage
    lock = ws["models"] / "rule" / ".lock"
    seen = {}

    def spy(*args, **kwargs):
        seen["locked"] = lock.exists()
        runner.approve(runs_dir=ws["runs"], run_id=awaiting["run_id"], proposal_id=2)   # 잠금이 없으니 바로 된다
        return original(*args, **kwargs)
    monkeypatch.setattr(stages, "validation_stage", spy)
    with pytest.raises(runner.WorkflowError, match="승인 대기 상태가 아니다"):
        _revise(ws, awaiting, 2, TW(90))
    assert seen == {"locked": False} and not lock.exists()
    assert len(RunStore(ws["runs"]).load_stage(awaiting["run_id"], "proposals")["proposals"]) == 4


def test_changes_must_be_a_mapping(ws, awaiting):
    with pytest.raises(runner.WorkflowError, match="형태여야"):
        _revise(ws, awaiting, 2, [{"path": "matching.time_window_min[2]", "value": 90}])
