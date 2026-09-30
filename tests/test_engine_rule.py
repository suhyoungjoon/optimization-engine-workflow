import yaml

from core import check_params
from domains.dispatch import get_pack

from engines import get_engine
from modelreg import Registry
from tests.conftest import ROOT

V1 = Registry(ROOT / "models", "rule").params_path(1)


def test_v1_params_start_as_core_copy_and_pass_bounds():
    engine = get_engine("rule")
    params = engine.load_params(V1)
    core_params = yaml.safe_load(open(get_pack().params_path(), encoding="utf-8"))
    assert params == core_params and params["version"] == 1
    assert check_params(params, engine.pack_factory(params).dimensions()) == []


def test_pack_factory_uses_given_params_not_core_default():
    engine = get_engine("rule")
    params = engine.load_params(V1)
    params["travel"]["avg_speed_kmh"] = 45
    assert engine.pack_factory(params).params["travel"]["avg_speed_kmh"] == 45


def test_solve_is_deterministic_and_valid():
    engine = get_engine("rule")
    params = engine.load_params(V1)
    pack = engine.pack_factory(params)
    inst, _ = pack.generate(7, ["P4"])
    a, b = pack.solve(inst, params), pack.solve(inst, params)
    assert [(d.item_id, d.decision, d.status) for d in a] == [(d.item_id, d.decision, d.status) for d in b]
    assert pack.validate(inst, a) == []
