"""无仓工作区身份检查；不创建伪 Git 仓库。"""

import json
import shutil
from pathlib import Path

from dtcoder_agentic_dev.domain.errors import FatalError
from dtcoder_agentic_dev.domain.paths import contained_path
from dtcoder_agentic_dev.infrastructure.filesystem.atomic import atomic_write


class LocalTaskWorkspace:
    def __init__(self, root):
        self.root = Path(root).resolve()

    def _path(self, run):
        raw = self.root / run.run_id
        if raw.is_symlink():
            raise FatalError("任务工作区不能是符号链接")
        path = contained_path(str(self.root), run.run_id)
        if run.workspace_path and Path(run.workspace_path).resolve() != path:
            raise FatalError("无仓任务工作区身份不匹配")
        return path

    def _owner(self, run, path):
        marker = path / ".workflow/owner.json"
        if marker.is_symlink() or marker.parent.is_symlink():
            raise FatalError("任务身份文件不能是符号链接")
        try:
            owner = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise FatalError("任务工作区缺少可靠身份记录") from exc
        if owner != {"schema_version": 1, "run_id": run.run_id}:
            raise FatalError("任务工作区归属不匹配")

    def prepare(self, run):
        path = self._path(run)
        if path.exists():
            self._owner(run, path)
        else:
            path.mkdir(parents=True, exist_ok=False)
            marker = path / ".workflow/owner.json"
            marker.parent.mkdir()
            atomic_write(marker, json.dumps({"schema_version": 1, "run_id": run.run_id}))
        return str(path)

    def cleanup(self, run):
        path = self._path(run)
        if path.exists():
            self._owner(run, path)
            shutil.rmtree(path)
