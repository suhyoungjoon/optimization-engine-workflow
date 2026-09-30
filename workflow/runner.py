"""워크플로우 실행과 사람의 결정(승인·반려·되돌리기).

run_workflow는 1~4단계를 돌리고 awaiting_approval에서 멈춘다. 개선적용(5단계)은 approve로만 일어난다 (자동 승인 없음).
승인하면 모델 레지스트리에 새 버전을 등록하고 챔피언으로 지정한다.
경로(모델 레지스트리, runs, 시나리오 세트)는 모두 인자로 받는다. 코어의 기본 경로(site-packages)는 쓰지 않는다.
"""

import copy
import json
import time
import traceback
from importlib import metadata
from pathlib import Path

from core import params_errors

from engines import get_engine
from engines.base import Engine
from modelreg import Registry, RegistryError

from . import stages
from .compare import BudgetExceeded
from .judge import check_config
from .scenarios import check_disjoint, load_set
from .state import ANALYZED, APPLIED, AWAITING_APPROVAL, FAILED, PROPOSED, REJECTED, VALIDATED
from .storage import RunStore, now


class WorkflowError(Exception):
    """사람이 고칠 수 있는 요청 오류 (잘못된 상태, 없는 개선안, 챔피언 불일치 등)."""


def core_ref() -> dict:
    """설치된 코어 패키지의 버전과 고정 커밋 (재현용)."""
    try:
        dist = metadata.distribution("optimization-agent-harness")
    except metadata.PackageNotFoundError:
        return {}
    ref = {"version": dist.version}
    direct = dist.read_text("direct_url.json")
    if direct:
        info = json.loads(direct)
        ref["url"] = info.get("url")
        ref["commit"] = (info.get("vcs_info") or {}).get("commit_id")
    return ref


def model_name(engine_name: str, version: int) -> str:
    return f"{engine_name}@v{version}"


def _progress_writer(store: RunStore, run_id: str, min_interval: float = 0.2):
    """단계 진행을 run.json의 progress에 남긴다 (화면이 읽는다). 너무 잦은 쓰기는 건너뛴다."""
    last = [0.0]

    def write(stage: str, done: int, total: int, detail: str = "") -> None:
        t = time.monotonic()
        if done < total and t - last[0] < min_interval:
            return
        last[0] = t
        store.update(run_id, progress={"stage": stage, "done": done, "total": total, "detail": detail,
                                       "at": now()})
    return write


def run_workflow(engine: Engine, *, models_dir: str | Path, runs_dir: str | Path, train_path: str | Path,
                 holdout_path: str | Path, llm, llm_config: dict, settings: dict, rehearsal: bool = False,
                 on_start=None) -> dict:
    """on_start(run_id): 실행 기록을 만든 직후 부른다 (화면이 백그라운드 실행의 ID를 바로 받기 위해)."""
    store = RunStore(runs_dir)
    run_id = store.create({
        "engine": engine.name,
        "model": None,              # 준비 단계에서 채운다 (준비 실패도 이 실행의 failed로 남기기 위해)
        "champion_version": None,
        "models_dir": str(Path(models_dir).resolve()),
        "scenarios": {"train": {"path": str(train_path)}, "holdout": {"path": str(holdout_path)}},
        "llm": {"model": llm.model, "rehearsal": rehearsal},
        "core": core_ref(),
    })
    if on_start:
        on_start(run_id)
    stage = "setup"
    try:
        registry = Registry(models_dir, engine.name)
        champion = registry.champion()
        params = engine.load_params(registry.params_path(champion))
        sets = {"train": load_set(train_path), "holdout": load_set(holdout_path)}
        check_disjoint(sets["train"], sets["holdout"])
        analyze_calls, propose_calls = settings["llm"]["analyze_max_calls"], settings["llm"]["propose_max_calls"]
        max_seconds = settings["validation"]["max_seconds"]
        judgment = settings["judgment"]
        errors = check_config(judgment)
        if errors:
            raise ValueError("판정 기준 설정 오류: " + "; ".join(errors))
        store.update(run_id, model=model_name(engine.name, champion), champion_version=champion,
                     scenarios=sets)

        progress = _progress_writer(store, run_id)
        stage = "run"
        progress("run", 0, len(sets["train"]["cases"]), "챔피언 실행 시작")
        pack, instance, decisions, result = stages.run_stage(engine, params, sets["train"]["cases"], progress)
        store.save_stage(run_id, "run", result)
        store.save_stage(run_id, "decisions", decisions)   # 대표 케이스의 결정

        stage = "analysis"
        progress("analysis", 0, 1, "분석 agent 실행 중")
        report = stages.analysis_stage(pack, instance, decisions, llm, llm_config, analyze_calls)
        store.save_stage(run_id, "analysis", report)
        if report["stop"] != "submitted" or not report["findings"]:
            return store.set_status(run_id, FAILED, f"결과분석: 근거 있는 발견 없음 (stop={report['stop']})")
        store.set_status(run_id, ANALYZED)

        stage = "proposals"
        progress("proposals", 0, 1, "개선 agent 실행 중")
        out = stages.proposal_stage(engine, pack, instance, params, report, llm, llm_config, propose_calls)
        store.save_stage(run_id, "proposals", out)
        if not any(p["state"] == stages.VALID for p in out["proposals"]):
            return store.set_status(run_id, FAILED, f"개선안 도출: 검증할 params 안 없음 (stop={out['stop']})")
        store.set_status(run_id, PROPOSED)

        stage = "validation"
        try:
            validation = stages.validation_stage(engine, params, out["proposals"], report, sets, judgment,
                                                 max_seconds, progress)
        except BudgetExceeded as exc:
            return store.set_status(run_id, FAILED, f"검증: {exc}")
        store.save_stage(run_id, "validation", validation)
        store.set_status(run_id, VALIDATED)
        return store.set_status(run_id, AWAITING_APPROVAL)   # 여기서 멈춘다. 적용은 approve로만
    except Exception as exc:
        store.update(run_id, error={"stage": stage, "traceback": traceback.format_exc()})
        store.set_status(run_id, FAILED, f"{stage}: {type(exc).__name__}: {exc}")
        raise


def _awaiting(store: RunStore, run_id: str) -> dict:
    try:
        run = store.load(run_id)
    except KeyError as exc:
        raise WorkflowError(str(exc)) from None
    if run["status"] != AWAITING_APPROVAL:
        raise WorkflowError(f"{run_id}는 승인 대기 상태가 아니다 (현재 {run['status']})")
    return run


def _registry(run: dict) -> Registry:
    return Registry(run["models_dir"], run["engine"])


def approve(*, runs_dir: str | Path, run_id: str, proposal_id: int | None = None, note: str | None = None,
            override_reason: str | None = None) -> dict:
    """사람의 승인: 판정을 통과한 안(또는 사유를 남기고 판정을 무시한 안)을 새 버전으로 등록하고 챔피언으로 지정한다."""
    store = RunStore(runs_dir)
    registry = _registry(_awaiting(store, run_id))
    try:
        with registry.lock():
            return _approve_locked(store, registry, run_id, proposal_id, note, override_reason)
    except RegistryError as exc:
        raise WorkflowError(str(exc)) from None


def _approve_locked(store: RunStore, registry: Registry, run_id: str, proposal_id: int | None, note: str | None,
                    override_reason: str | None) -> dict:
    run = _awaiting(store, run_id)   # 잠금 안에서 상태를 다시 확인한다
    results = {r["id"]: r for r in store.load_stage(run_id, "validation")["results"]}
    passing = [i for i, r in results.items() if r["verdict"]["pass"]]
    if proposal_id is None:
        if len(passing) != 1:
            raise WorkflowError(f"승인할 개선안을 --proposal로 고른다 (판정 통과: {passing or '없음'}, "
                                f"검증된 안: {sorted(results)})")
        proposal_id = passing[0]
    if proposal_id not in results:
        raise WorkflowError(f"개선안 {proposal_id}는 검증되지 않았다 (검증된 안: {sorted(results)})")
    result = results[proposal_id]
    if result["violations"]:
        raise WorkflowError(f"개선안 {proposal_id}는 필수조건 위반 {result['violations']}건이라 승인할 수 없다")
    overridden = not result["verdict"]["pass"]
    if overridden and not override_reason:
        raise WorkflowError(f"개선안 {proposal_id}는 판정을 통과하지 못했다: " + "; ".join(result["verdict"]["reasons"])
                            + ". 판정을 무시하고 승인하려면 --override-verdict --reason ...")
    champion = registry.champion()
    if champion != run["champion_version"]:
        raise WorkflowError(f"챔피언이 v{run['champion_version']}에서 v{champion}로 바뀌었다. "
                            "이 실행의 비교 결과는 더 이상 유효하지 않으니 새로 실행한다")

    proposal = next(p for p in store.load_stage(run_id, "proposals")["proposals"] if p["id"] == proposal_id)
    version = registry.register(champion, proposal["proposal"], {
        "run_id": run_id, "proposal_id": proposal_id, "proposal": proposal["proposal"],
        "scenarios": {k: {"name": s["name"], "path": s["path"], "cases": s["cases"]}
                      for k, s in run["scenarios"].items()},
        "validation": {k: result[k]["summary"] for k in ("train", "holdout")},
        "verdict": {"pass": result["verdict"]["pass"], "overfit": result["verdict"]["overfit"],
                    "reasons": result["verdict"]["reasons"]},
        "override": {"reason": override_reason} if overridden else None,
        "origin": proposal.get("origin"),   # 사람이 수정한 AI 안이면 원래 안 번호와 수정 내용
        "approved_at": now(), "note": note,
    })
    registry.set_champion(version, "approve", run_id=run_id, proposal_id=proposal_id)
    before, after = model_name(run["engine"], champion), model_name(run["engine"], version)
    store.save_stage(run_id, "apply", {
        "decision": "approved", "at": now(), "note": note, "proposal_id": proposal_id,
        "title": proposal["proposal"].get("title"), "override_reason": override_reason if overridden else None,
        "version_before": champion, "version_after": version, "model_before": before, "model_after": after,
        "params_path": str(registry.params_path(version)),
    })
    reason = f"개선안 {proposal_id} 승인: {before} → {after}" + (" (판정 무시)" if overridden else "")
    return store.set_status(run_id, APPLIED, reason)


def reject(*, runs_dir: str | Path, run_id: str, reason: str) -> dict:
    """사람의 반려: 레지스트리는 그대로 두고 기록만 남긴다."""
    store = RunStore(runs_dir)
    registry = _registry(_awaiting(store, run_id))
    try:
        with registry.lock():
            _awaiting(store, run_id)
            store.save_stage(run_id, "apply", {"decision": "rejected", "at": now(), "reason": reason})
            return store.set_status(run_id, REJECTED, reason)
    except RegistryError as exc:
        raise WorkflowError(str(exc)) from None


def rollback(*, models_dir: str | Path, engine: str, reason: str, to: int | None = None) -> tuple[int, int]:
    """챔피언을 이전 버전으로 되돌린다. 버전 파일은 지우지 않는다. (이전 챔피언, 새 챔피언)"""
    registry = Registry(models_dir, engine)
    try:
        with registry.lock():
            return registry.rollback(reason, to)
    except RegistryError as exc:
        raise WorkflowError(str(exc)) from None


# --- AI 개선안 수정 (값만) ---

def _shape(proposal: dict) -> tuple:
    """값을 뺀 개선안의 모양: 전역 변경 경로 목록, 구간 조건의 when과 set 경로."""
    return (tuple(c["path"] for c in proposal.get("params_changes") or []),
            tuple((json.dumps(r["when"], sort_keys=True), tuple(r["set"])) for r in proposal.get("override_rules") or []))


def revise(*, runs_dir: str | Path, run_id: str, proposal_id: int, changes: dict, max_seconds: float,
           note: str | None = None) -> dict:
    """사람이 AI 개선안의 값을 고친 안을 새 안으로 추가하고, 원래 안과 같은 방식으로 검증·판정한다.

    changes: {"params_changes": [...], "override_rules": [...]} 원래 안과 경로·구간 조건이 같고 값만 다르다.
    비교 기준은 이 실행의 챔피언 params, 시나리오 세트, 판정 기준이다 (실행 당시 스냅샷).
    """
    store = RunStore(runs_dir)
    registry = _registry(_awaiting(store, run_id))
    try:
        with registry.lock():   # 승인·반려와 단계 파일 쓰기가 섞이지 않게
            return _revise_locked(store, run_id, proposal_id, changes, max_seconds, note)
    except RegistryError as exc:
        raise WorkflowError(str(exc)) from None


def _revise_locked(store: RunStore, run_id: str, proposal_id: int, changes: dict, max_seconds: float,
                   note: str | None) -> dict:
    run = _awaiting(store, run_id)
    proposals = store.load_stage(run_id, "proposals")
    validation = store.load_stage(run_id, "validation")
    original = next((p for p in proposals["proposals"] if p["id"] == proposal_id), None)
    if original is None or original["state"] != stages.VALID:
        raise WorkflowError(f"개선안 {proposal_id}는 수정할 수 없다 (검증된 params 안만 수정한다)")
    edited = {"params_changes": copy.deepcopy(changes.get("params_changes") or []),
              "override_rules": copy.deepcopy(changes.get("override_rules") or [])}
    try:
        same_shape = _shape(edited) == _shape(original["proposal"])
    except (KeyError, TypeError):
        same_shape = False
    if not same_shape:
        raise WorkflowError("값만 고칠 수 있다: 변경 경로와 구간 조건(when, set 경로)은 원래 안과 같아야 한다")
    if all(edited[k] == (original["proposal"].get(k) or []) for k in edited):
        raise WorkflowError("원래 안과 값이 같다")

    engine = get_engine(run["engine"])
    params = store.load_stage(run_id, "run")["params"]
    pack = engine.pack_factory(params)
    errors = params_errors(params, edited, pack.dimensions())
    if errors:
        raise WorkflowError("허용 범위 검사 실패: " + "; ".join(errors))

    new_id = max(p["id"] for p in proposals["proposals"]) + 1
    base = original["proposal"]
    proposal = {**{k: v for k, v in base.items() if k not in ("params_changes", "override_rules")}, **edited,
                "title": f"{base.get('title')} (사람 수정)"}
    entry = {"id": new_id, "state": stages.VALID, "errors": [], "proposal": proposal,
             "origin": {"revised_from": proposal_id, "by": "human", "at": now(), "note": note,
                        "before": {k: base.get(k) or [] for k in edited}, "after": edited}}
    sets = {k: {"name": s["name"], "cases": s["cases"]} for k, s in run["scenarios"].items()}
    report = store.load_stage(run_id, "analysis")
    try:
        result = stages.validation_stage(engine, params, [entry], report, sets, validation["judgment"],
                                         max_seconds)["results"][0]
    except BudgetExceeded as exc:
        raise WorkflowError(f"수정안 검증: {exc}") from None
    proposals["proposals"].append(entry)
    validation["results"].append({**result, "origin": entry["origin"]})
    store.save_stage(run_id, "proposals", proposals)
    store.save_stage(run_id, "validation", validation)
    return {"proposal": entry, "result": result}
