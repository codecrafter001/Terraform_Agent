"""Reference (data source) blocks use each data source's real lookup argument,
and every terraform/tofu call reuses the shared provider cache."""

import os

import pytest

from agents.graph import _plural
from tools.hcl_generator import DATA_SOURCE_LOOKUP, HCLGenerator
from tools.terraform_runner import _with_cache_config


def _data_block(r_type, res):
    gen = HCLGenerator(job_id="job-ds", region="us-east-1", engine_name="terraform", engine_version="1.8.5")
    return gen._compose_data_source(r_type, res, "x")


@pytest.mark.parametrize("r_type, res, expected", [
    # aws_internet_gateway rejects `id` at validate - the bug seen on a default IGW.
    ("aws_internet_gateway", {"id": "igw-0fe5a09676e826894"}, 'internet_gateway_id = "igw-0fe5a09676e826894"'),
    ("aws_vpc", {"id": "vpc-1"}, 'id = "vpc-1"'),
    ("aws_route_table", {"id": "rtb-1"}, 'route_table_id = "rtb-1"'),
    ("aws_s3_bucket", {"id": "my-bucket"}, 'bucket = "my-bucket"'),
    ("aws_iam_role", {"id": "my-role"}, 'name = "my-role"'),
    ("aws_kms_key", {"id": "k-1", "arn": "arn:aws:kms:us-east-1:1:key/k-1"}, 'key_id = "arn:aws:kms:us-east-1:1:key/k-1"'),
    ("aws_sqs_queue", {"id": "https://sqs.us-east-1.amazonaws.com/1/jobs", "name": "jobs"}, 'name = "jobs"'),
    ("aws_sns_topic", {"id": "arn:aws:sns:us-east-1:1:alerts", "name": "alerts"}, 'name = "alerts"'),
])
def test_data_source_uses_its_lookup_argument(r_type, res, expected):
    block = _data_block(r_type, res)
    assert block.startswith(f'data "{r_type}" "x" {{')
    assert expected in block


def test_internet_gateway_data_source_never_sets_id():
    assert "\n  id =" not in _data_block("aws_internet_gateway", {"id": "igw-1"})


def test_every_discovered_type_has_a_lookup():
    from tools.resource_classifier import DISCOVERY_RESOURCE_TYPES
    assert DISCOVERY_RESOURCE_TYPES <= set(DATA_SOURCE_LOOKUP)


def test_lookup_value_falls_back_to_id_and_is_escaped():
    assert 'key_id = "k-1"' in _data_block("aws_kms_key", {"id": "k-1"})
    assert '\\"' in _data_block("aws_s3_bucket", {"id": 'x"\ny'})


def test_runner_env_reuses_the_provider_cache(tmp_path, monkeypatch):
    cache = tmp_path / "plugin-cache"
    monkeypatch.setenv("TF_PLUGIN_CACHE_DIR", str(cache))
    monkeypatch.delenv("TF_CLI_CONFIG_FILE", raising=False)

    inherited = _with_cache_config(None)
    config = inherited["TF_CLI_CONFIG_FILE"]
    assert inherited["CHECKPOINT_DISABLE"] == "1" and inherited["PATH"] == os.environ["PATH"]
    text = open(config, encoding="utf-8").read()
    assert "plugin_cache_may_break_dependency_lock_file = true" in text and str(cache).replace("\\", "\\\\") in text

    # The credential-scoped env of plan_json keeps its keys and gains only these two.
    scoped = {"PATH": "/bin", "TF_PLUGIN_CACHE_DIR": str(cache), "AWS_ACCESS_KEY_ID": "AKIAIOSFODNN7EXAMPLE"}
    out = _with_cache_config(scoped)
    assert set(out) - set(scoped) == {"TF_CLI_CONFIG_FILE", "CHECKPOINT_DISABLE"}
    assert "AKIA" not in text  # the config file never holds credentials


def test_runner_env_untouched_without_a_cache_or_with_a_user_config(monkeypatch):
    monkeypatch.delenv("TF_PLUGIN_CACHE_DIR", raising=False)
    assert _with_cache_config(None) is None
    env = {"TF_PLUGIN_CACHE_DIR": "/c", "TF_CLI_CONFIG_FILE": "/mine.tfrc"}
    assert _with_cache_config(env) is env


def test_plural():
    assert _plural(35, "dependency", "dependencies") == "35 dependencies"
    assert _plural(1, "dependency", "dependencies") == "1 dependency"
    assert _plural(1, "region") == "1 region" and _plural(3, "region") == "3 regions"
