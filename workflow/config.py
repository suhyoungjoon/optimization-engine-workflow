"""기준정보 편집: 판정 기준·실행 상한(settings/workflow.yaml)과 시나리오 세트(scenarios/*.yaml).

저장 전에 검사하고, 통과하면 주석을 보존하며(ruamel) 파일에 쓴다. 이미 끝난 실행은 자기가 쓴 값을 스냅샷으로 갖고 있다.
"""

import copy
from numbers import Number
from pathlib import Path

import yaml
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq

from .judge import check_config
from .scenarios import check_disjoint, parse_set

EDITABLE = ("llm", "validation", "judgment")


def _positive_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v > 0


def settings_errors(settings: dict) -> list[str]:
    errors = []
    llm = settings.get("llm") or {}
    for key in ("analyze_max_calls", "propose_max_calls"):
        if not _positive_int(llm.get(key)):
            errors.append(f"llm.{key}는 1 이상의 정수여야 한다")
    seconds = (settings.get("validation") or {}).get("max_seconds")
    if not (isinstance(seconds, Number) and not isinstance(seconds, bool) and seconds > 0):
        errors.append("validation.max_seconds는 0보다 큰 숫자여야 한다")
    return errors + check_config(settings.get("judgment") or {})


def load_settings(path: str | Path) -> dict:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def _merge(node, value):
    """ruamel 노드에 값을 덮어쓴다. 같은 키는 제자리에서 바꿔 주석을 남기고, 없어진 키는 지운다."""
    if isinstance(node, CommentedMap) and isinstance(value, dict):
        for key in [k for k in node if k not in value]:
            del node[key]
        for key, v in value.items():
            node[key] = _merge(node[key], v) if key in node else v
        return node
    return value


def save_settings(path: str | Path, updates: dict) -> dict:
    """updates: {"llm": {...}, "validation": {...}, "judgment": {...}} 중 바꿀 섹션 전체."""
    unknown = set(updates) - set(EDITABLE)
    if unknown:
        raise ValueError(f"편집할 수 없는 섹션: {sorted(unknown)}")
    merged = {**load_settings(path), **copy.deepcopy(updates)}
    errors = settings_errors(merged)
    if errors:
        raise ValueError("; ".join(errors))
    ry = YAML()
    path = Path(path)
    doc = ry.load(path.read_text(encoding="utf-8"))
    for section, value in updates.items():
        doc[section] = _merge(doc[section], value) if section in doc else value
    with path.open("w", encoding="utf-8") as f:
        ry.dump(doc, f)
    return load_settings(path)


def save_set(path: str | Path, cases: list[dict], other_path: str | Path, known_faults: set[str]) -> dict:
    """시나리오 세트의 케이스 목록을 바꾼다. 형식, 알려진 결함, 다른 세트와의 seed 중복을 검사한다."""
    path = Path(path)
    ry = YAML()
    doc = ry.load(path.read_text(encoding="utf-8"))
    candidate = parse_set({"name": doc.get("name"), "cases": cases}, path)
    unknown = sorted({f for c in candidate["cases"] for f in c["faults"]} - known_faults)
    if unknown:
        raise ValueError(f"알 수 없는 결함: {unknown} (가능: {sorted(known_faults)})")
    other = parse_set(yaml.safe_load(Path(other_path).read_text(encoding="utf-8")) or {}, other_path)
    check_disjoint(candidate, other)
    seq = CommentedSeq()
    for c in candidate["cases"]:
        item = CommentedMap(seed=c["seed"], faults=CommentedSeq(c["faults"]))
        item.fa.set_flow_style()
        item["faults"].fa.set_flow_style()
        seq.append(item)
    doc["cases"] = seq
    with path.open("w", encoding="utf-8") as f:
        ry.dump(doc, f)
    return candidate
