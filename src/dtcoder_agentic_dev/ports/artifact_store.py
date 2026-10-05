from typing import Protocol

from dtcoder_agentic_dev.domain.models import Artifact


class ArtifactStore(Protocol):
    def read_text(self, workspace: str, relative_path: str) -> str: ...
    def ensure_parent(self, workspace: str, relative_path: str) -> None: ...

    def capture(
        self, workspace: str, relative_path: str, *, run_id: str, step_name: str, kind: str
    ) -> Artifact: ...
