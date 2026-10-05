from dtcoder_agentic_dev.domain.errors import CapabilityNotConfigured


class DisabledPipelineAdapter:
    def trigger(self, repository_id, branch, idempotency_key):
        raise CapabilityNotConfigured("流水线能力未配置")

    def get_status(self, repository_id, external_id):
        raise CapabilityNotConfigured("流水线能力未配置")

    def cancel(self, repository_id, external_id):
        raise CapabilityNotConfigured("流水线能力未配置")
