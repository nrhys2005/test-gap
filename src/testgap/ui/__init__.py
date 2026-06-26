"""Interactive UI surfaces for TestGap (separated from CLI dispatch)."""

from testgap.ui.interactive import (
    AppliedFile,
    ReviewOutcome,
    default_editor_fn,
    default_prompt_fn,
    run_review_session,
)

__all__ = [
    "AppliedFile",
    "ReviewOutcome",
    "default_editor_fn",
    "default_prompt_fn",
    "run_review_session",
]
