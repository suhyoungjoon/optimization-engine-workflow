"""시나리오 세트: {seed, faults} 케이스 목록 (scenarios/*.yaml). 같은 seed·결함이면 항상 같은 인스턴스다."""

from pathlib import Path

import yaml


def load_set(path: str | Path) -> dict:
    path = Path(path)
    return parse_set(yaml.safe_load(path.read_text(encoding="utf-8")) or {}, path)


def parse_set(data: dict, path: str | Path) -> dict:
    """세트 데이터를 검사해 {name, path, cases}로 만든다. path는 오류 메시지와 기록용."""
    path = Path(path)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: 세트는 {{name, cases}} 형태여야 한다")
    cases = data.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError(f"{path}: cases가 비어 있다")
    out = []
    for i, case in enumerate(cases):
        if not isinstance(case, dict):
            raise ValueError(f"{path}: cases[{i}]는 {{seed, faults}} 형태여야 한다")
        seed, faults = case.get("seed"), case.get("faults", [])
        if not isinstance(seed, int) or isinstance(seed, bool):
            raise ValueError(f"{path}: cases[{i}].seed는 정수여야 한다")
        if not isinstance(faults, list) or not all(isinstance(f, str) for f in faults):
            raise ValueError(f"{path}: cases[{i}].faults는 문자열 목록이어야 한다")
        out.append({"seed": seed, "faults": faults})
    seeds = [c["seed"] for c in out]
    if len(set(seeds)) != len(seeds):
        raise ValueError(f"{path}: 세트 안에 같은 seed가 두 번 있다")
    return {"name": data.get("name") or path.stem, "path": str(path), "cases": out}


def check_disjoint(train: dict, holdout: dict) -> None:
    """검증용이 학습용과 seed를 공유하면 판정이 학습용을 다시 보는 셈이 된다."""
    shared = sorted({c["seed"] for c in train["cases"]} & {c["seed"] for c in holdout["cases"]})
    if shared:
        raise ValueError(f"학습용과 검증용 세트가 seed를 공유한다: {shared}")
