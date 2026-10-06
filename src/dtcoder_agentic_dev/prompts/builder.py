"""统一上下文注入；模板内容在提交时冻结，运行时只渲染快照。"""

import hashlib
import json
from importlib.resources import files
from pathlib import Path
from string import Formatter

from dtcoder_agentic_dev.domain.errors import ConfigurationError


class TaskPromptBuilder:
    def __init__(self, directory):
        self.directory = Path(directory).resolve()

    def freeze(self, language="zh"):
        if language not in {"zh", "en"}:
            raise ConfigurationError("任务 language 必须是 zh 或 en")
        path = self.directory / f"agent_{language}.txt"
        if path.is_symlink():
            raise ConfigurationError("拒绝读取符号链接 agent 模板")
        if path.exists():
            if path.stat().st_size > 64 * 1024:
                raise ConfigurationError("agent 模板不能超过 64 KiB")
            text = path.read_text(encoding="utf-8")
        else:
            text = (
                files("dtcoder_agentic_dev.prompts")
                .joinpath(f"templates/agent_{language}.txt")
                .read_text(encoding="utf-8")
            )
        result = {
            "schema_version": 1,
            "language": language,
            "text": text,
            "sha256": hashlib.sha256(text.encode()).hexdigest(),
        }
        self.build("校验", {}, result)
        return result

    def build(self, instructions, context, template):
        text = template.get("text")
        if (
            template.get("schema_version") != 1
            or not isinstance(text, str)
            or not text.strip()
            or hashlib.sha256(text.encode()).hexdigest() != template.get("sha256")
        ):
            raise ConfigurationError("agent 模板快照无效")
        keys = []
        try:
            for _, key, spec, conversion in Formatter().parse(text):
                if key is not None:
                    if key not in {"instructions", "context"} or spec or conversion:
                        raise ConfigurationError("agent 模板只支持 instructions 和 context")
                    keys.append(key)
            if not {"instructions", "context"}.issubset(keys):
                raise ConfigurationError("agent 模板必须包含 instructions 和 context")
            return text.format_map(
                {
                    "instructions": instructions,
                    "context": json.dumps(context, ensure_ascii=False, indent=2),
                }
            )
        except ValueError as exc:
            raise ConfigurationError("agent 模板格式无效") from exc
