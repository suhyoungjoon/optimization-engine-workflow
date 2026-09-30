"""엔진 어댑터. 이름으로 엔진을 고른다 (워크플로우 CLI가 쓰는 유일한 진입점)."""

from engines.base import Engine

ENGINES = ("rule",)


def get_engine(name: str) -> Engine:
    if name == "rule":
        from engines.rule import RuleEngine
        return RuleEngine()
    raise ValueError(f"알 수 없는 엔진: {name} (가능: {', '.join(ENGINES)})")


__all__ = ["ENGINES", "Engine", "get_engine"]
