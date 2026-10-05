from copy import deepcopy

from dtcoder_agentic_dev.domain.workflow import StepOutcome


def checkpoint_context(context: dict, step: str, outcome: StepOutcome, head: str | None) -> dict:
    snapshot = deepcopy(context)
    snapshot.setdefault("outputs", {})[step] = {
        "type": outcome.type.value,
        "facts": deepcopy(outcome.facts),
        "external_refs": deepcopy(outcome.external_refs),
    }
    snapshot.update(deepcopy(outcome.facts))
    snapshot.update(deepcopy(outcome.external_refs))
    snapshot["git_head"] = head
    snapshot["next_poll_at"] = outcome.next_poll_at
    snapshot.pop("technical_failures", None)
    snapshot.get("recovery_inputs", {}).pop(step, None)
    return snapshot
