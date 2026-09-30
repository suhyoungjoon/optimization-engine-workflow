"""규칙 엔진 어댑터: 코어 dispatch 팩의 결정적 탐욕 규칙 엔진을 감싼다.

params는 이 레포의 파일(engines/rule/params.yaml)에서 읽는다. 코어 팩의 기본 params(site-packages)는 쓰지 않는다.
"""

from pathlib import Path

import yaml

from core import DomainPack
from domains.dispatch import get_pack

DEFAULT_PARAMS_PATH = Path(__file__).resolve().parent / "params.yaml"


class RuleEngine:
    name = "rule"

    def load_params(self, path: str | Path) -> dict:
        return yaml.safe_load(Path(path).read_text(encoding="utf-8"))

    def pack_factory(self, params: dict) -> DomainPack:
        return get_pack(params)   # params를 항상 넘긴다: None이면 코어가 site-packages의 params.yaml을 읽는다


__all__ = ["DEFAULT_PARAMS_PATH", "RuleEngine"]
