"""화면 API: 리허설 실행을 시작해 승인까지, 모델 버전, 기준정보 편집."""

import shutil
import threading
import time

import pytest
import yaml
from fastapi.testclient import TestClient

from tests.conftest import ROOT, P1_P4, write_set
from tests.fake_llm import FakeLLM
from ui.app import create_app, params_diff
from workflow.rehearsal import policy


@pytest.fixture
def ui(tmp_path):
    for name in ("models", "settings"):
        shutil.copytree(ROOT / name, tmp_path / name)
    (tmp_path / "scenarios").mkdir()
    write_set(tmp_path / "scenarios" / "train.yaml", "train", [(1, P1_P4), (2, P1_P4)])
    write_set(tmp_path / "scenarios" / "holdout.yaml", "holdout", [(201, ["P1", "P2", "P3"]), (202, [])])
    gate = threading.Event()
    gate.set()

    def gated(item, n, messages, tools):   # 테스트가 실행 도중을 붙잡을 수 있게
        gate.wait(10)
        return policy(item, n, messages, tools)

    app = create_app(runs_dir=tmp_path / "runs", models_dir=tmp_path / "models", settings_dir=tmp_path / "settings",
                     scenarios_dir=tmp_path / "scenarios", llm_factory=lambda: FakeLLM(gated))
    return TestClient(app), tmp_path, gate


def _wait(client, run_id, status="awaiting_approval"):
    for _ in range(600):
        run = client.get(f"/api/runs/{run_id}").json()["run"]
        if run["status"] == status or run["status"] == "failed":
            return run
        time.sleep(0.05)
    raise TimeoutError(run_id)


def test_page_and_meta(ui):
    client, _, _ = ui
    assert "엔진 개선 워크플로우" in client.get("/").text
    assert client.get("/static/app.js").status_code == 200
    meta = client.get("/api/meta").json()
    assert [s["key"] for s in meta["stages"]] == ["run", "analysis", "proposals", "validation", "apply"]
    assert meta["rehearsal_only"] and {f["id"] for f in meta["faults"]} == {"P1", "P2", "P3", "P4"}
    assert "assignment_rate" in meta["metrics"] and meta["dimensions"]["area_zone"]


def test_run_from_start_to_approval(ui):
    client, _, _ = ui
    run_id = client.post("/api/runs", json={}).json()["run_id"]
    run = _wait(client, run_id)
    assert run["status"] == "awaiting_approval" and run["llm"]["rehearsal"]
    detail = client.get(f"/api/runs/{run_id}").json()
    assert set(detail["stages"]) == {"run", "analysis", "proposals", "validation", "apply"}
    assert detail["stages"]["apply"] is None and not detail["final"]

    revised = client.post(f"/api/runs/{run_id}/revise", json={"proposal_id": 2, "note": "x", "changes": {
        "params_changes": [{"path": "matching.time_window_min[2]", "value": 90}]}})
    assert revised.status_code == 200 and revised.json()["proposal"]["id"] == 5
    bad = client.post(f"/api/runs/{run_id}/revise", json={"proposal_id": 2, "changes": {
        "params_changes": [{"path": "cei.master_threshold", "value": 70}]}})
    assert bad.status_code == 400 and "값만" in bad.json()["detail"]

    assert client.post(f"/api/runs/{run_id}/approve", json={"proposal_id": 1}).status_code == 400   # 판정 불통과
    ok = client.post(f"/api/runs/{run_id}/approve", json={"proposal_id": 2, "note": "화면 승인"})
    assert ok.status_code == 200 and ok.json()["status"] == "applied"
    assert client.get(f"/api/runs/{run_id}").json()["stages"]["apply"]["model_after"] == "rule@v2"
    assert client.get("/api/runs").json()["runs"][0]["run_id"] == run_id


def test_only_one_run_at_a_time(ui):
    client, _, gate = ui
    gate.clear()                                    # 첫 실행을 분석 단계에서 붙잡는다
    first = client.post("/api/runs", json={}).json()["run_id"]
    busy = client.post("/api/runs", json={})
    assert busy.status_code == 409
    listed = client.get("/api/runs").json()
    assert listed["busy"] and listed["runs"][0]["run_id"] == first
    gate.set()
    _wait(client, first)
    for _ in range(100):                            # 승인 대기가 기록된 뒤 실행 스레드가 끝난다
        if not client.get("/api/runs").json()["busy"]:
            break
        time.sleep(0.05)
    assert client.post("/api/runs", json={}).status_code == 200


def test_reject_needs_reason(ui):
    client, _, _ = ui
    run_id = client.post("/api/runs", json={}).json()["run_id"]
    _wait(client, run_id)
    assert client.post(f"/api/runs/{run_id}/reject", json={"reason": " "}).status_code == 400
    assert client.post(f"/api/runs/{run_id}/reject", json={"reason": "부작용"}).json()["status"] == "rejected"


def test_models_diff_and_rollback(ui):
    client, _, _ = ui
    run_id = client.post("/api/runs", json={}).json()["run_id"]
    _wait(client, run_id)
    client.post(f"/api/runs/{run_id}/approve", json={"proposal_id": 2})
    models = client.get("/api/models/rule").json()
    assert models["champion"] == 2 and [v["version"] for v in models["versions"]] == [1, 2]
    v2 = client.get("/api/models/rule/versions/2").json()
    assert v2["champion"] and v2["card"]["parent"] == 1 and "version: 2" in v2["params_text"]
    diff = client.get("/api/models/rule/diff", params={"a": 1, "b": 2}).json()["rows"]
    assert diff == [{"path": "matching.time_window_min", "a": [0, 30, 60], "b": [0, 30, 75], "kind": "changed"}]
    assert client.post("/api/models/rule/rollback", json={"to": 1}).status_code == 400      # 사유 필수
    assert client.post("/api/models/rule/rollback", json={"to": 1, "reason": "되돌림"}).json() == {"before": 2, "after": 1}
    assert client.get("/api/models/rule").json()["champion"] == 1
    assert client.get("/api/models/rule/versions/9").status_code == 404
    assert client.get("/api/models/nope").status_code == 404


def test_settings_edit(ui):
    client, tmp, _ = ui
    values = client.get("/api/settings").json()["values"]
    assert set(values) == {"llm", "validation", "judgment"}
    new = {"target": {"metric": "assignment_rate", "min_improvement": 0.02}, "guards": {"avg_travel_min": {"max_increase": 1.0}}}
    assert client.put("/api/settings", json={"judgment": new}).json()["values"]["judgment"] == new
    text = (tmp / "settings" / "workflow.yaml").read_text(encoding="utf-8")
    assert "# 판정 기준" in text and "min_improvement: 0.02" in text        # 주석 보존
    assert client.put("/api/settings", json={"judgment": {"target": {"metric": "x", "min_improvement": True}}}).status_code == 400
    assert client.put("/api/settings", json={"llm": {"analyze_max_calls": 0, "propose_max_calls": 5}}).status_code == 400
    assert client.put("/api/settings", json={"core": {}}).status_code == 400
    ok = client.put("/api/settings", json={"validation": {"max_seconds": 60}})
    assert ok.status_code == 200 and ok.json()["values"]["validation"]["max_seconds"] == 60


def test_scenario_edit(ui):
    client, tmp, _ = ui
    sets = client.get("/api/scenarios").json()
    assert [c["seed"] for c in sets["train"]["cases"]] == [1, 2]
    ok = client.put("/api/scenarios/train", json={"cases": [{"seed": 3, "faults": ["P4"]}, {"seed": 4, "faults": []}]})
    assert ok.status_code == 200
    assert yaml.safe_load((tmp / "scenarios" / "train.yaml").read_text(encoding="utf-8"))["cases"] == [
        {"seed": 3, "faults": ["P4"]}, {"seed": 4, "faults": []}]
    assert client.put("/api/scenarios/train", json={"cases": [{"seed": 201, "faults": []}]}).status_code == 400  # 검증용과 겹침
    assert "알 수 없는 결함" in client.put("/api/scenarios/holdout", json={"cases": [{"seed": 9, "faults": ["P9"]}]}).json()["detail"]
    assert client.put("/api/scenarios/train", json={"cases": []}).status_code == 400
    assert client.put("/api/scenarios/other", json={"cases": []}).status_code == 404


def test_params_diff_flattens_nested_values():
    a = {"version": 1, "m": {"x": [0, 1], "y": 2}, "overrides": {"rules": []}}
    b = {"version": 2, "m": {"x": [0, 2], "y": 2}, "overrides": {"rules": [{"when": {"z": ["b"]}, "set": {"m.x[1]": 3}}]}}
    rows = {r["path"]: r for r in params_diff(a, b)}
    assert rows["m.x"]["kind"] == "changed" and "version" not in rows
    assert rows["overrides.rules[0].set.m.x[1]"]["kind"] == "added"
