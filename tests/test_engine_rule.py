import yaml

from core import check_params
from domains.dispatch import get_pack

from engines import get_engine
from engines.rule import DEFAULT_PARAMS_PATH


def test_repo_params_start_as_core_copy_and_pass_bounds():
    engine = get_engine("rule")
    params = engine.load_params(DEFAULT_PARAMS_PATH)
    core_params = yaml.safe_load(open(get_pack().params_path(), encoding="utf-8"))
    assert params == core_params and params["version"] == 1
    assert check_params(params, engine.pack_factory(params).dimensions()) == []


def test_pack_factory_uses_given_params_not_core_default():
    engine = get_engine("rule")
    params = engine.load_params(DEFAULT_PARAMS_PATH)
    params["travel"]["avg_speed_kmh"] = 45
    assert engine.pack_factory(params).params["travel"]["avg_speed_kmh"] == 45


def test_solve_is_deterministic_and_valid():
    engine = get_engine("rule")
    params = engine.load_params(DEFAULT_PARAMS_PATH)
    pack = engine.pack_factory(params)
    inst, _ = pack.generate(7, ["P4"])
    a, b = pack.solve(inst, params), pack.solve(inst, params)
    assert [(d.item_id, d.decision, d.status) for d in a] == [(d.item_id, d.decision, d.status) for d in b]
    assert pack.validate(inst, a) == []
