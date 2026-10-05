import logging
from pathlib import Path


class ContextFilter(logging.Filter):
    def filter(self, record):
        if not hasattr(record, "run_id"):
            record.run_id = "-"
        if not hasattr(record, "step"):
            record.step = "-"
        return True


FORMAT = "%(asctime)s %(levelname)s %(name)s run_id=%(run_id)s step=%(step)s %(message)s"


def configure_logging(directory: str):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("dtcoder_agentic_dev")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    targets = {"console": None, "scheduler": directory / "scheduler.log"}
    # 一个进程可能打开多个配置，只保留当前状态目录的文件 handler。
    for handler in list(logger.handlers):
        if (
            getattr(handler, "_dtcoder_kind", None) == "scheduler"
            and Path(handler.baseFilename) != targets["scheduler"].resolve()
        ):
            logger.removeHandler(handler)
            handler.close()
    for kind, path in targets.items():
        if any(getattr(h, "_dtcoder_kind", None) == kind for h in logger.handlers):
            continue
        handler = (
            logging.StreamHandler() if path is None else logging.FileHandler(path, encoding="utf-8")
        )
        handler._dtcoder_kind = kind
        handler.setFormatter(logging.Formatter(FORMAT))
        handler.addFilter(ContextFilter())
        logger.addHandler(handler)
    return logger


class RunAuditHandler:
    def __init__(self, directory):
        self.directory = Path(directory)

    def __call__(self, event):
        path = self.directory / "runs" / event.run_id
        path.mkdir(parents=True, exist_ok=True)
        # LoggerAdapter 注入上下文，handler 每次关闭，避免长期运行泄漏描述符。
        logger = logging.getLogger(f"dtcoder_agentic_dev.audit.{event.run_id}")
        handler = logging.FileHandler(path / "run.log", encoding="utf-8")
        handler.setFormatter(logging.Formatter(FORMAT))
        handler.addFilter(ContextFilter())
        logger.addHandler(handler)
        try:
            logging.LoggerAdapter(logger, {"run_id": event.run_id, "step": event.step or "-"}).info(
                event.type.value
            )
        finally:
            logger.removeHandler(handler)
            handler.close()
