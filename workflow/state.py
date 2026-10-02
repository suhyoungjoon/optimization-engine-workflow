"""워크플로우 상태 머신 (docs/plan.md 3장).

running → analyzed → proposed → searched → validated → awaiting_approval → applied | rejected
중간 단계 실패는 failed(사유 기록). awaiting_approval에서 나가는 길은 사람의 승인·반려뿐이다.
"""

RUNNING = "running"
ANALYZED = "analyzed"
PROPOSED = "proposed"
SEARCHED = "searched"
VALIDATED = "validated"
AWAITING_APPROVAL = "awaiting_approval"
APPLIED = "applied"
REJECTED = "rejected"
FAILED = "failed"

TRANSITIONS: dict[str, set[str]] = {
    RUNNING: {ANALYZED, FAILED},
    ANALYZED: {PROPOSED, FAILED},
    PROPOSED: {SEARCHED, FAILED},
    SEARCHED: {VALIDATED, FAILED},
    VALIDATED: {AWAITING_APPROVAL, FAILED},
    AWAITING_APPROVAL: {APPLIED, REJECTED},
    APPLIED: set(),
    REJECTED: set(),
    FAILED: set(),
}
STATES = tuple(TRANSITIONS)
FINAL = frozenset(s for s, nxt in TRANSITIONS.items() if not nxt)


class TransitionError(ValueError):
    pass


def check_transition(current: str, new: str) -> None:
    if new not in TRANSITIONS.get(current, set()):
        raise TransitionError(f"{current} → {new} 전이는 허용되지 않는다")
