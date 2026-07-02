import pytest

from testgap.cost import BudgetExceeded, CostTracker


def test_records_within_budget():
    tracker = CostTracker(max_cost_per_run=1.0)
    tracker.record(label="f1", cost_usd=0.3, input_tokens=100, output_tokens=50)
    tracker.record(label="f2", cost_usd=0.2)

    assert tracker.spent == 0.5
    assert tracker.remaining == 0.5
    assert tracker.near_limit() is False


def test_near_limit_threshold():
    tracker = CostTracker(max_cost_per_run=1.0)
    tracker.record(label="f1", cost_usd=0.8)
    assert tracker.near_limit() is True


def test_would_exceed():
    tracker = CostTracker(max_cost_per_run=1.0)
    tracker.record(label="f1", cost_usd=0.6)
    assert tracker.would_exceed(0.5) is True
    assert tracker.would_exceed(0.3) is False


def test_raises_when_budget_exceeded():
    tracker = CostTracker(max_cost_per_run=1.0)
    tracker.record(label="f1", cost_usd=0.9)
    with pytest.raises(BudgetExceeded):
        tracker.record(label="f2", cost_usd=0.2)


def test_zero_cost_runs_dont_advance_budget():
    tracker = CostTracker(max_cost_per_run=0.5)
    for i in range(5):
        tracker.record(label=f"f{i}", cost_usd=0.0)
    assert tracker.spent == 0.0
    assert tracker.remaining == 0.5


def test_zero_budget_is_unlimited():
    """TG-401 D1: ``max_cost_per_run=0`` means "no cap" (Ollama-friendly)."""
    tracker = CostTracker(max_cost_per_run=0)
    # Massive cost must not raise.
    tracker.record(label="f", cost_usd=999.0)
    assert tracker.spent == 999.0
    assert tracker.remaining == float("inf")
    assert tracker.would_exceed(10_000.0) is False
    # ``near_limit`` should return False so ``run_diff`` never bails out.
    assert tracker.near_limit(0.8) is False
