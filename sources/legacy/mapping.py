"""필드 매핑 명세(mapping.yaml) 읽기와 검사. 코드는 코어 dimensions.yaml에 있는 값으로만 옮길 수 있다."""

from pathlib import Path

import yaml

from domains.dispatch import rule_engine as R
from domains.dispatch.generator import load_yaml

REQUIRED = {
    "orders": ("id", "date", "branch", "work_type", "media", "difficulty", "building_type", "desired", "lat", "lon"),
    "workers": ("id", "branch", "skills", "certs", "cei", "lat", "lon", "start", "end"),
    "assignments": ("order", "worker", "start"),
}
OPTIONAL = {"assignments": ("arrival", "end", "cancelled")}
CODED = {   # (파일, 필드) → 코어 차원 이름 (자격은 차원이 아니라 규칙 엔진의 자격 목록)
    ("orders", "branch"): "branch", ("orders", "work_type"): "work_type", ("orders", "media"): "media",
    ("orders", "difficulty"): "difficulty", ("orders", "building_type"): "building_type",
    ("workers", "branch"): "branch", ("workers", "skills"): "media", ("workers", "certs"): None,
}


def load_mapping(path: str | Path) -> dict:
    path = Path(path)
    mapping = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    mapping["_path"] = str(path.resolve())
    return mapping


def core_values() -> dict[str, set]:
    dims = load_yaml("dimensions.yaml")["dimensions"]
    return {k: set(v.get("values") or []) for k, v in dims.items()}


def mapping_errors(mapping: dict) -> list[str]:
    errors = []
    for key in ("encoding", "date_format", "time_format", "list_separator", "region", "files"):
        if key not in mapping:
            errors.append(f"{key}가 없다")
    for part, fields in REQUIRED.items():
        if part not in (mapping.get("files") or {}):
            errors.append(f"files.{part}가 없다")
        columns = (mapping.get(part) or {}).get("columns") or {}
        errors += [f"{part}.columns.{f}가 없다" for f in fields if not columns.get(f)]
        errors += [f"{part}.columns.{f}는 알 수 없는 필드다" for f in columns
                   if f not in fields + OPTIONAL.get(part, ())]
    values = core_values()
    certs = set(R.REQUIRED_CERT.values())
    for (part, field), dim in CODED.items():
        codes = ((mapping.get(part) or {}).get("codes") or {}).get(field)
        if not isinstance(codes, dict) or not codes:
            errors.append(f"{part}.codes.{field}가 없다")
            continue
        allowed = certs if dim is None else values[dim]
        for legacy, core in codes.items():
            if core not in allowed:
                errors.append(f"{part}.codes.{field}: {legacy} → {core}는 코어 dimensions.yaml에 없는 값이다 "
                              f"(가능: {sorted(allowed)}). 더 많은 값이 필요하면 코어 수정이 필요하다")
    for code in ((mapping.get("region") or {}).get("branches") or {}):
        if code not in values["branch"]:
            errors.append(f"region.branches: 지점 {code}는 코어 dimensions.yaml에 없다 (코어 수정 필요)")
    cancelled = ((mapping.get("assignments") or {}).get("codes") or {}).get("cancelled") or {}
    errors += [f"assignments.codes.cancelled.{k}는 true/false여야 한다" for k, v in cancelled.items()
               if not isinstance(v, bool)]
    return errors
