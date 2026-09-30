import shutil
from pathlib import Path

import pytest
import yaml

from core import load_config

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def ws(tmp_path):
    """이 레포 파일을 건드리지 않도록 params 사본과 runs 폴더를 임시 폴더에 만든다."""
    params = tmp_path / "params.yaml"
    shutil.copy(ROOT / "engines" / "rule" / "params.yaml", params)
    return {"params": params, "runs": tmp_path / "runs",
            "settings": yaml.safe_load((ROOT / "settings" / "workflow.yaml").read_text(encoding="utf-8")),
            "llm_config": load_config(ROOT / "settings" / "llm.yaml")}
