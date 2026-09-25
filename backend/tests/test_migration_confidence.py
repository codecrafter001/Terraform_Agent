"""Unit tests for Migration Confidence Scoring & Blast-Radius Engine."""

import pytest
from tools.confidence_scorer import MigrationConfidenceScorer


def test_migration_confidence_high_score_clean():
    """Test 100% High Confidence Score with zero destructive drift."""
    res = MigrationConfidenceScorer.calculate_score(
        drift_results={"findings": [], "resources_missing": []},
        plan_equivalence_results={"blocking_actions": []},
        validation_results={"passed": True},
        security_results={"critical_count": 0, "high_count": 0, "medium_count": 0},
        generation_manifest={"resources_generated": 10, "unresolved_attributes": [], "warnings": []},
        dependency_graph={"dangling_references": []}
    )

    assert res["score"] == 100
    assert res["tier"] == "HIGH"
    assert res["verdict"] == "SAFE_TO_ADOPT"
    assert res["zero_destruction_guarantee"] is True
    assert res["blast_radius"]["destructive_count"] == 0
    assert res["blast_radius"]["no_op_count"] == 10


def test_migration_confidence_destructive_drift_penalized():
    """Test that destructive ForceNew attribute mismatches heavily drop score and trigger LOW tier."""
    res = MigrationConfidenceScorer.calculate_score(
        drift_results={
            "findings": [
                {
                    "resource_id": "vpc-123",
                    "resource_type": "aws_vpc",
                    "attribute": "cidr_block",
                    "tier": "destructive_equivalent",
                    "impact": "replacement",
                    "reason": "ForceNew CIDR mismatch"
                }
            ],
            "resources_missing": []
        },
        plan_equivalence_results={},
        validation_results={"passed": True},
        security_results={"critical_count": 0, "high_count": 0},
        generation_manifest={"resources_generated": 5, "unresolved_attributes": []},
        dependency_graph={}
    )

    assert res["score"] <= 65
    assert res["tier"] == "LOW"
    assert res["verdict"] == "ACTION_REQUIRED"
    assert res["zero_destruction_guarantee"] is False
    assert res["blast_radius"]["destructive_count"] == 1


def test_migration_confidence_medium_tier_with_warnings():
    """Test medium confidence when minor non-destructive behavior differences exist."""
    res = MigrationConfidenceScorer.calculate_score(
        drift_results={
            "findings": [
                {
                    "resource_id": "sg-123",
                    "resource_type": "aws_security_group",
                    "attribute": "description",
                    "tier": "behavior_changing",
                    "impact": "configuration_mismatch"
                }
            ]
        },
        validation_results={"passed": True},
        security_results={"critical_count": 0, "high_count": 1, "medium_count": 2},
        generation_manifest={"resources_generated": 8, "unresolved_attributes": [], "warnings": ["Tag mismatch"]},
        dependency_graph={}
    )

    assert 70 <= res["score"] < 90
    assert res["tier"] == "MEDIUM"
    assert res["verdict"] == "REVIEW_RECOMMENDED"
    assert res["zero_destruction_guarantee"] is True
