from dataclasses import dataclass, field


class BudgetExceeded(Exception):
    pass


@dataclass
class CostEntry:
    label: str
    cost_usd: float
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class CostTracker:
    max_cost_per_run: float
    entries: list[CostEntry] = field(default_factory=list)

    @property
    def spent(self) -> float:
        return round(sum(e.cost_usd for e in self.entries), 6)

    @property
    def remaining(self) -> float:
        return max(0.0, round(self.max_cost_per_run - self.spent, 6))

    def would_exceed(self, estimated: float) -> bool:
        return (self.spent + estimated) > self.max_cost_per_run

    def near_limit(self, ratio: float = 0.8) -> bool:
        return self.spent >= self.max_cost_per_run * ratio

    def record(
        self, *, label: str, cost_usd: float, input_tokens: int = 0, output_tokens: int = 0
    ) -> CostEntry:
        if self.spent + cost_usd > self.max_cost_per_run:
            raise BudgetExceeded(
                f"recording ${cost_usd:.4f} would exceed budget "
                f"${self.max_cost_per_run:.2f} (already spent ${self.spent:.4f})"
            )
        entry = CostEntry(
            label=label, cost_usd=cost_usd, input_tokens=input_tokens, output_tokens=output_tokens
        )
        self.entries.append(entry)
        return entry
