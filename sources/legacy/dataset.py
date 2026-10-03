"""데이터셋 스냅샷: 가져오기(importer)가 만든 정규화 JSON. 시나리오 세트의 기간 케이스가 이것으로 인스턴스를 만든다.

- 위치: $OEW_DATA_DIR/legacy/<이름>.json (기본 data/, git 제외). 이름은 한 번 정하면 내용을 바꾸지 않는다.
- hash: 내용(경계·작업자·지시서·배정·실적)의 해시. 실행 기록에 남겨 같은 데이터로 재현했는지 확인한다.
- 기간 인스턴스: 기간 안의 지시서만, 날짜를 1일차부터 다시 센다. 작업자는 데이터셋 전체 (가능시간은 하루 하나: 코어 제약).
"""

import datetime as dt
import hashlib
import json
import math
import os
import re
from pathlib import Path

from core import DecisionRecord
from domains.dispatch import rule_engine as R
from domains.dispatch.models import Branch, Instance, Order, Worker, min_to_hhmm

ROOT = Path(__file__).resolve().parents[2]
NAME = re.compile(r"^[a-z0-9_]+$")
CONTENT = ("branches", "geo", "boundary_zone_km", "workers", "orders", "assignments", "actuals")
_CACHE: dict = {}


def data_dir() -> Path:
    """OEW_DATA_DIR이 있으면 그곳, 없으면 레포의 data/."""
    return Path(os.environ.get("OEW_DATA_DIR") or ROOT / "data")


def snapshot_path(name: str) -> Path:
    return data_dir() / "legacy" / f"{name}.json"


def content_hash(snap: dict) -> str:
    body = json.dumps({k: snap[k] for k in CONTENT}, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


def load_snapshot(name: str) -> dict:
    if not isinstance(name, str) or not NAME.match(name):
        raise ValueError(f"데이터셋 이름 {name!r}: 영문 소문자·숫자·_만 쓴다")
    path = snapshot_path(name)
    if not path.is_file():
        raise ValueError(f"데이터셋 {name}가 없다 ({path}). python -m workflow import-legacy로 먼저 가져온다")
    key = (str(path), path.stat().st_mtime_ns)
    if key not in _CACHE:
        _CACHE.clear()   # 한 번에 하나만 기억한다 (스냅샷은 수 MB)
        _CACHE[key] = json.loads(path.read_text(encoding="utf-8"))
    return _CACHE[key]


def list_datasets() -> list[dict]:
    d = data_dir() / "legacy"
    out = []
    for path in sorted(d.glob("*.json")) if d.is_dir() else []:
        snap = json.loads(path.read_text(encoding="utf-8"))
        r = snap["report"]
        out.append({"name": snap["name"], "hash": snap["hash"], "period": snap["period"],
                    "imported_at": snap["imported_at"], "orders": len(snap["orders"]),
                    "workers": len(snap["workers"]), "assignments": len(snap["assignments"]),
                    "read": r["read"], "excluded": r["excluded"], "problems": len(r["problems"]),
                    "actuals": r["actuals"], "source": snap["source"]})
    return out


def to_km(geo: dict, lat: float, lon: float) -> tuple[float, float]:
    """위경도 → 코어의 km 평면 (남서쪽 모서리 원점, 등장방형 근사: 코어 load_region과 같은 식)."""
    (lon0, lat0), (kx, ky) = geo["origin"], _scale(geo)
    return round((lon - lon0) * kx, 3), round((lat - lat0) * ky, 3)


def to_latlon(geo: dict, x: float, y: float) -> tuple[float, float]:
    (lon0, lat0), (kx, ky) = geo["origin"], _scale(geo)
    return round(lat0 + y / ky, 8), round(lon0 + x / kx, 8)


def _scale(geo: dict) -> tuple[float, float]:
    lat0 = geo["origin"][1]
    return 111.32 * math.cos(math.radians(lat0)), 110.57


def _date(text: str) -> dt.date:
    return dt.date.fromisoformat(str(text))


def dataset_instance(snap: dict, start: str, end: str) -> Instance:
    """기간 [start, end]의 인스턴스. 날짜는 start를 1일차로 다시 센다."""
    first, last = _date(start), _date(end)
    lo, hi = (_date(p) for p in snap["period"])
    if first > last or first < lo or last > hi:
        raise ValueError(f"기간 {start}~{end}가 데이터셋 {snap['name']}의 기간 {lo}~{hi} 밖이다")
    orders = []
    for o in snap["orders"]:
        day = _date(o["date"])
        if first <= day <= last:
            orders.append(Order(id=o["id"], day=(day - first).days + 1, branch=o["branch"], work_type=o["work_type"],
                                media=o["media"], difficulty=o["difficulty"], building_type=o["building_type"],
                                desired=o["desired"], x=o["x"], y=o["y"]))
    if not orders:
        raise ValueError(f"기간 {start}~{end}에 데이터셋 {snap['name']}의 지시서가 없다")
    workers = [Worker(id=w["id"], branch=w["branch"], skills=list(w["skills"]), certs=list(w["certs"]), cei=w["cei"],
                      x=w["x"], y=w["y"], available=tuple(w["available"])) for w in snap["workers"]]
    branches = {k: Branch(name=b["name"], polygon=[tuple(p) for p in b["polygon"]]) for k, b in snap["branches"].items()}
    return Instance(branches=branches, boundary_zone_km=snap["boundary_zone_km"], workers=workers, orders=orders,
                    days=(last - first).days + 1, geo=snap["geo"])


def legacy_decisions(snap: dict, inst: Instance, params: dict) -> list[DecisionRecord]:
    """과거 실제 배정을 결정 레코드로: 엔진 결과와 같은 validate()·metrics()로 잰다 (레거시 기준선).

    매칭 단계는 params의 범위로 다시 계산한다 (범위 밖이면 None). 미배정은 사유를 모르므로 reason_code 없이 남긴다.
    """
    plan = {a["order"]: a for a in snap["assignments"]}
    out = []
    for o in inst.orders:
        a = plan.get(o.id)
        dims = R.dims_of(inst, o)
        worker = inst.worker_index.get(a["worker"]) if a and a["worker"] else None
        if worker is None:
            out.append(DecisionRecord(o.id, None, "failed", None, "레거시: 미배정", dims))
            continue
        stage = R.actual_stage(inst, o, worker, a["start"], params)
        out.append(DecisionRecord(
            o.id, {"worker_id": worker.id, "start_time": min_to_hhmm(a["start"]), "matching_stage": stage},
            "success", None, f"레거시 실제 배정: {worker.id} {min_to_hhmm(a['start'])}", dims,
            {"matching_stage": float(stage or 0), "time_diff_min": float(abs(a["start"] - o.desired))}))
    return out
