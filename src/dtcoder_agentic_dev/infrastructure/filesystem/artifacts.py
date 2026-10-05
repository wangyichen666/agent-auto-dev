import hashlib

from dtcoder_agentic_dev.domain.errors import TechnicalError
from dtcoder_agentic_dev.domain.models import Artifact
from dtcoder_agentic_dev.domain.paths import contained_path


class FileArtifactStore:
    def __init__(self, clock, ids):
        self.clock, self.ids = clock, ids

    def read_text(self, workspace, relative_path):
        try:
            return contained_path(workspace, relative_path).read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise TechnicalError(f"产物不存在或无法读取：{relative_path}") from exc

    def ensure_parent(self, workspace, relative_path):
        contained_path(workspace, relative_path).parent.mkdir(parents=True, exist_ok=True)

    def capture(self, workspace, relative_path, *, run_id, step_name, kind):
        path = contained_path(workspace, relative_path)
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise TechnicalError(f"产物不存在或不可读：{relative_path}") from exc
        if not content.strip():
            raise TechnicalError(f"产物为空：{relative_path}")
        return Artifact(
            self.ids.new(),
            run_id,
            step_name,
            kind,
            relative_path,
            hashlib.sha256(content).hexdigest(),
            created_at=self.clock.now(),
        )
