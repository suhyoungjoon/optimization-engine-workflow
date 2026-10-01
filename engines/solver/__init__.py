"""solver 엔진 어댑터: OR-Tools CP-SAT로 배정한다 (model.py).

코어 dispatch 팩을 그대로 쓰고 solve만 바꾼다. validate·metrics·분석 도구는 코어 것이다.
params는 모델 레지스트리(models/solver/)에서 읽는다. 규칙 엔진 params의 섹션(검증·지표에 쓰임)에 objective를 더한 것이다.
"""

import json
import os
import threading
from collections import OrderedDict
from pathlib import Path

import yaml

from core import DomainPack
from domains.dispatch import get_pack

from .model import solve_instance

_CACHE: OrderedDict = OrderedDict()   # (인스턴스, params) → 결정. 검증이 같은 챔피언을 여러 번 풀지 않도록
_CACHE_SIZE = 64
_LOCK = threading.Lock()


def _threads() -> int:
    return int(os.environ.get("SOLVER_THREADS") or os.cpu_count() or 1)


class SolverPack:
    """코어 dispatch 팩을 감싸 solve만 solver로 바꾼다. 나머지 속성은 코어 팩에 넘긴다."""

    def __init__(self, params: dict):
        self._pack = get_pack(params)

    def __getattr__(self, name):
        return getattr(self._pack, name)

    def solve(self, instance, params: dict):
        key = (id(instance), json.dumps(params, sort_keys=True, default=str))
        with _LOCK:
            hit = _CACHE.get(key)
            if hit is not None and hit[0] is instance:
                _CACHE.move_to_end(key)
                return list(hit[1])
        records = solve_instance(instance, params, _threads())
        with _LOCK:
            _CACHE[key] = (instance, records)   # 인스턴스를 붙잡아 id가 재사용되지 않게 한다
            while len(_CACHE) > _CACHE_SIZE:
                _CACHE.popitem(last=False)
        return list(records)


class SolverEngine:
    name = "solver"

    def load_params(self, path: str | Path) -> dict:
        return yaml.safe_load(Path(path).read_text(encoding="utf-8"))

    def pack_factory(self, params: dict) -> DomainPack:
        return SolverPack(params)


def clear_cache() -> None:
    with _LOCK:
        _CACHE.clear()


__all__ = ["SolverEngine", "SolverPack", "clear_cache"]
