"""워크플로우 5단계. 각 단계는 코어 함수를 조합하고, 엔진은 Engine 프로토콜로만 다룬다.

1 실행(코드) → 2 결과분석(AI+코드) → 3 개선안 도출(AI+코드) → 4 검증(코드) → 5 개선적용(사람+코드)
"""

from collections import Counter
from pathlib import Path

from core import (analyze, apply_params, check_params, finding_slices, params_errors, propose, simulate_params,
                  write_params)

from engines.base import Engine

# 3단계 개선안 상태: 검증 대상은 valid뿐이다
VALID, INVALID, UNSUPPORTED = "valid", "invalid", "unsupported"


def run_stage(engine: Engine, params: dict, seed: int, faults: list[str]) -> tuple:
    """1. 실행: 챔피언 params로 solve → validate → metrics. (pack, instance, decisions, 결과)"""
    pack = engine.pack_factory(params)
    errors = check_params(params, pack.dimensions())
    if errors:
        raise ValueError("params 파일이 허용 범위 검사를 통과하지 못함: " + "; ".join(errors))
    instance, _truth = pack.generate(seed, faults)   # 정답표는 분석·제안에 넘기지 않는다
    decisions = pack.solve(instance, params)
    violations = pack.validate(instance, decisions)
    result = {
        "items": len(decisions),
        "status": dict(Counter(d.status for d in decisions)),
        "reasons": dict(Counter(d.reason_code for d in decisions if d.status != "success").most_common()),
        "metrics": pack.metrics(instance, decisions),
        "violations": len(violations),
        "violation_rules": dict(Counter(v.rule for v in violations)),
        "params": params,
    }
    return pack, instance, decisions, result


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


def validation_stage(engine: Engine, instance, params: dict, proposals: list[dict], report: dict) -> dict:
    """4. 검증(비교): valid 안마다 챔피언(현재 params) 대 도전자를 같은 인스턴스에서 1회 비교한다.

    M1은 simulate_params 1회 비교만 한다. 여러 seed·검증용 세트와 판정 기준은 M2.
    필수조건 위반은 도전자 팩의 validate()로 센다. 위반이 1건이라도 있으면 승인할 수 없다.
    """
    slices = finding_slices(report)
    results = []
    for p in proposals:
        if p["state"] != VALID:
            continue
        sim = simulate_params(engine.pack_factory, instance, params, apply_params(params, p["proposal"]), slices)
        delta = {k: round(sim["after"][k] - v, 6) for k, v in sim["before"].items() if k in sim["after"]}
        results.append({"id": p["id"], "title": p["proposal"].get("title"), **sim, "delta": delta,
                        "approvable": sim["violations_after"] == 0})
    return {"method": "simulate_params (1 instance, 1 comparison)", "results": results}


def apply_stage(params_path: str | Path, proposal: dict) -> tuple[int, int]:
    """5. 개선적용: 사람이 승인한 안을 이 레포의 params 파일에 쓰고 version +1. (이전, 새)"""
    return write_params(params_path, proposal)
