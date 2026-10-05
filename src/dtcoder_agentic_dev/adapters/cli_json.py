"""平台命令的安全边界：错误只保存类型，不保存远端输出或敏感参数。"""

import hashlib
import json
from urllib.parse import urlsplit

from dtcoder_agentic_dev.domain.errors import (
    CapabilityNotConfigured,
    ExternalCommandError,
    ExternalCommandTimeout,
    TechnicalError,
)


def marker(key):
    return "<!-- dtcoder:" + hashlib.sha256(key.encode()).hexdigest() + " -->"


def json_output(text, *, banner=False):
    try:
        if not banner:
            return json.loads(text)
        decoder = json.JSONDecoder()
        for index, char in enumerate(text):
            if char in "[{":
                try:
                    value, end = decoder.raw_decode(text[index:])
                    if not text[index + end :].strip():
                        return value
                except ValueError:
                    continue
    except (ValueError, TypeError):
        pass
    raise TechnicalError("平台返回非法 JSON")


def unwrap(value):
    while isinstance(value, dict) and isinstance(value.get("result"), dict):
        value = value["result"]
    return value


def items(value):
    value = unwrap(value)
    result = value.get("items") if isinstance(value, dict) else value
    if not isinstance(result, list) or any(not isinstance(v, dict) for v in result):
        raise TechnicalError("平台列表缺少 items 数组")
    # 禁止只扫描第一页后误判不存在。
    if isinstance(value, dict) and (
        value.get("has_more")
        or value.get("next_page")
        or value.get("next_cursor")
        or (isinstance(value.get("total"), int) and value["total"] > len(result))
    ):
        raise CapabilityNotConfigured("CLI 查询结果分页未完整返回；请启用支持 --all 的 CLI 版本")
    return result


def required(value, *names):
    if not isinstance(value, dict):
        raise TechnicalError("平台返回对象类型错误")
    for name in names:
        field = value.get(name)
        if isinstance(field, (str, int)) and not isinstance(field, bool) and str(field).strip():
            return str(field)
    raise TechnicalError("平台返回缺少字段：" + "/".join(names))


class JsonCLI:
    def __init__(self, commands, binary, timeout, profile="", banner=False):
        self.commands, self.binary, self.timeout = commands, binary, timeout
        self.profile, self.banner, self.capabilities = profile, banner, set()

    def command(self, args):
        prefix = [self.binary]
        if self.profile:
            prefix += ["--profile", self.profile]
        return prefix + list(args)

    def execute(self, args):
        try:
            result = self.commands.run(self.command(args), timeout=self.timeout, check=False)
        except ExternalCommandTimeout:
            raise ExternalCommandTimeout("平台命令超时") from None
        except (ExternalCommandError, OSError):
            raise ExternalCommandError("平台命令无法启动") from None
        if result.returncode:
            raise ExternalCommandError("平台命令执行失败", returncode=result.returncode)
        return result.stdout

    def call(self, args):
        value = json_output(self.execute([*args, "--json"]), banner=self.banner)
        if isinstance(value, dict) and (
            value.get("success") is False or value.get("error") or value.get("errcode", 0) != 0
        ):
            raise TechnicalError("平台响应拒绝操作")
        return value

    def require(self, group, flags=()):
        key = (tuple(group), tuple(flags))
        if key in self.capabilities:
            return
        try:
            help_text = self.execute([*group, "--help"])
        except ExternalCommandError:
            raise CapabilityNotConfigured("CLI 缺少所需查询能力；请检查版本及登录状态") from None
        if any(flag not in help_text for flag in flags):
            raise CapabilityNotConfigured(
                "CLI 缺少完整查询/幂等能力；请升级 CLI，参阅 docs/operations.md"
            )
        self.capabilities.add(key)


def safe_url(value):
    if not value:
        return ""
    if not isinstance(value, str):
        raise TechnicalError("平台 URL 类型错误")
    parsed = urlsplit(value)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise TechnicalError("平台返回带敏感参数的 URL，拒绝持久化")
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise TechnicalError("平台返回无效 URL")
    return value
