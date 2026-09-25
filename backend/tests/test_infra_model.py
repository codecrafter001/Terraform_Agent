"""Canonical Infra Model (tools/infra_model.py) and how adoption decisions
flow into the adoption plan the composer consumes."""

from tools.adoption_planner import build_adoption_plan
from tools.infra_model import IMPORT_ID_FIELDS, SOURCE_API, build_infra_model, import_id_for
from tools.resource_classifier import DISCOVERY_RESOURCE_TYPES, classify_resources

RESOURCES = [
    {"id": "vpc-1", "resource_type": "aws_vpc", "name": "main", "cidr_block": "10.0.0.0/16",
     "tags": [{"Key": "Name", "Value": "main"}]},
    {"id": "subnet-1", "resource_type": "aws_subnet", "vpc_id": "vpc-1", "cidr_block": "10.0.1.0/24", "tags": []},
    {"id": "app-role", "resource_type": "aws_iam_role", "name": "app-role",
     "arn": "arn:aws:iam::123:role/app-role", "tags": []},
    {"id": "fn-1", "resource_type": "aws_lambda_function", "tags": []},
]
GRAPH = {
    "nodes": [{"id": r["id"]} for r in RESOURCES],
    "links": [{"source": "vpc-1", "target": "subnet-1"}, {"source": "app-role", "target": "subnet-1"}],
    "stacks": [{"name": "foundation", "resource_ids": ["subnet-1", "vpc-1"]}],
}


def _model():
    classification = classify_resources(RESOURCES, GRAPH).model_dump()
    return build_infra_model(RESOURCES, GRAPH, classification, "ap-south-1", discovered_at="2026-09-25T00:00:00")


def test_every_discoverable_type_has_an_import_id_rule_and_source_api():
    assert set(IMPORT_ID_FIELDS) == DISCOVERY_RESOURCE_TYPES
    assert set(SOURCE_API) == DISCOVERY_RESOURCE_TYPES


def test_import_ids_come_from_the_lookup_table():
    assert import_id_for({"resource_type": "aws_iam_role", "id": "AROAX", "name": "app-role"}) == "app-role"
    assert import_id_for({"resource_type": "aws_subnet", "id": "subnet-1"}) == "subnet-1"
    assert import_id_for({"resource_type": "aws_lambda_function", "id": "fn-1"}) is None


def test_records_carry_facts_decision_and_evidence():
    model = _model()
    by_id = {r["id"]: r for r in model["records"]}

    subnet = by_id["subnet-1"]
    assert subnet["decision"] == "manage"
    assert subnet["dependencies"] == ["app-role", "vpc-1"]  # edges run dependency -> dependent
    assert subnet["stack"] == "foundation"
    assert subnet["region"] == "ap-south-1"
    assert subnet["attributes"]["cidr_block"] == "10.0.1.0/24"
    assert subnet["evidence"] == {"source_api": "ec2:DescribeSubnets", "discovered_at": "2026-09-25T00:00:00",
                                  "rule": "unmanaged_default"}

    assert by_id["vpc-1"]["tags"] == {"Name": "main"}
    assert by_id["app-role"]["decision"] == "review"
    assert by_id["fn-1"]["decision"] == "exclude"
    assert model["summary"] == {"total": 4, "manage": 2, "reference": 0, "exclude": 1, "review": 1}


def test_records_are_sorted_deterministically():
    ids = [r["id"] for r in _model()["records"]]
    assert ids == ["app-role", "fn-1", "subnet-1", "vpc-1"]  # by (type, id)


def test_decisions_map_onto_the_composer_categories():
    classification = classify_resources(RESOURCES, GRAPH).model_dump()
    plan = build_adoption_plan(classification, GRAPH, RESOURCES)
    by_category = {c.category: set(c.resource_ids) for c in plan.categories}

    assert {"vpc-1", "subnet-1"} <= by_category["safe_to_import"]
    assert "app-role" in by_category["review_required"]
    # Excluded because unsupported - still counted separately in the report.
    assert "fn-1" in by_category["unsupported"]
