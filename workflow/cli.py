"""워크플로우 CLI.

python -m workflow run --engine rule --seed 42 --faults P1,P2,P3,P4 --rehearsal
python -m workflow status [run_id]
python -m workflow approve <run_id> [--proposal N] [--note ...]
python -m workflow reject <run_id> --reason ...
"""

import argparse
import sys
from pathlib import Path

import yaml

from core import AnthropicClient, ResponseCache, load_config
from core.llm.client import load_dotenv

from engines import ENGINES, get_engine
from engines.rule import DEFAULT_PARAMS_PATH

from . import runner
from .storage import RunStore

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RUNS = ROOT / "runs"
DEFAULT_SETTINGS = ROOT / "settings"
DEFAULT_PARAMS = {"rule": DEFAULT_PARAMS_PATH}


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


def print_status(store: RunStore, run_id: str) -> None:
    run = store.load(run_id)
    print(f"{run['run_id']}  [{run['status']}]  {run['model']}  seed={run['scenario']['seed']} "
          f"faults={','.join(run['scenario']['faults']) or '-'}  llm={run['llm']['model']}"
          f"{' (rehearsal)' if run['llm']['rehearsal'] else ''}")
    for h in run["history"]:
        print(f"  {h['at']}  {h['status']}{'  ' + h['reason'] if h.get('reason') else ''}")
    if (r := store.load_stage(run_id, "run")):
        print(f"1 실행      {r['items']}건 {r['status']}, 위반 {r['violations']}건")
        print("            " + ", ".join(f"{k}={_fmt(v)}" for k, v in r["metrics"].items()))
    if (a := store.load_stage(run_id, "analysis")):
        print(f"2 결과분석  발견 {len(a['findings'])}개 (근거 없어 제외 {len(a['dropped'])}개), "
              f"LLM {a['usage']['llm_calls']}회")
        for f in a["findings"]:
            print(f"            {f['id']} {f['title']}: {f['description']}")
    if (p := store.load_stage(run_id, "proposals")):
        print(f"3 개선안    {len(p['proposals'])}개, 시험 {p['trials']}회, LLM {p['usage']['llm_calls']}회")
        for item in p["proposals"]:
            why = f"  ({'; '.join(item['errors'])})" if item["errors"] else ""
            print(f"            [{item['id']}] {item['state']:11s} {item['proposal'].get('title')}{why}")
    if (v := store.load_stage(run_id, "validation")):
        print(f"4 검증      {v['method']}")
        for r in v["results"]:
            changed = ", ".join(f"{k} {_fmt(r['before'][k])}→{_fmt(r['after'][k])}"
                                for k, d in r["delta"].items() if d)
            print(f"            [{r['id']}] 위반 {r['violations_after']}건, "
                  f"{'승인 가능' if r['approvable'] else '승인 불가'}: {changed or '변화 없음'}")
            for name, s in r["slices"].items():
                print(f"                {name} 실패율 {s['before']['fail_rate']:.3f}→{s['after']['fail_rate']:.3f}")
    if (d := store.load_stage(run_id, "apply")):
        if d["decision"] == "approved":
            print(f"5 개선적용  개선안 {d['proposal_id']} 승인: {d['model_before']} → {d['model_after']}")
        else:
            print(f"5 개선적용  반려: {d['reason']}")
    if run["status"] == "awaiting_approval":
        print(f"\n승인: python -m workflow approve {run_id} [--proposal N]   반려: python -m workflow reject "
              f"{run_id} --reason ...")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="workflow", description="최적화 엔진 개선 워크플로우")
    parser.add_argument("--runs-dir", type=Path, default=DEFAULT_RUNS)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="1~4단계 실행 후 승인 대기에서 멈춘다")
    p_run.add_argument("--engine", choices=ENGINES, default="rule")
    p_run.add_argument("--params", type=Path, help="엔진 params 파일 (기본: 엔진 폴더의 params.yaml)")
    p_run.add_argument("--settings", type=Path, default=DEFAULT_SETTINGS)
    p_run.add_argument("--seed", type=int, default=42)
    p_run.add_argument("--faults", default="P1,P2,P3,P4", help="쉼표 구분, 빈 문자열이면 결함 없음")
    p_run.add_argument("--rehearsal", action="store_true", help="가짜 LLM으로 실행 (API 호출 없음)")

    p_status = sub.add_parser("status", help="실행 목록 또는 한 실행의 단계별 결과")
    p_status.add_argument("run_id", nargs="?")

    p_approve = sub.add_parser("approve", help="사람의 승인: 개선안을 params에 반영하고 버전을 올린다")
    p_approve.add_argument("run_id")
    p_approve.add_argument("--proposal", type=int)
    p_approve.add_argument("--note")

    p_reject = sub.add_parser("reject", help="사람의 반려: 기록만 남긴다")
    p_reject.add_argument("run_id")
    p_reject.add_argument("--reason", required=True)

    args = parser.parse_args(argv)
    store = RunStore(args.runs_dir)
    try:
        if args.cmd == "run":
            engine = get_engine(args.engine)
            settings = yaml.safe_load((args.settings / "workflow.yaml").read_text(encoding="utf-8"))
            llm, config = _llm(args.rehearsal, args.settings, args.runs_dir)
            run = runner.run_workflow(engine, params_path=args.params or DEFAULT_PARAMS[args.engine],
                                      runs_dir=args.runs_dir, seed=args.seed,
                                      faults=[f for f in args.faults.split(",") if f], llm=llm,
                                      llm_config=config, settings=settings, rehearsal=args.rehearsal)
            print_status(store, run["run_id"])
            return 0 if run["status"] == "awaiting_approval" else 1
        if args.cmd == "status":
            if args.run_id:
                print_status(store, args.run_id)
            else:
                for run in store.list():
                    print(f"{run['run_id']}  {run['status']:17s}  {run['model']}  llm={run['llm']['model']}")
            return 0
        if args.cmd == "approve":
            runner.approve(get_engine(store.load(args.run_id)["engine"]), runs_dir=args.runs_dir, run_id=args.run_id, proposal_id=args.proposal,
                           note=args.note)
        elif args.cmd == "reject":
            runner.reject(runs_dir=args.runs_dir, run_id=args.run_id, reason=args.reason)
        print_status(store, args.run_id)
        return 0
    except (runner.WorkflowError, KeyError) as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 2
