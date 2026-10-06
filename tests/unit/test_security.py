import pytest

from dtcoder_agentic_dev.domain.security import redact, redact_text


@pytest.mark.parametrize(
    "value,secret",
    [
        ("Authorization: Bearer abc-token", "abc-token"),
        ("Cookie: session=private-cookie; x=y", "private-cookie"),
        ("token=private-token", "private-token"),
        ("https://name:private-password@example.invalid/a?token=private-query", "private-password"),
        ("AK=private-key SK=private-secret", "private-key"),
        ('{"password": "json-secret"}', "json-secret"),
    ],
)
def test_redacts_common_credentials(value, secret):
    assert secret not in redact_text(value)


def test_recursive_redaction():
    assert redact({"access_token": "private", "nested": [{"password": "private"}]}) == {
        "access_token": "<已隐藏>",
        "nested": [{"password": "<已隐藏>"}],
    }


@pytest.mark.parametrize(
    "value,secret",
    [
        ('password="secret with spaces"', "with spaces"),
        ("AWS_ACCESS_KEY_ID=cloud-private-id", "cloud-private-id"),
        ('{"api_token": "json-private-value"}', "json-private-value"),
    ],
)
def test_redacts_quoted_values_and_prefixed_secret_keys(value, secret):
    assert secret not in redact_text(value)
