"""판정: 도전자가 챔피언을 대체해도 되는지 코드로 정한다 (settings/workflow.yaml의 judgment).

- 필수조건 위반 0건은 절대 기준이다. 설정으로 바꿀 수 없다.
- 목표 지표(높을수록 좋은 지표)의 세트 평균 Δ가 min_improvement 이상이어야 한다.
- 부작용 한도(guards): 지표별 세트 평균 Δ가 max_drop만큼 넘게 떨어지거나 max_increase만큼 넘게 오르면 불통과.
- 학습용·검증용 세트 모두에서 통과해야 통과다. 학습용에서만 목표를 채우면 overfit으로 표시한다.
"""

import math
from numbers import Number

EPS = 1e-9
SET_LABELS = {"train": "학습용", "holdout": "검증용"}


def _number(v) -> bool:
    """bool은 Number의 하위 타입이라 따로 막는다 (YAML의 true/false가 임계값으로 들어오지 않게)."""
    return isinstance(v, Number) and not isinstance(v, bool) and math.isfinite(v)


def check_config(cfg: dict) -> list[str]:
    """설정의 모양이 틀려도 예외 대신 오류 목록을 돌려준다 (화면의 설정 저장이 400으로 답하도록)."""
    if cfg is not None and not isinstance(cfg, dict):
        return ["judgment는 {target, guards} 형태여야 한다"]
    errors = []
    target = (cfg or {}).get("target") or {}
    if not isinstance(target, dict):
        target = {}
        errors.append("judgment.target은 {metric, min_improvement} 형태여야 한다")
    if not isinstance(target.get("metric"), str):
        errors.append("judgment.target.metric이 필요하다")
    if not _number(target.get("min_improvement")):
        errors.append("judgment.target.min_improvement는 숫자여야 한다")
    guards = (cfg or {}).get("guards") or {}
    if not isinstance(guards, dict):
        return errors + ["judgment.guards는 {지표: {max_drop 또는 max_increase}} 형태여야 한다"]
    for metric, limit in guards.items():
        keys = set(limit) if isinstance(limit, dict) else set()
        if len(keys) != 1 or not keys <= {"max_drop", "max_increase"}:
            errors.append(f"judgment.guards.{metric}에는 max_drop 또는 max_increase 하나만 쓴다")
        elif not _number(next(iter(limit.values()))) or next(iter(limit.values())) < 0:
            errors.append(f"judgment.guards.{metric} 한도는 0 이상의 숫자여야 한다")
    return errors


def _check(name: str, ok: bool, value, threshold, message: str) -> dict:
    return {"name": name, "ok": ok, "value": value, "threshold": threshold, "message": message}


def judge_set(cfg: dict, summary: dict) -> dict:
    metrics = summary["metrics"]
    checks = [_check("violations", summary["violations"] == 0, summary["violations"], 0,
                     f"필수조건 위반 {summary['violations']}건")]
    target, floor = cfg["target"]["metric"], cfg["target"]["min_improvement"]
    if target not in metrics:
        checks.append(_check("target", False, None, floor, f"목표 지표 {target}가 없다"))
    else:
        d = metrics[target]["delta_mean"]
        checks.append(_check("target", d >= floor - EPS, d, floor,
                             f"{target} 평균 Δ {d:+.3f} (최소 개선 {floor:+.3f})"))
    for metric, limit in (cfg.get("guards") or {}).items():
        if metric not in metrics:
            checks.append(_check(f"guard:{metric}", False, None, limit, f"부작용 지표 {metric}가 없다"))
            continue
        d = metrics[metric]["delta_mean"]
        if "max_drop" in limit:
            ok, text = d >= -limit["max_drop"] - EPS, f"하락 한도 {limit['max_drop']:.3f}"
        else:
            ok, text = d <= limit["max_increase"] + EPS, f"증가 한도 {limit['max_increase']:.3f}"
        checks.append(_check(f"guard:{metric}", ok, d, limit, f"{metric} 평균 Δ {d:+.3f} ({text})"))
    return {"pass": all(c["ok"] for c in checks), "checks": checks}


def judge(cfg: dict, summaries: dict[str, dict]) -> dict:
    """summaries: {"train": 요약, "holdout": 요약}."""
    sets = {name: judge_set(cfg, s) for name, s in summaries.items()}
    target_ok = {name: next(c["ok"] for c in r["checks"] if c["name"] == "target") for name, r in sets.items()}
    reasons = [f"{SET_LABELS.get(name, name)}: {c['message']}"
               for name, r in sets.items() for c in r["checks"] if not c["ok"]]
    return {"pass": all(r["pass"] for r in sets.values()), "sets": sets,
            "overfit": target_ok.get("train", False) and not target_ok.get("holdout", True),
            "reasons": reasons}
