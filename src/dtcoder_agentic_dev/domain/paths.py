"""与平台无关的工作区路径约束。"""

from pathlib import Path

from dtcoder_agentic_dev.domain.errors import TechnicalError


def contained_path(root: str, relative: str) -> Path:
    supplied = Path(relative)
    if supplied.is_absolute() or ".." in supplied.parts or "\\" in relative:
        raise TechnicalError("产物路径必须是工作区内相对路径，禁止 .. 和绝对路径")
    base = Path(root).resolve()
    path = (base / relative).resolve()
    if path == base or base not in path.parents:
        raise TechnicalError("产物路径必须位于当前运行工作区内")
    return path


def artifact_path(root: str, relative: str) -> Path:
    path = contained_path(root, relative)
    components = (*Path(relative).parts, *path.relative_to(Path(root).resolve()).parts)
    if any(part in {".git", ".workflow"} for part in components):
        raise TechnicalError("文件产物不得读写受管 Git 或任务身份目录")
    return path
