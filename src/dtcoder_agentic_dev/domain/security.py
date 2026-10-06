"""日志与快照共用的脱敏边界。"""

import re
from urllib.parse import urlsplit, urlunsplit

SECRET_KEYS = re.compile(
    r"authorization|cookie|password|passwd|token|secret|credential|access[_-]?key|^ak$|^sk$", re.I
)
QUOTED_VALUE = r""""(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*' """.strip()


def redact_text(value: str) -> str:
    def json_field(match):
        if not SECRET_KEYS.search(match["key"]):
            return match[0]
        quote = match["value"][0] if match["value"][0] in "\"'" else '"'
        return match["prefix"] + quote + "<已隐藏>" + quote

    value = re.sub(
        r"""(?P<prefix>["'](?P<key>[^"']+)["']\s*:\s*)(?P<value>""" + QUOTED_VALUE + r"|[^,\s;}]+)",
        json_field,
        value,
    )

    def url(match):
        try:
            parsed = urlsplit(match[0])
            host = parsed.hostname or ""
            if parsed.port:
                host += f":{parsed.port}"
            query = re.sub(
                r"([?&]?(?:[\w-]*(?:token|key|password|secret)[\w-]*)=)[^&\s]+",
                r"\1<已隐藏>",
                parsed.query,
                flags=re.I,
            )
            return urlunsplit((parsed.scheme, host, parsed.path, query, parsed.fragment))
        except ValueError:
            return "<已隐藏的 URL>"

    value = re.sub(r"https?://[^\s<>]+", url, value)
    value = re.sub(
        r"(?i)(authorization\s*[:=]\s*)(?:bearer\s+|basic\s+)?[^\s,;]+",
        r"\1<已隐藏>",
        value,
    )
    value = re.sub(r"(?i)(cookie\s*[:=]\s*)[^\r\n]+", r"\1<已隐藏>", value)
    value = re.sub(
        r"(?i)((?:[\w-]*(?:password|passwd|token|secret|access[_-]?key)[\w-]*|ak|sk)\s*[:=]\s*)("
        + QUOTED_VALUE
        + r"|[^\s,;]+)",
        r"\1<已隐藏>",
        value,
    )
    return value


def redact(value):
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {
            k: "<已隐藏>" if SECRET_KEYS.search(str(k)) else redact(v) for k, v in value.items()
        }
    if isinstance(value, (tuple, list)):
        return [redact(v) for v in value]
    return value
