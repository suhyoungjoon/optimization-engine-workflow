"""챔피언/도전자 비교: 세트의 케이스마다 simulate_params를 돌리고 지표 Δ를 집계한다.

필수조건 위반은 케이스마다 도전자 팩의 validate()로 센다 (simulate_params의 violations_after).
"""

import statistics
import time

from core import simulate_params


class BudgetExceeded(Exception):
    """settings/workflow.yaml의 validation.max_seconds를 넘겼다."""


def check_deadline(deadline: float | None) -> None:
    if deadline is not None and time.monotonic() > deadline:
        raise BudgetExceeded("검증 시간 예산을 넘겼다 (settings/workflow.yaml validation.max_seconds)")


def compare_cases(pack_factory, params: dict, candidate: dict, instances: list[tuple[dict, object]],
                  slices: dict | None = None, deadline: float | None = None) -> dict:
    """instances: [(케이스 {seed, faults}, 인스턴스)]. deadline: time.monotonic() 기준."""
    rows = []
    for case, instance in instances:
        check_deadline(deadline)
        sim = simulate_params(pack_factory, instance, params, candidate, slices)
        check_deadline(deadline)   # 마지막 케이스가 예산을 넘겨도 결과로 쓰지 않는다
        rows.append({**case, "before": sim["before"], "after": sim["after"],
                     "violations_after": sim["violations_after"], "slices": sim["slices"],
                     "seconds": round(sim["seconds"], 3)})
    return {"cases": rows, "summary": summarize(rows)}


def summarize(rows: list[dict]) -> dict:
    metrics = [m for m in rows[0]["before"] if all(m in r["before"] and m in r["after"] for r in rows)]
    out = {"cases": len(rows), "violations": sum(r["violations_after"] for r in rows), "metrics": {}}
    for m in metrics:
        deltas = [r["after"][m] - r["before"][m] for r in rows]
        out["metrics"][m] = {
            "before": round(statistics.mean(r["before"][m] for r in rows), 6),
            "after": round(statistics.mean(r["after"][m] for r in rows), 6),
            "delta_mean": round(statistics.mean(deltas), 6),
            "delta_min": round(min(deltas), 6),
            "delta_max": round(max(deltas), 6),
            "improved": sum(d > 0 for d in deltas),
            "worsened": sum(d < 0 for d in deltas),
        }
    names = [n for n in rows[0]["slices"] if all(n in r["slices"] for r in rows)]
    out["slices"] = {n: {k: round(statistics.mean(r["slices"][n][k]["fail_rate"] for r in rows), 6)
                         for k in ("before", "after")} for n in names}
    return out
