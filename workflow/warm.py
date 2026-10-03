"""캐시 미리 채우기: 현재 챔피언으로 시나리오 세트의 모든 케이스를 풀어 둔다.

결정적인 엔진(예: solver)은 풀이 결과를 디스크에 저장하므로, 미리 풀어 두면 다음 실행의 1단계와 검증 단계의 챔피언 쪽이
다시 풀지 않는다. 캐시가 없는 엔진은 풀기만 한다. 실행 기록·판정과 무관하다. 엔진은 Engine 프로토콜로만 다룬다.
"""

import time
from pathlib import Path

from engines.base import Engine
from modelreg import Registry

from .scenarios import case_label, load_set, make_instance


def warm(engine: Engine, *, models_dir: str | Path, set_paths: list[str | Path], progress=None) -> dict:
    reg = Registry(models_dir, engine.name)
    version = reg.champion()
    params = engine.load_params(reg.params_path(version))
    pack = engine.pack_factory(params)
    sets = [load_set(p) for p in set_paths]   # 세트를 먼저 모두 읽어 잘못된 이름을 풀기 전에 알린다
    total, done, out = sum(len(s["cases"]) for s in sets), 0, []
    for s in sets:
        cases = []
        for case in s["cases"]:
            started = time.time()
            pack.solve(make_instance(pack, case), params)
            cases.append({"case": case_label(case), "seconds": round(time.time() - started, 2)})
            done += 1
            if progress:
                progress(done, total, f"{s['name']} {case_label(case)}")
        out.append({"name": s["name"], "path": s["path"], "cases": cases})
    return {"model": f"{engine.name}@v{version}", "sets": out,
            "seconds": round(sum(c["seconds"] for s in out for c in s["cases"]), 2)}
