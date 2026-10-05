from importlib.resources import files
from pathlib import Path

from dtcoder_agentic_dev.adapters.persistence.sqlite import SQLiteRunRepository
from dtcoder_agentic_dev.config import DEFAULT_CONFIG, load_config


def initialize(path=None):
    target = Path(path or DEFAULT_CONFIG).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        text = (
            files("dtcoder_agentic_dev.resources")
            .joinpath("config.example.yaml")
            .read_text(encoding="utf-8")
        )
        target.write_text(text, encoding="utf-8")
    config = load_config(target)
    for directory in (
        config.state.directory,
        config.workspace.mirrors,
        config.workspace.workspaces,
        config.prompts.directory,
        str(Path(config.state.directory) / "logs/runs"),
    ):
        Path(directory).mkdir(parents=True, exist_ok=True)
    template_resources = files("dtcoder_agentic_dev.prompts").joinpath("templates")
    for name in ("requirements", "coding", "code_review", "fix_loop", "pr_create"):
        destination = Path(config.prompts.directory) / f"{name}.txt"
        if not destination.exists():
            destination.write_text(
                template_resources.joinpath(f"{name}.txt").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
    database = SQLiteRunRepository(config.state.database)
    database.close()
    (Path(config.state.directory) / "logs/scheduler.log").touch(exist_ok=True)
    return target
