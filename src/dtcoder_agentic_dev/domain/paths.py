"""与平台无关的工作区路径约束。"""

from pathlib import Path

from dtcoder_agentic_dev.domain.errors import TechnicalError


def contained_path(root: str, relative: str) -> Path:
    base = Path(root).resolve()
    path = (base / relative).resolve()
    if path == base or base not in path.parents:
        raise TechnicalError("产物路径必须位于当前运行工作区内")
    return path
