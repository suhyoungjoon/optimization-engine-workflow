import time

import pytest

from workflow.compare import BudgetExceeded, compare_cases, summarize


def _row(before, after, violations=0, fail=(0.5, 0.25)):
    return {"seed": 0, "faults": [], "before": before, "after": after, "violations_after": violations,
            "slices": {"F1": {"before": {"fail_rate": fail[0]}, "after": {"fail_rate": fail[1]}}}}


def test_summarize_means_and_counts():
    rows = [_row({"a": 0.5, "b": 1.0}, {"a": 0.6, "b": 0.9}),
            _row({"a": 0.5, "b": 1.0}, {"a": 0.4, "b": 1.0}, violations=2),
            _row({"a": 0.5}, {"a": 0.8})]                      # b가 없는 케이스: b는 집계하지 않는다
    s = summarize(rows)
    assert s["cases"] == 3 and s["violations"] == 2 and list(s["metrics"]) == ["a"]
    a = s["metrics"]["a"]
    assert a["delta_mean"] == pytest.approx(0.1) and (a["delta_min"], a["delta_max"]) == (pytest.approx(-0.1), 0.3)
    assert (a["improved"], a["worsened"]) == (2, 1)
    assert s["slices"]["F1"] == {"before": 0.5, "after": 0.25}


def test_budget_is_checked_between_cases():
    with pytest.raises(BudgetExceeded):
        compare_cases(None, {}, {}, [({"seed": 1, "faults": []}, None)], deadline=time.monotonic() - 1)


def test_budget_is_checked_after_the_last_case(monkeypatch):
    def slow(*args, **kwargs):
        time.sleep(0.05)
        return {"before": {}, "after": {}, "violations_after": 0, "slices": {}, "seconds": 0.05}

    monkeypatch.setattr("workflow.compare.simulate_params", slow)
    with pytest.raises(BudgetExceeded):
        compare_cases(None, {}, {}, [({"seed": 1, "faults": []}, None)], deadline=time.monotonic() + 0.01)
