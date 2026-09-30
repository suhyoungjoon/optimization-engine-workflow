import json

import pytest
import yaml

from modelreg import Registry, RegistryError
from tests.conftest import ROOT

RULE = {"when": {"area_zone": ["boundary"]}, "set": {"matching.area_extension_km[2]": 4}}
TW = {"params_changes": [{"path": "matching.time_window_min[2]", "value": 75}]}


@pytest.fixture
def reg(tmp_path):
    r = Registry(tmp_path / "models", "rule")
    r.init(ROOT / "models" / "rule" / "v1" / "params.yaml", note="test")
    return r


def _params(reg, v):
    return yaml.safe_load(reg.params_path(v).read_text(encoding="utf-8"))


def test_repo_registry_starts_at_v1():
    r = Registry(ROOT / "models", "rule")
    assert r.versions() == [1] and r.champion() == 1 and r.card(1)["parent"] is None
    assert _params(r, 1)["version"] == 1


def test_register_creates_new_version_without_touching_parent(reg):
    parent_text = reg.params_path(1).read_text(encoding="utf-8")
    v = reg.register(1, {"override_rules": [RULE]}, {"run_id": "r1"})
    assert v == 2 and reg.versions() == [1, 2] and reg.champion() == 1   # 챔피언 지정은 따로
    assert reg.params_path(1).read_text(encoding="utf-8") == parent_text
    new = _params(reg, 2)
    assert new["version"] == 2 and new["overrides"]["rules"] == [RULE]
    assert "# 이 값 이상이면 명장" in reg.params_path(2).read_text(encoding="utf-8")   # 주석 보존
    assert reg.card(2)["parent"] == 1 and reg.card(2)["run_id"] == "r1"
    with pytest.raises(FileExistsError):
        (reg.root / "v2").mkdir()


def test_champion_history_and_rollback(reg):
    reg.set_champion(reg.register(1, TW, {}), "approve", run_id="r1")
    assert reg.champion() == 2
    assert reg.rollback("bad") == (2, 1) and reg.champion() == 1
    assert [h["action"] for h in reg.history()] == ["init", "approve", "rollback"]
    assert reg.history()[-1]["reason"] == "bad" and reg.history()[-1]["previous"] == 2
    with pytest.raises(RegistryError, match="부모 버전이 없어"):
        reg.rollback("again")
    assert reg.rollback("forward", to=2) == (1, 2)
    with pytest.raises(RegistryError, match="이미 챔피언"):
        reg.rollback("x", to=2)
    with pytest.raises(RegistryError, match="버전이 없다"):
        reg.rollback("x", to=9)


def test_version_number_follows_directory_after_rollback(reg):
    reg.set_champion(reg.register(1, TW, {}), "approve")
    reg.rollback("back")                                  # 챔피언 v1, v2는 남아 있다
    v = reg.register(1, {"override_rules": [RULE]}, {})
    assert v == 3 and _params(reg, 3)["version"] == 3     # 부모(v1)+1이 아니라 디렉터리 번호
    assert json.loads((reg.root / "v3" / "card.json").read_text(encoding="utf-8"))["parent"] == 1


def test_lock_is_exclusive(reg):
    with reg.lock():
        with pytest.raises(RegistryError, match="진행 중"):
            with reg.lock():
                pass
    with reg.lock():
        pass


def test_missing_registry(tmp_path):
    r = Registry(tmp_path / "none", "rule")
    with pytest.raises(RegistryError, match="레지스트리가 없다"):
        r.champion()
    with pytest.raises(RegistryError, match="레지스트리가 없다"):
        with r.lock():
            pass
    with pytest.raises(RegistryError, match="이미 버전이 있다"):
        Registry(ROOT / "models", "rule").init(ROOT / "models" / "rule" / "v1" / "params.yaml")
