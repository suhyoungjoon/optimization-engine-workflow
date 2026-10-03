"""워크플로우 단계. 각 단계는 코어 함수를 조합하고, 엔진은 Engine 프로토콜로만 다룬다.

1 실행(코드) → 2 결과분석(AI+코드) → 3 개선안 도출(AI+코드) → 4 파라미터 탐색(AI+코드) → 5 검증(코드)
→ 6 개선적용(사람+코드). 6단계는 사람의 승인으로만 일어나며 runner.approve가 모델 레지스트리에 새 버전을 등록한다.
"""

import json
import statistics
import time
from collections import Counter
from pathlib import Path

from core import (analyze, apply_params, check_params, finding_slices, params_errors, propose, run_tool_loop,
                  usage_dict)

from engines.base import Engine

from .compare import BudgetExceeded, check_deadline, compare_cases
from .judge import judge
from .scenarios import case_label, make_instance
from .search import run_search, space_errors

# 3단계 개선안 상태: 검증 대상은 valid뿐이다
VALID, INVALID, UNSUPPORTED = "valid", "invalid", "unsupported"


def _no_progress(stage: str, done: int, total: int, detail: str = "") -> None:
    pass


def run_stage(engine: Engine, params: dict, cases: list[dict], progress=_no_progress) -> tuple:
    """1. 실행: 챔피언 params로 학습용 케이스마다 solve → validate → metrics.

    (pack, 대표 인스턴스, 대표 결정, 결과). 대표 케이스(첫 케이스)는 2·3단계(분석·개선안 도출)에 쓴다.
    """
    pack = engine.pack_factory(params)
    errors = check_params(params, pack.dimensions())
    if errors:
        raise ValueError("params 파일이 허용 범위 검사를 통과하지 못함: " + "; ".join(errors))
    rows, representative = [], None
    for case in cases:
        instance = make_instance(pack, case)   # 정답표는 분석·제안에 넘기지 않는다
        decisions = pack.solve(instance, params)
        violations = pack.validate(instance, decisions)
        rows.append({
            **case,
            "items": len(decisions),
            "status": dict(Counter(d.status for d in decisions)),
            "reasons": dict(Counter(d.reason_code for d in decisions if d.status != "success").most_common()),
            "metrics": pack.metrics(instance, decisions),
            "violations": len(violations),
            "violation_rules": dict(Counter(v.rule for v in violations)),
        })
        if representative is None:
            representative = (instance, decisions)
        progress("run", len(rows), len(cases), f"학습용 {case_label(case)}")
    metrics = [m for m in rows[0]["metrics"] if all(m in r["metrics"] for r in rows)]
    result = {
        "cases": rows,
        "summary": {"cases": len(rows), "violations": sum(r["violations"] for r in rows),
                    "metrics": {m: round(statistics.mean(r["metrics"][m] for r in rows), 6) for m in metrics}},
        "representative": cases[0],
        "params": params,
    }
    return pack, representative[0], representative[1], result


def analysis_stage(pack, instance, decisions, llm, llm_config: dict, max_calls: int) -> dict:
    """2. 결과분석: 분석 agent. 수치 근거가 없는 발견은 코어가 제외한다."""
    return analyze(pack, instance, decisions, llm, llm_config, max_calls=max_calls)


def proposal_stage(engine: Engine, pack, instance, params: dict, report: dict, llm, llm_config: dict,
                   max_calls: int) -> dict:
    """3. 개선안 도출: 개선 agent가 제안하고, 코드가 params 안을 허용 범위로 다시 검사한다.

    spec 안은 AI agent 재실행이 필요해 이 레포에서 검증할 수 없으므로 unsupported로 기록만 한다.
    """
    spec_text = Path(pack.spec_path()).read_text(encoding="utf-8")   # 코어 패키지의 명세, 읽기만 한다
    out = propose(engine.pack_factory, instance, params, spec_text, pack.dimensions(), report, llm, llm_config,
                  max_calls=max_calls)
    proposals = []
    for i, item in enumerate(out["proposals"], start=1):
        p = item["proposal"]
        if p.get("kind") != "params":
            state, errors = UNSUPPORTED, [f"{p.get('kind')} 안은 이 워크플로우에서 검증하지 않는다"]
        else:
            errors = params_errors(params, p, pack.dimensions())   # propose의 검사를 믿지 않고 다시 검사
            state = INVALID if errors else VALID
        proposals.append({"id": i, "state": state, "errors": errors, "proposal": p})
    return {**out, "proposals": proposals}


SEARCH_SYSTEM = """너는 배정 엔진 파라미터의 탐색 범위를 정하는 agent다.
분석 리포트의 발견을 보고, 바꾸면 효과가 있을 숫자 파라미터와 그 범위를 고른다. 값 하나를 정하지 말고 범위를 낸다.
범위 안의 조합은 코드가 학습용 시나리오에서 직접 풀어 보고 고른다. 범위는 각 섹션의 bounds 안이어야 한다.
경로는 '섹션.키' 또는 '섹션.키[인덱스]' 형식이고 숫자 값 하나를 가리켜야 한다. get_params로 현재 값과 bounds를 본다.
submit_search_space로 제출한다."""

_SPACE_TOOL = {
    "name": "submit_search_space",
    "description": "탐색할 파라미터와 범위를 제출한다. 코드가 경로·허용 범위를 검사하고, 문제가 있으면 한 번 고쳐 오게 한다.",
    "input_schema": {"type": "object", "required": ["space", "rationale"], "properties": {
        "rationale": {"type": "string", "description": "이 범위를 고른 이유 (어떤 발견과 연결되는지)"},
        "space": {"type": "array", "items": {"type": "object", "required": ["path", "low", "high"], "properties": {
            "path": {"type": "string"}, "low": {"type": "number"}, "high": {"type": "number"},
            "reason": {"type": "string"}}}}}},
}


def _space_agent(params: dict, dimensions: dict, report: dict, llm, llm_config: dict, max_calls: int,
                 max_dims: int) -> tuple[dict, dict]:
    """AI가 탐색 범위를 낸다. (제출, agent 기록). 코드가 범위를 검사하고 문제가 있으면 한 번 되돌려 보낸다."""
    tools = [{"name": "get_params", "handler": lambda args: {"params": params},
              "description": "현재 챔피언 파라미터(허용 범위 bounds·설명 docs 포함).",
              "input_schema": {"type": "object", "properties": {}}}]
    findings = [{k: f.get(k) for k in ("id", "title", "description", "slice", "reason_codes", "hypothesis")}
                for f in report.get("findings", [])]
    user = ("# 분석 리포트\n" + json.dumps({"summary": report.get("summary"), "findings": findings}, ensure_ascii=False,
                                         indent=1)
            + f"\n\n한 번에 고를 수 있는 파라미터는 {max_dims}개까지다.")
    result = run_tool_loop(llm, system=SEARCH_SYSTEM, user=user, tools=tools, submit_tool=_SPACE_TOOL,
                           max_calls=max_calls,
                           check_submission=lambda sub, calls: space_errors(params, sub.get("space"), dimensions,
                                                                            max_dims))
    agent = {"stop": result.stop, "calls": result.calls, "usage": usage_dict(result, llm.model, llm_config)}
    return result.submission or {}, agent


def search_stage(engine: Engine, params: dict, report: dict, cases: list[dict], judgment: dict, cfg: dict, llm,
                 llm_config: dict, max_calls: int, first_id: int, progress=_no_progress) -> dict:
    """4. 파라미터 탐색: AI가 범위를 내고, 코드가 학습용 세트에서 조합을 풀어 판정 규칙으로 점수를 매긴다.

    검증용 세트는 쓰지 않는다. 상위 top_k개를 개선안(id는 first_id부터)으로 만들어 5단계 검증에 넘긴다.
    범위를 못 받거나 학습용 판정을 통과한 조합이 없으면 후보 없이 끝난다 (실행을 실패시키지 않는다).
    """
    pack = engine.pack_factory(params)
    dims = pack.dimensions()
    submission, agent = _space_agent(params, dims, report, llm, llm_config, max_calls, cfg["max_dims"])
    space = submission.get("space")
    errors = space_errors(params, space, dims, cfg["max_dims"]) if agent["stop"] == "submitted" else []
    out = {"agent": agent, "rationale": submission.get("rationale"), "space": space, "space_errors": errors,
           "settings": cfg, "cases": list(cases), "evals": [], "candidates": []}
    if agent["stop"] != "submitted" or errors:
        return {**out, "stop": "no_space"}

    deadline = time.monotonic() + cfg["max_seconds"]
    instances = []
    try:
        for c in cases:   # 인스턴스 생성도 예산에 넣는다
            check_deadline(deadline)
            instances.append((c, make_instance(pack, c)))
    except BudgetExceeded:
        return {**out, "stop": "max_seconds"}   # 탐색만 멈춘다. 실행은 개선 agent의 안으로 계속된다
    slices = finding_slices(report)

    done = 0

    def evaluate(point: dict) -> dict:
        nonlocal done
        candidate = apply_params(params, _changes(point))
        summary = compare_cases(engine.pack_factory, params, candidate, instances, slices, deadline)["summary"]
        done += 1
        progress("search", done, cfg["max_evals"], "조합 " + ", ".join(f"{k}={v}" for k, v in point.items()))
        return summary

    progress("search", 0, cfg["max_evals"], "탐색 시작")
    found = run_search(evaluate, params, space, judgment, cfg)
    candidates = []
    for rank, e in enumerate(found["top"], start=1):
        delta = e["target"]
        proposal = {"title": f"탐색 {rank}위: " + ", ".join(f"{k}={v}" for k, v in e["point"].items()),
                    "kind": "params", "params_changes": _changes(e["point"])["params_changes"],
                    "rationale": (f"학습용 {len(cases)}건에서 조합 {len(found['evals'])}개를 풀어 본 결과 {rank}위 "
                                  f"(목표 지표 평균 Δ {delta:+.3f}, 학습용 판정 통과)"),
                    "target_findings": [], "expected_effect": submission.get("rationale")}
        errors = params_errors(params, proposal, dims)   # 범위를 검사했어도 제출 안은 다시 검사한다
        candidates.append({"id": first_id + rank - 1, "state": INVALID if errors else VALID, "errors": errors,
                           "proposal": proposal, "origin": {"by": "search", "rank": rank, "eval": e["i"]}})
    return {**out, "judgment": found["judgment"], "evals": found["evals"], "stop": found["stop"],
            "candidates": candidates}


def _changes(point: dict) -> dict:
    return {"kind": "params", "params_changes": [{"path": k, "value": v} for k, v in point.items()]}


def validation_stage(engine: Engine, params: dict, proposals: list[dict], report: dict, sets: dict[str, dict],
                     judgment: dict, max_seconds: float, progress=_no_progress) -> dict:
    """4. 검증(비교): valid 안마다 챔피언 대 도전자를 학습용·검증용 세트의 모든 케이스에서 비교하고 판정한다.

    sets: {"train": 세트, "holdout": 세트}. 인스턴스는 세트마다 한 번만 만든다.
    판정을 통과하지 못한 안도 결과는 남긴다 (사람이 판정을 무시하고 승인할 수 있다, 단 위반 0건일 때만).
    """
    deadline = time.monotonic() + max_seconds
    pack = engine.pack_factory(params)
    valid = [p for p in proposals if p["state"] == VALID]
    n_cases = sum(len(s["cases"]) for s in sets.values())
    total, done = n_cases * (1 + len(valid)), 0   # 인스턴스 생성 + 안마다 케이스 비교
    instances = {}
    for name, s in sets.items():   # 인스턴스 생성도 예산에 넣는다
        instances[name] = []
        for c in s["cases"]:
            check_deadline(deadline)
            instances[name].append((c, make_instance(pack, c)))
            done += 1
            progress("validation", done, total, f"시나리오 생성 {case_label(c)}")
    slices = finding_slices(report)
    results = []
    for p in valid:
        candidate = apply_params(params, p["proposal"])
        compared = {}
        for name, insts in instances.items():
            def on_case(case, name=name, pid=p["id"]):
                nonlocal done
                done += 1
                progress("validation", done, total, f"개선안 {pid} {name} {case_label(case)}")
            compared[name] = compare_cases(engine.pack_factory, params, candidate, insts, slices, deadline, on_case)
        verdict = judge(judgment, {name: c["summary"] for name, c in compared.items()})
        results.append({"id": p["id"], "title": p["proposal"].get("title"), **compared, "verdict": verdict,
                        "violations": sum(c["summary"]["violations"] for c in compared.values())})
    return {"method": "simulate_params per case (train + holdout)", "judgment": judgment,
            "sets": {name: {"name": s["name"], "cases": len(s["cases"])} for name, s in sets.items()},
            "results": results}
