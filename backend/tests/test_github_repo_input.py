"""The PR form's repository field accepts what people paste (a github.com URL)
and always hands the GitHub client a clean owner/repo - the value becomes part
of an API path, so anything else is rejected before any call is made."""

import pytest
from pydantic import ValidationError

from models.scan import CreatePullRequestRequest
from routers.settings import WorkspaceDefaults
from tools.github_repo import normalize_repo


@pytest.mark.parametrize("value", [
    "codecrafter001/Terraform-Test",
    "  codecrafter001/Terraform-Test  ",
    "https://github.com/codecrafter001/Terraform-Test",
    "https://github.com/codecrafter001/Terraform-Test/",
    "https://github.com/codecrafter001/Terraform-Test.git",
    "https://www.github.com/codecrafter001/Terraform-Test",
    "http://github.com/codecrafter001/Terraform-Test",
    "github.com/codecrafter001/Terraform-Test",
    "https://github.com/codecrafter001/Terraform-Test/tree/main",
    "git@github.com:codecrafter001/Terraform-Test.git",
])
def test_common_forms_normalise_to_owner_repo(value):
    assert normalize_repo(value) == "codecrafter001/Terraform-Test"


@pytest.mark.parametrize("value", [
    "", "owner", "a/b/c", "https://github.com/owner", "https://gitlab.com/a/b", "git@gitlab.com:a/b.git",
    "../x", "a/..", "a/b?x=1", "javascript:alert(1)", "ftp://github.com/a/b", "-owner/repo",
])
def test_anything_else_is_rejected(value):
    with pytest.raises(ValueError):
        normalize_repo(value)


def test_pr_request_and_settings_default_use_the_normalised_value():
    req = CreatePullRequestRequest(github_token="ghp_x", repo="https://github.com/codecrafter001/Terraform-Test")
    assert req.repo == "codecrafter001/Terraform-Test"
    assert WorkspaceDefaults(github_repo="https://github.com/acme/infra.git").github_repo == "acme/infra"
    with pytest.raises(ValidationError):
        CreatePullRequestRequest(github_token="ghp_x", repo="https://gitlab.com/a/b")
    with pytest.raises(ValidationError):
        CreatePullRequestRequest(github_token="ghp_x", repo="a/b", base_branch="../main")
