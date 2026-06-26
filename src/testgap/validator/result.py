from dataclasses import dataclass, field
from enum import Enum
from typing import ClassVar


class TestOutcome(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    ERROR = "error"
    SKIP = "skip"


TestOutcome.__test__ = False  # type: ignore[attr-defined]


@dataclass
class TestCaseResult:
    __test__: ClassVar[bool] = False  # not a pytest test class
    name: str
    outcome: TestOutcome
    message: str = ""


@dataclass
class ValidatorResult:
    cases: list[TestCaseResult] = field(default_factory=list)
    duration_seconds: float = 0.0
    raw_stdout: str = ""
    raw_stderr: str = ""
    exit_code: int = 0
    environment_error: str | None = None

    @property
    def all_passed(self) -> bool:
        return bool(self.cases) and all(c.outcome == TestOutcome.PASS for c in self.cases)

    @property
    def passed(self) -> list[TestCaseResult]:
        return [c for c in self.cases if c.outcome == TestOutcome.PASS]

    @property
    def failed(self) -> list[TestCaseResult]:
        return [c for c in self.cases if c.outcome in (TestOutcome.FAIL, TestOutcome.ERROR)]
