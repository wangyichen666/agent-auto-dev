"""钉钉卡片适配器；异常不包含 URL、响应或接收人身份。"""

import hashlib
import json
import logging
import os
from urllib.request import Request, urlopen

from dtcoder_agentic_dev.domain.errors import TechnicalError


class UrllibHttpTransport:
    def __init__(self, token_env=""):
        self.token_env = token_env

    def post(self, url, payload, timeout):
        try:
            headers = {"Content-Type": "application/json"}
            if self.token_env:
                token = os.environ.get(self.token_env, "")
                if not token:
                    raise TechnicalError("通知认证环境变量为空")
                headers["x-acs-dingtalk-access-token"] = token
            request = Request(
                url,
                data=json.dumps(payload).encode(),
                headers=headers,
                method="POST",
            )
            with urlopen(request, timeout=timeout) as response:
                if response.status >= 300:
                    raise TechnicalError("通知服务 HTTP 失败")
                body = json.loads(response.read().decode())
                result = body.get("result", {}) if isinstance(body, dict) else {}
                if not isinstance(result, dict):
                    result = {}
                acknowledged = isinstance(body, dict) and (
                    body.get("success") is True
                    or body.get("errcode") == 0
                    or result.get("success") is True
                    or body.get("outTrackId")
                    or result.get("outTrackId")
                    or result.get("cardInstanceId")
                )
                if (
                    not acknowledged
                    or body.get("success") is False
                    or result.get("success") is False
                    or body.get("errcode", 0) != 0
                ):
                    raise TechnicalError("通知服务未确认请求成功")
        except Exception:
            raise TechnicalError("通知 HTTP 请求失败") from None


class DingTalkNotifier:
    def __init__(self, config, views, http=None):
        self.config, self.views = config, views
        self.http = http or UrllibHttpTransport(config.access_token_env)
        self.logger = logging.getLogger(__name__)

    def notify(self, event):
        view = self.views.build(event)
        if view is None:
            return
        candidates = []
        if self.config.recipients in {"assignees", "both"}:
            candidates += view["assignees"]
        if self.config.recipients in {"configured", "both"}:
            candidates += self.config.receiver_ids
        recipients = list(
            dict.fromkeys(
                v.strip().zfill(6) if v.strip().isdigit() else v.strip()
                for v in candidates
                if v.strip()
            )
        )
        if not recipients:
            self.logger.info("通知接收人为空，跳过发送")
            return
        payload = {
            "cardTemplateId": self.config.card_template_id,
            "outTrackId": event.event_id,
            "receiverUserIdList": recipients,
            "cardData": {"cardParamMap": {**view["variables"], "content": view["text"]}},
        }
        payloads = [payload]
        if self.config.api_mode == "direct":
            payloads = [
                {
                    "cardTemplateId": self.config.card_template_id,
                    "outTrackId": hashlib.sha256(
                        (event.event_id + ":" + recipient).encode()
                    ).hexdigest(),
                    "cardData": payload["cardData"],
                    "openSpaceId": "dtv1.card//IM_ROBOT." + recipient,
                    "imRobotOpenSpaceModel": {"supportForward": False},
                    "imRobotOpenDeliverModel": {"spaceType": "IM_ROBOT"},
                }
                for recipient in recipients
            ]
        for card in payloads:
            self._send(card)

    def _send(self, payload):
        for attempt in range(2):
            try:
                self.http.post(self.config.api_url, payload, self.config.timeout)
                return
            except Exception:
                if attempt:
                    raise TechnicalError("通知请求重试后仍失败") from None
