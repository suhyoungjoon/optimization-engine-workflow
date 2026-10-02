"""엔진 간 비교: 엔진마다 현재 챔피언을 같은 시나리오 세트에서 풀어 지표·필수조건 위반·풀이 시간을 나란히 본다.

판정·승인과 무관한 정보용이다 (운영 기준 엔진을 바꾸는 절차는 없다). 엔진은 Engine 프로토콜로만 다룬다.
"""

import statistics
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from engines.base import Engine
from modelreg import Registry

from .scenarios import load_set, make_instance
from .storage import _write, now


def compare_engines(engines: list[Engine], *, models_dir: str | Path, set_path: str | Path, progress=None) -> dict:
    sset = load_set(set_path)
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
        for e, _v, params, pack in champs:
            started = time.time()
            decisions = pack.solve(instance, params)
            row["engines"][e.name] = {"metrics": pack.metrics(instance, decisions),
                                      "violations": len(pack.validate(instance, decisions)),
                                      "seconds": round(time.time() - started, 2)}
            done += 1
            if progress:
                progress(done, total, f"{e.name} seed {case['seed']}")
        cases.append(row)
    names = [e.name for e, *_ in champs]
    metrics = list(cases[0]["engines"][names[0]]["metrics"])
    summary = {n: {"model": f"{n}@v{v}",
                   "metrics": {m: round(statistics.mean(c["engines"][n]["metrics"][m] for c in cases), 6) for m in metrics},
                   "violations": sum(c["engines"][n]["violations"] for c in cases),
                   "seconds_mean": round(statistics.mean(c["engines"][n]["seconds"] for c in cases), 2)}
               for (n, v) in ((e.name, v) for e, v, *_ in champs)}
    base = names[0]
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
