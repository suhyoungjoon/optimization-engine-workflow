"""실행 ID별 저장: runs/<run_id>/run.json(메타·상태 이력)과 단계별 JSON.

재현에 필요한 것(엔진, 모델 버전, params 스냅샷, 시나리오, LLM 모델명, 코어 버전)을 run.json과 1_run.json에 남긴다.
"""

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core import to_jsonable

from .state import RUNNING, check_transition

STAGE_FILES = {"run": "1_run.json", "analysis": "2_analysis.json", "proposals": "3_proposals.json",
               "validation": "4_validation.json", "apply": "5_apply.json", "decisions": "decisions.json"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _write(path: Path, data: Any) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(to_jsonable(data), ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


class RunStore:
    def __init__(self, runs_dir: str | Path):
        self.root = Path(runs_dir)

    def _dir(self, run_id: str) -> Path:
        root = self.root.resolve()
        d = (root / run_id).resolve()
        if d.parent != root or not (d / "run.json").is_file():   # ../ 나 절대 경로로 runs 밖을 가리키지 못하게
            raise KeyError(f"실행을 찾을 수 없음: {run_id}")
        return d

    def create(self, meta: dict) -> str:
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:6]
        d = self.root / run_id
        d.mkdir(parents=True)
        at = now()
        _write(d / "run.json", {"run_id": run_id, "status": RUNNING, "created_at": at, **meta,
                                "history": [{"status": RUNNING, "at": at}]})
        return run_id

    def load(self, run_id: str) -> dict:
        return json.loads((self._dir(run_id) / "run.json").read_text(encoding="utf-8"))

    def update(self, run_id: str, **fields) -> dict:
        run = {**self.load(run_id), **fields}
        _write(self._dir(run_id) / "run.json", run)
        return run

    def set_status(self, run_id: str, status: str, reason: str | None = None) -> dict:
        run = self.load(run_id)
        check_transition(run["status"], status)
        entry = {"status": status, "at": now(), **({"reason": reason} if reason else {})}
        return self.update(run_id, status=status, history=run["history"] + [entry])

    def save_stage(self, run_id: str, stage: str, data: Any) -> None:
        _write(self._dir(run_id) / STAGE_FILES[stage], data)

    def load_stage(self, run_id: str, stage: str) -> Any | None:
        path = self._dir(run_id) / STAGE_FILES[stage]
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None

    def list(self) -> list[dict]:
        if not self.root.is_dir():
            return []
        runs = [json.loads(p.read_text(encoding="utf-8")) for p in self.root.glob("*/run.json")]
        return sorted(runs, key=lambda r: r["run_id"])
