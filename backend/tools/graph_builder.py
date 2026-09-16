"""Graph builder using NetworkX for DAG dependencies & D3 visualization."""

import os
from typing import Any, Dict, List, Optional, Set

import networkx as nx

DEPENDENCY_CONFIDENCE_THRESHOLD = float(os.getenv("DEPENDENCY_CONFIDENCE_THRESHOLD", "0.80"))


class DependencyGraphBuilder:
    def __init__(self, confidence_threshold: Optional[float] = None):
        self.graph = nx.DiGraph()
        self.confidence_threshold = (
            confidence_threshold if confidence_threshold is not None else DEPENDENCY_CONFIDENCE_THRESHOLD
        )

    def build_graph(self, resources: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Build graph from discovered AWS resources and extract relationships with confidence & evidence."""
        self.graph.clear()

        if not resources:
            return {
                "nodes": [],
                "links": [],
                "is_dag": True,
                "node_count": 0,
                "edge_count": 0,
                "high_confidence_edge_count": 0,
                "advisory_edge_count": 0,
                "cycles": [],
                "topological_order": [],
                "stacks": [],
                "dot": "digraph TerraAgent {\n  rankdir=\"LR\";\n  node [shape=box, style=filled, fontsize=10];\n}"
            }

        # Step 1: Add all resource nodes
        for r in resources:
            if not isinstance(r, dict):
                continue
            r_id = r.get("id")
            if not isinstance(r_id, str) or not r_id.strip():
                continue
            r_type = r.get("resource_type") or "aws_resource"
            r_name = r.get("name") or r_id
            tags = r.get("tags") if isinstance(r.get("tags"), list) else []
            self.graph.add_node(
                r_id,
                type=r_type,
                name=r_name,
                tags=tags,
                raw=r
            )

        # Helper to add an edge with confidence, relation, and evidence
        def add_edge_if_target_exists(
            source_id: str,
            target_id: str,
            relation: str,
            confidence: float,
            evidence: str
        ):
            if not isinstance(source_id, str) or not isinstance(target_id, str):
                return
            if not source_id or not target_id or source_id == target_id:
                return
            if self.graph.has_node(source_id) and self.graph.has_node(target_id):
                # If edge already exists, update only if new confidence is higher
                if self.graph.has_edge(source_id, target_id):
                    existing_conf = self.graph[source_id][target_id].get("confidence", 0.0)
                    if confidence <= existing_conf:
                        return
                is_trusted = confidence >= self.confidence_threshold
                self.graph.add_edge(
                    source_id,
                    target_id,
                    relation=relation,
                    relationship_type=relation,
                    confidence=confidence,
                    evidence=evidence,
                    is_trusted=is_trusted
                )

        # Step 2: Establish dependency edges based on references & evidence
        for r in resources:
            if not isinstance(r, dict):
                continue
            src_id = r.get("id")
            if not isinstance(src_id, str) or not src_id.strip():
                continue

            r_type = r.get("resource_type", "")

            # VPC dependency - direct AWS ID reference (Highest confidence: 1.0)
            vpc_id = r.get("vpc_id")
            if isinstance(vpc_id, str) and vpc_id:
                add_edge_if_target_exists(
                    vpc_id,
                    src_id,
                    relation="contains",
                    confidence=1.0,
                    evidence=f"explicit AWS API relationship: {r_type} '{src_id}' references vpc_id '{vpc_id}'"
                )

            # Subnet dependency - direct AWS ID reference (Highest confidence: 1.0)
            subnet_id = r.get("subnet_id")
            if isinstance(subnet_id, str) and subnet_id:
                add_edge_if_target_exists(
                    subnet_id,
                    src_id,
                    relation="hosted_in",
                    confidence=1.0,
                    evidence=f"explicit AWS API relationship: {r_type} '{src_id}' references subnet_id '{subnet_id}'"
                )

            # Security group dependencies - direct AWS ID references (Highest confidence: 1.0)
            sgs = r.get("security_groups") if isinstance(r.get("security_groups"), list) else []
            for sg_id in sgs:
                if isinstance(sg_id, str) and sg_id:
                    add_edge_if_target_exists(
                        sg_id,
                        src_id,
                        relation="secured_by",
                        confidence=1.0,
                        evidence=f"explicit AWS API relationship: {r_type} '{src_id}' is secured by security group '{sg_id}'"
                    )

            # Route table -> associated subnets (Highest confidence: 1.0)
            if r_type == "aws_route_table":
                associated_subnets = r.get("associated_subnets") if isinstance(r.get("associated_subnets"), list) else []
                for sub_id in associated_subnets:
                    if isinstance(sub_id, str) and sub_id:
                        add_edge_if_target_exists(
                            src_id,
                            sub_id,
                            relation="routes_subnet",
                            confidence=1.0,
                            evidence=f"explicit AWS API relationship: route table '{src_id}' associates with subnet '{sub_id}'"
                        )

            # Instance -> IAM role dependency.
            profile_arn = r.get("iam_instance_profile_arn")
            if isinstance(profile_arn, str) and profile_arn:
                role_name = profile_arn.rsplit("/", 1)[-1]
                add_edge_if_target_exists(
                    role_name,
                    src_id,
                    relation="assumes_role",
                    confidence=0.80,
                    evidence=f"IAM instance profile ARN naming heuristic: instance profile '{profile_arn}' maps to IAM role '{role_name}'"
                )

            # Trust-policy signal: stored as node metadata on IAM role
            if r_type == "aws_iam_role":
                trust_policy = r.get("assume_role_policy")
                services = []
                if isinstance(trust_policy, dict):
                    statements = trust_policy.get("Statement") or []
                    if isinstance(statements, list):
                        for stmt in statements:
                            if isinstance(stmt, dict):
                                principal = stmt.get("Principal") or {}
                                if isinstance(principal, dict):
                                    service = principal.get("Service")
                                    if isinstance(service, str):
                                        services.append(service)
                                    elif isinstance(service, list):
                                        services.extend(s for s in service if isinstance(s, str))
                if services and self.graph.has_node(src_id):
                    self.graph.nodes[src_id]["trusted_by_service_types"] = services

            # Tag-based relationships (Medium confidence: 0.70)
            # Scan tags for references to other known resource IDs
            tags = r.get("tags") if isinstance(r.get("tags"), list) else []
            for tag in tags:
                if isinstance(tag, dict):
                    k = str(tag.get("Key", ""))
                    v = str(tag.get("Value", ""))
                    if v and self.graph.has_node(v) and v != src_id:
                        add_edge_if_target_exists(
                            v,
                            src_id,
                            relation="tag_reference",
                            confidence=0.70,
                            evidence=f"Tag relationship: {r_type} '{src_id}' tag '{k}' references resource ID '{v}'"
                        )

        # Step 3: Format D3, adjacency data, and analyze graph properties
        nodes = []
        for n, d in self.graph.nodes(data=True):
            nodes.append({
                "id": n,
                "name": d.get("name", n),
                "type": d.get("type", "unknown"),
                "category": self._categorize_type(d.get("type", "")),
                "tags": d.get("tags", []),
                "trusted_by_service_types": d.get("trusted_by_service_types", [])
            })

        links = []
        high_confidence_count = 0
        advisory_count = 0
        for u, v, d in self.graph.edges(data=True):
            conf = d.get("confidence", 1.0)
            is_trusted = d.get("is_trusted", conf >= self.confidence_threshold)
            if is_trusted:
                high_confidence_count += 1
            else:
                advisory_count += 1
            links.append({
                "source": u,
                "target": v,
                "relation": d.get("relation", "depends_on"),
                "relationship_type": d.get("relationship_type", d.get("relation", "depends_on")),
                "confidence": conf,
                "evidence": d.get("evidence", f"Observed relationship between {u} and {v}"),
                "is_trusted": is_trusted
            })

        # Cycle and DAG analysis
        is_dag = nx.is_directed_acyclic_graph(self.graph)
        cycles = []
        topological_order = []
        depths: Dict[str, int] = {}

        if is_dag:
            try:
                topological_order = list(nx.topological_sort(self.graph))
                for node in topological_order:
                    preds = list(self.graph.predecessors(node))
                    depths[node] = 0 if not preds else 1 + max(depths[p] for p in preds)
            except Exception:
                pass
        else:
            # Graph contains cycles in observed AWS infrastructure.
            # Find cycles without mutating the observed dependency graph.
            try:
                raw_cycles = list(nx.simple_cycles(self.graph))
                cycles = raw_cycles[:10]  # Cap to prevent unbounded lists
            except Exception:
                cycles = []

            # Compute depths based on trusted DAG subgraph where cycles are avoided
            trusted_subgraph = nx.DiGraph()
            for u, v, d in self.graph.edges(data=True):
                if d.get("is_trusted", True):
                    trusted_subgraph.add_edge(u, v)
            for n in self.graph.nodes():
                if not trusted_subgraph.has_node(n):
                    trusted_subgraph.add_node(n)

            if nx.is_directed_acyclic_graph(trusted_subgraph):
                try:
                    topological_order = list(nx.topological_sort(trusted_subgraph))
                    for node in topological_order:
                        preds = list(trusted_subgraph.predecessors(node))
                        depths[node] = 0 if not preds else 1 + max(depths[p] for p in preds)
                except Exception:
                    pass

        for n in nodes:
            n["depth"] = depths.get(n["id"], 0)

        return {
            "nodes": nodes,
            "links": links,
            "is_dag": is_dag,
            "node_count": len(nodes),
            "edge_count": len(links),
            "high_confidence_edge_count": high_confidence_count,
            "advisory_edge_count": advisory_count,
            "cycles": cycles,
            "topological_order": topological_order,
            "stacks": self._compute_stacks(nodes),
            "dot": self._to_dot()
        }

    _CATEGORY_TO_STACK = {
        "Networking": "foundation",
        "Security": "security",
        "Database": "data",
        "Storage": "data",
        "Compute": "application",
        "General": "application",
    }

    def _compute_stacks(self, nodes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Suggests Terraform stack/module boundaries - deterministic, no LLM."""
        if not nodes or self.graph.number_of_nodes() == 0:
            return []
        category_by_id = {n["id"]: n["category"] for n in nodes}
        undirected = self.graph.to_undirected()
        components = list(nx.connected_components(undirected))

        stacks: Dict[str, List[str]] = {}
        for component in components:
            if len(component) <= 6:
                categories = [category_by_id.get(rid, "General") for rid in component]
                dominant = max(sorted(set(categories)), key=categories.count)
                stack_name = self._CATEGORY_TO_STACK.get(dominant, "application")
                stacks.setdefault(stack_name, []).extend(component)
            else:
                for rid in component:
                    category = category_by_id.get(rid, "General")
                    stack_name = self._CATEGORY_TO_STACK.get(category, "application")
                    stacks.setdefault(stack_name, []).append(rid)

        return [
            {"name": name, "resource_ids": sorted(ids), "resource_count": len(ids)}
            for name, ids in sorted(stacks.items())
        ]

    def _to_dot(self) -> str:
        """Render the graph in Graphviz DOT format for offline PNG rendering."""
        lines = ["digraph TerraAgent {", '  rankdir="LR";', '  node [shape=box, style=filled, fontsize=10];']
        for n, d in self.graph.nodes(data=True):
            label = str(d.get("name", n)).replace('"', "'")
            color = self._dot_color(d.get("type") or "")
            lines.append(f'  "{n}" [label="{label}", fillcolor="{color}"];')
        for u, v, d in self.graph.edges(data=True):
            relation = d.get("relation", "depends_on")
            conf = d.get("confidence", 1.0)
            lines.append(f'  "{u}" -> "{v}" [label="{relation} ({conf:.2f})", fontsize=8];')
        lines.append("}")
        return "\n".join(lines)

    @staticmethod
    def _dot_color(resource_type: str) -> str:
        colors = {
            "Networking": "#c4b5fd",
            "Compute": "#93c5fd",
            "Storage": "#6ee7b7",
            "Database": "#fcd34d",
            "Security": "#fca5a5",
            "General": "#e2e8f0",
        }
        return colors.get(DependencyGraphBuilder._categorize_type(resource_type), colors["General"])

    @staticmethod
    def _categorize_type(resource_type: str) -> str:
        if "db" in resource_type or "rds" in resource_type:
            return "Database"
        elif "vpc" in resource_type or "subnet" in resource_type or "gateway" in resource_type or "route" in resource_type:
            return "Networking"
        elif "instance" in resource_type:
            return "Compute"
        elif "s3" in resource_type:
            return "Storage"
        elif "security_group" in resource_type or "iam" in resource_type:
            return "Security"
        return "General"

