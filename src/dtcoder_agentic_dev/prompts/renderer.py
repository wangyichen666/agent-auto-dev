import hashlib
from dataclasses import dataclass
from pathlib import Path
from string import Formatter

from dtcoder_agentic_dev.domain.errors import TechnicalError


@dataclass(frozen=True)
class RenderedPrompt:
    text: str
    sha256: str
    template_path: str


class StrictPromptRenderer:
    def __init__(self, directory: str):
        self.directory = Path(directory).resolve()

    def render(self, name: str, variables: dict) -> RenderedPrompt:
        path = (self.directory / f"{name}.txt").resolve()
        if path.parent != self.directory:
            raise TechnicalError("模板名称无效")
        try:
            template = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise TechnicalError(f"模板不存在或无法读取：{path}") from exc
        if not template.strip():
            raise TechnicalError(f"模板为空：{path}")
        try:
            for _, key, spec, conversion in Formatter().parse(template):
                if key is not None:
                    if not key.isidentifier() or spec or conversion:
                        raise TechnicalError(f"模板只支持简单变量：{path}")
                    if key not in variables:
                        raise TechnicalError(f"模板缺少变量 {key}：{path}")
            rendered = template.format_map(variables)
        except (KeyError, ValueError) as exc:
            raise TechnicalError(f"模板格式错误：{path}") from exc
        return RenderedPrompt(
            rendered, hashlib.sha256(template.encode("utf-8")).hexdigest(), str(path)
        )
