"""以类型表达恢复和业务语义，禁止根据异常字符串迁移状态。"""


class AgenticDevError(Exception):
    """应用错误基类。"""


class ConfigurationError(AgenticDevError):
    """配置或定义无效。"""


class TechnicalError(AgenticDevError):
    """可按策略重试的技术故障。"""


class BusinessError(AgenticDevError):
    """业务阻断，交由节点图处理。"""


class FatalError(AgenticDevError):
    """本次运行不可恢复的错误。"""


class ExternalCommandError(TechnicalError):
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


class ConcurrencyConflict(AgenticDevError):
    """租约或乐观版本冲突。"""


class CapabilityNotConfigured(FatalError):
    """所需外部能力未装配。"""
