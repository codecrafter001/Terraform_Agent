"""The two customer bootstraps (Terraform and CloudFormation) must grant exactly the
same permissions: a customer who bootstraps with either gets a working deployment."""

import os
import re
from typing import Dict, Set, Tuple

import hcl2
import pytest
import yaml

BOOTSTRAP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "deploy", "bootstrap")

# Terraform resource name -> how to find the same policy document in the CloudFormation template.
POLICIES = {
    "workload_boundary": lambda cfn: cfn["Resources"]["TerraAgentWorkloadBoundary"]["Properties"]["PolicyDocument"],
    "plan_policy": lambda cfn: cfn["Resources"]["TerraAgentDeployPlanRole"]["Properties"]["Policies"][0]["PolicyDocument"],
    "apply_policy": lambda cfn: cfn["Resources"]["TerraAgentDeployApplyRole"]["Properties"]["Policies"][0]["PolicyDocument"],
}
_STATEMENT = re.compile(r'Sid = "(\w+)", Effect = "(\w+)", Action = (\[[^\]]*\]|"[^"]*")')

Statements = Dict[str, Tuple[str, Set[str]]]


class _IgnoreTagsLoader(yaml.SafeLoader):
    """Reads !Ref / !Sub / !GetAtt as plain values; only the action lists matter here."""


_IgnoreTagsLoader.add_multi_constructor("!", lambda loader, suffix, node: loader.construct_scalar(node)
                                        if isinstance(node, yaml.ScalarNode) else None)


def _terraform() -> Dict[str, Statements]:
    with open(os.path.join(BOOTSTRAP, "terraform", "main.tf"), encoding="utf-8") as f:
        parsed = hcl2.load(f)
    out: Dict[str, Statements] = {}
    for block in parsed["resource"]:
        for _, items in block.items():
            for name, body in items.items():
                name = name.strip('"')
                if name in POLICIES:
                    out[name] = {sid: (effect, set(re.findall(r'"([^"]+)"', actions)))
                                 for sid, effect, actions in _STATEMENT.findall(str(body["policy"]))}
    return out


def _cloudformation() -> Dict[str, Statements]:
    with open(os.path.join(BOOTSTRAP, "cloudformation.yaml"), encoding="utf-8") as f:
        cfn = yaml.load(f, Loader=_IgnoreTagsLoader)  # noqa: S506 - SafeLoader subclass
    out: Dict[str, Statements] = {}
    for name, find in POLICIES.items():
        statements = {}
        for st in find(cfn)["Statement"]:
            actions = st["Action"]
            statements[st["Sid"]] = (st["Effect"], set([actions] if isinstance(actions, str) else actions))
        out[name] = statements
    return out


@pytest.mark.parametrize("policy", sorted(POLICIES))
def test_terraform_and_cloudformation_bootstraps_grant_the_same_actions(policy):
    tf, cfn = _terraform()[policy], _cloudformation()[policy]
    assert tf, f"no statements parsed from the Terraform {policy}"
    assert sorted(tf) == sorted(cfn), f"{policy}: statement Sids differ"
    for sid in tf:
        assert tf[sid] == cfn[sid], f"{policy}.{sid}: Terraform {tf[sid]} vs CloudFormation {cfn[sid]}"


def test_plan_role_can_read_the_vpc_quota():
    assert "servicequotas:GetServiceQuota" in _terraform()["plan_policy"]["AllowReadServices"][1]
