from dataclasses import dataclass

from dtcoder_agentic_dev.domain.models import Artifact, ExternalOperation, StepAttempt, WorkflowRun


@dataclass(frozen=True)
class RunDetails:
    run: WorkflowRun
    attempts: list[StepAttempt]
    artifacts: list[Artifact]
    operations: list[ExternalOperation]


@dataclass(frozen=True)
class Diagnostic:
    component: str
    ok: bool
    detail: str
    status: str = ""
