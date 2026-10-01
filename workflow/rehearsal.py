"""리허설: API 키 없이 가짜 LLM으로 워크플로우를 한 바퀴 돌린다. 수치는 AI 품질과 무관하다 (흐름 확인용).

- 분석 agent: 집계 도구로 B지점 오전, C지점 승주, 관할 경계 여부 구간을 조회하고 발견 3개(F1~F3)를 낸다.
- 개선 agent: params를 조회하고 경계 지역 구간 조건을 한 번 시뮬레이션한 뒤 네 안을 낸다 (solver면 다섯).
  1) 경계 지역만 3단계 지역 범위 +1km (valid): 경계 지역 수요(P4)가 있는 케이스에서만 효과, 희망시간 일치율 하락이 크다
  2) 3단계 시간 허용 오차 60→75분 (valid): 결함 조합이 달라도 효과가 남는다
  3) 전역 9km (허용 범위 밖, invalid) 4) 명세 수정 (unsupported)
  5) solver 엔진만: 희망시각 차이 감점 5→8 (목적함수 가중치)

가짜 LLM은 tests/fake_llm.py(코어 레포 tests/의 사본)를 쓰므로 레포 루트에서 실행한다.
"""

import json

from tests.fake_llm import FakeLLM, tool_use

BOUNDARY_RULE = {"when": {"area_zone": ["boundary"]}, "set": {"matching.area_extension_km[2]": 4}}
ANALYST_QUERIES = [
    ("aggregate", {"group_by": ["branch", "hour"], "filters": {"branch": ["B"], "hour": ["09", "10"]}}),
    ("aggregate", {"group_by": ["branch", "difficulty"], "filters": {"branch": ["C"], "difficulty": ["pole"]}}),
    ("aggregate", {"group_by": ["area_zone"]}),
]


def _tool_calls(messages):
    return [b for m in messages if m["role"] == "assistant" for b in m["content"] if b.get("type") == "tool_use"]


def _tool_outputs(messages):
    return [json.loads(b["content"]) for m in messages if m["role"] == "user" and isinstance(m["content"], list)
            for b in m["content"] if b.get("type") == "tool_result" and not b.get("is_error")]


def _analyst(n, messages):
    if n < len(ANALYST_QUERIES):
        return tool_use(*ANALYST_QUERIES[n])
    ids = [c["id"] for c in _tool_calls(messages)]
    b_am, c_pole, zones = _tool_outputs(messages)
    zone = {r["area_zone"]: r for r in zones["rows"]}["boundary"]
    return tool_use("submit_report", {"summary": "리허설: 가짜 분석 agent의 리포트", "findings": [
        {"title": "B지점 오전 실패 집중", "slice": {"branch": ["B"], "hour": ["09", "10"]},
         "reason_codes": ["CAPACITY"], "description": f"{b_am['items']}건 중 {b_am['failed']}건 실패",
         "hypothesis": "오전 수요가 몰려 용량이 부족하다", "cited_calls": [ids[0]]},
        {"title": "C지점 승주 작업 미할당", "slice": {"branch": ["C"], "difficulty": ["pole"]},
         "reason_codes": ["NO_CERT"], "description": f"{c_pole['items']}건 중 {c_pole['failed']}건 실패",
         "hypothesis": "C지점에 승주 자격자가 없다", "cited_calls": [ids[1]]},
        {"title": "경계 지역 실패", "slice": {"area_zone": ["boundary"]}, "reason_codes": ["OUT_OF_AREA"],
         "description": f"{zone['items']}건 중 {zone['failed']}건 실패",
         "hypothesis": "3단계 지역 범위가 경계 지역 수요에 비해 좁다", "cited_calls": [ids[2]]},
    ]})


SOLVER_WEIGHT = {"params_changes": [{"path": "objective.time_diff_per_min", "value": 8}]}


def _proposer(n, messages):
    if n == 0:
        return tool_use("get_params", {})
    if n == 1:
        return tool_use("simulate_params", {"override_rules": [BOUNDARY_RULE]})
    params = _tool_outputs(messages)[0]["params"]
    solver_only = [   # solver 엔진(objective 섹션이 있을 때)에만: 목적함수 가중치 조정
        {"title": "희망시각 차이 감점 5→8", "kind": "params", "target_findings": ["F1"],
         "rationale": "배정 수를 크게 줄이지 않으면서 희망시간 일치를 더 지키게 한다",
         "expected_effect": "희망시간 일치율 상승, 할당 소폭 하락", **SOLVER_WEIGHT},
    ] if "objective" in params else []
    return tool_use("submit_proposals", {"proposals": [
        {"title": "경계 지역만 3단계 지역 범위 +1km", "kind": "params", "target_findings": ["F3"],
         "rationale": "경계 지역 실패는 대부분 OUT_OF_AREA. 전역 완화 대신 경계 구간에만 적용한다",
         "expected_effect": "경계 지역 할당 증가, 다른 구간 영향 최소", "override_rules": [BOUNDARY_RULE]},
        {"title": "3단계 시간 허용 오차 60→75분", "kind": "params", "target_findings": ["F1"],
         "rationale": "오전 수요 집중 구간의 용량 부족을 시간 완화로 흡수한다",
         "expected_effect": "할당 소폭 증가, 희망시간 일치율 소폭 하락",
         "params_changes": [{"path": "matching.time_window_min[2]", "value": 75}]},
        {"title": "전역 3단계 지역 범위 대폭 완화", "kind": "params", "target_findings": ["F3"],
         "rationale": "허용 범위 검사를 보여주기 위한 과도한 제안",
         "params_changes": [{"path": "matching.area_extension_km[2]", "value": 9}]},
        {"title": "예외 처리: 경계 지역은 3단계까지 시도", "kind": "spec", "target_findings": ["F3"],
         "rationale": "명세 수정 안은 이 워크플로우에서 검증하지 않음을 보여주기 위한 제안",
         "spec_edits": [{"section": "예외 처리",
                         "text": "- 관할 경계 지역 지시서는 3단계 매칭까지 모두 시도한 뒤에만 미배정으로 남긴다"}]},
        *solver_only,
    ]})


def policy(item, n, messages, tools):
    # FakeLLM의 n은 문자열 메시지 대화 전체를 하나로 센다. 분석·제안 대화를 따로 세려고 이 대화의 턴 수를 쓴다
    n = sum(1 for m in messages if m["role"] == "assistant")
    names = {t["name"] for t in tools}
    if "submit_report" in names:
        return _analyst(n, messages)
    if "submit_proposals" in names:
        return _proposer(n, messages)
    raise AssertionError(f"리허설 정책이 모르는 호출: {sorted(names)}")


def rehearsal_llm() -> FakeLLM:
    return FakeLLM(policy)
