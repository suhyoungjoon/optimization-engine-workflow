"""모델 레지스트리: 엔진별 params 버전 스냅샷, 모델 카드, 챔피언 지정, 되돌리기.

models/<engine>/v<N>/params.yaml   버전 params. 한 번 만들면 바꾸지 않는다
models/<engine>/v<N>/card.json     모델 카드: 부모 버전, 만든 실행·개선안, 검증 요약, 판정, 승인 정보
models/<engine>/champion.json      현재 챔피언과 지정·되돌리기 이력

새 버전은 부모 버전의 params를 복사한 뒤 코어 write_params로 개선안을 적용해 만든다 (주석 보존).
"""

import json
import os
import shutil
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from ruamel.yaml import YAML

from core import to_jsonable, write_params


class RegistryError(Exception):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _write_json(path: Path, data) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(to_jsonable(data), ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    tmp.replace(path)


def _set_version(params_path: Path, version: int) -> None:
    """파일의 version을 레지스트리 버전 번호와 맞춘다 (되돌린 뒤 승인하면 부모+1과 다를 수 있다)."""
    yaml = YAML()
    doc = yaml.load(params_path.read_text(encoding="utf-8"))
    if doc.get("version") != version:
        doc["version"] = version
        with params_path.open("w", encoding="utf-8") as f:
            yaml.dump(doc, f)


class Registry:
    def __init__(self, models_dir: str | Path, engine: str):
        self.engine = engine
        self.root = Path(models_dir) / engine

    # --- 조회 ---

    def versions(self) -> list[int]:
        if not self.root.is_dir():
            return []
        return sorted(int(p.name[1:]) for p in self.root.glob("v*")
                      if p.name[1:].isdigit() and (p / "params.yaml").is_file())

    def params_path(self, version: int) -> Path:
        path = self.root / f"v{version}" / "params.yaml"
        if not path.is_file():
            raise RegistryError(f"{self.engine}@v{version} 버전이 없다 ({path})")
        return path

    def card(self, version: int) -> dict:
        self.params_path(version)
        return json.loads((self.root / f"v{version}" / "card.json").read_text(encoding="utf-8"))

    def _champion_doc(self) -> dict:
        path = self.root / "champion.json"
        if not path.is_file():
            raise RegistryError(f"레지스트리가 없다: {self.root} (champion.json 없음)")
        return json.loads(path.read_text(encoding="utf-8"))

    def champion(self) -> int:
        return int(self._champion_doc()["version"])

    def history(self) -> list[dict]:
        return self._champion_doc()["history"]

    # --- 변경 (lock 안에서 부른다) ---

    def init(self, params_src: str | Path, note: str = "") -> int:
        """첫 버전 v1을 만들고 챔피언으로 지정한다."""
        if self.versions():
            raise RegistryError(f"이미 버전이 있다: {self.root}")
        d = self.root / "v1"
        d.mkdir(parents=True)
        shutil.copyfile(params_src, d / "params.yaml")
        _set_version(d / "params.yaml", 1)
        _write_json(d / "card.json", {"version": 1, "engine": self.engine, "parent": None, "created_at": _now(),
                                      "source": str(params_src), "note": note})
        _write_json(self.root / "champion.json", {"version": 1, "history": [
            {"action": "init", "version": 1, "at": _now()}]})
        return 1

    def register(self, parent: int, proposal: dict, card: dict) -> int:
        """부모 버전에 개선안을 적용한 새 버전을 만든다. 챔피언 지정은 set_champion으로 따로 한다."""
        parent_path = self.params_path(parent)
        version = max(self.versions()) + 1
        d = self.root / f"v{version}"
        d.mkdir()
        try:
            path = d / "params.yaml"
            shutil.copyfile(parent_path, path)
            write_params(path, proposal)
            _set_version(path, version)
            _write_json(d / "card.json", {"version": version, "engine": self.engine, "parent": parent,
                                          "created_at": _now(), **card})
        except BaseException:
            shutil.rmtree(d, ignore_errors=True)   # 반쯤 만든 버전을 남기지 않는다
            raise
        return version

    def set_champion(self, version: int, action: str, **info) -> None:
        self.params_path(version)
        doc = self._champion_doc()
        doc["history"].append({"action": action, "version": version, "previous": doc["version"], "at": _now(),
                               **info})
        doc["version"] = version
        _write_json(self.root / "champion.json", doc)

    def rollback(self, reason: str, to: int | None = None) -> tuple[int, int]:
        """챔피언을 이전 버전으로 되돌린다. to가 없으면 현재 챔피언의 부모. (이전 챔피언, 새 챔피언)"""
        current = self.champion()
        target = to if to is not None else self.card(current)["parent"]
        if target is None:
            raise RegistryError(f"{self.engine}@v{current}는 부모 버전이 없어 되돌릴 수 없다")
        if target == current:
            raise RegistryError(f"{self.engine}@v{target}는 이미 챔피언이다")
        self.set_champion(target, "rollback", reason=reason)
        return current, target

    @contextmanager
    def lock(self):
        """챔피언 확인과 변경(승인·반려·되돌리기) 사이에 다른 변경이 끼어들지 못하게 한다."""
        path = self.root / ".lock"
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise RegistryError(f"다른 승인·반려·되돌리기가 진행 중이다 ({path}). "
                                "진행 중인 작업이 없으면 이 파일을 지운다") from None
        except FileNotFoundError:
            raise RegistryError(f"레지스트리가 없다: {self.root}") from None
        try:
            os.close(fd)
            yield
        finally:
            path.unlink(missing_ok=True)


__all__ = ["Registry", "RegistryError"]
