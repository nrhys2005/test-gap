from testgap.validator.result import TestCaseResult, TestOutcome, ValidatorResult


def test_all_passed_true_when_every_case_passes():
    result = ValidatorResult(
        cases=[
            TestCaseResult(name="t1", outcome=TestOutcome.PASS),
            TestCaseResult(name="t2", outcome=TestOutcome.PASS),
        ]
    )
    assert result.all_passed is True


def test_all_passed_false_when_any_fails():
    result = ValidatorResult(
        cases=[
            TestCaseResult(name="t1", outcome=TestOutcome.PASS),
            TestCaseResult(name="t2", outcome=TestOutcome.FAIL),
        ]
    )
    assert result.all_passed is False
    assert len(result.passed) == 1
    assert len(result.failed) == 1


def test_empty_result_is_not_passed():
    assert ValidatorResult().all_passed is False
