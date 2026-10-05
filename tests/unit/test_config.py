import pytest
import yaml

from dtcoder_agentic_dev.config import RepoConfig, load_config, redacted_config
from dtcoder_agentic_dev.domain.errors import ConfigurationError


def write(tmp_path, data):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def test_defaults(tmp_path):
    config = load_config(write(tmp_path, {}))
    assert config.state.directory == str(tmp_path)
    assert config.state.database == str(tmp_path / "state.db")
    assert config.prompts.directory == str(tmp_path / "prompts")
    assert config.scheduler.max_nodes_per_tick == 20


def test_expand_and_relative_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_STATE", str(tmp_path / "expanded"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    config = load_config(
        write(
            tmp_path,
            {"state": {"directory": "$TEST_STATE"}, "prompts": {"directory": "~/templates"}},
        )
    )
    assert config.state.database == str(tmp_path / "expanded/state.db")
    assert config.prompts.directory == str(tmp_path / "home/templates")


@pytest.mark.parametrize(
    "data",
    [
        {"scheduler": {"poll_interval": 0}},
        {"codex": {"timeout": -1}},
        {"workflow": {"max_retries": -1}},
        {"workflow": {"name": "unknown"}},
        {"codex": {"output_format": "bad"}},
        {"scheduler": {"lease_seconds": 10}},
        {"scheduler": {"max_nodes_per_tick": True}},
        {"dry_run": "false"},
        {"repositories": {}},
        {"mystery": 1},
        {"state": {"unknown": "a"}},
        {"repositories": [{"name": "r", "remote": ""}]},
        {"repositories": [{"name": "r", "remote": "https://user:password@example.invalid/repo"}]},
        {"repositories": [{"name": "r", "remote": "x", "base_branch": "../bad"}]},
        {"repositories": [{"name": "r", "remote": "x", "path": "/does/not/exist"}]},
        {
            "repositories": [
                {"name": "r", "remote": "x", "validation_commands": ["python -m pytest"]}
            ]
        },
        {"repositories": [{"name": "r", "remote": "x", "pipeline_enabled": True}]},
    ],
)
def test_invalid(tmp_path, data):
    with pytest.raises(ConfigurationError):
        load_config(write(tmp_path, data))


def test_duplicate_repo_and_stable_id(tmp_path):
    a = RepoConfig("a", "https://example.invalid/x", provider="p")
    b = RepoConfig("b", "https://example.invalid/x", provider="p")
    c = RepoConfig("c", "https://example.invalid/x", provider="q")
    assert a.repository_id == b.repository_id != c.repository_id
    with pytest.raises(ConfigurationError):
        load_config(
            write(
                tmp_path,
                {"repositories": [{"name": "a", "remote": "x"}, {"name": "b", "remote": "x"}]},
            )
        )


def test_redaction(tmp_path):
    config = load_config(write(tmp_path, {"codex": {"extra_args": ["private-value"]}}))
    assert "private-value" not in str(redacted_config(config))


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_invalid_nonfinite_intervals(tmp_path, value):
    with pytest.raises(ConfigurationError):
        load_config(write(tmp_path, {"scheduler": {"poll_interval": value}}))


def test_external_adapter_name_can_be_injected(tmp_path):
    config = load_config(
        write(
            tmp_path,
            {
                "pipeline": {"adapter": "custom-ci"},
                "code_host": {"adapter": "custom-host"},
                "notification": {"adapter": "custom-notifier"},
                "repositories": [
                    {
                        "name": "r",
                        "remote": "https://example.invalid/repo",
                        "pipeline_enabled": True,
                    }
                ],
            },
        )
    )
    assert config.pipeline.adapter == "custom-ci"
