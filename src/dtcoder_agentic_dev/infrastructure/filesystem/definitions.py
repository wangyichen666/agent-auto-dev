"""运行定义原子快照；恢复只信任已保存且摘要一致的副本。"""

from hashlib import sha256
from pathlib import Path

from dtcoder_agentic_dev.domain.errors import ConfigurationError
from dtcoder_agentic_dev.domain.paths import contained_path
from dtcoder_agentic_dev.infrastructure.filesystem.atomic import atomic_write


class WorkflowDefinitionStore:
    def __init__(self, root, skills_directory=None):
        self.root = Path(root).resolve()
        self.skills = Path(skills_directory).resolve() if skills_directory else None

    def save(self, run_id, original, resolved):
        directory = contained_path(str(self.root), run_id)
        directory.mkdir(parents=True, exist_ok=False)
        reference = {"schema_version": 1}
        for name, content in (("workflow.yaml", original), ("resolved_workflow.yaml", resolved)):
            atomic_write(directory / name, content)
            reference[name] = {
                "path": f"{run_id}/{name}",
                "sha256": sha256(content.encode()).hexdigest(),
            }
        return reference

    def read(self, run_id, reference):
        if reference.get("schema_version") != 1:
            raise ConfigurationError("工作流快照 schema version 不支持")
        contents = []
        for name in ("workflow.yaml", "resolved_workflow.yaml"):
            entry = reference.get(name, {})
            if entry.get("path") != f"{run_id}/{name}":
                raise ConfigurationError("工作流快照身份与 run 不一致")
            try:
                content = contained_path(str(self.root), entry["path"]).read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                raise ConfigurationError("工作流快照缺失或不可读，拒绝推测恢复") from exc
            if sha256(content.encode()).hexdigest() != entry.get("sha256"):
                raise ConfigurationError("工作流快照摘要不匹配，拒绝执行被修改的历史定义")
            contents.append(content)
        return tuple(contents)

    def read_skill(self, relative_path):
        if self.skills is None:
            raise ConfigurationError("skill 目录未配置")
        try:
            path = contained_path(str(self.skills), relative_path)
            if path.stat().st_size > 1024 * 1024:
                raise ConfigurationError("skill 文件超过 1 MiB 限制")
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise ConfigurationError("skill 文件不存在或不是 UTF-8 文本") from exc
