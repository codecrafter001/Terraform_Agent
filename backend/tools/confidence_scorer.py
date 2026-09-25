"""Migration Confidence & Blast-Radius Scoring Engine for TerraAgent.

Calculates a deterministic 0-100% Migration Confidence Score and categorizes
blast-radius risk (No-Op, Safe In-Place Update, Behavior-Changing, Destructive Recreate).

Provides executive and DevOps leaders with mathematical evidence proving whether
adopting unmanaged infrastructure into Terraform will cause any production downtime or drift.
"""

from typing import Any, Dict, List, Optional


class MigrationConfidenceScorer:
    """Calculates migration confidence and blast radius impact."""

    @staticmethod
    def calculate_score(
        drift_results: Optional[Dict[str, Any]] = None,
        plan_equivalence_results: Optional[Dict[str, Any]] = None,
        validation_results: Optional[Dict[str, Any]] = None,
        security_results: Optional[Dict[str, Any]] = None,
        generation_manifest: Optional[Dict[str, Any]] = None,
        dependency_graph: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        drift = drift_results or {}
        plan = plan_equivalence_results or {}
        validation = validation_results or {}
        security = security_results or {}
        manifest = generation_manifest or {}
        graph = dependency_graph or {}

        # 1. Pillar 1: Drift & Plan Safety (Max 40 points)
        drift_pts = 40
        drift_penalties: List[str] = []

        drift_findings = drift.get("findings", []) or []
        destructive_drift = [f for f in drift_findings if f.get("tier") == "destructive_equivalent" or f.get("impact") == "replacement"]
        behavior_drift = [f for f in drift_findings if f.get("tier") == "behavior_changing" or f.get("impact") == "configuration_mismatch"]
        missing_resources = drift.get("resources_missing", []) or []

        if destructive_drift:
            penalty = min(35, len(destructive_drift) * 35)
            drift_pts -= penalty
            drift_penalties.append(f"{len(destructive_drift)} destructive/ForceNew attribute mismatch(es) detected (-{penalty} pts)")

        if behavior_drift:
            penalty = min(15, len(behavior_drift) * 5)
            drift_pts -= penalty
            drift_penalties.append(f"{len(behavior_drift)} behavior-changing attribute mismatch(es) detected (-{penalty} pts)")

        if missing_resources:
            penalty = min(20, len(missing_resources) * 10)
            drift_pts -= penalty
            drift_penalties.append(f"{len(missing_resources)} resource(s) missing from live AWS (-{penalty} pts)")

        # Plan equivalence deductions
        plan_blocking = plan.get("blocking_actions", []) or []
        if plan_blocking:
            drift_pts -= 20
            drift_penalties.append(f"Plan Equivalence detected {len(plan_blocking)} blocking actions (-20 pts)")

        drift_pts = max(0, drift_pts)

        # 2. Pillar 2: Deterministic Attribute Resolution & HCL Quality (Max 25 points)
        attr_pts = 25
        attr_penalties: List[str] = []

        unresolved = manifest.get("unresolved_attributes", []) or []
        if unresolved:
            penalty = min(20, len(unresolved) * 5)
            attr_pts -= penalty
            attr_penalties.append(f"{len(unresolved)} unresolved attribute(s) requiring review (-{penalty} pts)")

        if not validation.get("passed", True):
            attr_pts -= 15
            attr_penalties.append("Terraform validate / syntax check failed (-15 pts)")

        attr_pts = max(0, attr_pts)

        # 3. Pillar 3: Policy-as-Code & Security Compliance (Max 20 points)
        sec_pts = 20
        sec_penalties: List[str] = []

        crit_count = security.get("critical_count", 0) or 0
        high_count = security.get("high_count", 0) or 0
        med_count = security.get("medium_count", 0) or 0

        if crit_count > 0:
            penalty = min(15, crit_count * 10)
            sec_pts -= penalty
            sec_penalties.append(f"{crit_count} CRITICAL security finding(s) (-{penalty} pts)")

        if high_count > 0:
            penalty = min(10, high_count * 5)
            sec_pts -= penalty
            sec_penalties.append(f"{high_count} HIGH security finding(s) (-{penalty} pts)")

        if med_count > 0:
            penalty = min(5, med_count * 1)
            sec_pts -= penalty
            sec_penalties.append(f"{med_count} MEDIUM security finding(s) (-{penalty} pts)")

        sec_pts = max(0, sec_pts)

        # 4. Pillar 4: Dependency Graph & Topological Integrity (Max 15 points)
        graph_pts = 15
        graph_penalties: List[str] = []

        dangling_nodes = graph.get("dangling_references", []) or []
        if dangling_nodes:
            penalty = min(10, len(dangling_nodes) * 3)
            graph_pts -= penalty
            graph_penalties.append(f"{len(dangling_nodes)} dangling cross-resource reference(s) (-{penalty} pts)")

        graph_pts = max(0, graph_pts)

        # Total Score
        total_score = drift_pts + attr_pts + sec_pts + graph_pts

        # Determine Tier & Verdict
        if total_score >= 90 and len(destructive_drift) == 0:
            tier = "HIGH"
            verdict = "SAFE_TO_ADOPT"
            summary_message = "High Confidence: Zero destructive drift detected. Clean, validated modular HCL is safe for automated adoption."
        elif total_score >= 70 and len(destructive_drift) == 0:
            tier = "MEDIUM"
            verdict = "REVIEW_RECOMMENDED"
            summary_message = "Medium Confidence: Non-destructive attribute drift or minor security warnings present. Review recommended before apply."
        else:
            tier = "LOW"
            verdict = "ACTION_REQUIRED"
            summary_message = "Low Confidence: Destructive ForceNew drift, unresolved attributes, or critical policy violations detected. Human review required."

        # Blast Radius Breakdown
        destructive_count = len(destructive_drift)
        behavior_count = len(behavior_drift)
        no_op_count = manifest.get("resources_generated", 0) - (destructive_count + behavior_count)
        no_op_count = max(0, no_op_count)

        return {
            "score": total_score,
            "tier": tier,
            "verdict": verdict,
            "zero_destruction_guarantee": destructive_count == 0,
            "summary_message": summary_message,
            "breakdown": {
                "drift_and_plan": {
                    "points": drift_pts,
                    "max": 40,
                    "penalties": drift_penalties,
                },
                "attribute_resolution": {
                    "points": attr_pts,
                    "max": 25,
                    "penalties": attr_penalties,
                },
                "policy_and_security": {
                    "points": sec_pts,
                    "max": 20,
                    "penalties": sec_penalties,
                },
                "graph_topology": {
                    "points": graph_pts,
                    "max": 15,
                    "penalties": graph_penalties,
                },
            },
            "blast_radius": {
                "no_op_count": no_op_count,
                "safe_update_count": len(manifest.get("warnings", [])),
                "behavior_changing_count": behavior_count,
                "destructive_count": destructive_count,
                "missing_resources_count": len(missing_resources),
            },
        }
