"""엔진 간 비교: 엔진마다 현재 챔피언을 같은 시나리오 세트에서 풀어 지표·필수조건 위반·풀이 시간을 나란히 본다.

판정·승인과 무관한 정보용이다 (운영 기준 엔진을 바꾸는 절차는 없다). 엔진은 Engine 프로토콜로만 다룬다.
baseline="legacy": 기간 케이스(레거시 데이터셋)에서 과거 실제 배정을 같은 validate()·metrics()로 재어 기준으로 둔다.
"""

import statistics
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from engines.base import Engine
from modelreg import Registry

from sources.legacy import legacy_decisions, load_snapshot

from .scenarios import case_label, load_set, make_instance, pin_cases
from .storage import _write, now


LEGACY = "legacy"


def compare_engines(engines: list[Engine], *, models_dir: str | Path, set_path: str | Path, progress=None,
                    baseline: str | None = None) -> dict:
    sset = pin_cases(load_set(set_path))
    if baseline not in (None, LEGACY):
        raise ValueError(f"알 수 없는 기준선: {baseline} (가능: {LEGACY})")
    if baseline and not all("dataset" in c for c in sset["cases"]):
        raise ValueError("레거시 기준선은 기간 케이스({dataset, from, to})로만 된 세트에서만 쓸 수 있다")
    champs = []
    for e in engines:
        reg = Registry(models_dir, e.name)
        version = reg.champion()
        params = e.load_params(reg.params_path(version))
        champs.append((e, version, params, e.pack_factory(params)))
    cases, total, done = [], len(sset["cases"]) * len(engines), 0
    for case in sset["cases"]:
        instance = make_instance(champs[0][3], case)   # 같은 도메인: 한 번 만들어 모든 엔진에 쓴다
        row = {**case, "engines": {}}
        if baseline:   # 과거 실제 배정: 첫 엔진의 챔피언 params로 잰다 (작업 소요·매칭 범위의 기준)
            _e, _v, params, pack = champs[0]
            decisions = legacy_decisions(load_snapshot(case["dataset"]), instance, params)
            row["engines"][LEGACY] = {"metrics": pack.metrics(instance, decisions),
                                      "violations": len(pack.validate(instance, decisions)), "seconds": 0.0}
        for e, _v, params, pack in champs:
            started = time.time()
            decisions = pack.solve(instance, params)
            row["engines"][e.name] = {"metrics": pack.metrics(instance, decisions),
                                      "violations": len(pack.validate(instance, decisions)),
                                      "seconds": round(time.time() - started, 2)}
            done += 1
            if progress:
                progress(done, total, f"{e.name} {case_label(case)}")
        cases.append(row)
    models = {e.name: f"{e.name}@v{v}" for e, v, *_ in champs}
    if baseline:
        models = {LEGACY: "레거시 실제 배정", **models}
    names = list(models)
    metrics = list(cases[0]["engines"][names[0]]["metrics"])
    summary = {n: {"model": model,
                   "metrics": {m: round(statistics.mean(c["engines"][n]["metrics"][m] for c in cases), 6) for m in metrics},
                   "violations": sum(c["engines"][n]["violations"] for c in cases),
                   "seconds_mean": round(statistics.mean(c["engines"][n]["seconds"] for c in cases), 2)}
               for n, model in models.items()}
    base = names[0]   # 기준선이 있으면 기준선, 없으면 첫 엔진
    for n in names[1:]:
        summary[n]["delta_vs_" + base] = {m: round(summary[n]["metrics"][m] - summary[base]["metrics"][m], 6)
                                          for m in metrics}
    return {"set": {"name": sset["name"], "path": sset["path"], "cases": sset["cases"]}, "engines": names,
            "base": base, "summary": summary, "cases": cases, "at": now()}


def save_comparison(runs_dir: str | Path, result: dict) -> str:
    d = Path(runs_dir) / "comparisons"
    d.mkdir(parents=True, exist_ok=True)
    cid = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:6]
    _write(d / f"{cid}.json", {"id": cid, **result})
    return cid
