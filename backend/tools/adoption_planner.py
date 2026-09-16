"""Deterministic adoption plan builder - no LLM, no new external calls.

Transforms a ClassificationReport (tools/resource_classifier.py) and
DependencyGraph (tools/graph_builder.py) into an AdoptionPlan: a human-facing
migration plan grouping resources into 5 plan-level categories, an interpretable
risk score, trusted-dependency migration waves, and safe cycle handling.
"""

import os
from typing import Any, Dict, List, Literal, Set, Tuple

import networkx as nx

from models.adoption import AdoptionCategoryPlan, AdoptionPlan, AdoptionWave

PlanCategory = Literal["safe_to_import", "review_required", "do_not_manage", "use_data_source", "unsupported"]

DEPENDENCY_CONFIDENCE_THRESHOLD = float(os.getenv("DEPENDENCY_CONFIDENCE_THRESHOLD", "0.80"))
MAX_ADOPTION_WAVE_SIZE = int(os.getenv("MAX_ADOPTION_WAVE_SIZE", "25"))

_HIGH_BLAST_RADIUS_TYPES = {"aws_db_instance", "aws_iam_role"}

_ACTION_TO_CATEGORY: Dict[str, PlanCategory] = {
    "import": "safe_to_import",
    "data_source": "use_data_source",
    "skip": "do_not_manage",
}


def _category_for(classification: Dict[str, Any]) -> PlanCategory:
    action = classification.get("recommended_action")
    if action == "manual_review":
        return "unsupported" if classification.get("category") == "unsupported" else "review_required"
    return _ACTION_TO_CATEGORY.get(action, "review_required") if isinstance(action, str) else "review_required"


def _category_name_for_types(types: List[str]) -> str:
    """Assigns an intuitive wave title based on dominant resource types in the wave."""
    type_set = set(types)
    if any("vpc" in t or "subnet" in t or "route" in t or "gateway" in t for t in type_set):
        return "Network Foundation"
    elif any("iam" in t or "security_group" in t for t in type_set):
        return "Security & IAM"
    elif any("instance" in t or "ec2" in t for t in type_set):
        return "Compute Infrastructure"
    elif any("db" in t or "rds" in t or "s3" in t for t in type_set):
        return "Data & Storage Services"
    return "Application Services"


def _extract_trusted_safe_subgraph(
    safe_to_import_ids: List[str],
    dependency_graph: Dict[str, Any],
    confidence_threshold: float = DEPENDENCY_CONFIDENCE_THRESHOLD
) -> Tuple[nx.DiGraph, Set[str], List[List[str]]]:
    """Builds a NetworkX DiGraph on safe_to_import resources using ONLY trusted edges.
    If cycles exist in trusted edges, identifies cycle participants to be moved to review_required."""
    safe_set = set(safe_to_import_ids)
    graph = nx.DiGraph()
    for rid in safe_set:
        graph.add_node(rid)

    for link in dependency_graph.get("links", []) or []:
        source = link.get("source")
        target = link.get("target")
        conf = link.get("confidence", 1.0)
        is_trusted = link.get("is_trusted", conf >= confidence_threshold)

        # Only consider trusted dependencies between resources both marked safe_to_import
        if is_trusted and source in safe_set and target in safe_set and source != target:
            graph.add_edge(source, target)

    cycle_nodes: Set[str] = set()
    cycles: List[List[str]] = []
    if not nx.is_directed_acyclic_graph(graph):
        try:
            raw_cycles = list(nx.simple_cycles(graph))
            cycles = raw_cycles[:10]
            for cycle in cycles:
                cycle_nodes.update(cycle)
        except Exception:
            pass

    return graph, cycle_nodes, cycles


def _local_depths(
    dag: nx.DiGraph,
    nodes: Set[str]
) -> Dict[str, int]:
    """Computes dependency depths on the verified DAG of trusted edges."""
    depths: Dict[str, int] = {}
    try:
        topological = list(nx.topological_sort(dag))
        for node in topological:
            if node in nodes:
                preds = [p for p in dag.predecessors(node) if p in nodes]
                depths[node] = 0 if not preds else 1 + max(depths.get(p, 0) for p in preds)
    except Exception:
        for n in nodes:
            depths[n] = 0

    return depths


def _build_waves(
    safe_to_import_ids: List[str],
    dependency_graph: Dict[str, Any],
    resources_by_id: Dict[str, Dict[str, Any]],
    confidence_threshold: float = DEPENDENCY_CONFIDENCE_THRESHOLD
) -> Tuple[List[AdoptionWave], List[str], Set[str], List[List[str]]]:
    """Groups safe_to_import resources into sequential migration waves by trusted dependency depth."""
    if not safe_to_import_ids:
        return [], [], set(), []

    dag, cycle_nodes, detected_cycles = _extract_trusted_safe_subgraph(
        safe_to_import_ids, dependency_graph, confidence_threshold
    )

    # Any resources in cycles cannot be safely automatically sequenced;
    # remove them from safe waves so they get flagged for review.
    verified_safe_ids = [rid for rid in safe_to_import_ids if rid not in cycle_nodes]
    if not verified_safe_ids:
        return [], [], cycle_nodes, detected_cycles

    depth_by_id = _local_depths(dag, set(verified_safe_ids))

    ids_by_depth: Dict[int, List[str]] = {}
    for rid in verified_safe_ids:
        ids_by_depth.setdefault(depth_by_id.get(rid, 0), []).append(rid)

    ordered_chunks: List[List[str]] = []
    for depth in sorted(ids_by_depth.keys()):
        bucket = sorted(ids_by_depth[depth])
        for i in range(0, len(bucket), MAX_ADOPTION_WAVE_SIZE):
            ordered_chunks.append(bucket[i:i + MAX_ADOPTION_WAVE_SIZE])

    # Precompute resources touching low-confidence (advisory) edges
    low_confidence_ids: Set[str] = set()
    for link in dependency_graph.get("links", []) or []:
        conf = link.get("confidence", 1.0)
        is_trusted = link.get("is_trusted", conf >= confidence_threshold)
        if not is_trusted or conf < 1.0:
            low_confidence_ids.add(link.get("source"))
            low_confidence_ids.add(link.get("target"))

    waves: List[AdoptionWave] = []
    import_order: List[str] = []
    for wave_number, wave_ids in enumerate(ordered_chunks, start=1):
        signals: List[str] = []
        import_order.extend(wave_ids)

        resource_types = [
            resources_by_id.get(rid, {}).get("resource_type", "")
            for rid in wave_ids
        ]

        blast_types = sorted({
            t for t in resource_types if t in _HIGH_BLAST_RADIUS_TYPES
        })
        if blast_types:
            signals.append(f"contains high-blast-radius resource type(s): {', '.join(blast_types)}")

        low_confidence_count = sum(1 for rid in wave_ids if rid in low_confidence_ids)
        if low_confidence_count:
            signals.append(f"{low_confidence_count} resource(s) linked by advisory/low-confidence dependency")

        untagged_count = sum(1 for rid in wave_ids if not resources_by_id.get(rid, {}).get("tags"))
        if untagged_count:
            signals.append(f"{untagged_count} untagged resource(s)")

        risk_level: Literal["low", "medium", "high"]
        if blast_types:
            risk_level = "high"
        elif low_confidence_count or untagged_count:
            risk_level = "medium"
        else:
            risk_level = "low"

        category_title = _category_name_for_types(resource_types)

        waves.append(
            AdoptionWave(
                wave=wave_number,
                category_name=category_title,
                resource_ids=wave_ids,
                risk_level=risk_level,
                risk_signals=signals
            )
        )

    return waves, import_order, cycle_nodes, detected_cycles


def deterministic_summary(plan: AdoptionPlan) -> str:
    high_risk_waves = sum(1 for w in plan.waves if w.risk_level == "high")
    wave_sentence = (
        f" Safe-to-import resources are grouped into {plan.wave_count} migration wave(s), "
        f"ordered by trusted dependency depth, with {high_risk_waves} flagged high-risk."
        if plan.waves else ""
    )
    cycle_sentence = (
        f" Warning: {len(plan.cycles_detected)} dependency cycle(s) detected and routed to manual review."
        if plan.cycles_detected else ""
    )
    return (
        f"{plan.total_resource_count} resource(s) discovered. "
        f"{plan.managed_count} are safe to import directly, "
        f"{plan.data_source_count} should be referenced via data source, "
        f"{plan.do_not_manage_count} are AWS/shared-managed and should not be adopted, "
        f"{plan.review_count} need manual review, and "
        f"{plan.unsupported_count} have no adoption support yet. "
        f"Total dependencies: {plan.total_dependencies} ({plan.high_confidence_dependencies} high-confidence). "
        f"Overall risk score: {plan.risk_score}/100.{wave_sentence}{cycle_sentence}"
    )


def build_adoption_plan(
    classification_report: Dict[str, Any],
    dependency_graph: Dict[str, Any],
    resources: List[Dict[str, Any]],
    confidence_threshold: float = DEPENDENCY_CONFIDENCE_THRESHOLD
) -> AdoptionPlan:
    classifications = classification_report.get("classifications", []) or []
    total = len(classifications)
    resources_by_id: Dict[str, Dict[str, Any]] = {r["id"]: r for r in resources if r.get("id")}

    ids_by_category: Dict[PlanCategory, List[str]] = {}
    for c in classifications:
        category = _category_for(c)
        ids_by_category.setdefault(category, []).append(c["resource_id"])

    initial_safe_ids = list(ids_by_category.get("safe_to_import", []))

    waves, import_order, cycle_nodes, detected_cycles = _build_waves(
        initial_safe_ids, dependency_graph, resources_by_id, confidence_threshold
    )

    # Move cycle-involved resources to review_required
    if cycle_nodes:
        ids_by_category["safe_to_import"] = [rid for rid in initial_safe_ids if rid not in cycle_nodes]
        review_list = ids_by_category.setdefault("review_required", [])
        for cn in sorted(cycle_nodes):
            if cn not in review_list:
                review_list.append(cn)

    categories = [
        AdoptionCategoryPlan(category=category, resource_ids=ids, resource_count=len(ids))
        for category, ids in sorted(ids_by_category.items())
    ]

    managed_count = len(ids_by_category.get("safe_to_import", []))
    review_count = len(ids_by_category.get("review_required", []))
    data_source_count = len(ids_by_category.get("use_data_source", []))
    unsupported_count = len(ids_by_category.get("unsupported", []))
    do_not_manage_count = len(ids_by_category.get("do_not_manage", []))

    review_and_unsupported = review_count + unsupported_count
    risk_score = round(100 * review_and_unsupported / total) if total else 0

    total_deps = dependency_graph.get("edge_count", len(dependency_graph.get("links", [])))
    high_conf_deps = dependency_graph.get(
        "high_confidence_edge_count",
        sum(1 for l in dependency_graph.get("links", []) if l.get("confidence", 1.0) >= confidence_threshold)
    )

    plan = AdoptionPlan(
        categories=categories,
        total_resource_count=total,
        managed_count=managed_count,
        review_count=review_count,
        data_source_count=data_source_count,
        unsupported_count=unsupported_count,
        do_not_manage_count=do_not_manage_count,
        total_dependencies=total_deps,
        high_confidence_dependencies=high_conf_deps,
        wave_count=len(waves),
        risk_score=risk_score,
        import_order=import_order,
        waves=waves,
        cycles_detected=detected_cycles,
        summary=None
    )
    return plan
