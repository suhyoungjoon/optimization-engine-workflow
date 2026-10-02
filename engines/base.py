"""엔진 계약. 워크플로우는 이 프로토콜만 알고 특정 엔진(규칙·solver·학습형)은 모른다.

엔진은 코어 DomainPack 계약(docs/handoff.md 5장)을 따르는 팩을 만든다. 필수조건 판정은 항상 팩의 validate()로 한다.
"""

from pathlib import Path
from typing import Protocol

from core import DomainPack


class Engine(Protocol):
    name: str

    def load_params(self, path: str | Path) -> dict: ...

    def pack_factory(self, params: dict) -> DomainPack:
        """params로 팩을 새로 만든다. validate·metrics가 팩 생성 시 params를 쓰므로 후보 평가는 매번 새 팩으로 한다."""
        ...
