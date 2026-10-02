"""CP-SAT 배정 모델. 날짜 × 지점 문제로 나눠 푼다 (작업자는 자기 지점 지시서만, 날짜끼리는 독립).

- 반드시 지키는 조건: 기술·자격, 3단계 매칭 범위(희망시간 ±분, 관할 밖 km, 구간 조건 적용), 가능시간,
  같은 작업자의 작업 겹침(이동시간 포함). 결과는 도메인 팩의 validate()로 따로 검증한다 (핵심 원칙 3).
- 목적함수(최대화): 배정 수 × assign − 희망시각 차이(분) × time_diff_per_min − 거점 거리(km) × home_km
  + 명장 배정 × master. 거점 거리는 순서에 따라 달라지는 이동시간을 대신하는 근사다.
- 결정성: 단일 스레드, 고정 seed, 결정적 시간 한도(max_deterministic_time). 규칙 엔진 해를 시작 힌트로 넣는다.
"""

import math
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

from ortools.sat.python import cp_model

from core import DecisionRecord
from domains.dispatch import rule_engine as R
from domains.dispatch.models import min_to_hhmm

SEED = 0


def _candidates(inst, order, workers, params, dur):
    """(작업자, 시작 하한, 시작 상한): 기술·자격·3단계 지역 범위·희망시간 범위·가능시간을 모두 만족하는 후보."""
    ep = R.effective_params(inst, order, params)
    window, ext = ep["matching"]["time_window_min"][-1], ep["matching"]["area_extension_km"][-1]
    cert = R.REQUIRED_CERT.get(order.difficulty)
    out = []
    for w in workers:
        if order.media not in w.skills or (cert and cert not in w.certs):
            continue
        if inst.distance_outside(w.branch, order.x, order.y) > ext:
            continue
        lo = max(order.desired - window, w.available[0])
        hi = min(order.desired + window, w.available[1] - dur)
        if lo <= hi:
            out.append((w, lo, hi))
    return out


def solve_group(inst, orders, workers, params, hint: dict) -> dict:
    """한 날짜·지점 문제. {지시서 ID: (작업자 ID, 시작 분)}."""
    obj = params["objective"]
    threshold = params["cei"]["master_threshold"]
    m = cp_model.CpModel()
    x, start, dur = {}, {}, {}
    terms = []
    for o in orders:
        dur[o.id] = R.duration_min(o, params)
        for w, lo, hi in _candidates(inst, o, workers, params, dur[o.id]):
            v, s = m.NewBoolVar(f"x_{o.id}_{w.id}"), m.NewIntVar(lo, hi, f"s_{o.id}_{w.id}")
            x[o.id, w.id], start[o.id, w.id] = v, s
            gap = m.NewIntVar(0, 24 * 60, f"d_{o.id}_{w.id}")
            m.Add(gap >= s - o.desired).OnlyEnforceIf(v)
            m.Add(gap >= o.desired - s).OnlyEnforceIf(v)
            m.Add(gap == 0).OnlyEnforceIf(v.Not())
            home = round(math.hypot(w.x - o.x, w.y - o.y) * obj["home_km"])
            gain = obj["assign"] - home + (obj["master"] if w.cei >= threshold else 0)
            terms += [gain * v, -obj["time_diff_per_min"] * gap]
            hinted = hint.get(o.id)
            m.AddHint(v, 1 if hinted and hinted[0] == w.id else 0)
            if hinted and hinted[0] == w.id:
                m.AddHint(s, hinted[1])
    by_order, by_worker = defaultdict(list), defaultdict(list)
    for oid, wid in x:
        by_order[oid].append(wid)
        by_worker[wid].append(oid)
    for oid, wids in by_order.items():
        m.Add(sum(x[oid, w] for w in wids) <= 1)
    index = {o.id: o for o in orders}
    for wid, oids in by_worker.items():   # 같은 작업자의 두 작업은 이동시간을 두고 앞뒤로 놓인다
        for i, a_id in enumerate(oids):
            for b_id in oids[i + 1:]:
                a, b = index[a_id], index[b_id]
                both = [x[a_id, wid], x[b_id, wid]]
                a_first = m.NewBoolVar(f"o_{a_id}_{b_id}_{wid}")
                m.Add(start[b_id, wid] >= start[a_id, wid] + dur[a_id] + R.travel_min(a.x, a.y, b.x, b.y, params)
                      ).OnlyEnforceIf([a_first, *both])
                m.Add(start[a_id, wid] >= start[b_id, wid] + dur[b_id] + R.travel_min(b.x, b.y, a.x, a.y, params)
                      ).OnlyEnforceIf([a_first.Not(), *both])
    m.Maximize(sum(terms))
    solver = cp_model.CpSolver()
    p = solver.parameters
    p.num_workers, p.random_seed, p.max_deterministic_time = 1, SEED, float(obj["time_limit"])
    status = solver.Solve(m)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return {oid: hint[oid] for oid in index if oid in hint}   # 해를 못 찾으면 규칙 엔진 해를 그대로 쓴다
    return {oid: (wid, solver.Value(start[oid, wid])) for (oid, wid), v in x.items() if solver.Value(v)}


def _reason(inst, order, params) -> tuple[str, str]:
    """배정하지 못한 지시서의 사유. 코어 도메인의 사유 코드를 같은 기준으로 가린다."""
    _pool, skilled, certified = R.eligible_workers(inst, order)
    if not skilled:
        return "NO_SKILL", f"{order.media} 기술 보유자 없음"
    if not certified:
        return "NO_CERT", "필요 자격 보유자 없음"
    ep = R.effective_params(inst, order, params)
    window, ext = ep["matching"]["time_window_min"][-1], ep["matching"]["area_extension_km"][-1]
    in_area = [w for w in certified if inst.distance_outside(w.branch, order.x, order.y) <= ext]
    if not in_area:
        return "OUT_OF_AREA", f"최대 완화(+{ext}km) 범위 안에 후보 없음"
    dur = R.duration_min(order, params)
    lo, hi = order.desired - window, order.desired + window
    if any(max(lo, w.available[0]) <= min(hi, w.available[1] - dur) for w in in_area):
        return "CAPACITY", f"희망시간 ±{window}분에 가능한 후보의 일정을 solver가 다른 지시서에 썼음"
    return "NO_TIME_MATCH", f"희망시간 ±{window}분이 후보 가능시간과 맞지 않음"


def solve_instance(inst, params: dict, threads: int) -> list[DecisionRecord]:
    rule = R.solve(inst, params)   # 시작 힌트: solver는 규칙 엔진 해에서 출발한다
    hint = {d.item_id: (d.decision["worker_id"], R.parse_start(d.decision)) for d in rule if d.status == "success"}
    groups = defaultdict(list)
    for o in inst.orders:
        groups[o.day, o.branch].append(o)
    workers = defaultdict(list)
    for w in inst.workers:
        workers[w.branch].append(w)
    keys = sorted(groups)
    with ThreadPoolExecutor(max_workers=max(1, threads)) as pool:   # 문제끼리 독립이라 병렬로 풀어도 결과가 같다
        parts = pool.map(lambda k: solve_group(inst, groups[k], workers[k[1]], params, hint), keys)
        assigned = {oid: v for part in parts for oid, v in part.items()}
    records = []
    for o in sorted(inst.orders, key=lambda o: (o.day, o.desired, o.id)):
        dims = R.dims_of(inst, o)
        if o.id not in assigned:
            code, why = _reason(inst, o, params)
            records.append(DecisionRecord(o.id, None, "failed", code, f"solver: {why}", dims))
            continue
        wid, s = assigned[o.id]
        stage = R.actual_stage(inst, o, inst.worker_index[wid], s, params)
        records.append(DecisionRecord(
            o.id, {"worker_id": wid, "start_time": min_to_hhmm(s), "matching_stage": stage}, "success", None,
            f"solver: {wid} {min_to_hhmm(s)} 시작 (희망 {min_to_hhmm(o.desired)}, {stage}단계 범위)", dims,
            {"matching_stage": float(stage or 0), "time_diff_min": float(abs(s - o.desired))}))
    return records
