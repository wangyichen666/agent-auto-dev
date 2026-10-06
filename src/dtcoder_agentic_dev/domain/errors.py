"""以类型表达恢复和业务语义，禁止根据异常字符串迁移状态。"""


class AgenticDevError(Exception):
    """应用错误基类。"""

    code = "AGENTIC_DEV_ERROR"


class ConfigurationError(AgenticDevError):
    """配置或定义无效。"""

    code = "CONFIGURATION_INVALID"


class TechnicalError(AgenticDevError):
    """可按策略重试的技术故障。"""

    code = "TECHNICAL_ERROR"


class BusinessError(AgenticDevError):
    """业务阻断，交由节点图处理。"""

    code = "BUSINESS_ERROR"


class FatalError(AgenticDevError):
    """本次运行不可恢复的错误。"""

    code = "FATAL_ERROR"


class ExternalCommandError(TechnicalError):
    code = "COMMAND_FAILED"

    def __init__(
        self,
        message: str,
        *,
        returncode: int | None = None,
        stdout: str = "",
        stderr: str = "",
        duration_seconds: float | None = None,
    ):
        super().__init__(message)
        self.returncode = returncode
        self.stdout, self.stderr = stdout, stderr
        self.duration_seconds = duration_seconds


class ExternalCommandTimeout(ExternalCommandError):
    """命令执行超时。"""

    code = "TIMEOUT"


class ConcurrencyConflict(AgenticDevError):
    """租约或乐观版本冲突。"""

    code = "CONCURRENCY_CONFLICT"


class CapabilityNotConfigured(FatalError):
    """所需外部能力未装配。"""

    code = "CAPABILITY_NOT_CONFIGURED"
