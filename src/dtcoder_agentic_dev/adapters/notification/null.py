import logging


class NullNotifier:
    def notify(self, event):
        logging.getLogger(__name__).debug("通知已禁用：%s", event.type.value)
