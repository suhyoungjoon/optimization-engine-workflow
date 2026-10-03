"""레거시 데이터 로더 (M6): 가상 레거시 내보내기 → 매핑 명세로 가져오기 → 인스턴스 복원, 품질 리포트, 레거시 기준선."""

import copy
import csv
import datetime as dt
import shutil

import pytest
import yaml

from domains.dispatch import get_pack
from sources.legacy import (ImportRefused, dataset_instance, export_legacy, import_legacy, legacy_decisions,
                            list_datasets, load_mapping, load_snapshot, mapping_errors)
from tests.conftest import P1_P4, ROOT

MAPPING = ROOT / "sources" / "legacy" / "mapping.yaml"
PARAMS = yaml.safe_load((ROOT / "models" / "rule" / "v1" / "params.yaml").read_text(encoding="utf-8"))
START = dt.date(2026, 9, 1)


@pytest.fixture(scope="module")
def exported(tmp_path_factory):
    out = tmp_path_factory.mktemp("legacy_export")
    summary = export_legacy(out, seed=1, faults=P1_P4, start=START, params=PARAMS)
    return out, summary


@pytest.fixture
def data(tmp_path, monkeypatch):
    d = tmp_path / "data"
    monkeypatch.setenv("OEW_DATA_DIR", str(d))
    return d


def _rows(path):
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _write(path, rows):
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


def test_round_trip_restores_the_generated_instance(exported, data):
    out, summary = exported
    snap = import_legacy(out, MAPPING, "demo")
    assert snap["report"]["excluded"] == {"orders": 0, "workers": 0, "assignments": 0}
    assert snap["period"] == ["2026-09-01", "2026-09-10"]
    pack = get_pack(PARAMS)
    original, _truth = pack.generate(1, P1_P4)
    restored = dataset_instance(load_snapshot("demo"), "2026-09-01", "2026-09-10")
    assert restored.days == original.days == 10
    assert [vars(o) for o in restored.orders] == [vars(o) for o in original.orders]
    assert [vars(w) for w in restored.workers] == [vars(w) for w in original.workers]
    assert restored.branches == original.branches and restored.boundary_zone_km == original.boundary_zone_km
    # 같은 인스턴스이므로 규칙 엔진 결과도 같다
    key = lambda ds: [(d.item_id, d.status, d.decision) for d in ds]   # noqa: E731
    assert key(pack.solve(restored, PARAMS)) == key(pack.solve(original, PARAMS))


def test_period_selects_days_and_renumbers(exported, data):
    import_legacy(exported[0], MAPPING, "demo")
    inst = dataset_instance(load_snapshot("demo"), "2026-09-04", "2026-09-05")
    assert inst.days == 2 and {o.day for o in inst.orders} == {1, 2}
    assert {o.id[:3] for o in inst.orders} == {"D04", "D05"}               # 9/4 → 1일차, 9/5 → 2일차
    with pytest.raises(ValueError, match="기간"):
        dataset_instance(load_snapshot("demo"), "2026-10-01", "2026-10-02")


def test_legacy_baseline_is_measured_like_any_engine(exported, data):
    out, summary = exported
    import_legacy(out, MAPPING, "demo")
    snap = load_snapshot("demo")
    inst = dataset_instance(snap, "2026-09-01", "2026-09-10")
    pack = get_pack(PARAMS)
    legacy = legacy_decisions(snap, inst, PARAMS)
    assert len(legacy) == len(inst.orders)
    placed = [d for d in legacy if d.status == "success"]
    assert len(placed) == summary["assigned"] and all(d.decision["matching_stage"] in (1, 2, 3, None) for d in placed)
    # 가상 레거시는 규칙 엔진 결과에 사람의 수동 변경을 섞었다: 일부는 필수조건을 어긴다
    rule = {d.item_id: d.decision for d in pack.solve(inst, PARAMS) if d.decision}
    overridden = {d.item_id for d in placed if d.decision["worker_id"] != rule[d.item_id]["worker_id"]}
    assert len(overridden) == summary["manual_overrides"] > 0
    violations = pack.validate(inst, legacy)
    skill_or_cert = {v.item_id for v in violations if v.rule in ("skill_required", "cert_required")}
    assert violations and skill_or_cert <= overridden                     # 기술·자격 위반은 수동 변경에서만 나온다
    assert pack.metrics(inst, legacy)["assignment_rate"] == pytest.approx(summary["assigned"] / len(inst.orders))


def test_actuals_are_kept_but_personal_columns_are_not(exported, data):
    out, _ = exported
    snap = import_legacy(out, MAPPING, "demo")
    assert {"고객명", "연락처"} <= set(snap["report"]["ignored_columns"]["orders"])
    text = (data / "legacy" / "demo.json").read_text(encoding="utf-8")
    first_name = _rows(out / "orders.csv")[0]["고객명"]
    assert first_name not in text                                            # 개인정보는 스냅샷에 없다
    actuals = snap["report"]["actuals"]
    assert actuals["with_times"] > 0 and 0 < actuals["cancel_rate"] < 0.1


def test_bad_rows_are_reported_by_line_and_excluded(exported, data, tmp_path):
    src = tmp_path / "bad"
    shutil.copytree(exported[0], src)
    orders = _rows(src / "orders.csv")
    orders[0]["매체"] = "위성"                    # 모르는 코드 (2행)
    orders[1]["작업일자"] = "2026/09/01"          # 날짜 형식 (3행)
    orders[2]["지시번호"] = ""                   # 빈 ID (4행)
    orders[3]["지시번호"] = orders[4]["지시번호"]  # 중복 ID: 뒤 행(6행)을 뺀다
    orders[5]["위도"] = "35.1"                   # 관할에서 아주 먼 좌표 (7행)
    _write(src / "orders.csv", orders)
    assignments = _rows(src / "assignments.csv")
    assignments.append({**assignments[0], "지시번호": "없는지시"})
    _write(src / "assignments.csv", assignments)

    snap = import_legacy(src, MAPPING, "bad")
    problems = {(p["file"], p["line"]): p["problem"] for p in snap["report"]["problems"]}
    assert "모르는 코드" in problems[("orders", 2)] and "위성" in problems[("orders", 2)]
    assert "날짜" in problems[("orders", 3)] and "비어" in problems[("orders", 4)]
    assert "중복" in problems[("orders", 6)] and "관할" in problems[("orders", 7)]
    assert "없는 지시서" in problems[("assignments", len(assignments) + 1)]
    assert snap["report"]["excluded"]["orders"] == 5 and len(snap["orders"]) == len(orders) - 5


def test_missing_column_and_bad_mapping_stop_the_import(exported, data, tmp_path):
    src = tmp_path / "nocol"
    shutil.copytree(exported[0], src)
    rows = [{k: v for k, v in r.items() if k != "희망시각"} for r in _rows(src / "orders.csv")]
    _write(src / "orders.csv", rows)
    with pytest.raises(ValueError, match="희망시각"):
        import_legacy(src, MAPPING, "nocol")

    mapping = load_mapping(MAPPING)
    assert mapping_errors(mapping) == []
    mapping["orders"]["codes"]["branch"]["분당"] = "D"                       # 코어에 없는 지점
    mapping["orders"]["codes"]["media"]["위성"] = "SAT"
    errors = mapping_errors(mapping)
    assert any("D" in e and "코어" in e for e in errors) and any("SAT" in e for e in errors)


def test_snapshot_names_are_immutable(exported, data, tmp_path):
    out, _ = exported
    first = import_legacy(out, MAPPING, "demo")
    assert import_legacy(out, MAPPING, "demo")["hash"] == first["hash"]   # 같은 내용이면 그대로
    src = tmp_path / "changed"
    shutil.copytree(out, src)
    rows = _rows(src / "orders.csv")
    rows[0]["희망시각"] = "16:30"
    _write(src / "orders.csv", rows)
    with pytest.raises(ImportRefused, match="다른 이름"):
        import_legacy(src, MAPPING, "demo")
    assert [d["name"] for d in list_datasets()] == ["demo"]
    with pytest.raises(ValueError, match="이름"):
        import_legacy(out, MAPPING, "../x")


# --- 시나리오 세트의 기간 케이스 ---

from workflow.scenarios import case_label, check_disjoint, make_instance, parse_set, pin_cases  # noqa: E402


def _set(cases, name="s"):
    return parse_set({"name": name, "cases": cases}, f"{name}.yaml")


def test_period_cases_are_parsed_and_checked():
    s = _set([{"dataset": "demo", "from": dt.date(2026, 9, 1), "to": "2026-09-03"}, {"seed": 7, "faults": []}])
    assert s["cases"][0] == {"dataset": "demo", "from": "2026-09-01", "to": "2026-09-03"}   # YAML 날짜도 받는다
    assert case_label(s["cases"][0]) == "demo 2026-09-01~09-03" and case_label(s["cases"][1]) == "seed 7"
    for bad, match in [({"dataset": "demo", "from": "2026-09-03", "to": "2026-09-01"}, "from"),
                       ({"dataset": "demo", "from": "2026/09/01", "to": "2026-09-02"}, "날짜"),
                       ({"dataset": "Demo!", "from": "2026-09-01", "to": "2026-09-02"}, "dataset"),
                       ({"dataset": "demo", "from": "2026-09-01"}, "to")]:
        with pytest.raises(ValueError, match=match):
            _set([bad])
    with pytest.raises(ValueError, match="겹친다"):
        _set([{"dataset": "demo", "from": "2026-09-01", "to": "2026-09-03"},
              {"dataset": "demo", "from": "2026-09-03", "to": "2026-09-04"}])


def test_train_period_must_come_before_holdout():
    train = _set([{"dataset": "demo", "from": "2026-09-01", "to": "2026-09-05"}])
    check_disjoint(train, _set([{"dataset": "demo", "from": "2026-09-06", "to": "2026-09-10"}]))
    check_disjoint(train, _set([{"dataset": "other", "from": "2026-09-01", "to": "2026-09-05"}]))   # 다른 데이터셋
    with pytest.raises(ValueError, match="겹친다"):
        check_disjoint(train, _set([{"dataset": "demo", "from": "2026-09-05", "to": "2026-09-07"}]))
    with pytest.raises(ValueError, match="앞서야"):
        check_disjoint(train, _set([{"dataset": "demo", "from": "2026-08-01", "to": "2026-08-05"}]))


def test_pinned_snapshot_must_match(exported, data, tmp_path):
    import_legacy(exported[0], MAPPING, "demo")
    pack = get_pack(PARAMS)
    s = pin_cases(_set([{"dataset": "demo", "from": "2026-09-01", "to": "2026-09-02", "items": 50}]))
    case = s["cases"][0]
    assert case["snapshot"] == load_snapshot("demo")["hash"]
    assert len(make_instance(pack, case).orders) == 50
    with pytest.raises(ValueError, match="hash"):
        make_instance(pack, {**case, "snapshot": "0" * 16})
    with pytest.raises(ValueError, match="import-legacy"):
        pin_cases(_set([{"dataset": "nope", "from": "2026-09-01", "to": "2026-09-02"}]))


# --- 워크플로우·엔진 비교·CLI ---

from core import load_config  # noqa: E402
from engines import get_engine  # noqa: E402
from tests.conftest import SETTINGS, _fast_search, write_set  # noqa: E402
from workflow import runner  # noqa: E402
from workflow.cli import main  # noqa: E402
from workflow.compare_engines import compare_engines  # noqa: E402
from workflow.rehearsal import rehearsal_llm  # noqa: E402
from workflow.storage import RunStore  # noqa: E402


def _period_sets(tmp_path):
    train = tmp_path / "legacy_train.yaml"
    holdout = tmp_path / "legacy_holdout.yaml"
    train.write_text(yaml.safe_dump({"name": "legacy_train", "cases": [
        {"dataset": "demo", "from": "2026-09-01", "to": "2026-09-02"}]}), encoding="utf-8")
    holdout.write_text(yaml.safe_dump({"name": "legacy_holdout", "cases": [
        {"dataset": "demo", "from": "2026-09-06", "to": "2026-09-07"},
        {"dataset": "demo", "from": "2026-09-08", "to": "2026-09-09"}]}), encoding="utf-8")
    return train, holdout


def test_workflow_runs_on_past_periods_and_pins_the_snapshot(exported, data, tmp_path):
    snap = import_legacy(exported[0], MAPPING, "demo")
    shutil.copytree(ROOT / "models", tmp_path / "models")
    train, holdout = _period_sets(tmp_path)
    run = runner.run_workflow(get_engine("rule"), models_dir=tmp_path / "models", runs_dir=tmp_path / "runs",
                              train_path=train, holdout_path=holdout, llm=rehearsal_llm(),
                              llm_config=load_config(ROOT / "settings" / "llm.yaml"),
                              settings=_fast_search(copy.deepcopy(SETTINGS)), rehearsal=True)
    assert run["status"] == "awaiting_approval"
    assert all(c["snapshot"] == snap["hash"] for s in run["scenarios"].values() for c in s["cases"])
    validation = RunStore(tmp_path / "runs").load_stage(run["run_id"], "validation")
    assert all(len(r["holdout"]["cases"]) == 2 and r["violations"] == 0 for r in validation["results"])


def test_compare_engines_against_the_legacy_baseline(exported, data, tmp_path):
    import_legacy(exported[0], MAPPING, "demo")
    _train, holdout = _period_sets(tmp_path)
    result = compare_engines([get_engine("rule")], models_dir=ROOT / "models", set_path=holdout, baseline="legacy")
    assert result["engines"] == ["legacy", "rule"] and result["base"] == "legacy"
    s = result["summary"]
    assert s["legacy"]["model"] == "레거시 실제 배정" and s["legacy"]["violations"] > 0   # 수동 변경의 위반이 드러난다
    assert s["rule"]["violations"] == 0 and set(s["rule"]["delta_vs_legacy"]) == set(s["rule"]["metrics"])
    seed_set = write_set(tmp_path / "seeds.yaml", "seeds", [(1, [])])
    with pytest.raises(ValueError, match="기간 케이스"):
        compare_engines([get_engine("rule")], models_dir=ROOT / "models", set_path=seed_set, baseline="legacy")


def test_cli_export_import_datasets(data, tmp_path, capsys):
    out = tmp_path / "export"
    assert main(["export-legacy", "--seed", "1", "--faults", "P1,P4", "--start", "2026-09-01", "--out", str(out)]) == 0
    assert "2026-09-01 ~ 2026-09-10" in capsys.readouterr().out
    assert main(["import-legacy", "--input", str(out), "--name", "cli_demo"]) == 0
    text = capsys.readouterr().out
    assert "cli_demo" in text and "제외 0" in text and "가져오지 않은 컬럼" in text
    assert main(["datasets"]) == 0 and "cli_demo" in capsys.readouterr().out
    assert main(["import-legacy", "--input", str(tmp_path / "nowhere"), "--name", "x"]) == 2


def test_unreadable_csv_is_a_clear_error(exported, data, tmp_path):
    src = tmp_path / "broken"
    shutil.copytree(exported[0], src)
    rows = _rows(src / "orders.csv")
    rows[0]["고객명"] = "x" * (csv.field_size_limit() + 1)          # CSV 파서가 읽지 못하는 파일
    _write(src / "orders.csv", rows)
    with pytest.raises(ValueError, match="orders.csv"):
        import_legacy(src, MAPPING, "broken")
