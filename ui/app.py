"""워크플로우 화면의 API. 경로는 모두 create_app 인자로 받는다 (테스트는 임시 폴더를 넘긴다)."""

import threading
from pathlib import Path

import yaml
from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from core import list_faults, load_config

from engines import ENGINES, get_engine
from modelreg import Registry, RegistryError
from workflow import config, runner
from workflow.judge import check_config
from workflow.pipeline import default_pipeline
from workflow.rehearsal import rehearsal_llm
from workflow.state import FINAL
from workflow.storage import STAGE_FILES, RunStore

ROOT = Path(__file__).resolve().parent.parent
STATIC = Path(__file__).resolve().parent / "static"
SET_NAMES = ("train", "holdout")


def _flatten(value, prefix: str = "") -> dict:
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            out.update(_flatten(v, f"{prefix}.{k}" if prefix else str(k)))
        return out
    if isinstance(value, list) and any(isinstance(v, (dict, list)) for v in value):
        out = {}
        for i, v in enumerate(value):
            out.update(_flatten(v, f"{prefix}[{i}]"))
        return out
    return {prefix: value}


def params_diff(a: dict, b: dict) -> list[dict]:
    fa, fb = _flatten(a), _flatten(b)
    rows = []
    for path in sorted(set(fa) | set(fb)):
        if path == "version" or fa.get(path) == fb.get(path):
            continue
        kind = "added" if path not in fa else "removed" if path not in fb else "changed"
        rows.append({"path": path, "a": fa.get(path), "b": fb.get(path), "kind": kind})
    return rows


def create_app(*, runs_dir: Path = ROOT / "runs", models_dir: Path = ROOT / "models",
               settings_dir: Path = ROOT / "settings", scenarios_dir: Path = ROOT / "scenarios",
               llm_factory=rehearsal_llm) -> FastAPI:
    app = FastAPI(title="optimization-engine-workflow", docs_url=None, redoc_url=None)
    store = RunStore(runs_dir)
    active: dict = {"thread": None}
    lock = threading.Lock()
    workflow_yaml = settings_dir / "workflow.yaml"

    def fail(exc: Exception, status: int = 400):
        raise HTTPException(status, str(exc)) from None

    def engine_or_404(engine: str):
        if engine not in ENGINES:
            raise HTTPException(404, f"알 수 없는 엔진: {engine}")
        return get_engine(engine)

    def registry(engine: str) -> Registry:
        engine_or_404(engine)
        return Registry(models_dir, engine)

    def champion_pack(engine: str = "rule"):
        eng, reg = engine_or_404(engine), registry(engine)
        return eng.pack_factory(eng.load_params(reg.params_path(reg.champion())))

    def set_path(name: str) -> Path:
        if name not in SET_NAMES:
            raise HTTPException(404, f"알 수 없는 시나리오 세트: {name}")
        return scenarios_dir / f"{name}.yaml"

    def load_run(run_id: str) -> dict:
        try:
            return store.load(run_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from None

    # --- 메타 ---

    @app.get("/api/meta")
    def meta():
        pack = champion_pack()
        dims = pack.dimensions()
        stages = [{k: s[k] for k in ("key", "label", "actor", "kind")} for s in default_pipeline()["stages"]]
        return {"engines": list(ENGINES), "stages": stages, "rehearsal_only": True,
                "faults": list_faults(pack),
                "metrics": sorted(_metric_names(pack)),
                "dimensions": {k: v.get("label") for k, v in dims.get("dimensions", {}).items()}}

    metric_cache: dict = {}

    def _metric_names(pack) -> list[str]:
        if "names" not in metric_cache:   # 지표 이름은 도메인이 정한다: 작은 인스턴스로 한 번 계산해 둔다
            inst, _ = pack.generate(0, [])
            small = pack.subset(inst, pack.items(inst)[:5])
            metric_cache["names"] = list(pack.metrics(small, pack.solve(small, pack.params)))
        return metric_cache["names"]

    @app.get("/api/pipeline")
    def pipeline():
        return default_pipeline()

    # --- 실행 ---

    @app.get("/api/runs")
    def runs():
        busy = active["thread"] is not None and active["thread"].is_alive()
        return {"busy": busy, "runs": [{k: r.get(k) for k in ("run_id", "status", "created_at", "model", "engine",
                                                              "champion_version", "llm", "progress", "history")}
                                       for r in reversed(store.list())]}

    @app.get("/api/runs/{run_id}")
    def run_detail(run_id: str):
        run = load_run(run_id)
        stages = {key: store.load_stage(run_id, key) for key in STAGE_FILES if key != "decisions"}
        return {"run": run, "stages": stages, "final": run["status"] in FINAL}

    @app.post("/api/runs")
    def start_run(body: dict = Body(default={})):
        engine = engine_or_404(body.get("engine", "rule"))
        train, holdout = set_path(body.get("train", "train")), set_path(body.get("holdout", "holdout"))
        started, box = threading.Event(), {}
        with lock:
            if active["thread"] is not None and active["thread"].is_alive():
                raise HTTPException(409, "다른 실행이 진행 중이다. 끝난 뒤 다시 시작한다")
            settings = config.load_settings(workflow_yaml)
            llm_config = load_config(settings_dir / "llm.yaml")

            def target():
                try:
                    runner.run_workflow(engine, models_dir=models_dir, runs_dir=runs_dir, train_path=train,
                                        holdout_path=holdout, llm=llm_factory(), llm_config=llm_config,
                                        settings=settings, rehearsal=True,
                                        on_start=lambda rid: (box.update(run_id=rid), started.set()))
                except Exception as exc:   # 실행 기록에 failed로 남는다
                    box.setdefault("error", str(exc))
                finally:
                    started.set()

            active["thread"] = threading.Thread(target=target, daemon=True)
            active["thread"].start()
        started.wait(10)
        if "run_id" not in box:
            raise HTTPException(500, box.get("error", "실행을 시작하지 못했다"))
        return {"run_id": box["run_id"]}

    def decide(call):
        try:
            return call()
        except (runner.WorkflowError, RegistryError, ValueError) as exc:
            fail(exc)

    @app.post("/api/runs/{run_id}/approve")
    def approve(run_id: str, body: dict = Body(default={})):
        return decide(lambda: runner.approve(runs_dir=runs_dir, run_id=run_id, proposal_id=body.get("proposal_id"),
                                             note=body.get("note"), override_reason=body.get("override_reason")))

    @app.post("/api/runs/{run_id}/reject")
    def reject(run_id: str, body: dict = Body(default={})):
        if not str(body.get("reason") or "").strip():
            raise HTTPException(400, "반려 사유가 필요하다")
        return decide(lambda: runner.reject(runs_dir=runs_dir, run_id=run_id, reason=body["reason"]))

    @app.post("/api/runs/{run_id}/revise")
    def revise(run_id: str, body: dict = Body(default={})):
        seconds = config.load_settings(workflow_yaml)["validation"]["max_seconds"]
        return decide(lambda: runner.revise(runs_dir=runs_dir, run_id=run_id, proposal_id=body.get("proposal_id"),
                                            changes=body.get("changes") or {}, note=body.get("note"),
                                            max_seconds=seconds))

    # --- 모델 버전 ---

    @app.get("/api/models/{engine}")
    def models(engine: str):
        reg = registry(engine)
        try:
            return {"engine": engine, "champion": reg.champion(), "history": reg.history(),
                    "versions": [reg.card(v) for v in reg.versions()]}
        except RegistryError as exc:
            fail(exc, 404)

    @app.get("/api/models/{engine}/versions/{version}")
    def model_version(engine: str, version: int):
        reg = registry(engine)
        try:
            path = reg.params_path(version)
        except RegistryError as exc:
            fail(exc, 404)
        text = path.read_text(encoding="utf-8")
        return {"card": reg.card(version), "params": yaml.safe_load(text), "params_text": text,
                "champion": reg.champion() == version}

    @app.get("/api/models/{engine}/diff")
    def model_diff(engine: str, a: int, b: int):
        eng, reg = engine_or_404(engine), registry(engine)
        try:
            return {"a": a, "b": b, "rows": params_diff(eng.load_params(reg.params_path(a)),
                                                        eng.load_params(reg.params_path(b)))}
        except RegistryError as exc:
            fail(exc, 404)

    @app.post("/api/models/{engine}/rollback")
    def rollback(engine: str, body: dict = Body(default={})):
        engine_or_404(engine)
        if not str(body.get("reason") or "").strip():
            raise HTTPException(400, "되돌리기 사유가 필요하다")
        before, after = decide(lambda: runner.rollback(models_dir=models_dir, engine=engine, reason=body["reason"],
                                                       to=body.get("to")))
        return {"before": before, "after": after}

    # --- 기준정보 ---

    @app.get("/api/settings")
    def get_settings():
        return {"values": {k: v for k, v in config.load_settings(workflow_yaml).items() if k in config.EDITABLE}}

    @app.put("/api/settings")
    def put_settings(body: dict = Body(...)):
        if "judgment" in body and check_config(body["judgment"]):
            raise HTTPException(400, "; ".join(check_config(body["judgment"])))
        try:
            values = config.save_settings(workflow_yaml, body)
        except ValueError as exc:
            fail(exc)
        return {"values": {k: v for k, v in values.items() if k in config.EDITABLE}}

    @app.get("/api/scenarios")
    def get_scenarios():
        from workflow.scenarios import load_set
        return {name: load_set(set_path(name)) for name in SET_NAMES}

    @app.put("/api/scenarios/{name}")
    def put_scenario(name: str, body: dict = Body(...)):
        path = set_path(name)
        other = set_path(next(n for n in SET_NAMES if n != name))
        known = {f["id"] for f in list_faults(champion_pack())}
        try:
            return config.save_set(path, body.get("cases"), other, known)
        except ValueError as exc:
            fail(exc)

    # --- 화면 ---

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app

