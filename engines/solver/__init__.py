"""solver 엔진 어댑터: OR-Tools CP-SAT로 배정한다 (model.py).

코어 dispatch 팩을 그대로 쓰고 solve만 바꾼다. validate·metrics·분석 도구는 코어 것이다.
params는 모델 레지스트리(models/solver/)에서 읽는다. 규칙 엔진 params의 섹션(검증·지표에 쓰임)에 objective를 더한 것이다.
"""

import hashlib
import json
import os
import pickle
import threading
from collections import OrderedDict
from pathlib import Path

import ortools
import yaml

from core import DecisionRecord, DomainPack, to_jsonable
from domains.dispatch import get_pack

from . import model
from .model import solve_instance

ROOT = Path(__file__).resolve().parents[2]
_CACHE: OrderedDict = OrderedDict()   # (인스턴스, params) → 결정. 검증이 같은 챔피언을 여러 번 풀지 않도록
_CACHE_SIZE = 64
_LOCK = threading.Lock()


def _threads() -> int:
    return int(os.environ.get("SOLVER_THREADS") or os.cpu_count() or 1)


# --- 디스크 캐시: solver는 결정적이라 같은 인스턴스·params·모델 코드면 결과가 같다. 실행을 넘어 다시 쓴다 ---

_MODEL_CODE = hashlib.sha256(Path(model.__file__).read_bytes()).hexdigest()[:16]


def _disk_dir() -> Path | None:
    """SOLVER_CACHE_DIR: 비우면 끈다. 없으면 runs/solver_cache (git 제외)."""
    value = os.environ.get("SOLVER_CACHE_DIR")
    if value == "":
        return None
    return Path(value) if value else ROOT / "runs" / "solver_cache"


def _disk_key(instance, params: dict) -> str:
    content = pickle.dumps((instance.branches, instance.workers, instance.orders, instance.boundary_zone_km, instance.days))
    h = hashlib.sha256(content)
    h.update(json.dumps(params, sort_keys=True, default=str).encode())
    h.update(f"{_MODEL_CODE}|{ortools.__version__}".encode())   # 모델 코드나 OR-Tools가 바뀌면 다시 푼다
    return h.hexdigest()


def _disk_get(instance, params: dict):
    d = _disk_dir()
    path = d / f"{_disk_key(instance, params)}.json" if d else None
    if path is None or not path.is_file():
        return None
    try:
        return [DecisionRecord(**r) for r in json.loads(path.read_text(encoding="utf-8"))]
    except (ValueError, TypeError):
        return None   # 깨진 파일은 무시하고 다시 푼다


def _disk_put(instance, params: dict, records) -> None:
    d = _disk_dir()
    if d is None:
        return
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{_disk_key(instance, params)}.json"
    tmp = path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(to_jsonable(records), ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


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
        records = _disk_get(instance, params)
        if records is None:
            records = solve_instance(instance, params, _threads())
            _disk_put(instance, params, records)
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
