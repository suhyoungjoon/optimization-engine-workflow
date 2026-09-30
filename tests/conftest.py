import copy
import shutil
from pathlib import Path

import pytest
import yaml

from core import load_config

ROOT = Path(__file__).resolve().parent.parent
SETTINGS = yaml.safe_load((ROOT / "settings" / "workflow.yaml").read_text(encoding="utf-8"))
P1_P4 = ["P1", "P2", "P3", "P4"]


def write_set(path: Path, name: str, cases: list[tuple[int, list[str]]]) -> Path:
    path.write_text(yaml.safe_dump({"name": name, "cases": [{"seed": s, "faults": f} for s, f in cases]}),
                    encoding="utf-8")
    return path


@pytest.fixture
def ws(tmp_path):
    """이 레포 파일을 건드리지 않도록 레지스트리 사본, runs 폴더, 작은 시나리오 세트를 임시 폴더에 만든다.

    검증용 세트에는 경계 지역 수요(P4)가 없다: 경계 지역 안은 학습용에서만 효과가 있다 (M2 완료 기준).
    """
    shutil.copytree(ROOT / "models", tmp_path / "models")
    return {"models": tmp_path / "models", "runs": tmp_path / "runs",
            "train": write_set(tmp_path / "train.yaml", "train", [(1, P1_P4), (2, P1_P4)]),
            "holdout": write_set(tmp_path / "holdout.yaml", "holdout-no-p4", [(201, ["P1", "P2", "P3"]), (202, [])]),
            "settings": copy.deepcopy(SETTINGS),
            "llm_config": load_config(ROOT / "settings" / "llm.yaml")}
