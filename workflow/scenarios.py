"""시나리오 세트: 케이스 목록 (scenarios/*.yaml). 같은 케이스면 항상 같은 인스턴스다.

케이스는 두 가지다.
- 가상: {seed, faults}. 코어 생성기가 만든다.
- 기간: {dataset, from, to}. 레거시 데이터셋 스냅샷의 과거 기간 (sources/legacy, M6). 실행할 때 스냅샷 hash를 고정해
  (pin_cases) 실행 기록에 남기고, 인스턴스를 만들 때 같은 데이터인지 확인한다.
items: 처리 순서 앞쪽 N개 항목만 남긴다 (도메인 팩의 items·subset 계약). 시연처럼 빨리 보여야 할 때 쓴다.
"""

import datetime as dt
import re
from pathlib import Path

import yaml

from sources.legacy import dataset_instance, load_snapshot

DATASET = re.compile(r"^[a-z0-9_]+$")


def _iso(value, where: str) -> str:
    if isinstance(value, dt.date):
        return value.isoformat()
    try:
        return dt.date.fromisoformat(str(value)).isoformat()
    except ValueError:
        raise ValueError(f"{where}는 날짜(YYYY-MM-DD)여야 한다") from None


def _period_case(path: Path, i: int, case: dict) -> dict:
    name = case.get("dataset")
    if not isinstance(name, str) or not DATASET.match(name):
        raise ValueError(f"{path}: cases[{i}].dataset은 데이터셋 이름(영문 소문자·숫자·_)이어야 한다")
    for key in ("from", "to"):
        if case.get(key) is None:
            raise ValueError(f"{path}: cases[{i}].{key}가 없다")
    start, end = _iso(case["from"], f"{path}: cases[{i}].from"), _iso(case["to"], f"{path}: cases[{i}].to")
    if start > end:
        raise ValueError(f"{path}: cases[{i}].from이 to보다 늦다")
    return {"dataset": name, "from": start, "to": end}


def case_label(case: dict) -> str:
    if "dataset" in case:
        return f"{case['dataset']} {case['from']}~{case['to'][5:]}"
    return f"seed {case['seed']}"


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
            raise ValueError(f"{path}: cases[{i}]는 {{seed, faults}} 또는 {{dataset, from, to}} 형태여야 한다")
        if "dataset" in case:
            item = _period_case(path, i, case)
        else:
            seed, faults = case.get("seed"), case.get("faults", [])
            if not isinstance(seed, int) or isinstance(seed, bool):
                raise ValueError(f"{path}: cases[{i}].seed는 정수여야 한다")
            if not isinstance(faults, list) or not all(isinstance(f, str) for f in faults):
                raise ValueError(f"{path}: cases[{i}].faults는 문자열 목록이어야 한다")
            item = {"seed": seed, "faults": faults}
        items = case.get("items")
        if items is not None and (not isinstance(items, int) or isinstance(items, bool) or items <= 0):
            raise ValueError(f"{path}: cases[{i}].items는 1 이상의 정수여야 한다")
        out.append({**item, **({"items": items} if items else {})})
    seeds = [c["seed"] for c in out if "seed" in c]
    if len(set(seeds)) != len(seeds):
        raise ValueError(f"{path}: 세트 안에 같은 seed가 두 번 있다")
    periods = sorted((c["dataset"], c["from"], c["to"]) for c in out if "dataset" in c)
    for (d1, _f1, t1), (d2, f2, _t2) in zip(periods, periods[1:]):
        if d1 == d2 and f2 <= t1:
            raise ValueError(f"{path}: 세트 안에서 {d1}의 기간이 겹친다 ({f2} ≤ {t1})")
    return {"name": data.get("name") or path.stem, "path": str(path), "cases": out}


def check_disjoint(train: dict, holdout: dict) -> None:
    """검증용이 학습용과 seed나 기간을 공유하면 판정이 학습용을 다시 보는 셈이 된다.

    같은 데이터셋의 기간 케이스는 학습용이 모두 검증용보다 앞서야 한다 (뒤 기간으로 앞 기간을 판정하면 미래 정보가 섞인다).
    """
    shared = sorted({c["seed"] for c in train["cases"] if "seed" in c}
                    & {c["seed"] for c in holdout["cases"] if "seed" in c})
    if shared:
        raise ValueError(f"학습용과 검증용 세트가 seed를 공유한다: {shared}")
    for t in (c for c in train["cases"] if "dataset" in c):
        for h in (c for c in holdout["cases"] if c.get("dataset") == t["dataset"]):
            if h["from"] <= t["to"] and t["from"] <= h["to"]:
                raise ValueError(f"학습용 {case_label(t)}와 검증용 {case_label(h)}의 기간이 겹친다")
            if t["from"] > h["to"]:
                raise ValueError(f"학습용 기간이 검증용 기간보다 앞서야 한다: {case_label(t)} > {case_label(h)}")


def pin_cases(sset: dict) -> dict:
    """기간 케이스에 지금 데이터셋 스냅샷의 hash를 붙인다 (실행 기록에 남겨 재현을 확인)."""
    cases = [{**c, "snapshot": load_snapshot(c["dataset"])["hash"]} if "dataset" in c else c for c in sset["cases"]]
    return {**sset, "cases": cases}


def make_instance(pack, case: dict):
    """케이스의 인스턴스. items가 있으면 처리 순서 앞쪽 N개만 남긴다 (정답표는 쓰지 않는다)."""
    if "dataset" in case:
        snap = load_snapshot(case["dataset"])
        if case.get("snapshot") and case["snapshot"] != snap["hash"]:
            raise ValueError(f"데이터셋 {case['dataset']}가 실행 기록의 hash {case['snapshot']}와 다르다 "
                             f"(지금 {snap['hash']}). 같은 데이터로 재현할 수 없다")
        instance = dataset_instance(snap, case["from"], case["to"])
    else:
        instance, _truth = pack.generate(case["seed"], case["faults"])
    if case.get("items"):
        instance = pack.subset(instance, pack.items(instance)[: case["items"]])
    return instance
