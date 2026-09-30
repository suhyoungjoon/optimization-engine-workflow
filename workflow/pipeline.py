"""워크플로우 정의(workflow/pipeline.yaml) 읽기와 검사.

정의는 화면(다이어그램·실행 화면)과 CLI가 단계 이름·주체·입출력을 보여 주는 기준이다.
실행 순서 자체는 runner가 정하고, 둘이 같은지는 테스트가 확인한다.
"""

import importlib
from functools import lru_cache
from pathlib import Path

import yaml

PIPELINE_PATH = Path(__file__).resolve().parent / "pipeline.yaml"
ACTORS = ("ai", "code", "human")
KINDS = ("code", "agent", "gate")


def load_pipeline(path: str | Path = PIPELINE_PATH) -> dict:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def default_pipeline() -> dict:
    return load_pipeline()


def stage_labels(defn: dict | None = None) -> dict[str, str]:
    return {s["key"]: s["label"] for s in (defn or default_pipeline())["stages"]}


def _resolves(dotted: str) -> bool:
    module, _, name = dotted.rpartition(".")
    try:
        return callable(getattr(importlib.import_module(module), name))
    except (ImportError, AttributeError, ValueError):
        return False


def _has_setting(settings: dict, dotted: str) -> bool:
    node = settings
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return False
        node = node[part]
    return True


def pipeline_errors(defn: dict, settings: dict | None = None) -> list[str]:
    """정의가 스스로 맞는지: 주체·종류, 코드 위치, 입출력 순서, 설정 키, 게이트 위치."""
    errors = []
    inputs = {i["key"] for i in defn.get("inputs", [])}
    artifacts = {a["key"] for a in defn.get("artifacts", [])}
    stages = defn.get("stages", [])
    keys = [s["key"] for s in stages]
    every = keys + [i["key"] for i in defn.get("inputs", [])] + [a["key"] for a in defn.get("artifacts", [])]
    if len(set(every)) != len(every):
        errors.append("단계·입력·산출물의 key는 서로 겹치면 안 된다")
    produced: dict[str, int] = {}
    for i, s in enumerate(stages):
        name = s.get("key", f"#{i}")
        if s.get("actor") not in ACTORS:
            errors.append(f"{name}: actor는 {ACTORS} 중 하나")
        if s.get("kind") not in KINDS:
            errors.append(f"{name}: kind는 {KINDS} 중 하나")
        if not _resolves(str(s.get("impl", ""))):
            errors.append(f"{name}: impl {s.get('impl')}를 찾을 수 없다")
        for r in s.get("reads", []):
            if r in artifacts and r not in produced:
                errors.append(f"{name}: {r}는 앞 단계가 만들지 않았다")
            elif r not in artifacts and r not in inputs:
                errors.append(f"{name}: 알 수 없는 입력 {r}")
        for w in s.get("writes", []):
            if w not in artifacts:
                errors.append(f"{name}: 알 수 없는 산출물 {w}")
            elif w in produced:
                errors.append(f"{name}: {w}를 두 단계가 만든다")
            produced[w] = i
        if settings is not None:
            errors += [f"{name}: 설정 {k}가 settings/workflow.yaml에 없다"
                       for k in s.get("settings", []) if not _has_setting(settings, k)]
    gates = [i for i, s in enumerate(stages) if s.get("kind") == "gate"]
    if gates != [len(stages) - 1] or stages[-1].get("actor") != "human":
        errors.append("사람 승인 게이트(kind: gate, actor: human)는 마지막 단계 하나여야 한다")
    errors += [f"산출물 {a}를 만드는 단계가 없다" for a in sorted(artifacts - set(produced))]
    return errors
