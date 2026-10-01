"""워크플로우 CLI.

python -m workflow run --engine rule --rehearsal [--train scenarios/train.yaml --holdout scenarios/holdout.yaml]
python -m workflow status [run_id]
python -m workflow approve <run_id> [--proposal N] [--note ...] [--override-verdict --reason ...]
python -m workflow reject <run_id> --reason ...
python -m workflow models [--engine rule]
python -m workflow rollback [--engine rule] [--to N] --reason ...
python -m workflow compare-engines [--engines rule,solver] [--set solver_holdout]
"""

import argparse
import sys
from pathlib import Path

import yaml

from core import AnthropicClient, ResponseCache, load_config
from core.llm.client import load_dotenv

from engines import ENGINES, get_engine
from modelreg import Registry, RegistryError

from . import runner
from .compare_engines import compare_engines, save_comparison
from .config import for_engine
from .judge import SET_LABELS
from .pipeline import stage_labels
from .storage import RunStore

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RUNS = ROOT / "runs"
DEFAULT_MODELS = ROOT / "models"
DEFAULT_SETTINGS = ROOT / "settings"
DEFAULT_SCENARIOS = ROOT / "scenarios"


def _llm(rehearsal: bool, settings_dir: Path, runs_dir: Path):
    config = load_config(settings_dir / "llm.yaml")
    if rehearsal:
        from .rehearsal import rehearsal_llm
        return rehearsal_llm(), config
    load_dotenv(ROOT / ".env")   # 코어 기본값은 site-packages의 .env를 본다
    cache = ResponseCache(runs_dir / "llm_cache.sqlite") if config.get("cache") else None
    return AnthropicClient(config, cache=cache), config


def _fmt(v) -> str:
    return f"{v:.3f}" if isinstance(v, float) else str(v)


def _cases(s: dict) -> str:
    return ", ".join(f"{c['seed']}:{'+'.join(c['faults']) or '-'}" for c in s.get("cases", []))


def _judged_metrics(judgment: dict) -> list[str]:
    return [judgment["target"]["metric"], *(judgment.get("guards") or {})]


def print_status(store: RunStore, run_id: str) -> None:
    run = store.load(run_id)
    head = {k: f"{i} {v}".ljust(10) for i, (k, v) in enumerate(stage_labels().items(), start=1)}
    print(f"{run['run_id']}  [{run['status']}]  {run['model'] or '-'}  llm={run['llm']['model']}"
          f"{' (rehearsal)' if run['llm']['rehearsal'] else ''}")
    for key, s in run["scenarios"].items():
        print(f"  {SET_LABELS[key]} {s.get('name', '')} ({len(s.get('cases', []))}건): {_cases(s)}")
    for h in run["history"]:
        print(f"  {h['at']}  {h['status']}{'  ' + h['reason'] if h.get('reason') else ''}")
    if (r := store.load_stage(run_id, "run")):
        rep = r["representative"]
        print(f"{head['run']}  학습용 {r['summary']['cases']}건, 위반 {r['summary']['violations']}건, "
              f"대표 케이스 seed {rep['seed']} (분석·개선안 도출에 사용)")
        print("            평균 " + ", ".join(f"{k}={_fmt(v)}" for k, v in r["summary"]["metrics"].items()))
    if (a := store.load_stage(run_id, "analysis")):
        print(f"{head['analysis']}  발견 {len(a['findings'])}개 (근거 없어 제외 {len(a['dropped'])}개), "
              f"LLM {a['usage']['llm_calls']}회")
        for f in a["findings"]:
            print(f"            {f['id']} {f['title']}: {f['description']}")
    if (p := store.load_stage(run_id, "proposals")):
        print(f"{head['proposals']}  {len(p['proposals'])}개, 시험 {p['trials']}회, LLM {p['usage']['llm_calls']}회")
        for item in p["proposals"]:
            why = f"  ({'; '.join(item['errors'])})" if item["errors"] else ""
            print(f"            [{item['id']}] {item['state']:11s} {item['proposal'].get('title')}{why}")
    if (v := store.load_stage(run_id, "validation")):
        metrics = _judged_metrics(v["judgment"])
        print(f"{head['validation']}  학습용·검증용 케이스별 챔피언/도전자 비교, 두 세트 모두 통과해야 판정 통과")
        for r in v["results"]:
            verdict = r["verdict"]
            label = "통과" if verdict["pass"] else "불통과" + (" (학습용에서만 효과, 과적합)" if verdict["overfit"] else "")
            print(f"            [{r['id']}] {label}  {r['title']}")
            for key in ("train", "holdout"):
                s = r[key]["summary"]
                deltas = ", ".join(f"{m} Δ{s['metrics'][m]['delta_mean']:+.3f}" for m in metrics if m in s["metrics"])
                print(f"                {SET_LABELS[key]} {s['cases']}건 위반 {s['violations']}: {deltas}")
            for reason in verdict["reasons"]:
                print(f"                - {reason}")
    if (d := store.load_stage(run_id, "apply")):
        if d["decision"] == "approved":
            extra = f" (판정 무시: {d['override_reason']})" if d.get("override_reason") else ""
            print(f"{head['apply']}  개선안 {d['proposal_id']} 승인: {d['model_before']} → {d['model_after']}{extra}")
        else:
            print(f"{head['apply']}  반려: {d['reason']}")
    if run["status"] == "awaiting_approval":
        print(f"\n승인: python -m workflow approve {run_id} [--proposal N]   반려: python -m workflow reject "
              f"{run_id} --reason ...")


def print_models(registry: Registry) -> None:
    champion = registry.champion()
    for v in registry.versions():
        card = registry.card(v)
        verdict = card.get("verdict")
        how = ("판정 무시: " + card["override"]["reason"]) if card.get("override") else \
            ("판정 통과" if verdict and verdict["pass"] else "초기 버전" if card["parent"] is None else "")
        mark = "*" if v == champion else " "
        print(f"{mark} {registry.engine}@v{v}  부모 {('v' + str(card['parent'])) if card['parent'] else '-'}  "
              f"{card['created_at']}  {how}{'  실행 ' + card['run_id'] if card.get('run_id') else ''}")
    print("챔피언 이력:")
    for h in registry.history():
        extra = h.get("reason") or (f"실행 {h['run_id']} 개선안 {h['proposal_id']}" if h.get("run_id") else "")
        print(f"  {h['at']}  {h['action']:8s} v{h['version']}  {extra}")


def print_comparison(result: dict) -> None:
    names, base = result["engines"], result["base"]
    print(f"엔진 간 비교: {result['set']['name']} ({len(result['cases'])}건), 기준 {base} (정보용, 판정·승인과 무관)")
    print("  " + "지표".ljust(26) + "".join(result["summary"][n]["model"].rjust(14) for n in names))
    for m in result["summary"][base]["metrics"]:
        print("  " + m.ljust(26) + "".join(f"{result['summary'][n]['metrics'][m]:14.3f}" for n in names))
    print("  " + "필수조건 위반".ljust(23) + "".join(f"{result['summary'][n]['violations']:14d}" for n in names))
    print("  " + "1회 풀이 시간(초)".ljust(22) + "".join(f"{result['summary'][n]['seconds_mean']:14.2f}" for n in names))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="workflow", description="최적화 엔진 개선 워크플로우")
    parser.add_argument("--runs-dir", type=Path, default=DEFAULT_RUNS)
    parser.add_argument("--models-dir", type=Path, default=DEFAULT_MODELS)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="1~4단계 실행 후 승인 대기에서 멈춘다")
    p_run.add_argument("--engine", choices=ENGINES, default="rule")
    p_run.add_argument("--train", type=Path, help="학습용 시나리오 세트 (기본: 설정의 엔진별 scenarios.train)")
    p_run.add_argument("--holdout", type=Path, help="검증용 시나리오 세트 (기본: 설정의 엔진별 scenarios.holdout)")
    p_run.add_argument("--settings", type=Path, default=DEFAULT_SETTINGS)
    p_run.add_argument("--rehearsal", action="store_true", help="가짜 LLM으로 실행 (API 호출 없음)")

    p_status = sub.add_parser("status", help="실행 목록 또는 한 실행의 단계별 결과")
    p_status.add_argument("run_id", nargs="?")

    p_approve = sub.add_parser("approve", help="사람의 승인: 새 버전을 등록하고 챔피언으로 지정한다")
    p_approve.add_argument("run_id")
    p_approve.add_argument("--proposal", type=int)
    p_approve.add_argument("--note")
    p_approve.add_argument("--override-verdict", action="store_true",
                           help="판정을 통과하지 못한 안을 승인한다 (--reason 필수, 필수조건 위반 안은 불가)")
    p_approve.add_argument("--reason")

    p_reject = sub.add_parser("reject", help="사람의 반려: 기록만 남긴다")
    p_reject.add_argument("run_id")
    p_reject.add_argument("--reason", required=True)

    p_models = sub.add_parser("models", help="모델 버전 목록과 챔피언 이력")
    p_models.add_argument("--engine", choices=ENGINES, default="rule")

    p_rollback = sub.add_parser("rollback", help="챔피언을 이전 버전으로 되돌린다")
    p_rollback.add_argument("--engine", choices=ENGINES, default="rule")
    p_rollback.add_argument("--to", type=int, help="되돌릴 버전 (기본: 현재 챔피언의 부모)")
    p_rollback.add_argument("--reason", required=True)

    p_cmp = sub.add_parser("compare-engines", help="엔진마다 현재 챔피언을 같은 시나리오 세트에서 비교한다 (정보용)")
    p_cmp.add_argument("--engines", default=",".join(ENGINES), help="쉼표 구분, 첫 엔진이 기준")
    p_cmp.add_argument("--set", default="solver_holdout", help="시나리오 세트 이름 (scenarios/<이름>.yaml)")
    p_cmp.add_argument("--scenarios-dir", type=Path, default=DEFAULT_SCENARIOS)

    args = parser.parse_args(argv)
    if args.cmd == "approve" and args.override_verdict and not args.reason:
        parser.error("--override-verdict에는 --reason이 필요하다")
    store = RunStore(args.runs_dir)
    try:
        if args.cmd == "run":
            engine = get_engine(args.engine)
            settings = yaml.safe_load((args.settings / "workflow.yaml").read_text(encoding="utf-8"))
            sets = for_engine(settings, engine.name)["scenarios"]
            train = args.train or DEFAULT_SCENARIOS / f"{sets['train']}.yaml"
            holdout = args.holdout or DEFAULT_SCENARIOS / f"{sets['holdout']}.yaml"
            llm, config = _llm(args.rehearsal, args.settings, args.runs_dir)
            run = runner.run_workflow(engine, models_dir=args.models_dir, runs_dir=args.runs_dir,
                                      train_path=train, holdout_path=holdout, llm=llm, llm_config=config,
                                      settings=settings, rehearsal=args.rehearsal)
            print_status(store, run["run_id"])
            return 0 if run["status"] == "awaiting_approval" else 1
        if args.cmd == "status":
            if args.run_id:
                print_status(store, args.run_id)
            else:
                for run in store.list():
                    print(f"{run['run_id']}  {run['status']:17s}  {run['model'] or '-'}  llm={run['llm']['model']}")
            return 0
        if args.cmd == "models":
            print_models(Registry(args.models_dir, args.engine))
            return 0
        if args.cmd == "compare-engines":
            names = [n for n in args.engines.split(",") if n]
            result = compare_engines([get_engine(n) for n in names], models_dir=args.models_dir,
                                     set_path=args.scenarios_dir / f"{args.set}.yaml",
                                     progress=lambda d, t, msg: print(f"  {d}/{t} {msg}", file=sys.stderr))
            cid = save_comparison(args.runs_dir, result)
            print_comparison(result)
            print(f"\n저장: {args.runs_dir / 'comparisons' / (cid + '.json')}")
            return 0
        if args.cmd == "rollback":
            before, after = runner.rollback(models_dir=args.models_dir, engine=args.engine, reason=args.reason,
                                            to=args.to)
            print(f"챔피언 되돌림: {args.engine}@v{before} → {args.engine}@v{after}")
            return 0
        if args.cmd == "approve":
            runner.approve(runs_dir=args.runs_dir, run_id=args.run_id, proposal_id=args.proposal, note=args.note,
                           override_reason=args.reason if args.override_verdict else None)
        elif args.cmd == "reject":
            runner.reject(runs_dir=args.runs_dir, run_id=args.run_id, reason=args.reason)
        print_status(store, args.run_id)
        return 0
    except (runner.WorkflowError, RegistryError, KeyError) as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:   # run_workflow는 failed로 기록한 뒤 다시 던진다. 상세는 run.json의 error
        print(f"실행 실패: {type(exc).__name__}: {exc}", file=sys.stderr)
        for run in store.list()[-1:]:
            if run["status"] == "failed":
                print(f"기록: {run['run_id']} (python -m workflow status {run['run_id']})", file=sys.stderr)
        return 1
