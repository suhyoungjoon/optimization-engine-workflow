"""파라미터 자동 탐색: AI가 낸 탐색 범위 안에서 코드가 여러 조합을 실제로 풀어 보고 학습용 점수가 좋은 안을 고른다.

- 범위 검사: 경로가 숫자 값 하나를 가리키고, 범위 양 끝이 params의 허용 범위(bounds) 안이어야 한다 (코어 params_errors).
- 표본: 고정 seed의 Halton 수열(저불일치 표본)로 전체 범위를 고르게 본 뒤, 가장 좋은 점 주변을 좁혀 가며 다시 본다.
  같은 입력이면 항상 같은 점을 같은 순서로 평가한다. 챔피언 값이면 정수, 아니면 소수 넷째 자리로 맞춘다.
- 점수: 학습용 세트 요약에 판정 규칙(judge_set)을 쓰되, 부작용 한도는 guard_margin만큼만 쓴다 (예: 0.8이면 한도의 80%).
  목표 지표를 최대화하면 학습용에서 한도 바로 앞까지 가고, 검증용에서는 조금만 달라도 한도를 넘기 때문이다.
  검증용 세트는 탐색에 쓰지 않는다 (과적합 확인은 검증 단계, 원래 판정 기준으로).
- 후보: 학습용 판정을 통과한 점 중 좋은 순으로, 이미 고른 점과 범위의 MIN_GAP 이상 떨어진 점만 고른다.
- 예산: 평가 수(max_evals)와 시간(max_seconds). 시간을 넘기면 탐색을 멈추고 그때까지의 결과로 고른다.
"""

import math
from numbers import Number

from core import get_path, params_errors

from .compare import BudgetExceeded
from .judge import judge_set

SEARCH_DEFAULTS = {"max_evals": 40, "max_seconds": 300, "top_k": 2, "max_dims": 3, "seed": 0, "guard_margin": 0.8}
MAX_DIMS = 5
PRIMES = (2, 3, 5, 7, 11)
COARSE_SHARE = 0.6        # 평가 수 중 전체 범위를 고르게 보는 몫. 나머지는 가장 좋은 점 주변
REFINE_WIDTH = 0.25       # 주변 탐색의 첫 반폭 (범위 대비). REFINE_ROUND 번마다 반으로 줄인다
REFINE_ROUND = 4
WIDEN_AFTER = 10          # 주변에서 새 점을 연달아 이만큼 못 찾으면 폭을 두 배로
MIN_GAP = 0.1             # 후보끼리 떨어져야 하는 거리 (범위 대비, 차원 중 가장 큰 차이)
MAX_MISSES = 20           # 새 점을 못 찾는 시도가 평가 수 × 이 값을 넘으면 범위를 다 본 것으로 본다


def _int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _number(v) -> bool:
    return isinstance(v, Number) and not isinstance(v, bool) and math.isfinite(v)


def search_errors(cfg) -> list[str]:
    if not isinstance(cfg, dict):
        return ["search는 {max_evals, max_seconds, top_k, max_dims, seed, guard_margin} 형태여야 한다"]
    errors = [f"search.{k}는 1 이상의 정수여야 한다" for k in ("max_evals", "top_k")
              if not (_int(cfg.get(k)) and cfg[k] >= 1)]
    if not (_number(cfg.get("max_seconds")) and cfg["max_seconds"] > 0):
        errors.append("search.max_seconds는 0보다 큰 숫자여야 한다")
    if not (_int(cfg.get("max_dims")) and 1 <= cfg["max_dims"] <= MAX_DIMS):
        errors.append(f"search.max_dims는 1~{MAX_DIMS}의 정수여야 한다")
    if not (_int(cfg.get("seed")) and cfg["seed"] >= 0):
        errors.append("search.seed는 0 이상의 정수여야 한다")
    if not (_number(cfg.get("guard_margin")) and 0 < cfg["guard_margin"] <= 1):
        errors.append("search.guard_margin은 0보다 크고 1 이하인 숫자여야 한다")
    return errors


def space_errors(params: dict, space, dimensions: dict, max_dims: int) -> list[str]:
    """space: [{path, low, high}]. 문제가 없으면 []."""
    if not isinstance(space, list) or not space:
        return ["탐색 범위가 비어 있다: [{path, low, high}] 목록이어야 한다"]
    if len(space) > max_dims:
        return [f"한 번에 찾는 값은 {max_dims}개까지다 (받은 수 {len(space)})"]
    errors, paths = [], []
    for i, dim in enumerate(space):
        if not isinstance(dim, dict) or not isinstance(dim.get("path"), str):
            errors.append(f"범위 {i + 1}: {{path, low, high}} 형태여야 한다")
            continue
        path, low, high = dim["path"], dim.get("low"), dim.get("high")
        if path in paths:
            errors.append(f"{path}: 같은 경로가 두 번 있다")
        paths.append(path)
        if not (_number(low) and _number(high)) or low >= high:
            errors.append(f"{path}: low < high인 숫자여야 한다")
            continue
        try:
            current = get_path(params, path)
        except (KeyError, IndexError, TypeError, ValueError):
            errors.append(f"{path}: 없는 파라미터 경로")
            continue
        if not _number(current):
            errors.append(f"{path}: 숫자 값 하나를 가리켜야 한다 (현재 값 {current!r})")
            continue
        for value in (low, high):
            errors += params_errors(params, {"kind": "params", "params_changes": [{"path": path, "value": value}]},
                                    dimensions)
    return errors


def _halton(index: int, base: int) -> float:
    f, r = 1.0, 0.0
    while index > 0:
        f /= base
        r += f * (index % base)
        index //= base
    return r


def _caster(params: dict, dim: dict):
    if _int(get_path(params, dim["path"])) and float(dim["low"]).is_integer() and float(dim["high"]).is_integer():
        return lambda v: int(round(v))
    return lambda v: round(float(v), 4)


def _points(params: dict, space: list[dict], seed: int, center=None, width: float = 1.0, start: int = 0):
    """무한 표본 생성기. center가 있으면 그 점 주변 (범위 × width 반폭) 안에서만."""
    casts = [_caster(params, d) for d in space]
    k = 1 + seed * 100 + start
    while True:
        point = {}
        for j, d in enumerate(space):
            u = _halton(k, PRIMES[j])
            lo, hi = d["low"], d["high"]
            if center is None:
                v = lo + u * (hi - lo)
            else:
                half = (hi - lo) * width
                v = min(hi, max(lo, center[d["path"]] + (2 * u - 1) * half))
            point[d["path"]] = min(hi, max(lo, casts[j](v)))   # 반올림이 범위 끝을 넘지 않게
        yield point
        k += 1


def _key(point: dict) -> tuple:
    return tuple(sorted(point.items()))


def sample_points(params: dict, space: list[dict], n: int, seed: int) -> list[dict]:
    """전체 범위의 서로 다른 점 n개 (챔피언 값과 같은 점은 뺀다)."""
    champion = _key({d["path"]: get_path(params, d["path"]) for d in space})
    seen, out = {champion}, []
    for i, p in enumerate(_points(params, space, seed)):
        if len(out) == n or i > n * MAX_MISSES:
            return out
        if _key(p) not in seen:
            seen.add(_key(p))
            out.append(p)


def _rank(e: dict) -> tuple:
    """좋은 순: 판정 통과, 통과한 검사 수, 목표 지표 Δ. 같으면 먼저 평가한 점."""
    target = e["target"] if e["target"] is not None else -math.inf
    return (not e["pass"], -sum(c["ok"] for c in e.get("checks", [])), -target, e["i"])


def pick_top(evals: list[dict], k: int, space: list[dict]) -> list[dict]:
    """학습용 판정을 통과한 점 중 목표 지표 Δ가 큰 순으로, 서로 범위의 MIN_GAP 이상 떨어진 k개."""
    spans = {d["path"]: d["high"] - d["low"] for d in space}

    def gap(a: dict, b: dict) -> float:
        return max(abs(a[p] - b[p]) / spans[p] for p in spans)

    out = []
    for e in sorted((e for e in evals if e["pass"]), key=_rank):
        if len(out) == k:
            break
        if all(gap(e["point"], o["point"]) >= MIN_GAP - 1e-9 for o in out):
            out.append(e)
    return out


def search_judgment(judgment: dict, margin: float) -> dict:
    """탐색용 판정 기준: 부작용 한도만 margin배로 좁힌다. 목표 지표와 위반 0건은 그대로."""
    guards = {m: {k: round(v * margin, 9) for k, v in limit.items()} for m, limit in (judgment.get("guards") or {}).items()}
    return {**judgment, "guards": guards}


def run_search(evaluate, params: dict, space: list[dict], judgment: dict, cfg: dict) -> dict:
    """evaluate(point) → 학습용 비교 요약 {metrics: {지표: {delta_mean}}, violations}. 예산을 넘기면 BudgetExceeded."""
    n, seed = cfg["max_evals"], cfg["seed"]
    judgment = search_judgment(judgment, cfg["guard_margin"])
    champion = _key({d["path"]: get_path(params, d["path"]) for d in space})
    seen, evals, stop = {champion}, [], "max_evals"

    def attempt(point) -> bool:
        if _key(point) in seen:
            return False
        seen.add(_key(point))
        summary = evaluate(point)
        verdict = judge_set(judgment, summary)
        target = next(c["value"] for c in verdict["checks"] if c["name"] == "target")
        evals.append({"i": len(evals) + 1, "point": point, "pass": verdict["pass"], "target": target,
                      "checks": verdict["checks"], "summary": summary})
        return True

    try:
        coarse, misses = max(1, math.ceil(n * COARSE_SHARE)), 0
        for p in _points(params, space, seed):
            if len(evals) >= coarse or misses > n * MAX_MISSES:
                break
            misses += not attempt(p)
        refined, misses, level, stalled = 0, 0, 0, 0
        while len(evals) < n and misses <= n * MAX_MISSES:
            best = min(evals, key=_rank)["point"] if evals else None
            if best is None:
                break
            width = min(1.0, REFINE_WIDTH * 0.5 ** level)
            p = next(_points(params, space, seed, center=best, width=width, start=refined + misses))
            if attempt(p):
                refined, stalled = refined + 1, 0
                level += refined % REFINE_ROUND == 0         # REFINE_ROUND개를 볼 때마다 폭을 반으로
            else:
                misses, stalled = misses + 1, stalled + 1
                if stalled >= WIDEN_AFTER and width < 1.0:    # 폭이 칸보다 좁아져 새 점이 없으면 다시 넓힌다
                    level, stalled = level - 1, 0
        if len(evals) < n:
            stop = "exhausted"
    except BudgetExceeded:
        stop = "max_seconds"
    return {"evals": evals, "top": pick_top(evals, cfg["top_k"], space), "stop": stop, "judgment": judgment}
