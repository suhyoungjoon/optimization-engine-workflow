"""워크플로우 5단계. 각 단계는 코어 함수를 조합하고, 엔진은 Engine 프로토콜로만 다룬다.

1 실행(코드) → 2 결과분석(AI+코드) → 3 개선안 도출(AI+코드) → 4 검증(코드) → 5 개선적용(사람+코드)
5단계는 사람의 승인으로만 일어나며 runner.approve가 모델 레지스트리에 새 버전을 등록한다.
"""

import statistics
import time
from collections import Counter
from pathlib import Path

from core import analyze, apply_params, check_params, finding_slices, params_errors, propose

from engines.base import Engine

from .compare import compare_cases
from .judge import judge

# 3단계 개선안 상태: 검증 대상은 valid뿐이다
VALID, INVALID, UNSUPPORTED = "valid", "invalid", "unsupported"


def run_stage(engine: Engine, params: dict, cases: list[dict]) -> tuple:
    """1. 실행: 챔피언 params로 학습용 케이스마다 solve → validate → metrics.

    (pack, 대표 인스턴스, 대표 결정, 결과). 대표 케이스(첫 케이스)는 2·3단계(분석·개선안 도출)에 쓴다.
    """
    pack = engine.pack_factory(params)
    errors = check_params(params, pack.dimensions())
    if errors:
        raise ValueError("params 파일이 허용 범위 검사를 통과하지 못함: " + "; ".join(errors))
    rows, representative = [], None
    for case in cases:
        instance, _truth = pack.generate(case["seed"], case["faults"])   # 정답표는 분석·제안에 넘기지 않는다
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


def validation_stage(engine: Engine, params: dict, proposals: list[dict], report: dict, sets: dict[str, dict],
                     judgment: dict, max_seconds: float) -> dict:
    """4. 검증(비교): valid 안마다 챔피언 대 도전자를 학습용·검증용 세트의 모든 케이스에서 비교하고 판정한다.

    sets: {"train": 세트, "holdout": 세트}. 인스턴스는 세트마다 한 번만 만든다.
    판정을 통과하지 못한 안도 결과는 남긴다 (사람이 판정을 무시하고 승인할 수 있다, 단 위반 0건일 때만).
    """
    deadline = time.monotonic() + max_seconds
    pack = engine.pack_factory(params)
    instances = {name: [(c, pack.generate(c["seed"], c["faults"])[0]) for c in s["cases"]] for name, s in sets.items()}
    slices = finding_slices(report)
    results = []
    for p in proposals:
        if p["state"] != VALID:
            continue
        candidate = apply_params(params, p["proposal"])
        compared = {name: compare_cases(engine.pack_factory, params, candidate, insts, slices, deadline)
                    for name, insts in instances.items()}
        verdict = judge(judgment, {name: c["summary"] for name, c in compared.items()})
        results.append({"id": p["id"], "title": p["proposal"].get("title"), **compared, "verdict": verdict,
                        "violations": sum(c["summary"]["violations"] for c in compared.values())})
    return {"method": "simulate_params per case (train + holdout)", "judgment": judgment,
            "sets": {name: {"name": s["name"], "cases": len(s["cases"])} for name, s in sets.items()},
            "results": results}
