"""개발용 가상 레거시 내보내기: 코어 생성기의 인스턴스를 레거시 시스템이 내보냈을 법한 CSV로 쓴다.

실제 레거시 데이터가 오기 전에 가져오기·매핑 명세·기간 세트·레거시 기준선을 만들고 시험하기 위한 것이다.
- 컬럼·코드는 매핑 명세(mapping.yaml)를 거꾸로 써서 만든다 (한글 컬럼, 한글 코드, 위경도, 날짜).
- "레거시가 실제로 한 배정": 규칙 엔진 결과에 상황실의 수동 변경(manual_share 비율, 같은 지점의 다른 기사로 교체)을 섞는다.
  수동 변경은 기술·자격·일정을 확인하지 않으므로 일부는 필수조건을 어긴다 (기준선에서 드러나는지 보려고).
- 실적: 도착은 배정 시각 ± 몇 분, 소요는 모델 소요 × 0.8~1.4, 취소 3%. 판정에는 쓰지 않는다.
- 고객명·연락처 같은 가짜 개인정보 컬럼을 넣는다: 가져오기가 읽지 않는지 확인하려고.
같은 seed면 같은 파일이 나온다.
"""

import csv
import datetime as dt
import random
from pathlib import Path

from domains.dispatch import get_pack
from domains.dispatch import rule_engine as R
from domains.dispatch.models import min_to_hhmm

from .dataset import to_latlon
from .importer import load_branches
from .mapping import load_mapping

MAPPING = Path(__file__).resolve().parent / "mapping.yaml"
# 가상 레거시 시스템 = 규칙 엔진 v1 (챔피언이 바뀌어도 과거 데이터는 그대로여야 하므로 버전을 고정한다)
DEFAULT_PARAMS = Path(__file__).resolve().parents[2] / "models" / "rule" / "v1" / "params.yaml"


def _reverse(codes: dict) -> dict:
    return {core: legacy for legacy, core in codes.items()}


def _write(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def export_legacy(out_dir: str | Path, *, seed: int, faults: list[str], start: dt.date, params: dict,
                  mapping_path: str | Path = MAPPING, manual_share: float = 0.03) -> dict:
    mapping = load_mapping(mapping_path)
    _branches, geo = load_branches(mapping)
    pack = get_pack(params)
    inst, _truth = pack.generate(seed, faults)
    decisions = pack.solve(inst, params)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    oc, wc, ac = (mapping[p]["columns"] for p in ("orders", "workers", "assignments"))
    ocode = {f: _reverse(c) for f, c in mapping["orders"]["codes"].items()}
    wcode = {f: _reverse(c) for f, c in mapping["workers"]["codes"].items()}
    sep, tfmt = mapping["list_separator"], mapping["time_format"]
    date_of = lambda day: (start + dt.timedelta(days=day - 1)).strftime(mapping["date_format"])   # noqa: E731
    hhmm = lambda minutes: dt.datetime.strptime(min_to_hhmm(minutes), "%H:%M").strftime(tfmt)   # noqa: E731

    orders = []
    for n, o in enumerate(inst.orders, start=1):
        lat, lon = to_latlon(geo, o.x, o.y)
        orders.append({oc["id"]: o.id, oc["date"]: date_of(o.day), oc["branch"]: ocode["branch"][o.branch],
                       oc["work_type"]: ocode["work_type"][o.work_type], oc["media"]: ocode["media"][o.media],
                       oc["difficulty"]: ocode["difficulty"][o.difficulty],
                       oc["building_type"]: ocode["building_type"][o.building_type], oc["desired"]: hhmm(o.desired),
                       oc["lat"]: lat, oc["lon"]: lon, "고객명": f"고객{seed:02d}{n:05d}", "연락처": f"010-0000-{n % 10000:04d}"})
    workers = []
    for w in inst.workers:
        lat, lon = to_latlon(geo, w.x, w.y)
        workers.append({wc["id"]: w.id, wc["branch"]: wcode["branch"][w.branch],
                        wc["skills"]: sep.join(wcode["skills"][s] for s in w.skills),
                        wc["certs"]: sep.join(wcode["certs"][c] for c in w.certs), wc["cei"]: w.cei,
                        wc["lat"]: lat, wc["lon"]: lon, wc["start"]: hhmm(w.available[0]), wc["end"]: hhmm(w.available[1])})

    manual, acts = random.Random(f"{seed}-legacy-manual"), random.Random(f"{seed}-legacy-actuals")
    yes, no = (_reverse(mapping["assignments"]["codes"]["cancelled"])[v] for v in (True, False))
    by_branch = {}
    for w in inst.workers:
        by_branch.setdefault(w.branch, []).append(w.id)
    rows, assigned, overrides = [], 0, 0
    for d in decisions:
        order = inst.order_index[d.item_id]
        wid = start_min = None
        if d.status in R.PLACED_STATUSES and d.decision:
            wid, start_min = d.decision["worker_id"], R.parse_start(d.decision)
            assigned += 1
            if manual.random() < manual_share:   # 상황실 수동 변경: 같은 지점의 다른 기사
                wid = manual.choice([x for x in by_branch[order.branch] if x != wid])
                overrides += 1
        row = {ac["order"]: order.id, ac["worker"]: wid or "", ac["start"]: hhmm(start_min) if wid else "",
               ac["arrival"]: "", ac["end"]: "", ac["cancelled"]: no}
        if wid:
            if acts.random() < 0.03:
                row[ac["cancelled"]] = yes
            else:
                arrival = start_min + round(acts.gauss(0, 8))
                row[ac["arrival"]] = hhmm(arrival)
                row[ac["end"]] = hhmm(arrival + round(R.duration_min(order, params) * acts.uniform(0.8, 1.4)))
        rows.append(row)
    _write(out / mapping["files"]["orders"], orders)
    _write(out / mapping["files"]["workers"], workers)
    _write(out / mapping["files"]["assignments"], rows)
    return {"orders": len(orders), "workers": len(workers), "assigned": assigned, "manual_overrides": overrides,
            "period": [date_of(1), date_of(inst.days)]}
