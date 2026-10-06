"""节点输出写入文件，状态仅保存相对路径、摘要和有界摘要。"""

import json
from hashlib import sha256
from pathlib import Path

from dtcoder_agentic_dev.domain.paths import contained_path
from dtcoder_agentic_dev.domain.security import redact
from dtcoder_agentic_dev.infrastructure.filesystem.atomic import atomic_write


class FileExecutionJournal:
    def __init__(self, root):
        self.root = Path(root).resolve()

    def save(self, run_id, job, attempt_number, execution):
        execution = redact(execution)
        relative = f"{run_id}/{job}/attempt-{attempt_number}"
        directory = contained_path(str(self.root), relative)
        directory.mkdir(parents=True, exist_ok=True)
        files = {}
        for name in ("stdout", "stderr"):
            content = execution.get(name, "")
            atomic_write(directory / f"{name}.txt", content)
            files[name] = {
                "path": f"{relative}/{name}.txt",
                "sha256": sha256(content.encode()).hexdigest(),
            }
        metadata = {k: v for k, v in execution.items() if k not in {"stdout", "stderr"}}
        atomic_write(directory / "execution.json", json.dumps(metadata, ensure_ascii=False))
        return {"schema_version": 1, "files": files}
