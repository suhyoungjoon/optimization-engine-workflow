"""워크플로우 실행과 사람의 결정(승인·반려).

run_workflow는 1~4단계를 돌리고 awaiting_approval에서 멈춘다. 개선적용(5단계)은 approve로만 일어난다 (자동 승인 없음).
경로(params, runs, 설정)는 모두 인자로 받는다. 코어의 기본 경로(site-packages)는 쓰지 않는다.
"""

import json
import os
import traceback
from contextlib import contextmanager
from importlib import metadata
from pathlib import Path

from engines.base import Engine

from . import stages
from .state import ANALYZED, APPLIED, AWAITING_APPROVAL, FAILED, PROPOSED, REJECTED, VALIDATED
from .storage import RunStore, now


class WorkflowError(Exception):
    """사람이 고칠 수 있는 요청 오류 (잘못된 상태, 없는 개선안, 버전 불일치 등)."""


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


def run_workflow(engine: Engine, *, params_path: str | Path, runs_dir: str | Path, seed: int, faults: list[str],
                 llm, llm_config: dict, settings: dict, rehearsal: bool = False) -> dict:
    store = RunStore(runs_dir)
    params_path = Path(params_path).resolve()
    run_id = store.create({
        "engine": engine.name,
        "model": None,              # params를 읽은 뒤 채운다 (읽기 실패도 이 실행의 failed로 남기기 위해)
        "params_path": str(params_path),
        "params_version": None,
        "scenario": {"seed": seed, "faults": faults},
        "llm": {"model": llm.model, "rehearsal": rehearsal},
        "core": core_ref(),
    })
    stage = "setup"
    try:
        params = engine.load_params(params_path)
        store.update(run_id, model=model_name(engine.name, params["version"]), params_version=params["version"])
        analyze_calls, propose_calls = settings["llm"]["analyze_max_calls"], settings["llm"]["propose_max_calls"]

        stage = "run"
        pack, instance, decisions, result = stages.run_stage(engine, params, seed, faults)
        store.save_stage(run_id, "run", result)
        store.save_stage(run_id, "decisions", decisions)

        stage = "analysis"
        report = stages.analysis_stage(pack, instance, decisions, llm, llm_config, analyze_calls)
        store.save_stage(run_id, "analysis", report)
        if report["stop"] != "submitted" or not report["findings"]:
            return store.set_status(run_id, FAILED, f"결과분석: 근거 있는 발견 없음 (stop={report['stop']})")
        store.set_status(run_id, ANALYZED)

        stage = "proposals"
        out = stages.proposal_stage(engine, pack, instance, params, report, llm, llm_config, propose_calls)
        store.save_stage(run_id, "proposals", out)
        if not any(p["state"] == stages.VALID for p in out["proposals"]):
            return store.set_status(run_id, FAILED, f"개선안 도출: 검증할 params 안 없음 (stop={out['stop']})")
        store.set_status(run_id, PROPOSED)

        stage = "validation"
        validation = stages.validation_stage(engine, instance, params, out["proposals"], report)
        store.save_stage(run_id, "validation", validation)
        store.set_status(run_id, VALIDATED)
        return store.set_status(run_id, AWAITING_APPROVAL)   # 여기서 멈춘다. 적용은 approve로만
    except Exception as exc:
        store.update(run_id, error={"stage": stage, "traceback": traceback.format_exc()})
        store.set_status(run_id, FAILED, f"{stage}: {type(exc).__name__}: {exc}")
        raise


@contextmanager
def _params_lock(params_path: str | Path):
    """승인·반려를 params 파일 단위로 직렬화한다. 버전 확인과 쓰기 사이에 다른 승인이 끼어들지 못하게 한다."""
    lock = Path(f"{params_path}.lock")
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise WorkflowError(f"다른 승인·반려가 진행 중이다 ({lock}). 진행 중인 작업이 없으면 이 파일을 지운다") from None
    try:
        os.close(fd)
        yield
    finally:
        lock.unlink(missing_ok=True)


def _awaiting(store: RunStore, run_id: str) -> dict:
    try:
        run = store.load(run_id)
    except KeyError as exc:
        raise WorkflowError(str(exc)) from None
    if run["status"] != AWAITING_APPROVAL:
        raise WorkflowError(f"{run_id}는 승인 대기 상태가 아니다 (현재 {run['status']})")
    return run


def approve(engine: Engine, *, runs_dir: str | Path, run_id: str, proposal_id: int | None = None,
            note: str | None = None) -> dict:
    """사람의 승인: 검증을 통과한 안 하나를 params 파일에 반영하고 버전을 올린다."""
    store = RunStore(runs_dir)
    with _params_lock(_awaiting(store, run_id)["params_path"]):
        return _approve_locked(engine, store, run_id, proposal_id, note)


def _approve_locked(engine: Engine, store: RunStore, run_id: str, proposal_id: int | None, note: str | None) -> dict:
    run = _awaiting(store, run_id)   # 잠금 안에서 상태를 다시 확인한다
    results = {r["id"]: r for r in store.load_stage(run_id, "validation")["results"]}
    approvable = [i for i, r in results.items() if r["approvable"]]
    if proposal_id is None:
        if len(approvable) != 1:
            raise WorkflowError(f"승인할 개선안을 --proposal로 고른다 (승인 가능: {approvable or '없음'})")
        proposal_id = approvable[0]
    if proposal_id not in results:
        raise WorkflowError(f"개선안 {proposal_id}는 검증되지 않았다 (검증된 안: {sorted(results)})")
    if not results[proposal_id]["approvable"]:
        raise WorkflowError(f"개선안 {proposal_id}는 필수조건 위반 {results[proposal_id]['violations_after']}건이라 "
                            "승인할 수 없다")
    current = engine.load_params(run["params_path"])["version"]
    if current != run["params_version"]:
        raise WorkflowError(f"params가 v{run['params_version']}에서 v{current}로 바뀌었다. "
                            "이 실행의 비교 결과는 더 이상 유효하지 않으니 새로 실행한다")

    proposal = next(p for p in store.load_stage(run_id, "proposals")["proposals"] if p["id"] == proposal_id)
    before, after = stages.apply_stage(run["params_path"], proposal["proposal"])
    store.save_stage(run_id, "apply", {
        "decision": "approved", "at": now(), "note": note, "proposal_id": proposal_id,
        "title": proposal["proposal"].get("title"), "params_path": run["params_path"],
        "version_before": before, "version_after": after,
        "model_before": model_name(run["engine"], before), "model_after": model_name(run["engine"], after),
    })
    return store.set_status(run_id, APPLIED, f"개선안 {proposal_id} 승인: v{before} → v{after}")


def reject(*, runs_dir: str | Path, run_id: str, reason: str) -> dict:
    """사람의 반려: params는 그대로 두고 기록만 남긴다."""
    store = RunStore(runs_dir)
    with _params_lock(_awaiting(store, run_id)["params_path"]):
        _awaiting(store, run_id)
        store.save_stage(run_id, "apply", {"decision": "rejected", "at": now(), "reason": reason})
        return store.set_status(run_id, REJECTED, reason)
