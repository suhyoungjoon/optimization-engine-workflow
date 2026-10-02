import pytest

from workflow.state import (ANALYZED, APPLIED, AWAITING_APPROVAL, FAILED, FINAL, PROPOSED, REJECTED, RUNNING,
                            SEARCHED, VALIDATED, TransitionError, check_transition)


def test_happy_path_in_order():
    path = [RUNNING, ANALYZED, PROPOSED, SEARCHED, VALIDATED, AWAITING_APPROVAL, APPLIED]
    for a, b in zip(path, path[1:]):
        check_transition(a, b)
    check_transition(AWAITING_APPROVAL, REJECTED)


@pytest.mark.parametrize("a,b", [
    (RUNNING, PROPOSED),               # 단계 건너뛰기
    (PROPOSED, VALIDATED),             # 탐색 건너뛰기
    (VALIDATED, APPLIED),              # 승인 대기 없이 적용 (자동 승인)
    (AWAITING_APPROVAL, FAILED),       # 승인 대기에서 나가는 길은 승인·반려뿐
    (APPLIED, AWAITING_APPROVAL),
    (REJECTED, APPLIED),
])
def test_blocked_transitions(a, b):
    with pytest.raises(TransitionError):
        check_transition(a, b)


def test_every_intermediate_stage_can_fail_and_final_states_are_terminal():
    for s in (RUNNING, ANALYZED, PROPOSED, SEARCHED, VALIDATED):
        check_transition(s, FAILED)
    assert FINAL == {APPLIED, REJECTED, FAILED}
