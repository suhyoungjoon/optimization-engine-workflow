"""레거시 내보내기(CSV) → 데이터셋 스냅샷 + 품질 리포트.

- 매핑 명세에 적힌 컬럼만 읽는다. 나머지(고객명, 연락처 등)는 이름만 리포트에 남긴다.
- 문제가 있는 행은 조용히 버리지 않는다: 파일·줄 번호·필드·값·문제·처리(행 제외/값 비움)를 리포트에 남긴다.
- 컬럼이 아예 없거나 매핑 명세가 틀리면 가져오기를 멈춘다.
"""

import csv
import datetime as dt
import hashlib
import json
import statistics
from pathlib import Path

from core import to_jsonable
from domains.dispatch.generator import load_region
from domains.dispatch.models import Branch, Instance, hhmm_to_min

from .dataset import NAME, content_hash, load_snapshot, snapshot_path, to_km
from .mapping import load_mapping, mapping_errors

class ImportRefused(Exception):
    """같은 이름의 데이터셋이 다른 내용으로 이미 있다 (이름은 한 번 정하면 내용을 바꾸지 않는다)."""


def load_branches(mapping: dict) -> tuple[dict[str, Branch], dict]:
    """관할 경계: 'core:' 접두면 코어 dispatch 팩의 파일, 아니면 매핑 명세 기준 경로. 코어 load_region으로 km 평면에 옮긴다."""
    region = mapping["region"]
    geojson = region["geojson"]
    if geojson.startswith("core:"):
        geojson = geojson[len("core:"):]
    else:
        geojson = str((Path(mapping["_path"]).parent / geojson).resolve())
    return load_region({"region": {"geojson": geojson},
                        "branches": {code: {"name": name} for code, name in region["branches"].items()}})


class _Reader:
    def __init__(self, mapping: dict, part: str, path: Path, problems: list):
        self.mapping, self.part, self.problems = mapping, part, problems
        self.columns = mapping[part]["columns"]
        self.codes = mapping[part].get("codes") or {}
        with path.open(encoding=mapping["encoding"], newline="") as f:
            reader = csv.DictReader(f)
            header = reader.fieldnames or []
            missing = [c for c in self.columns.values() if c not in header]
            if missing:
                raise ValueError(f"{path.name}에 컬럼 {', '.join(missing)}이 없다 (매핑 명세 {part}.columns 확인)")
            self.ignored = [c for c in header if c not in self.columns.values()]
            self.rows = [(i, row) for i, row in enumerate(reader, start=2)]   # 1행은 머리글

    def report(self, line: int, row: dict, field: str, problem: str, action: str = "행 제외") -> None:
        id_column = self.columns.get("id") or self.columns.get("order")
        self.problems.append({"file": self.part, "line": line, "id": row.get(id_column) or None,
                              "field": field, "value": row.get(self.columns.get(field, ""), None),
                              "problem": problem, "action": action})


class _Bad(Exception):
    def __init__(self, field: str, problem: str):
        super().__init__(problem)
        self.field, self.problem = field, problem


def _text(r: _Reader, row: dict, field: str) -> str:
    value = (row.get(r.columns[field]) or "").strip()
    if not value:
        raise _Bad(field, "값이 비어 있다")
    return value


def _code(r: _Reader, row: dict, field: str, value: str | None = None) -> str:
    value = _text(r, row, field) if value is None else value
    codes = r.codes[field]
    if value not in codes:
        raise _Bad(field, f"모르는 코드 {value!r} (매핑 명세 {r.part}.codes.{field}: {sorted(codes)})")
    return codes[value]


def _codes(r: _Reader, row: dict, field: str, sep: str) -> list[str]:
    raw = (row.get(r.columns[field]) or "").strip()
    return [_code(r, row, field, token.strip()) for token in raw.split(sep) if token.strip()]


def _time(r: _Reader, row: dict, field: str) -> int:
    value = _text(r, row, field)
    try:
        dt.datetime.strptime(value, r.mapping["time_format"])
        return hhmm_to_min(value)
    except ValueError:
        raise _Bad(field, f"시각 형식이 {r.mapping['time_format']}가 아니다") from None


def _number(r: _Reader, row: dict, field: str, cast=float):
    try:
        return cast(_text(r, row, field))
    except ValueError:
        raise _Bad(field, "숫자가 아니다") from None


def _coords(r: _Reader, row: dict, geo: dict, area: Instance, branch: str, limit: float) -> tuple[float, float]:
    x, y = to_km(geo, _number(r, row, "lat"), _number(r, row, "lon"))
    outside = area.distance_outside(branch, x, y)
    if outside > limit:
        raise _Bad("lat", f"좌표가 소속 관할({branch})에서 {outside:.1f}km 떨어져 있다 (한도 {limit}km, 지오코딩 오류 의심)")
    return x, y


def _orders(r: _Reader, geo: dict, area: Instance, limit: float) -> list[dict]:
    out, seen = [], set()
    for line, row in r.rows:
        try:
            oid = _text(r, row, "id")
            if oid in seen:
                raise _Bad("id", "중복 지시번호 (앞 행을 남긴다)")
            try:
                date = dt.datetime.strptime(_text(r, row, "date"), r.mapping["date_format"]).date().isoformat()
            except ValueError:
                raise _Bad("date", f"날짜 형식이 {r.mapping['date_format']}가 아니다") from None
            branch = _code(r, row, "branch")
            o = {"id": oid, "date": date, "branch": branch, "work_type": _code(r, row, "work_type"),
                 "media": _code(r, row, "media"), "difficulty": _code(r, row, "difficulty"),
                 "building_type": _code(r, row, "building_type"), "desired": _time(r, row, "desired")}
            o["x"], o["y"] = _coords(r, row, geo, area, branch, limit)
        except _Bad as bad:
            r.report(line, row, bad.field, bad.problem)
            continue
        seen.add(oid)
        out.append(o)
    return out


def _workers(r: _Reader, geo: dict, area: Instance, limit: float, sep: str) -> list[dict]:
    out, seen = [], set()
    for line, row in r.rows:
        try:
            wid = _text(r, row, "id")
            if wid in seen:
                raise _Bad("id", "중복 사번 (앞 행을 남긴다)")
            branch = _code(r, row, "branch")
            skills = _codes(r, row, "skills", sep)
            if not skills:
                raise _Bad("skills", "보유 기술이 없다")
            start, end = _time(r, row, "start"), _time(r, row, "end")
            if start >= end:
                raise _Bad("end", "근무 종료가 시작보다 이르다")
            w = {"id": wid, "branch": branch, "skills": skills, "certs": _codes(r, row, "certs", sep),
                 "cei": _number(r, row, "cei", int), "available": [start, end]}
            w["x"], w["y"] = _coords(r, row, geo, area, branch, limit)
        except _Bad as bad:
            r.report(line, row, bad.field, bad.problem)
            continue
        seen.add(wid)
        out.append(w)
    return out


def _optional_time(r: _Reader, line: int, row: dict, field: str) -> int | None:
    if field not in r.columns or not (row.get(r.columns[field]) or "").strip():
        return None
    try:
        return _time(r, row, field)
    except _Bad as bad:
        r.report(line, row, field, bad.problem, action="값 비움")
        return None


def _assignments(r: _Reader, orders: dict, workers: set) -> tuple[list[dict], list[dict]]:
    plan, actuals, seen = [], [], set()
    for line, row in r.rows:
        try:
            oid = _text(r, row, "order")
            if oid not in orders:
                raise _Bad("order", "없는 지시서 (원본에 없거나 가져오지 않은 행)")
            if oid in seen:
                raise _Bad("order", "같은 지시서의 배정이 두 번 있다 (앞 행을 남긴다)")
            wid = (row.get(r.columns["worker"]) or "").strip() or None
            if wid is not None and wid not in workers:
                raise _Bad("worker", "없는 작업자 (원본에 없거나 가져오지 않은 행). 이 지시서는 미배정으로 본다")
            start = _time(r, row, "start") if wid else None
        except _Bad as bad:
            r.report(line, row, bad.field, bad.problem)
            continue
        seen.add(oid)
        plan.append({"order": oid, "worker": wid, "start": start})
        cancelled = None
        if "cancelled" in r.columns and (row.get(r.columns["cancelled"]) or "").strip():
            try:
                cancelled = _code(r, row, "cancelled")
            except _Bad as bad:
                r.report(line, row, bad.field, bad.problem, action="값 비움")
        actuals.append({"order": oid, "arrival": _optional_time(r, line, row, "arrival"),
                        "end": _optional_time(r, line, row, "end"), "cancelled": cancelled})
    return plan, actuals


def _actuals_summary(actuals: list[dict]) -> dict:
    timed = [a for a in actuals if a["arrival"] is not None and a["end"] is not None]
    flagged = [a for a in actuals if a["cancelled"] is not None]
    durations = [a["end"] - a["arrival"] for a in timed]
    return {"rows": len(actuals), "with_times": len(timed),
            "cancelled": sum(bool(a["cancelled"]) for a in flagged),
            "cancel_rate": round(sum(bool(a["cancelled"]) for a in flagged) / len(flagged), 4) if flagged else None,
            "mean_duration_min": round(statistics.mean(durations), 1) if durations else None,
            "note": "실적은 보관만 한다. 판정·비교에는 쓰지 않는다 (예측 + 최적화 단계에서 쓴다)"}


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def import_legacy(input_dir: str | Path, mapping_path: str | Path, name: str) -> dict:
    """input_dir의 CSV를 매핑 명세로 읽어 데이터셋 스냅샷을 만든다. 같은 이름이 다른 내용으로 있으면 거부한다."""
    if not NAME.match(name or ""):
        raise ValueError(f"데이터셋 이름 {name!r}: 영문 소문자·숫자·_만 쓴다")
    mapping = load_mapping(mapping_path)
    errors = mapping_errors(mapping)
    if errors:
        raise ValueError("매핑 명세 오류: " + "; ".join(errors))
    input_dir = Path(input_dir)
    branches, geo = load_branches(mapping)
    area = Instance(branches=branches, boundary_zone_km=mapping.get("boundary_zone_km", 1), workers=[], orders=[],
                    days=1, geo=geo)
    limit, sep = mapping.get("max_outside_km", 5), mapping["list_separator"]
    problems: list[dict] = []
    files = {part: input_dir / mapping["files"][part] for part in ("orders", "workers", "assignments")}
    readers = {part: _Reader(mapping, part, path, problems) for part, path in files.items()}
    orders = _orders(readers["orders"], geo, area, limit)
    if not orders:
        raise ValueError("가져올 수 있는 지시서가 없다 (품질 문제: " + "; ".join(p["problem"] for p in problems[:5]) + ")")
    workers = _workers(readers["workers"], geo, area, limit, sep)
    plan, actuals = _assignments(readers["assignments"], {o["id"]: o for o in orders}, {w["id"] for w in workers})
    kept = {"orders": len(orders), "workers": len(workers), "assignments": len(plan)}
    read = {part: len(r.rows) for part, r in readers.items()}
    dates = sorted(o["date"] for o in orders)
    snap = to_jsonable({
        "name": name,
        "branches": {k: {"name": b.name, "polygon": b.polygon} for k, b in branches.items()},
        "geo": geo, "boundary_zone_km": area.boundary_zone_km,
        "workers": workers, "orders": orders, "assignments": plan, "actuals": actuals,
    })
    snap["hash"] = content_hash(snap)
    snap.update({
        "period": [dates[0], dates[-1]],
        "imported_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "source": {"input": str(input_dir.resolve()), "files": {p: _sha(f) for p, f in files.items()},
                   "mapping": {"path": mapping["_path"], "sha256": _sha(Path(mapping["_path"]))}},
        "report": {"read": read, "excluded": {p: read[p] - kept[p] for p in read}, "problems": problems,
                   "ignored_columns": {p: r.ignored for p, r in readers.items()},
                   "actuals": _actuals_summary(actuals)},
    })
    path = snapshot_path(name)
    if path.is_file():
        existing = load_snapshot(name)
        if existing["hash"] != snap["hash"]:
            raise ImportRefused(f"데이터셋 {name}가 다른 내용(hash {existing['hash']})으로 이미 있다. "
                                "이전 실행의 재현을 위해 덮어쓰지 않는다: 다른 이름으로 가져온다")
        return existing
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(snap, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
    return snap
